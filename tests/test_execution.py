"""Execution simulator: latency, non-atomic legging, residuals, rate limits."""

import pytest

from kima.book import NO, YES
from kima.execution import (
    ExecConfig,
    ExecutionSimulator,
    TokenBucket,
    _worst_case_payoff_mu,
)
from kima.sizing import size_no_basket, size_yes_basket
from kima.units import CQ_PER_CONTRACT
from tests.conftest import make_book, make_event

ONE_LEVEL = dict(depth_cq=10_000, levels=1)


def _overround(zero_fee: bool = False):
    ev = make_event("E", 3, zero_fee=zero_fee)
    books = {f"E-{i}": make_book(f"E-{i}", 4_000, 5_500, **ONE_LEVEL) for i in range(3)}
    return ev, books


class TestWorstCasePayoff:
    def test_n1_is_sum_minus_max(self):
        """At most one leg settles YES, so every NO leg but the largest pays."""
        assert _worst_case_payoff_mu("N1", [100, 100, 100], 3) == 200 * 10_000
        # An unbalanced fill: the extra contracts on the biggest leg are naked.
        assert _worst_case_payoff_mu("N1", [100, 100, 250], 3) == 200 * 10_000

    def test_n2_is_the_smallest_leg_and_zero_if_one_is_missing(self):
        assert _worst_case_payoff_mu("N2", [100, 100, 100], 3) == 100 * 10_000
        assert _worst_case_payoff_mu("N2", [100, 80, 100], 3) == 80 * 10_000
        assert _worst_case_payoff_mu("N2", [100, 100], 3) == 0

    def test_a_single_leg_guarantees_nothing(self):
        assert _worst_case_payoff_mu("N1", [100], 3) == 0


class TestZeroLatencyControl:
    def test_l2_at_zero_latency_reproduces_l1_exactly(self):
        """The control for the whole latency experiment. If this drifts, every
        retention number in the study is measured against the wrong baseline."""
        ev, books = _overround()
        basket = size_no_basket(ev, books)
        sim = ExecutionSimulator(ExecConfig(latency_ms=0))
        sim.submit(basket, ev, 1_000)
        sim.advance(1_000, books)
        assert len(sim.results) == 1
        r = sim.results[0]
        assert r.complete
        assert r.net_mu == basket.profit_mu
        assert r.edge_retention == pytest.approx(1.0)


class TestPartialFills:
    def test_a_vanished_leg_breaks_the_basket(self):
        """Two NO legs guarantee only one payout between them, so what is left
        is a loser. The unwind then has a real decision to make."""
        ev, books = _overround()
        basket = size_no_basket(ev, books)
        # The world moves: one leg's resting liquidity disappears entirely.
        books["E-2"].apply_snapshot([], [(5_500, 10_000)], seq=2, ts_ms=2)
        sim = ExecutionSimulator(ExecConfig(latency_ms=50, unwind_delay_ms=0))
        sim.submit(basket, ev, 1_000)
        sim.advance(2_000, books)
        r = sim.results[0]
        assert not r.complete
        assert r.status == "partial"
        assert r.net_mu < basket.profit_mu

    def test_a_broken_remainder_is_sold_when_selling_beats_holding(self):
        """With two legs tied at the top, giving up one contract on each costs
        one dollar of guaranteed payoff and returns two bids. At a 55c bid that
        is worth doing, so the optimiser dumps the position rather than keeping
        a structurally loss-making basket."""
        ev, books = _overround(zero_fee=True)
        basket = size_no_basket(ev, books)
        books["E-2"].apply_snapshot([], [(5_500, 10_000)], seq=2, ts_ms=2)
        sim = ExecutionSimulator(ExecConfig(latency_ms=10, unwind_delay_ms=0))
        sim.submit(basket, ev, 1_000)
        sim.advance(2_000, books)
        r = sim.results[0]
        assert r.residual_cq > 0
        assert r.unwind_proceeds_mu > 0
        # Holding instead would have guaranteed only $100 against $120 of cost.
        hold_net = 100_000_000 - r.cost_mu
        assert r.net_mu > hold_net

    def test_a_broken_yes_basket_has_a_floor_of_zero(self):
        """N2 needs every leg: one missing outcome and the basket can pay nothing."""
        ev = make_event("E", 3, exhaustive=True)
        books = {f"E-{i}": make_book(f"E-{i}", 2_500, 7_000, **ONE_LEVEL) for i in range(3)}
        basket = size_yes_basket(ev, books)
        books["E-1"].apply_snapshot([(2_500, 10_000)], [], seq=2, ts_ms=2)
        sim = ExecutionSimulator(ExecConfig(latency_ms=10, unwind_delay_ms=0))
        sim.submit(basket, ev, 1_000)
        sim.advance(2_000, books)
        r = sim.results[0]
        assert r.worst_payoff_mu == 0
        assert r.residual_cq > 0

    def test_residual_is_unwound_by_crossing_back(self):
        ev, books = _overround(zero_fee=True)
        basket = size_no_basket(ev, books)
        # One leg thins to 20 contracts, so the other two overfill against it.
        books["E-2"].apply_snapshot([(4_000, 2_000)], [(5_500, 10_000)], seq=2, ts_ms=2)
        sim = ExecutionSimulator(ExecConfig(latency_ms=10, unwind_delay_ms=0))
        sim.submit(basket, ev, 1_000)
        sim.advance(2_000, books)
        r = sim.results[0]
        assert r.residual_cq > 0
        assert r.unwind_proceeds_mu > 0            # sold back into the NO bids
        assert sum(f.kept_cq for f in r.fills) + r.residual_cq == \
            sum(f.filled_cq for f in r.fills)

    def test_payoff_and_unwind_are_never_double_counted(self):
        """Crediting the floor of the full fill *and* the proceeds of selling
        part of it would count the same contracts twice. The floor must be
        computed on what is still held."""
        ev, books = _overround(zero_fee=True)
        basket = size_no_basket(ev, books)
        books["E-2"].apply_snapshot([(4_000, 2_000)], [(5_500, 10_000)], seq=2, ts_ms=2)
        sim = ExecutionSimulator(ExecConfig(latency_ms=10, unwind_delay_ms=0))
        sim.submit(basket, ev, 1_000)
        sim.advance(2_000, books)
        r = sim.results[0]
        kept = sorted((f.kept_cq for f in r.fills if f.kept_cq > 0), reverse=True)
        expected = (sum(kept) - kept[0]) * 10_000 if len(kept) >= 2 else 0
        assert r.worst_payoff_mu == expected
        assert sum(f.filled_cq - f.kept_cq for f in r.fills) == r.residual_cq


class TestOrderTypes:
    def test_fok_refuses_a_partial_leg_outright(self):
        ev, books = _overround()
        basket = size_no_basket(ev, books)
        books["E-2"].apply_snapshot([(4_000, 500)], [(5_500, 10_000)], seq=2, ts_ms=2)

        ioc = ExecutionSimulator(ExecConfig(latency_ms=10, order_type="IOC", unwind_delay_ms=0))
        fok = ExecutionSimulator(ExecConfig(latency_ms=10, order_type="FOK", unwind_delay_ms=0))
        for sim in (ioc, fok):
            sim.submit(basket, ev, 1_000)
            sim.advance(2_000, books)
        ioc_leg = next(f for f in ioc.results[0].fills if f.ticker == "E-2")
        fok_leg = next(f for f in fok.results[0].fills if f.ticker == "E-2")
        assert 0 < ioc_leg.filled_cq < ioc_leg.requested_cq
        assert fok_leg.filled_cq == 0


class TestRateLimiting:
    def test_token_bucket_refills_over_time(self):
        b = TokenBucket(tokens_per_s=100, burst_seconds=1.0)
        assert b.try_consume(0, 100)
        assert not b.try_consume(0, 10)
        assert b.try_consume(1_000, 100)          # a second later, refilled

    def test_a_five_leg_basket_costs_half_a_second_of_write_budget(self):
        """10 tokens per order against 100 tokens/s at Basic tier."""
        b = TokenBucket(tokens_per_s=100, burst_seconds=1.0)
        assert b.try_consume(0, 10 * 5)
        assert b.tokens == pytest.approx(50)

    def test_a_burst_is_delayed_rather_than_refused(self):
        """Orders go out one at a time, so exceeding the budget stretches a
        basket across time instead of making it impossible. Ten 3-leg baskets
        need 300 tokens against a 100/s budget: about two seconds of queue."""
        ev, books = _overround()
        basket = size_no_basket(ev, books)
        sim = ExecutionSimulator(ExecConfig(latency_ms=0, dedupe=False))
        accepted = [sim.submit(basket, ev, 1_000) for _ in range(10)]
        assert all(accepted), "a burst must queue, not vanish"
        assert sim.stats.rate_delayed > 0
        assert sim.stats.rate_delay_ms_total > 0
        assert sim.stats.rate_limited == 0

    def test_a_basket_larger_than_the_bucket_is_still_sendable(self):
        """The bug this replaced: a 28-leg basket needs 280 tokens against a
        bucket holding 100, and was refused forever rather than taking 2.8s."""
        from tests.conftest import make_book, make_event
        ev = make_event("W", 28, zero_fee=True)
        books = {f"W-{i}": make_book(f"W-{i}", 4_000, 5_500, depth_cq=10_000, levels=1)
                 for i in range(28)}
        basket = size_no_basket(ev, books)
        assert basket.n_legs > 10
        sim = ExecutionSimulator(ExecConfig(latency_ms=0, max_rate_limit_wait_ms=10_000))
        assert sim.submit(basket, ev, 1_000) is True
        assert sim.stats.rate_limited == 0
        sim.advance(60_000, books)
        assert sim.results[0].rate_delay_ms > 0

    def test_an_unaffordable_wait_is_refused(self):
        from tests.conftest import make_book, make_event
        ev = make_event("W", 28, zero_fee=True)
        books = {f"W-{i}": make_book(f"W-{i}", 4_000, 5_500, depth_cq=10_000, levels=1)
                 for i in range(28)}
        basket = size_no_basket(ev, books)
        sim = ExecutionSimulator(ExecConfig(latency_ms=0, max_rate_limit_wait_ms=100))
        assert sim.submit(basket, ev, 1_000) is False
        assert sim.stats.rate_limited == 1


class TestDeduplication:
    def test_same_book_state_fires_once(self):
        """A dislocation arrives as many deltas; without this the engine fires
        the identical basket once per inbound message."""
        from kima.risk import book_state_hash

        ev, books = _overround()
        basket = size_no_basket(ev, books)
        h = book_state_hash(basket)
        sim = ExecutionSimulator(ExecConfig(latency_ms=0))
        accepted = [sim.submit(basket, ev, 1_000 + i, state_hash=h) for i in range(5)]
        assert accepted[0] is True
        assert not any(accepted[1:])
        assert sim.stats.deduped == 4


class TestUnequalBasketReconciliation:
    """A NO basket may legitimately hold unequal leg sizes.

    Payoff is ``sum(q) - max(q)``, so only the largest leg's excess over the
    second largest earns nothing. Trimming everything down to the smallest fill
    -- the natural rule for an equal-size basket -- would unwind most of a
    correctly sized one.
    """

    def _unequal(self):
        from kima.book import MarketBook
        ev = make_event("E", 4, zero_fee=True)
        books = {}
        for t in ("E-0", "E-1", "E-2"):
            bk = MarketBook(t)
            bk.apply_snapshot([(4_000, 50_000)], [(5_500, 50_000)], seq=1, ts_ms=1)
            books[t] = bk
        shallow = MarketBook("E-3")
        shallow.apply_snapshot([(9_500, 2_000)], [(400, 2_000)], seq=1, ts_ms=1)
        books["E-3"] = shallow
        return ev, books

    def test_a_full_fill_of_an_unequal_basket_has_no_residual(self):
        ev, books = self._unequal()
        basket = size_no_basket(ev, books)
        assert len({l.qty_cq for l in basket.legs}) > 1, "expected unequal legs"
        sim = ExecutionSimulator(ExecConfig(latency_ms=0))
        sim.submit(basket, ev, 1_000)
        sim.advance(1_000, books)
        r = sim.results[0]
        assert r.complete
        assert r.residual_cq == 0, "a correctly sized basket was partly unwound"
        assert r.net_mu == basket.profit_mu

    def test_the_trim_beats_both_fixed_rules(self):
        """The trim target is chosen by value, not by a rule. Check it against
        the two rules it replaced: keep everything, or balance down to the
        smallest fill."""
        ev, books = self._unequal()
        basket = size_no_basket(ev, books)
        sim = ExecutionSimulator(ExecConfig(latency_ms=10, unwind_delay_ms=0))
        sim.submit(basket, ev, 1_000)
        books["E-1"].apply_snapshot([(4_000, 1_000)], [(5_500, 50_000)], seq=2, ts_ms=2)
        sim.advance(2_000, books)
        r = sim.results[0]

        filled = sorted((f.filled_cq for f in r.fills if f.filled_cq > 0), reverse=True)
        prices = {f.ticker: books[f.ticker].best_bid(f.buy_side) for f in r.fills}

        def value(target):
            keeps = sorted((min(f.filled_cq, target) for f in r.fills
                            if min(f.filled_cq, target) > 0), reverse=True)
            payoff = (sum(keeps) - keeps[0]) * 10_000 if len(keeps) >= 2 else 0
            proceeds = sum((f.filled_cq - min(f.filled_cq, target)) * prices[f.ticker]
                           for f in r.fills)
            return payoff + proceeds

        chosen = r.worst_payoff_mu + r.unwind_proceeds_mu
        assert chosen >= value(filled[0])          # at least as good as keeping all
        assert chosen >= value(min(filled))        # and as balancing to the minimum


class TestLegOrderingIsRealisedInTime:
    """Sequential submission must actually meet a later book.

    Otherwise all three orderings produce identical results and the experiment
    could never discover anything.
    """

    def test_sequential_legs_carry_increasing_arrival_times(self):
        ev, books = _overround()
        basket = size_no_basket(ev, books)
        sim = ExecutionSimulator(
            ExecConfig(latency_ms=0, leg_order="thinnest_first", inter_leg_ms=25)
        )
        sim.submit(basket, ev, 1_000)
        sim.advance(5_000, books)
        stamps = [f.arrive_ts_ms for f in sim.results[0].fills]
        assert stamps == sorted(stamps)
        assert stamps[-1] - stamps[0] == 25 * (len(stamps) - 1)

    def test_batching_gives_every_leg_the_same_arrival(self):
        ev, books = _overround()
        basket = size_no_basket(ev, books)
        sim = ExecutionSimulator(ExecConfig(latency_ms=0, leg_order="batch", inter_leg_ms=25))
        sim.submit(basket, ev, 1_000)
        sim.advance(5_000, books)
        stamps = {f.arrive_ts_ms for f in sim.results[0].fills}
        assert len(stamps) == 1

    def test_a_leg_that_vanishes_mid_sequence_is_missed(self):
        """The book really does move between legs."""
        ev, books = _overround()
        basket = size_no_basket(ev, books)
        sim = ExecutionSimulator(
            ExecConfig(latency_ms=0, leg_order="cheapest_first", inter_leg_ms=25,
                       unwind_delay_ms=0)
        )
        sim.submit(basket, ev, 1_000)
        sim.advance(1_000, books)          # first leg only
        books["E-2"].apply_snapshot([], [(5_500, 10_000)], seq=2, ts_ms=1_010)
        sim.advance(5_000, books)          # remaining legs meet the new book
        r = sim.results[0]
        gone = next(f for f in r.fills if f.ticker == "E-2")
        assert gone.filled_cq == 0
        assert not r.complete
