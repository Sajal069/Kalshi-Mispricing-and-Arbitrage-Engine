"""Execution simulator: L0, L1 and L2.

The gap between the three levels *is* the result of this project.

======  ===========================  ==============  =========  ==================
Level   Depth                        Fees            Latency    Fills
======  ===========================  ==============  =========  ==================
L0      top-of-book, unlimited       none            0          always full
L1      real ladder, capped          exact + rounding 0         always full
L2      real ladder at ``t+delta``   exact + rounding delta     per leg, partial
======  ===========================  ==============  =========  ==================

L2 is where the intellectual content lives, because Kalshi has **no cross-market
atomicity**. A partially filled basket is not an arbitrage -- it is a directional
position acquired at a bad price. So the simulator models legs as independent
orders, permits partial fills, computes the naked residual, and charges a second
taker fee to unwind it.

No look-ahead is required to do this. The simulator is a replay *listener*: when
an opportunity is detected at ``t`` it queues the order to arrive at ``t+delta``
and then simply waits for the replay clock to reach that point. The book it
matches against is whatever the tape has actually delivered by then.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .book import NO, YES, MarketBook
from .events import Event
from .fees import FeeSchedule, fee_for_fills
from .sizing import Basket, Leg
from .units import CQ_PER_CONTRACT, MU_PER_CQ

#: The latency ladder from the plan. Rung 0 is the control: L2(0) must reproduce L1.
LATENCY_RUNGS_MS: tuple[int, ...] = (0, 5, 10, 25, 50, 100, 250, 500, 1_000, 2_000)

LEG_ORDER_POLICIES = ("batch", "thinnest_first", "cheapest_first")
RESIDUAL_POLICIES = ("unwind", "hold")


@dataclass
class ExecConfig:
    """One execution regime. The sweep instantiates many of these."""

    latency_ms: int = 0
    #: Portion of ``latency_ms`` attributable to our own processing rather than
    #: the network. Reported separately because only this part is controllable.
    decision_ms: int = 0
    order_type: str = "IOC"                 # IOC | FOK
    leg_order: str = "batch"
    residual_policy: str = "unwind"
    #: Delay between consecutive legs when not batching. Batch submission
    #: collapses N round trips into one and minimises inter-leg dispersion.
    inter_leg_ms: int = 25
    #: Delay before a naked residual can be crossed back.
    unwind_delay_ms: int = 100
    #: Basic-tier token bucket: 100 write tokens/s, 10 tokens per order.
    rate_limit_tokens_per_s: int = 100
    tokens_per_order: int = 10
    burst_seconds: float = 1.0
    enforce_rate_limit: bool = True
    #: Give up if the write budget cannot clear the basket within this long.
    #: Beyond it the opportunity is stale anyway, so the order is never sent.
    max_rate_limit_wait_ms: float = 5_000.0
    #: Suppress re-firing on an unchanged book state.
    dedupe: bool = True

    @property
    def label(self) -> str:
        rl = "/ratelimit" if self.enforce_rate_limit else ""
        return (
            f"d{self.latency_ms}ms/{self.order_type}/{self.leg_order}/"
            f"{self.residual_policy}{rl}"
        )


class TokenBucket:
    """Write-side rate limiter.

    At Basic tier an N-leg basket costs ``10N`` tokens against a 100 tokens/s
    budget, so a 5-leg Fed basket consumes half a second of write capacity. When
    many events dislocate at once -- exactly what a macro print causes -- the
    rate limit, not the network, can be the binding constraint.
    """

    def __init__(self, tokens_per_s: int, burst_seconds: float = 1.0):
        self.rate = tokens_per_s
        self.capacity = tokens_per_s * burst_seconds
        self.tokens = float(self.capacity)
        self.last_ms: int | None = None

    def refill(self, ts_ms: int) -> None:
        if self.last_ms is None:
            self.last_ms = ts_ms
            return
        dt = max(0, ts_ms - self.last_ms) / 1000.0
        self.tokens = min(self.capacity, self.tokens + dt * self.rate)
        self.last_ms = ts_ms

    def try_consume(self, ts_ms: int, n: int) -> bool:
        self.refill(ts_ms)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def reserve(self, ts_ms: int, n: int) -> float:
        """Book ``n`` tokens and return how long, in ms, until the last is spent.

        Orders go out one at a time, so a basket needing more tokens than the
        bucket can hold is not refused -- it is stretched. Draining the bucket
        and then spending at the refill rate gives the wait exactly, and moving
        the bucket clock to the end of that window serialises the next caller
        behind this one rather than letting both spend the same tokens.
        """
        self.refill(ts_ms)
        if self.tokens >= n:
            self.tokens -= n
            return 0.0
        deficit = n - self.tokens
        self.tokens = 0.0
        wait_s = deficit / self.rate if self.rate else float("inf")
        self.last_ms = ts_ms + int(wait_s * 1000)
        return wait_s * 1000


@dataclass
class LegFill:
    ticker: str
    buy_side: int
    requested_cq: int
    filled_cq: int
    cost_mu: int
    fee_mu: int
    limit_price_cc: int
    arrive_ts_ms: int
    #: Contracts retained after residual handling -- what is still held at
    #: settlement, and therefore what the ex-post payoff is computed on.
    kept_cq: int = 0

    @property
    def complete(self) -> bool:
        return self.filled_cq >= self.requested_cq

    @property
    def avg_price_cc(self) -> int:
        return self.cost_mu // self.filled_cq if self.filled_cq else 0


@dataclass
class ExecutedBasket:
    """The realised outcome of one attempted basket."""

    condition: str
    event_ticker: str
    series_ticker: str
    detect_ts_ms: int
    arrive_ts_ms: int
    config_label: str
    latency_ms: int
    intended_qty_cq: int
    n_legs_intended: int
    fills: list[LegFill] = field(default_factory=list)
    balanced_qty_cq: int = 0
    cost_mu: int = 0
    fee_mu: int = 0
    worst_payoff_mu: int = 0
    residual_cq: int = 0
    unwind_proceeds_mu: int = 0
    unwind_fee_mu: int = 0
    net_mu: int = 0
    theoretical_mu: int = 0
    l0_mu: int = 0
    status: str = "filled"
    #: Milliseconds the write budget added before the first leg could be sent.
    rate_delay_ms: int = 0
    #: Realised payoff of the retained position, filled in by the ex-post
    #: settlement join. ``None`` when outcomes are not available.
    settled_payoff_mu: int | None = None
    settled_net_mu: int | None = None

    @property
    def complete(self) -> bool:
        return all(f.complete for f in self.fills) and len(self.fills) == self.n_legs_intended

    @property
    def any_fill(self) -> bool:
        return any(f.filled_cq > 0 for f in self.fills)

    @property
    def fill_ratio(self) -> float:
        want = sum(f.requested_cq for f in self.fills) or 1
        return sum(f.filled_cq for f in self.fills) / want

    @property
    def capital_mu(self) -> int:
        return self.cost_mu + self.fee_mu

    @property
    def edge_retention(self) -> float:
        if self.theoretical_mu <= 0:
            return 0.0
        return self.net_mu / self.theoretical_mu

    def to_row(self) -> dict:
        return {
            "condition": self.condition,
            "event_ticker": self.event_ticker,
            "series": self.series_ticker,
            "detect_ts_ms": self.detect_ts_ms,
            "arrive_ts_ms": self.arrive_ts_ms,
            "config": self.config_label,
            "latency_ms": self.latency_ms,
            "intended_qty": self.intended_qty_cq / CQ_PER_CONTRACT,
            "balanced_qty": self.balanced_qty_cq / CQ_PER_CONTRACT,
            "n_legs": self.n_legs_intended,
            "n_legs_filled": sum(1 for f in self.fills if f.filled_cq > 0),
            "complete": self.complete,
            "fill_ratio": self.fill_ratio,
            "cost": self.cost_mu / 1e6,
            "fees": self.fee_mu / 1e6,
            "worst_payoff": self.worst_payoff_mu / 1e6,
            "residual": self.residual_cq / CQ_PER_CONTRACT,
            "unwind_proceeds": self.unwind_proceeds_mu / 1e6,
            "unwind_fee": self.unwind_fee_mu / 1e6,
            "net": self.net_mu / 1e6,
            "theoretical_l1": self.theoretical_mu / 1e6,
            "l0": self.l0_mu / 1e6,
            "capital": self.capital_mu / 1e6,
            "status": self.status,
        }


@dataclass
class ExecStats:
    submitted: int = 0
    executed: int = 0
    complete: int = 0
    partial: int = 0
    missed: int = 0
    rate_limited: int = 0
    rate_delayed: int = 0
    rate_delay_ms_total: float = 0.0
    deduped: int = 0
    risk_blocked: int = 0
    legs_requested: int = 0
    legs_filled_full: int = 0
    legs_filled_partial: int = 0
    legs_missed: int = 0

    def to_dict(self) -> dict:
        return {
            "submitted": self.submitted,
            "executed": self.executed,
            "complete_baskets": self.complete,
            "partial_baskets": self.partial,
            "missed_baskets": self.missed,
            "rate_limited": self.rate_limited,
            "rate_delayed": self.rate_delayed,
            "mean_rate_delay_ms": (
                self.rate_delay_ms_total / self.rate_delayed if self.rate_delayed else 0.0
            ),
            "deduped": self.deduped,
            "risk_blocked": self.risk_blocked,
            "legs_requested": self.legs_requested,
            "legs_filled_full": self.legs_filled_full,
            "legs_filled_partial": self.legs_filled_partial,
            "legs_missed": self.legs_missed,
            "basket_completion_rate": self.complete / self.executed if self.executed else 0.0,
            "leg_fill_rate": self.legs_filled_full / self.legs_requested if self.legs_requested else 0.0,
        }


# --------------------------------------------------------------------------
def _worst_case_payoff_mu(condition: str, filled: Sequence[int], n_intended: int) -> int:
    """Guaranteed payoff of an actually-filled (possibly unbalanced) position.

    N1  ``sum(q) - max(q)``: at most one leg resolves YES, so every NO leg but
        the largest is guaranteed to pay.
    N2  ``min(q)``: exactly one leg pays $1, so the floor is the smallest leg;
        a missing leg drops the floor to zero, which is the whole point.
    N3  ``min(q_outer, q_inner)``.
    """
    if not filled:
        return 0
    if condition == "N1":
        if len(filled) < 2:
            return 0
        return (sum(filled) - max(filled)) * MU_PER_CQ
    if condition == "N2":
        if len(filled) < n_intended:
            return 0
        return min(filled) * MU_PER_CQ
    if condition == "N3":
        if len(filled) < 2:
            return 0
        return min(filled) * MU_PER_CQ
    return 0


class ExecutionSimulator:
    """Runs one execution regime over the replay, keeping its own accounting."""

    def __init__(self, cfg: ExecConfig):
        self.cfg = cfg
        self.stats = ExecStats()
        self.results: list[ExecutedBasket] = []
        self._pending: list[tuple[int, int, str, object]] = []
        self._counter = 0
        self._bucket = TokenBucket(cfg.rate_limit_tokens_per_s, cfg.burst_seconds)
        self._last_fire: dict[tuple[str, str], int] = {}

    # -- submission --------------------------------------------------------
    def submit(
        self,
        basket: Basket,
        event: Event,
        detect_ts_ms: int,
        *,
        l0_mu: int = 0,
        state_hash: int = 0,
    ) -> bool:
        cfg = self.cfg
        self.stats.submitted += 1

        if cfg.dedupe:
            key = (basket.event_ticker, basket.condition)
            if self._last_fire.get(key) == state_hash:
                self.stats.deduped += 1
                return False
            self._last_fire[key] = state_hash

        rate_delay_ms = 0.0
        if cfg.enforce_rate_limit:
            need = cfg.tokens_per_order * basket.n_legs
            rate_delay_ms = self._bucket.reserve(detect_ts_ms, need)
            if rate_delay_ms > cfg.max_rate_limit_wait_ms:
                self.stats.rate_limited += 1
                return False
            if rate_delay_ms:
                self.stats.rate_delayed += 1
                self.stats.rate_delay_ms_total += rate_delay_ms

        arrive = detect_ts_ms + cfg.latency_ms + int(rate_delay_ms)
        order = self._order_legs(basket)
        self._push(arrive, "arrive",
                   (basket, event, detect_ts_ms, order, l0_mu, int(rate_delay_ms)))
        return True

    def _order_legs(self, basket: Basket) -> list[Leg]:
        """Leg submission order.

        Sending the thinnest leg first is the hypothesis worth testing: the
        bottleneck leg caps the whole basket, so if it vanishes nothing else
        matters. Cheapest-first is the naive alternative.
        """
        if self.cfg.leg_order == "thinnest_first":
            return sorted(basket.legs, key=lambda l: l.qty_cq)
        if self.cfg.leg_order == "cheapest_first":
            return sorted(basket.legs, key=lambda l: l.avg_price_cc)
        return list(basket.legs)

    # -- clock -------------------------------------------------------------
    @property
    def has_pending(self) -> bool:
        """Cheap guard: the replay calls into every sim on every message."""
        return bool(self._pending)

    def advance(self, ts_ms: int, books: dict[str, MarketBook]) -> None:
        """Execute anything whose arrival time the replay clock has reached."""
        while self._pending and self._pending[0][0] <= ts_ms:
            _, _, kind, payload = heapq.heappop(self._pending)
            self._dispatch(kind, payload, books)

    def flush(self, ts_ms: int, books: dict[str, MarketBook]) -> None:
        while self._pending:
            _, _, kind, payload = heapq.heappop(self._pending)
            self._dispatch(kind, payload, books)

    def _dispatch(self, kind: str, payload, books: dict[str, MarketBook]) -> None:
        if kind == "arrive":
            basket, event, detect_ts, order, l0_mu, rate_delay = payload
            self._execute(basket, event, detect_ts, order, l0_mu, books, rate_delay)
        elif kind == "leg":
            result, leg, event, leg_ts = payload
            self._fill_leg(result, leg, event, books, leg_ts)
        elif kind == "reconcile":
            result, basket, event = payload
            self._reconcile(result, basket, event, books)
        elif kind == "unwind":
            self._unwind(payload, books)

    def _push(self, ts_ms: int, kind: str, payload) -> None:
        self._counter += 1
        heapq.heappush(self._pending, (ts_ms, self._counter, kind, payload))

    # -- matching ----------------------------------------------------------
    def _execute(
        self,
        basket: Basket,
        event: Event,
        detect_ts: int,
        order: Sequence[Leg],
        l0_mu: int,
        books: dict[str, MarketBook],
        rate_delay_ms: int = 0,
    ) -> None:
        cfg = self.cfg
        arrive = detect_ts + cfg.latency_ms + rate_delay_ms
        result = ExecutedBasket(
            condition=basket.condition,
            event_ticker=basket.event_ticker,
            series_ticker=event.series_ticker,
            detect_ts_ms=detect_ts,
            arrive_ts_ms=arrive,
            config_label=cfg.label,
            latency_ms=cfg.latency_ms,
            intended_qty_cq=basket.qty_cq,
            n_legs_intended=len(order),
            theoretical_mu=basket.profit_mu,
            l0_mu=l0_mu,
            rate_delay_ms=rate_delay_ms,
        )
        self.stats.executed += 1

        if cfg.leg_order == "batch" or cfg.inter_leg_ms <= 0 or len(order) < 2:
            # One round trip: every leg meets the same book.
            for leg in order:
                self._fill_leg(result, leg, event, books, arrive)
            self._reconcile(result, basket, event, books)
            return

        # Sequential submission: each leg meets the book as it stands when that
        # leg actually arrives, which is the whole point of the comparison.
        for i, leg in enumerate(order):
            self._push(arrive + i * cfg.inter_leg_ms, "leg", (result, leg, event, arrive + i * cfg.inter_leg_ms))
        self._push(
            arrive + (len(order) - 1) * cfg.inter_leg_ms,
            "reconcile",
            (result, basket, event),
        )

    def _fill_leg(
        self,
        result: ExecutedBasket,
        leg: Leg,
        event: Event,
        books: dict[str, MarketBook],
        leg_ts: int,
    ) -> None:
        """Match one leg against the book as it stands right now."""
        cfg = self.cfg
        sched: FeeSchedule = event.fee_schedule
        bk = books.get(leg.ticker)
        self.stats.legs_requested += 1
        if bk is None or not bk.trusted:
            result.fills.append(
                LegFill(leg.ticker, leg.buy_side, leg.qty_cq, 0, 0, 0, leg.limit_price_cc, leg_ts)
            )
            self.stats.legs_missed += 1
            return
        walk = bk.walk_buy(leg.buy_side, leg.qty_cq, leg.limit_price_cc)
        filled = walk.filled_cq
        if cfg.order_type == "FOK" and filled < leg.qty_cq:
            # All-or-nothing per leg: eliminates intra-leg partials but does
            # nothing about cross-leg exposure, and it kills the fill outright.
            filled, cost, fee = 0, 0, 0
        else:
            cost = walk.cost_mu
            fee = fee_for_fills(sched, [(f.price_cc, f.qty_cq) for f in walk.fills])
        result.fills.append(
            LegFill(leg.ticker, leg.buy_side, leg.qty_cq, filled, cost, fee,
                    leg.limit_price_cc, leg_ts)
        )
        result.cost_mu += cost
        result.fee_mu += fee
        if filled >= leg.qty_cq:
            self.stats.legs_filled_full += 1
        elif filled > 0:
            self.stats.legs_filled_partial += 1
        else:
            self.stats.legs_missed += 1

    def _reconcile(
        self, result: ExecutedBasket, basket: Basket, event: Event, books: dict[str, MarketBook]
    ) -> None:
        """Classify the fill, then decide what to do with anything naked.

        Under the *unwind* policy the trim target is not a fixed rule. It cannot
        be: for a NO basket the guaranteed payoff is ``sum(q) - max(q)``, so what
        a contract is worth keeping depends on how many legs are tied at the top,
        and what it is worth selling depends on the bid it can be sold into.

        So we ask :meth:`_best_trim` whether anything is worth selling at all. If
        nothing is, the basket is finished here. If something is, the sale is
        scheduled and re-decided against the later book, because crossing back
        takes time.
        """
        kept = [f for f in result.fills if f.filled_cq > 0]
        if not kept:
            result.status = "missed"
            result.net_mu = 0
            self.stats.missed += 1
            self.results.append(result)
            return

        if result.complete:
            result.status = "filled"
            self.stats.complete += 1
        else:
            result.status = "partial"
            self.stats.partial += 1

        filled = [f.filled_cq for f in kept]
        result.balanced_qty_cq = max(filled)
        for f in result.fills:
            f.kept_cq = f.filled_cq
        result.worst_payoff_mu = _worst_case_payoff_mu(
            result.condition, filled, result.n_legs_intended
        )
        result.residual_cq = 0

        if self.cfg.residual_policy == "unwind":
            # Check now whether anything is worth selling. A correctly sized
            # basket that filled completely has nothing naked in it -- the
            # sizer buys while marginal cost is below the marginal guaranteed
            # payoff, and the unwind sells while the *bid* is above it, so a
            # positive spread means the two can never both want to act.
            # Skipping the round trip in that case is what keeps L2(0)
            # identical to L1 rather than "L1 evaluated 100ms later".
            target, _ = self._best_trim(result, books)
            if all(f.filled_cq <= target for f in result.fills):
                result.balanced_qty_cq = target
                self._finalise(result)
                return
            self._push(result.arrive_ts_ms + self.cfg.unwind_delay_ms,
                       "unwind", (result, event, 0))
            return
        # Hold-to-settlement: keep everything and mark at the guaranteed floor.
        # An ex-post settlement join replaces that with the realised outcome.
        self._finalise(result)

    def _best_trim(
        self, result: ExecutedBasket, books: dict[str, MarketBook]
    ) -> tuple[int, int]:
        """Pick the trim target that maximises payoff plus sale proceeds.

        Trimming every leg to the smallest fill is the natural rule for an
        equal-size basket, and it is what the plan specifies -- but it is only
        *sometimes* right. Reducing the ``k`` legs tied at the top by one each
        costs ``k-1`` of guaranteed payoff and returns ``k`` sale prices, so it
        pays exactly when the legs are cheap enough to sell well relative to how
        many are tied. Rather than encode that as a rule, evaluate every distinct
        candidate level (there are at most one per leg) and take the best.
        """
        fills = [f for f in result.fills if f.filled_cq > 0]
        prices: dict[str, int] = {}
        for f in fills:
            bk = books.get(f.ticker)
            prices[f.ticker] = bk.best_bid(f.buy_side) if bk is not None else 0
            if prices[f.ticker] < 0:
                prices[f.ticker] = 0

        best_t, best_value = None, None
        for t in sorted({f.filled_cq for f in fills} | {0}, reverse=True):
            keeps = [min(f.filled_cq, t) for f in fills]
            payoff = _worst_case_payoff_mu(
                result.condition,
                [k for k in keeps if k > 0],
                result.n_legs_intended,
            )
            proceeds = sum(
                (f.filled_cq - min(f.filled_cq, t)) * prices[f.ticker]
                for f in fills
            )
            value = payoff + proceeds
            if best_value is None or value > best_value:
                best_value, best_t = value, t
        return best_t or 0, best_value or 0

    def _unwind(self, payload: tuple, books: dict[str, MarketBook]) -> None:
        result, event, _ = payload
        sched: FeeSchedule = event.fee_schedule
        target, _value = self._best_trim(result, books)
        result.balanced_qty_cq = target

        for f in result.fills:
            keep = min(f.filled_cq, target)
            excess = f.filled_cq - keep
            f.kept_cq = keep
            if excess <= 0:
                continue
            bk = books.get(f.ticker)
            if bk is None or not bk.trusted:
                # Cannot sell it; it stays on the book and is credited nothing.
                f.kept_cq = f.filled_cq
                continue
            walk = bk.walk_sell(f.buy_side, excess)
            if walk.filled_cq < excess:
                # Partially unsellable: the remainder is still held.
                f.kept_cq = keep + (excess - walk.filled_cq)
            result.unwind_proceeds_mu += walk.cost_mu
            result.unwind_fee_mu += fee_for_fills(
                sched, [(fl.price_cc, fl.qty_cq) for fl in walk.fills]
            )
            result.residual_cq += walk.filled_cq

        held = [f.kept_cq for f in result.fills if f.kept_cq > 0]
        result.worst_payoff_mu = _worst_case_payoff_mu(
            result.condition, held, result.n_legs_intended
        )
        self._finalise(result)

    def _finalise(self, result: ExecutedBasket) -> None:
        if self.cfg.residual_policy == "unwind":
            result.net_mu = (
                result.worst_payoff_mu
                - result.cost_mu
                - result.fee_mu
                + result.unwind_proceeds_mu
                - result.unwind_fee_mu
            )
        else:
            # Hold-to-settlement: the residual is marked at its worst case, i.e.
            # zero, unless an ex-post settlement join replaces this later.
            result.net_mu = result.worst_payoff_mu - result.cost_mu - result.fee_mu
        self.results.append(result)

    # -- reporting ---------------------------------------------------------
    @property
    def net_mu(self) -> int:
        return sum(r.net_mu for r in self.results)

    @property
    def capital_mu(self) -> int:
        return sum(r.capital_mu for r in self.results)

    def summary(self) -> dict:
        n = len(self.results)
        theo = sum(r.theoretical_mu for r in self.results)
        l0 = sum(r.l0_mu for r in self.results)
        return {
            "config": self.cfg.label,
            "latency_ms": self.cfg.latency_ms,
            "order_type": self.cfg.order_type,
            "leg_order": self.cfg.leg_order,
            "residual_policy": self.cfg.residual_policy,
            "rate_limited_config": self.cfg.enforce_rate_limit,
            "baskets": n,
            "net_pnl": self.net_mu / 1e6,
            "gross_l0": l0 / 1e6,
            "theoretical_l1": theo / 1e6,
            "capital_deployed": self.capital_mu / 1e6,
            "edge_retention_vs_l1": (self.net_mu / theo) if theo else 0.0,
            "edge_retention_vs_l0": (self.net_mu / l0) if l0 else 0.0,
            "residual_contracts": sum(r.residual_cq for r in self.results) / CQ_PER_CONTRACT,
            "unwind_cost": sum(r.unwind_fee_mu for r in self.results) / 1e6,
            **self.stats.to_dict(),
        }
