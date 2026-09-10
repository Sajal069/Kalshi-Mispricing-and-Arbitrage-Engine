"""Order-book reconstruction and the N0 invariant."""

import pytest

from kima.book import NO, YES, BookIntegrityError, MarketBook
from kima.ticks import KNOWN_STRUCTURES
from kima.units import NOTIONAL_CC, complement_cc
from tests.conftest import make_book


class TestBidOnlyIdentities:
    def test_implied_asks(self):
        bk = make_book("M", yes_bid_cc=4_000, no_bid_cc=5_500)
        assert bk.best_yes_bid == 4_000
        assert bk.best_no_bid == 5_500
        # A NO bid at $0.55 is a YES offer at $0.45.
        assert bk.best_yes_ask == 4_500
        assert bk.best_no_ask == 6_000
        assert bk.yes_spread_cc == 500

    def test_empty_side_reports_minus_one(self):
        bk = MarketBook("M")
        bk.apply_snapshot([(4_000, 100)], [], seq=1)
        assert bk.best_no_bid == -1
        assert bk.best_yes_ask == -1
        assert not bk.is_two_sided


class TestDeltaApplication:
    def test_best_pointer_tracks_additions_and_removals(self):
        bk = make_book("M", 4_000, 5_500)
        bk.apply_delta(YES, 4_100, 500, seq=2)
        assert bk.best_yes_bid == 4_100
        bk.apply_delta(YES, 4_100, -500, seq=3)
        assert bk.best_yes_bid == 4_000
        bk.apply_delta(YES, 4_000, -10_000, seq=4)
        assert bk.best_yes_bid == 3_900

    def test_emptying_a_side_entirely(self):
        bk = MarketBook("M")
        bk.apply_snapshot([(4_000, 100)], [(5_000, 100)], seq=1)
        bk.apply_delta(YES, 4_000, -100, seq=2)
        assert bk.best_yes_bid == -1
        assert bk.n_levels(YES) == 0

    def test_negative_resting_size_is_rejected(self):
        bk = make_book("M", 4_000, 5_500)
        with pytest.raises(BookIntegrityError):
            bk.apply_delta(YES, 4_000, -999_999, seq=2)

    def test_rescan_handles_tapered_grids(self):
        """Sub-penny tails mean the scan stride cannot be assumed uniform."""
        bk = MarketBook("M", KNOWN_STRUCTURES["center_deci_edge_centi_cent"])
        bk.apply_snapshot([(9_500, 10), (9_501, 10)], [(400, 10)], seq=1)
        assert bk.best_yes_bid == 9_501
        bk.apply_delta(YES, 9_501, -10, seq=2)
        assert bk.best_yes_bid == 9_500


class TestN0Invariant:
    def test_holds_for_normal_book(self):
        make_book("M", 4_000, 5_500).check_n0()

    def test_detects_crossed_book(self):
        bk = MarketBook("M")
        bk.apply_snapshot([(4_000, 100)], [(5_500, 100)], seq=1)
        bk.apply_delta(YES, 5_000, 100, seq=2)   # yes 5000 + no 5500 > $1
        assert bk.n0_violated()
        with pytest.raises(BookIntegrityError, match="N0 violated"):
            bk.check_n0()

    def test_exactly_one_dollar_is_legal(self):
        """A touching book is not a crossed book."""
        bk = MarketBook("M")
        bk.apply_snapshot([(4_000, 100)], [(6_000, 100)], seq=1)
        bk.check_n0()


class TestLadderWalk:
    def test_buying_no_consumes_the_yes_ladder(self):
        # YES bids at 40c/39c/38c/37c, 100 contracts each.
        bk = make_book("M", 4_000, 5_500, depth_cq=10_000)
        walk = bk.walk_buy(NO, 15_000)          # 150 contracts of NO
        assert walk.filled_cq == 15_000
        # 100 @ (1-0.40)=0.60 then 50 @ (1-0.39)=0.61
        assert walk.cost_mu == 6_000 * 10_000 + 6_100 * 5_000
        assert walk.worst_price_cc == 6_100
        assert len(walk.fills) == 2

    def test_partial_fill_when_ladder_runs_out(self):
        bk = MarketBook("M")
        bk.apply_snapshot([(4_000, 500)], [(5_500, 500)], seq=1)
        walk = bk.walk_buy(NO, 10_000)
        assert walk.filled_cq == 500
        assert walk.filled_cq < 10_000

    def test_limit_price_stops_the_walk(self):
        bk = make_book("M", 4_000, 5_500, depth_cq=10_000)
        walk = bk.walk_buy(NO, 100_000, limit_price_cc=6_000)
        assert walk.filled_cq == 10_000       # only the 60c level qualifies
        assert walk.worst_price_cc == 6_000

    def test_sell_consumes_the_same_side(self):
        bk = make_book("M", 4_000, 5_500, depth_cq=10_000)
        walk = bk.walk_sell(YES, 5_000)       # unwind 50 long YES
        assert walk.filled_cq == 5_000
        assert walk.cost_mu == 4_000 * 5_000  # proceeds at the 40c bid

    def test_buy_then_sell_round_trip_loses_the_spread(self):
        bk = make_book("M", 4_000, 5_500, depth_cq=10_000)
        buy = bk.walk_buy(YES, 1_000)         # lift the 45c ask
        sell = bk.walk_sell(YES, 1_000)       # hit the 40c bid
        assert buy.cost_mu > sell.cost_mu
        assert buy.cost_mu - sell.cost_mu == 500 * 1_000   # 5c spread

    def test_depth_at_or_better(self):
        bk = make_book("M", 4_000, 5_500, depth_cq=10_000, levels=3)
        assert bk.depth_at_or_better(NO, 6_000) == 10_000
        assert bk.depth_at_or_better(NO, 6_200) == 30_000


class TestSnapshot:
    def test_round_trip(self):
        bk = make_book("M", 4_000, 5_500)
        yes, no = bk.snapshot_levels(YES), bk.snapshot_levels(NO)
        other = MarketBook("M")
        other.apply_snapshot(yes, no, seq=1)
        assert other.snapshot_levels(YES) == yes
        assert other.snapshot_levels(NO) == no
        assert other.best_yes_bid == bk.best_yes_bid

    def test_snapshot_clears_stale_levels(self):
        bk = make_book("M", 4_000, 5_500)
        bk.apply_snapshot([(3_000, 50)], [(5_000, 50)], seq=9)
        assert bk.best_yes_bid == 3_000
        assert bk.n_levels(YES) == 1
        assert bk.trusted
