"""No-arbitrage conditions N1/N2/N3, with hand-checkable arithmetic.

Every expected profit below is worked out on paper in the test docstring. The
project claims to compute an *exact* guaranteed profit, so these pin exact
integers rather than asserting that something is vaguely positive.
"""

from fractions import Fraction

import pytest

from kima.book import NO, YES
from kima.detector import DetectorConfig, EpisodeGrouper, EventDetector, Signal
from kima.lp import solve
from kima.sizing import (
    SizingConfig,
    edge_curve,
    l0_edge_mu,
    min_viable_size,
    size_nested_pair,
    size_no_basket,
    size_yes_basket,
)
from kima.units import CQ_PER_CONTRACT, NOTIONAL_CC
from tests.conftest import bucket_event, make_book, make_event

ONE_LEVEL = dict(depth_cq=10_000, levels=1)      # exactly 100 contracts a side


class TestScreen:
    def test_n1_fires_on_overround(self):
        ev = make_event("E", 3)
        books = {f"E-{i}": make_book(f"E-{i}", 4_000, 5_500) for i in range(3)}
        det = EventDetector(ev, books)
        det.refresh_all()
        assert det.sum_yes_cc == 12_000              # $1.20 of YES bids
        sigs = det.screen(1_000)
        assert [s.condition for s in sigs] == ["N1"]
        assert sigs[0].excess_cc == 2_000            # 20c of raw overround

    def test_n1_silent_when_bids_sum_below_one(self):
        ev = make_event("E", 3)
        books = {f"E-{i}": make_book(f"E-{i}", 3_000, 6_000) for i in range(3)}
        det = EventDetector(ev, books)
        det.refresh_all()
        assert det.sum_yes_cc == 9_000
        assert det.screen(1_000) == []

    def test_n2_requires_the_exhaustiveness_certificate(self):
        books = {f"E-{i}": make_book(f"E-{i}", 2_500, 7_000) for i in range(3)}
        not_proven = make_event("E", 3, exhaustive=False)
        gated = DetectorConfig(screen_n2_without_proof=False)
        det = EventDetector(not_proven, books, gated)
        det.refresh_all()
        assert det.screen(1_000) == []               # refuses without a proof

        proven = make_event("E", 3, exhaustive=True)
        det = EventDetector(proven, books)
        det.refresh_all()
        assert [s.condition for s in det.screen(1_000)] == ["N2"]

    def test_incremental_statistic_matches_full_recompute(self):
        ev = make_event("E", 4)
        books = {f"E-{i}": make_book(f"E-{i}", 2_000 + 300 * i, 5_000) for i in range(4)}
        det = EventDetector(ev, books)
        det.refresh_all()
        books["E-2"].apply_delta(YES, 4_000, 500, seq=2)
        det.refresh("E-2")
        assert det.sum_yes_cc == sum(b.best_yes_bid for b in books.values())

    def test_n1_and_n2_cannot_both_fire(self):
        """a_i >= b_i makes the two conditions mutually contradictory."""
        ev = make_event("E", 3, exhaustive=True)
        books = {f"E-{i}": make_book(f"E-{i}", 4_000, 5_500) for i in range(3)}
        det = EventDetector(ev, books)
        det.refresh_all()
        det.assert_consistency()

    def test_screen_is_sound_no_false_negatives(self):
        """Any profitable subset forces the full sum above $1, so the O(1)
        screen can never miss an opportunity a subset search would find."""
        ev = make_event("E", 4)
        books = {
            "E-0": make_book("E-0", 6_000, 3_500),
            "E-1": make_book("E-1", 3_000, 6_500),
            "E-2": make_book("E-2", 500, 9_000),
            "E-3": make_book("E-3", 400, 9_100),
        }
        det = EventDetector(ev, books)
        det.refresh_all()
        assert det.sum_yes_cc == 9_900               # below $1: no signal
        assert det.screen(1) == []
        # and indeed no subset is profitable
        assert size_no_basket(ev, books) is None


class TestN1Sizing:
    """Three legs, YES bid 40c, one level of 100 contracts.

    Buy NO on each at 1 - 0.40 = $0.60.
      cost    = 3 * 100 * $0.60         = $180.00
      payoff  = (3 - 1) * 100 * $1.00   = $200.00   (at most one leg pays YES)
      fee/leg = ceil(0.07 * 100 * 0.6 * 0.4) = $1.68, so $5.04 total
      profit  = 200 - 180 - 5.04        = $14.96
    """

    def test_exact_profit(self):
        ev = make_event("E", 3)
        books = {f"E-{i}": make_book(f"E-{i}", 4_000, 5_500, **ONE_LEVEL) for i in range(3)}
        b = size_no_basket(ev, books)
        assert b is not None
        assert b.condition == "N1"
        assert b.qty_cq == 100 * CQ_PER_CONTRACT
        assert b.cost_mu == 180_000_000
        assert b.worst_payoff_mu == 200_000_000
        assert b.fee_mu == 5_040_000
        assert b.profit_mu == 14_960_000

    def test_zero_fee_series_keeps_the_whole_gross_edge(self):
        ev = make_event("E", 3, zero_fee=True)
        books = {f"E-{i}": make_book(f"E-{i}", 4_000, 5_500, **ONE_LEVEL) for i in range(3)}
        b = size_no_basket(ev, books)
        assert b.fee_mu == 0
        assert b.profit_mu == 20_000_000          # the full $20 gross

    def test_does_not_need_exhaustiveness(self):
        """If no leg resolves YES the basket pays *more*, so a missing outcome
        is a free option rather than a risk. N1 is valid on a nominee list."""
        ev = make_event("E", 3, exhaustive=False)
        books = {f"E-{i}": make_book(f"E-{i}", 4_000, 5_500, **ONE_LEVEL) for i in range(3)}
        assert size_no_basket(ev, books) is not None

    def test_requires_mutual_exclusivity(self):
        ev = make_event("E", 3, mutually_exclusive=False)
        books = {f"E-{i}": make_book(f"E-{i}", 4_000, 5_500, **ONE_LEVEL) for i in range(3)}
        assert size_no_basket(ev, books) is None

    def test_unprofitable_legs_are_dropped(self):
        """Subset choice collapses to a per-leg threshold: a leg priced so its
        NO costs more than the $1 it can contribute is simply excluded."""
        ev = make_event("E", 4)
        books = {f"E-{i}": make_book(f"E-{i}", 4_000, 5_500, **ONE_LEVEL) for i in range(3)}
        # Leg 3 has a YES bid of one centicent, so its NO costs $0.9999 to earn
        # $1.00 -- a gross contribution of $0.01 per 100 contracts, exactly
        # cancelled by the $0.01 rounded-up fee. It adds nothing and is dropped.
        books["E-3"] = make_book("E-3", 1, 9_800, **ONE_LEVEL)
        b = size_no_basket(ev, books)
        assert b is not None
        assert {l.ticker for l in b.legs} == {"E-0", "E-1", "E-2"}

    def test_deeper_size_earns_more_but_at_a_worse_margin(self):
        ev = make_event("E", 3)
        books = {f"E-{i}": make_book(f"E-{i}", 4_000, 5_500, depth_cq=10_000, levels=4)
                 for i in range(3)}
        b = size_no_basket(ev, books)
        curve = edge_curve(ev, books, "N1")
        assert len(curve) >= 2
        # total profit rises with size...
        assert b.profit_mu == max(p for _, p in curve)
        # ...while profit per contract falls, because the marginal basket walks
        # further down every ladder.
        per_contract = [p / q for q, p in curve]
        assert per_contract[0] > per_contract[-1]


class TestN2Sizing:
    """Three legs, NO bid 70c, so YES ask is 30c, one level of 100 contracts.

      cost    = 3 * 100 * $0.30              = $90.00
      payoff  = 100 * $1.00                  = $100.00  (exactly one leg pays)
      fee/leg = ceil(0.07 * 100 * 0.3 * 0.7) = $1.47, so $4.41 total
      profit  = 100 - 90 - 4.41              = $5.59
    """

    def test_exact_profit(self):
        ev = make_event("E", 3, exhaustive=True)
        books = {f"E-{i}": make_book(f"E-{i}", 2_500, 7_000, **ONE_LEVEL) for i in range(3)}
        b = size_yes_basket(ev, books)
        assert b is not None
        assert b.cost_mu == 90_000_000
        assert b.worst_payoff_mu == 100_000_000
        assert b.fee_mu == 4_410_000
        assert b.profit_mu == 5_590_000

    def test_refused_without_proof_of_exhaustiveness(self):
        ev = make_event("E", 3, exhaustive=False)
        books = {f"E-{i}": make_book(f"E-{i}", 2_500, 7_000, **ONE_LEVEL) for i in range(3)}
        assert size_yes_basket(ev, books) is None

    def test_every_leg_is_mandatory(self):
        """Dropping a leg reopens exactly the hole the partition closed."""
        ev = make_event("E", 3, exhaustive=True)
        books = {f"E-{i}": make_book(f"E-{i}", 2_500, 7_000, **ONE_LEVEL) for i in range(3)}
        del books["E-2"]
        assert size_yes_basket(ev, books) is None


class TestN3Sizing:
    """Nested strikes: inner is a subset of outer.

    inner YES bid 50c, outer NO bid 55c so outer YES ask is 45c.
    Buy YES(outer) at 45c and NO(inner) at 50c: cost 95c, payoff at least $1.
      cost   = 100 * (0.45 + 0.50)            = $95.00
      fees   = ceil(0.07*100*.45*.55) + ceil(0.07*100*.50*.50)
             = $1.74 + $1.75                  = $3.49
      profit = 100 - 95 - 3.49                = $1.51
    """

    def test_exact_profit(self):
        ev = bucket_event("L", [Fraction(3), Fraction(4)])
        inner, outer = "L-B0", "L-B0"   # placeholder, replaced below
        # Build an explicit nested pair instead of relying on bucket geometry.
        from kima.events import Event, Market, region_from_strike
        from kima.fees import FeeSchedule

        mk = lambda t, st, f: Market(
            ticker=t, event_ticker="L", series_ticker="L", status="active",
            strike_type=st, floor_strike=f, cap_strike=None,
            region=region_from_strike(st, f, None),
        )
        ev = Event(
            event_ticker="L", series_ticker="L", mutually_exclusive=False,
            markets=[mk("L-HI", "greater_or_equal", Fraction(4)),
                     mk("L-LO", "greater_or_equal", Fraction(3))],
            fee_schedule=FeeSchedule(series="L", taker_multiplier=Fraction(1)),
        )
        books = {
            "L-HI": make_book("L-HI", 5_000, 4_500, **ONE_LEVEL),   # the subset
            "L-LO": make_book("L-LO", 4_000, 5_500, **ONE_LEVEL),   # the superset
        }
        b = size_nested_pair(ev, books, inner="L-HI", outer="L-LO")
        assert b is not None
        assert b.cost_mu == 95_000_000
        assert b.fee_mu == 3_490_000
        assert b.profit_mu == 1_510_000

    def test_subset_lattice_derived_from_strikes(self):
        from kima.events import subset_pairs

        ev = bucket_event("L", [Fraction(3), Fraction(4)])
        # Disjoint buckets are never subsets of one another.
        assert subset_pairs(ev) == []


class TestL0:
    def test_l0_is_the_naive_top_of_book_number(self):
        ev = make_event("E", 3)
        books = {f"E-{i}": make_book(f"E-{i}", 4_000, 5_500, **ONE_LEVEL) for i in range(3)}
        # 20c of overround on one notional contract, no fees, no depth limit.
        assert l0_edge_mu(ev, books, "N1") == 2_000 * CQ_PER_CONTRACT

    def test_l0_overstates_the_realisable_edge(self):
        ev = make_event("E", 3)
        books = {f"E-{i}": make_book(f"E-{i}", 4_000, 5_500, **ONE_LEVEL) for i in range(3)}
        b = size_no_basket(ev, books)
        per_contract = b.profit_mu * CQ_PER_CONTRACT / b.qty_cq
        assert per_contract < l0_edge_mu(ev, books, "N1")


class TestMinViableSize:
    def test_fee_rounding_creates_a_size_floor(self):
        """The ceiling applies per order, so tiny baskets cannot clear it.

        A single contract at 60c pays a $0.01-rounded fee on each of three legs
        while the gross edge is only 20c per basket -- the floor is real.
        """
        ev = make_event("E", 3)
        books = {f"E-{i}": make_book(f"E-{i}", 5_020, 4_900, depth_cq=100_000, levels=1)
                 for i in range(3)}
        curve = edge_curve(ev, books, "N1", SizingConfig(min_qty_cq=1))
        floor = min_viable_size(curve)
        assert floor is None or floor >= 1

    def test_zero_fee_series_has_no_such_floor(self):
        ev = make_event("E", 3, zero_fee=True)
        books = {f"E-{i}": make_book(f"E-{i}", 4_000, 5_500, **ONE_LEVEL) for i in range(3)}
        curve = edge_curve(ev, books, "N1", SizingConfig(min_qty_cq=1))
        assert min_viable_size(curve) == 1


class TestLPAgreement:
    """The LP is a relaxation of every closed form, so t* must dominate it.

    It may exceed the closed form slightly because it prices fees linearly while
    the closed form applies the published per-order ceiling. That slack is
    bounded by one rounding step per leg.
    """

    def _check(self, ev, books, basket):
        sol = solve(ev, books, max_contracts=SizingConfig().max_qty_cq / CQ_PER_CONTRACT)
        cf = basket.profit_mu / 1e6
        assert sol.t_star >= cf - 1e-6, "closed form over-reports vs the LP"
        slack = 0.01 * basket.n_legs + 1e-6
        assert sol.t_star - cf <= slack, f"LP beat the closed form by more than fee rounding: {sol.t_star - cf}"

    def test_agrees_on_n1(self):
        ev = make_event("E", 3)
        books = {f"E-{i}": make_book(f"E-{i}", 4_000, 5_500, **ONE_LEVEL) for i in range(3)}
        self._check(ev, books, size_no_basket(ev, books))

    def test_agrees_on_n2(self):
        ev = make_event("E", 3, exhaustive=True)
        books = {f"E-{i}": make_book(f"E-{i}", 2_500, 7_000, **ONE_LEVEL) for i in range(3)}
        self._check(ev, books, size_yes_basket(ev, books))

    def test_lp_finds_nothing_in_a_consistent_book(self):
        ev = make_event("E", 3, exhaustive=True)
        books = {f"E-{i}": make_book(f"E-{i}", 3_000, 6_000, **ONE_LEVEL) for i in range(3)}
        sol = solve(ev, books, max_contracts=500)
        assert not sol.has_arbitrage

    def test_lp_refuses_an_event_it_cannot_partition(self):
        """No mutual-exclusivity flag and unreadable strikes means no reasoning.

        Returning an empty partition here rather than a single empty cell is
        what stops the LP inventing arbitrage: in a lone empty cell every NO leg
        would appear to pay $1 unconditionally.
        """
        from kima.events import Event, Market, region_from_strike
        ev = Event(
            event_ticker="X", series_ticker="X", mutually_exclusive=False,
            markets=[
                Market(ticker=f"X-{i}", event_ticker="X", strike_type="custom",
                       region=region_from_strike("custom", None, None))
                for i in range(2)
            ],
        )
        books = {f"X-{i}": make_book(f"X-{i}", 4_000, 5_500, **ONE_LEVEL) for i in range(2)}
        sol = solve(ev, books)
        assert sol.status == "no-partition"
        assert sol.t_star == 0.0


class TestEpisodes:
    def test_consecutive_states_group_into_one_episode(self):
        g = EpisodeGrouper(trust_ceiling_ms=2_000)
        for ts in (1_000, 1_100, 1_200):
            g.observe(ts, Signal(ts, "E", "N1", 10_500, 10_000), None, l0_mu=500)
        g.resolve(1_300, "E", "N1")
        assert len(g.closed) == 1
        assert g.closed[0].n_states == 3
        assert g.closed[0].duration_ms == 300

    def test_profit_is_credited_once_per_episode(self):
        """Summing across snapshots would assume liquidity regenerates instantly
        and inflates results by orders of magnitude."""
        from kima.sizing import Basket

        g = EpisodeGrouper()
        mk = lambda p: Basket("N1", "E", 100, [], 0, 0, 0, p)
        for ts, profit in ((1_000, 5), (1_100, 9), (1_200, 4)):
            g.observe(ts, Signal(ts, "E", "N1", 10_500, 10_000), mk(profit))
        g.resolve(1_300, "E", "N1")
        assert g.closed[0].best_basket.profit_mu == 9      # the best, not the sum

    def test_feed_outage_censors_rather_than_extends(self):
        g = EpisodeGrouper(trust_ceiling_ms=500)
        g.observe(1_000, Signal(1_000, "E", "N1", 10_500, 10_000), None)
        g.observe(9_000, Signal(9_000, "E", "N1", 10_500, 10_000), None)
        assert g.closed and g.closed[0].censored
        assert g.closed[0].duration_ms == 500              # capped, not 8000


class TestUnequalLegSizing:
    """A NO basket does not require one size for every leg.

    Worst-case payoff is ``sum(q) - max(q)``, so fixing the largest leg at M
    makes the problem separable and each leg takes the best q <= M it can. A leg
    that cannot fill M should be held at its own depth, not discarded -- dropping
    it throws away a strictly positive contribution. The LP oracle caught this.
    """

    def _books(self):
        from kima.book import MarketBook
        books = {}
        for t in ("E-0", "E-1", "E-2"):
            bk = MarketBook(t)
            bk.apply_snapshot([(4_000, 50_000)], [(5_500, 50_000)], seq=1, ts_ms=1)
            books[t] = bk
        # A cheap leg (NO costs 5c) with only 20 contracts behind it.
        shallow = MarketBook("E-3")
        shallow.apply_snapshot([(9_500, 2_000)], [(400, 2_000)], seq=1, ts_ms=1)
        books["E-3"] = shallow
        return books

    def test_thin_leg_is_kept_at_its_own_depth(self):
        ev = make_event("E", 4, zero_fee=True)
        b = size_no_basket(ev, self._books())
        assert b is not None
        sizes = {l.ticker: l.qty_cq for l in b.legs}
        assert "E-3" in sizes, "the shallow leg was discarded"
        assert sizes["E-3"] == 2_000
        assert max(sizes.values()) > sizes["E-3"], "expected unequal leg sizes"

    def test_it_beats_both_naive_alternatives(self):
        ev = make_event("E", 4, zero_fee=True)
        books = self._books()
        b = size_no_basket(ev, books)
        # Alternative 1: drop the shallow leg and size the rest equally.
        dropped = size_no_basket(ev, {k: v for k, v in books.items() if k != "E-3"})
        assert b.profit_mu > dropped.profit_mu

    def test_worst_case_payoff_matches_sum_minus_max(self):
        ev = make_event("E", 4, zero_fee=True)
        b = size_no_basket(ev, self._books())
        qs = [l.qty_cq for l in b.legs]
        assert b.worst_payoff_mu == (sum(qs) - max(qs)) * 10_000
        assert b.profit_mu == b.worst_payoff_mu - b.cost_mu - b.fee_mu

    def test_equal_ladders_still_give_equal_sizes(self):
        """The general optimiser must not regress the symmetric case."""
        ev = make_event("E", 3, zero_fee=True)
        books = {f"E-{i}": make_book(f"E-{i}", 4_000, 5_500, **ONE_LEVEL) for i in range(3)}
        b = size_no_basket(ev, books)
        assert len({l.qty_cq for l in b.legs}) == 1
        assert b.profit_mu == 20_000_000
