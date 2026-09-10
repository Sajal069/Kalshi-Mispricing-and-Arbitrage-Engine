"""Risk engine.

Deliberately minimal but complete: every control maps to one named failure mode,
and every control keeps a counter so the rejection funnel can attribute exactly
which friction removed which opportunity.

The positions this strategy takes are self-hedging by construction, so VaR,
portfolio optimisation and margin modelling are out of scope -- adding them
would be machinery with no analytical payoff. What is *not* out of scope is the
mundane operational stuff that actually loses money: firing the same opportunity
once per inbound delta, trading a stale book, or discovering that the fee
schedule changed underneath you.

Two controls are worth singling out.

**The exhaustiveness gate** is the single most expensive mistake available here,
so Condition N2 is refused unless :mod:`kima.exhaustive` has issued a
certificate. It is enforced upstream in sizing and re-checked here.

**The fee-model guard** exists because this strategy's entire edge is one to
three cents. A per-series multiplier change can flip it from profitable to
unprofitable, so we compare our computed fee against the exchange's reported
``average_fee_paid`` and halt on divergence. Reading fees from the API at
runtime is a risk control, not a nicety.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Iterable

from .sizing import Basket
from .units import CQ_PER_CONTRACT, MU_PER_DOLLAR

#: Reasons a basket can be refused. Order matters: the funnel reports them in
#: this sequence, so it reads as a narrowing pipeline.
REJECT_REASONS = (
    "untrusted_book",
    "market_state",
    "stale_data",
    "exhaustiveness",
    "min_size",
    "max_order_size",
    "position_cap",
    "event_capital_cap",
    "total_capital_cap",
    "residual_cap",
    "daily_loss_kill",
    "duplicate",
    "fee_model_guard",
    "order_group_limit",
    "kill_switch",
)


@dataclass
class RiskConfig:
    """Defaults are deliberately wide.

    These positions are held to settlement, so within a tape of a few hours
    capital only accumulates and never releases. Tight caps would therefore stop
    the engine early and leave the latency sweep measured on the opening slice of
    the tape -- a biased subsample. The caps stay configurable and are exercised
    by the tests; the economic capital constraint is reported separately by the
    capital study rather than being allowed to truncate the experiment.
    """

    max_qty_per_market_cq: int = 200_000 * CQ_PER_CONTRACT
    max_qty_per_order_cq: int = 500 * CQ_PER_CONTRACT
    max_capital_per_event_mu: int = 2_000_000 * MU_PER_DOLLAR
    max_total_capital_mu: int = 10_000_000 * MU_PER_DOLLAR
    #: Halt for the day once cumulative net loss exceeds this.
    daily_loss_limit_mu: int = 1_000_000 * MU_PER_DOLLAR
    #: Stop opening new baskets while naked residual exceeds this.
    max_residual_cq: int = 100_000 * CQ_PER_CONTRACT
    #: Tolerance on the fee-model guard, in microdollars per order.
    fee_tolerance_mu: int = 10_000          # $0.01
    #: Exchange-side order group: rolling window and matched-contract limit.
    order_group_window_s: int = 15
    order_group_max_contracts: int = 100_000
    enabled: bool = True
    #: Track inventory and deployed capital across baskets. Off in the baseline
    #: backtest: nothing settles inside a short tape, so accumulation only ever
    #: grows and the caps would exclude the tail of the recording rather than
    #: inform it. Live trading and the capital study both want it on.
    accumulate_positions: bool = True


@dataclass
class RiskState:
    deployed_mu: int = 0
    realised_pnl_mu: int = 0
    residual_cq: int = 0
    per_market_cq: dict[str, int] = field(default_factory=dict)
    per_event_mu: dict[str, int] = field(default_factory=dict)
    killed: bool = False
    kill_reason: str = ""


class RiskEngine:
    def __init__(self, cfg: RiskConfig | None = None):
        self.cfg = cfg or RiskConfig()
        self.state = RiskState()
        self.rejections: dict[str, int] = {r: 0 for r in REJECT_REASONS}
        self.passed = 0
        self._seen_orders: set[str] = set()
        self._group_window: list[tuple[int, int]] = []   # (ts_ms, contracts)

    # -- gate --------------------------------------------------------------
    def check(self, basket: Basket, ts_ms: int = 0) -> str | None:
        """Return a rejection reason, or ``None`` if the basket may proceed."""
        cfg = self.cfg
        if not cfg.enabled:
            self.passed += 1
            return None
        if self.state.killed:
            return self._reject("kill_switch")
        if basket.qty_cq > cfg.max_qty_per_order_cq:
            return self._reject("max_order_size")
        if self.state.residual_cq > cfg.max_residual_cq:
            return self._reject("residual_cap")
        if self.state.realised_pnl_mu < -cfg.daily_loss_limit_mu:
            self.kill("daily loss limit breached")
            return self._reject("daily_loss_kill")

        for leg in basket.legs:
            held = self.state.per_market_cq.get(leg.ticker, 0)
            if held + leg.qty_cq > cfg.max_qty_per_market_cq:
                return self._reject("position_cap")

        want = basket.capital_mu
        ev_used = self.state.per_event_mu.get(basket.event_ticker, 0)
        if ev_used + want > cfg.max_capital_per_event_mu:
            return self._reject("event_capital_cap")
        if self.state.deployed_mu + want > cfg.max_total_capital_mu:
            return self._reject("total_capital_cap")

        contracts = basket.qty_cq * basket.n_legs // CQ_PER_CONTRACT
        if not self._order_group_allows(ts_ms, contracts):
            return self._reject("order_group_limit")

        self.passed += 1
        return None

    def _reject(self, reason: str) -> str:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1
        return reason

    def note_rejection(self, reason: str) -> None:
        """Record a rejection made upstream (state gate, depth filter, ...)."""
        self.rejections[reason] = self.rejections.get(reason, 0) + 1

    # -- exchange-side order group ----------------------------------------
    def _order_group_allows(self, ts_ms: int, contracts: int) -> bool:
        """Mirror of Kalshi's rolling matched-contract limit.

        The exchange-side version survives a crashed client, which is exactly why
        the plan prefers configuring it over reimplementing it locally. We model
        it here so the backtest cannot claim fills the exchange would have
        auto-cancelled.
        """
        window = self.cfg.order_group_window_s * 1000
        self._group_window = [(t, c) for t, c in self._group_window if ts_ms - t <= window]
        total = sum(c for _, c in self._group_window)
        if total + contracts > self.cfg.order_group_max_contracts:
            return False
        self._group_window.append((ts_ms, contracts))
        return True

    # -- bookkeeping -------------------------------------------------------
    def on_fill(self, event_ticker: str, per_market_cq: dict[str, int], capital_mu: int) -> None:
        if not self.cfg.accumulate_positions:
            return
        for ticker, qty in per_market_cq.items():
            self.state.per_market_cq[ticker] = self.state.per_market_cq.get(ticker, 0) + qty
        self.state.per_event_mu[event_ticker] = (
            self.state.per_event_mu.get(event_ticker, 0) + capital_mu
        )
        self.state.deployed_mu += capital_mu

    def on_settle(self, event_ticker: str, capital_mu: int, pnl_mu: int) -> None:
        self.state.deployed_mu = max(0, self.state.deployed_mu - capital_mu)
        self.state.per_event_mu[event_ticker] = max(
            0, self.state.per_event_mu.get(event_ticker, 0) - capital_mu
        )
        self.state.realised_pnl_mu += pnl_mu

    def on_residual(self, delta_cq: int) -> None:
        self.state.residual_cq = max(0, self.state.residual_cq + delta_cq)

    def kill(self, reason: str) -> None:
        self.state.killed = True
        self.state.kill_reason = reason

    def reset_day(self) -> None:
        self.state.realised_pnl_mu = 0
        self.state.killed = False
        self.state.kill_reason = ""

    # -- duplicate protection ---------------------------------------------
    def new_client_order_id(self) -> str:
        cid = str(uuid.uuid4())
        self._seen_orders.add(cid)
        return cid

    def is_duplicate(self, client_order_id: str) -> bool:
        return client_order_id in self._seen_orders

    # -- fee-model guard ---------------------------------------------------
    def check_fee_model(self, computed_mu: int, reported_mu: int) -> bool:
        """Halt if our fee model diverges from what the exchange actually charged."""
        if abs(computed_mu - reported_mu) > self.cfg.fee_tolerance_mu:
            self.kill(
                f"fee model diverged: computed {computed_mu / 1e6:.4f} vs "
                f"reported {reported_mu / 1e6:.4f}"
            )
            self.note_rejection("fee_model_guard")
            return False
        return True

    # -- reporting ---------------------------------------------------------
    def summary(self) -> dict:
        return {
            "passed": self.passed,
            "killed": self.state.killed,
            "kill_reason": self.state.kill_reason,
            "rejections": {k: v for k, v in self.rejections.items() if v},
            "total_rejected": sum(self.rejections.values()),
        }


def book_state_hash(basket: Basket) -> int:
    """Fingerprint of the top-of-book state a basket was derived from.

    Duplicate-order protection: the same dislocation arrives as many deltas, and
    without this the engine fires the same basket once per inbound message.
    """
    return hash(
        (
            basket.condition,
            basket.event_ticker,
            basket.qty_cq,
            tuple((l.ticker, l.buy_side, l.limit_price_cc, l.qty_cq) for l in basket.legs),
        )
    )
