"""Risk controls.

Every control gets two tests: one that deliberately fires it, and one that
confirms it does not fire spuriously. A control that has only ever been tested
in the "does not fire" direction is not a control, it is decoration.
"""

from fractions import Fraction

import pytest

from kima.risk import RiskConfig, RiskEngine, book_state_hash
from kima.sizing import Basket, Leg
from kima.units import CQ_PER_CONTRACT, MU_PER_DOLLAR


def basket(qty_contracts: float = 10, n_legs: int = 3, capital_dollars: float = 100.0,
           event: str = "E") -> Basket:
    q = int(qty_contracts * CQ_PER_CONTRACT)
    legs = [
        Leg(ticker=f"{event}-{i}", buy_side=1, qty_cq=q, limit_price_cc=6_000,
            cost_mu=6_000 * q, fee_mu=0)
        for i in range(n_legs)
    ]
    cap = int(capital_dollars * MU_PER_DOLLAR)
    return Basket(condition="N1", event_ticker=event, qty_cq=q, legs=legs,
                  cost_mu=cap, fee_mu=0, worst_payoff_mu=cap * 2,
                  profit_mu=cap, capital_mu=cap)


class TestOrderSizeCap:
    def test_fires(self):
        r = RiskEngine(RiskConfig(max_qty_per_order_cq=5 * CQ_PER_CONTRACT))
        assert r.check(basket(qty_contracts=50)) == "max_order_size"

    def test_does_not_fire_below_the_cap(self):
        r = RiskEngine(RiskConfig(max_qty_per_order_cq=100 * CQ_PER_CONTRACT))
        assert r.check(basket(qty_contracts=50)) is None


class TestPositionCap:
    def test_fires_after_accumulating(self):
        r = RiskEngine(RiskConfig(max_qty_per_market_cq=15 * CQ_PER_CONTRACT))
        b = basket(qty_contracts=10)
        assert r.check(b) is None
        r.on_fill(b.event_ticker, {l.ticker: l.qty_cq for l in b.legs}, b.capital_mu)
        assert r.check(b) == "position_cap"

    def test_does_not_fire_on_a_fresh_book(self):
        r = RiskEngine(RiskConfig(max_qty_per_market_cq=100 * CQ_PER_CONTRACT))
        assert r.check(basket(qty_contracts=10)) is None


class TestCapitalCaps:
    def test_total_capital_cap_fires(self):
        r = RiskEngine(RiskConfig(max_total_capital_mu=250 * MU_PER_DOLLAR))
        b = basket(capital_dollars=100)
        for _ in range(2):
            assert r.check(b) is None
            r.on_fill(b.event_ticker, {}, b.capital_mu)
        assert r.check(b) == "total_capital_cap"

    def test_per_event_cap_fires_before_the_total(self):
        r = RiskEngine(RiskConfig(
            max_capital_per_event_mu=150 * MU_PER_DOLLAR,
            max_total_capital_mu=10_000 * MU_PER_DOLLAR,
        ))
        b = basket(capital_dollars=100)
        assert r.check(b) is None
        r.on_fill(b.event_ticker, {}, b.capital_mu)
        assert r.check(b) == "event_capital_cap"
        # A different event is unaffected.
        assert r.check(basket(capital_dollars=100, event="OTHER")) is None

    def test_settlement_releases_capital(self):
        r = RiskEngine(RiskConfig(max_total_capital_mu=150 * MU_PER_DOLLAR))
        b = basket(capital_dollars=100)
        r.check(b)
        r.on_fill(b.event_ticker, {}, b.capital_mu)
        assert r.check(b) == "total_capital_cap"
        r.on_settle(b.event_ticker, b.capital_mu, pnl_mu=0)
        assert r.check(b) is None


class TestDailyLossKill:
    def test_fires_and_latches(self):
        r = RiskEngine(RiskConfig(daily_loss_limit_mu=50 * MU_PER_DOLLAR))
        r.on_settle("E", 0, pnl_mu=-60 * MU_PER_DOLLAR)
        assert r.check(basket()) == "daily_loss_kill"
        assert r.state.killed
        # Latched: further baskets are refused by the kill switch, not re-evaluated.
        assert r.check(basket()) == "kill_switch"

    def test_does_not_fire_within_the_limit(self):
        r = RiskEngine(RiskConfig(daily_loss_limit_mu=50 * MU_PER_DOLLAR))
        r.on_settle("E", 0, pnl_mu=-10 * MU_PER_DOLLAR)
        assert r.check(basket()) is None

    def test_reset_day_clears_the_latch(self):
        r = RiskEngine(RiskConfig(daily_loss_limit_mu=50 * MU_PER_DOLLAR))
        r.on_settle("E", 0, pnl_mu=-60 * MU_PER_DOLLAR)
        r.check(basket())
        r.reset_day()
        assert r.check(basket()) is None


class TestResidualCap:
    def test_fires(self):
        r = RiskEngine(RiskConfig(max_residual_cq=10 * CQ_PER_CONTRACT))
        r.on_residual(50 * CQ_PER_CONTRACT)
        assert r.check(basket()) == "residual_cap"

    def test_clears_when_the_residual_is_unwound(self):
        r = RiskEngine(RiskConfig(max_residual_cq=10 * CQ_PER_CONTRACT))
        r.on_residual(50 * CQ_PER_CONTRACT)
        r.on_residual(-50 * CQ_PER_CONTRACT)
        assert r.check(basket()) is None


class TestOrderGroupLimit:
    """Mirrors Kalshi's rolling matched-contract limit.

    The exchange-side version survives a crashed client, which is why the plan
    prefers configuring it over reimplementing it. Modelling it here stops the
    backtest claiming fills the exchange would have auto-cancelled.
    """

    def test_fires_within_the_rolling_window(self):
        r = RiskEngine(RiskConfig(order_group_max_contracts=50, order_group_window_s=15))
        b = basket(qty_contracts=10, n_legs=3)     # 30 contracts per basket
        assert r.check(b, ts_ms=0) is None
        assert r.check(b, ts_ms=1_000) == "order_group_limit"

    def test_window_rolls_off(self):
        r = RiskEngine(RiskConfig(order_group_max_contracts=50, order_group_window_s=15))
        b = basket(qty_contracts=10, n_legs=3)
        assert r.check(b, ts_ms=0) is None
        assert r.check(b, ts_ms=20_000) is None    # the earlier fill has aged out


class TestFeeModelGuard:
    """The edge is one to three cents; a silent multiplier change flips its sign."""

    def test_halts_on_divergence(self):
        r = RiskEngine(RiskConfig(fee_tolerance_mu=10_000))
        assert not r.check_fee_model(computed_mu=1_680_000, reported_mu=3_360_000)
        assert r.state.killed
        assert "fee model diverged" in r.state.kill_reason

    def test_tolerates_rounding_noise(self):
        r = RiskEngine(RiskConfig(fee_tolerance_mu=10_000))
        assert r.check_fee_model(computed_mu=1_680_000, reported_mu=1_685_000)
        assert not r.state.killed


class TestKillSwitch:
    def test_blocks_everything_once_thrown(self):
        r = RiskEngine()
        assert r.check(basket()) is None
        r.kill("manual")
        assert r.check(basket()) == "kill_switch"


class TestDuplicateSuppression:
    def test_identical_book_state_hashes_identically(self):
        a, b = basket(), basket()
        assert book_state_hash(a) == book_state_hash(b)

    def test_a_price_change_changes_the_hash(self):
        a = basket()
        b = basket()
        b.legs[0].limit_price_cc += 1
        assert book_state_hash(a) != book_state_hash(b)

    def test_client_order_ids_are_unique(self):
        r = RiskEngine()
        ids = {r.new_client_order_id() for _ in range(200)}
        assert len(ids) == 200
        assert all(r.is_duplicate(i) for i in ids)


class TestDisabled:
    def test_disabling_the_engine_passes_everything(self):
        r = RiskEngine(RiskConfig(enabled=False, max_qty_per_order_cq=1))
        assert r.check(basket(qty_contracts=10_000)) is None


class TestSummary:
    def test_summary_reports_only_triggered_reasons(self):
        r = RiskEngine(RiskConfig(max_qty_per_order_cq=CQ_PER_CONTRACT))
        r.check(basket(qty_contracts=50))
        s = r.summary()
        assert s["rejections"] == {"max_order_size": 1}
        assert s["total_rejected"] == 1


class TestAccumulationSwitch:
    """Inventory tracking is opt-in.

    Nothing settles inside a short tape, so accumulation only ever grows. A cap
    that binds partway through would exclude the tail of the recording and bias
    every number measured after it, so the baseline backtest turns it off and
    reports the capital constraint separately.
    """

    def test_disabled_means_caps_never_accumulate(self):
        r = RiskEngine(RiskConfig(
            accumulate_positions=False,
            max_total_capital_mu=150 * MU_PER_DOLLAR,
            max_qty_per_market_cq=15 * CQ_PER_CONTRACT,
        ))
        b = basket(qty_contracts=10, capital_dollars=100)
        for _ in range(50):
            assert r.check(b) is None
            r.on_fill(b.event_ticker, {l.ticker: l.qty_cq for l in b.legs}, b.capital_mu)
        assert r.state.deployed_mu == 0
        assert r.state.per_market_cq == {}

    def test_enabled_still_binds(self):
        r = RiskEngine(RiskConfig(
            accumulate_positions=True,
            max_total_capital_mu=150 * MU_PER_DOLLAR,
        ))
        b = basket(capital_dollars=100)
        assert r.check(b) is None
        r.on_fill(b.event_ticker, {}, b.capital_mu)
        assert r.check(b) == "total_capital_cap"

    def test_per_order_controls_still_apply_when_disabled(self):
        """Turning off accumulation must not disable the stateless controls."""
        r = RiskEngine(RiskConfig(
            accumulate_positions=False,
            max_qty_per_order_cq=5 * CQ_PER_CONTRACT,
        ))
        assert r.check(basket(qty_contracts=50)) == "max_order_size"
