"""Basket construction: ladder-depth-limited sizing with exact fees.

This is the stage-2 evaluator. The detector's O(1) screen says "something might
be here"; this module answers "how much, at what price, for what guaranteed
profit" by walking the actual resting ladders and charging the exact published
fee formula with its rounding.

Sizing structure, stated precisely
----------------------------------

For a NO basket the worst-case payoff of per-leg quantities ``q_i`` is
``sum(q) - max(q)``: at most one leg settles YES, so every leg but the largest
is guaranteed to pay. Fixing the largest leg at ``M`` makes the problem
**separable**::

    profit(M) = sum_i [ q_i - cost_i(q_i) - fee_i(q_i) ]  -  M
                subject to 0 <= q_i <= M

so each leg independently picks the ``q_i <= M`` that maximises its own
contribution, and we search only over the scalar ``M``. Legs whose best
contribution is negative simply drop out -- the subset choice never needs a
combinatorial search.

Note what this does *not* say. Equal sizing across all legs is optimal only
when the legs have identical ladders; in general **a leg that cannot fill ``M``
should be held at its own depth, not discarded.** Dropping it throws away a
strictly positive ``q_i - cost_i(q_i)``. An earlier version of this module did
exactly that, and the LP oracle in :mod:`kima.lp` is what caught it -- on a
four-leg basket with one cheap, shallow leg the error was worth about 16% of
the optimum.

A YES basket (N2) and a nested pair (N3) are different: their payoff is
``min(q)``, so quantity above the smallest leg buys nothing and equal sizing
genuinely is optimal there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Iterable, Sequence

from .book import NO, YES, MarketBook, WalkResult
from .events import Event
from .fees import FeeSchedule, fee_for_fills
from .units import CQ_PER_CONTRACT, MU_PER_CQ, NOTIONAL_CC, complement_cc


@dataclass
class SizingConfig:
    """Filters that separate an *actionable* opportunity from a notional one."""

    #: Depth filter: baskets smaller than this are dust and are discarded.
    min_qty_cq: int = CQ_PER_CONTRACT              # 1 contract
    #: Fat-finger / ladder-walk-bug guard.
    max_qty_cq: int = 500 * CQ_PER_CONTRACT        # 500 contracts
    #: Safety buffer epsilon, in microdollars per basket.
    epsilon_mu: int = 0
    #: Multi-level fee rounding reading; see kima.fees.fee_for_fills.
    fee_scope: str = "order"
    #: Cap on distinct sizes evaluated per opportunity (hot-path budget).
    max_breakpoints: int = 256


@dataclass
class Leg:
    ticker: str
    buy_side: int                       # book.YES or book.NO
    qty_cq: int
    limit_price_cc: int                 # worst price this leg would pay
    cost_mu: int
    fee_mu: int
    fills: list[tuple[int, int]] = field(default_factory=list)

    @property
    def side_name(self) -> str:
        return "yes" if self.buy_side == YES else "no"

    @property
    def avg_price_cc(self) -> int:
        return self.cost_mu // self.qty_cq if self.qty_cq else 0

    @property
    def total_mu(self) -> int:
        return self.cost_mu + self.fee_mu


@dataclass
class Basket:
    """A quantity-bounded, fee-inclusive, worst-case-positive trade intent."""

    condition: str                      # N1 | N2 | N3 | LP
    event_ticker: str
    qty_cq: int
    legs: list[Leg]
    cost_mu: int
    fee_mu: int
    worst_payoff_mu: int
    #: Worst-case profit, net of fees. Positive means arbitrage.
    profit_mu: int
    ts_ms: int = 0
    level: str = "L1"
    #: Cash actually posted (cost); collateral return is applied downstream.
    capital_mu: int = 0
    detail: str = ""

    @property
    def n_legs(self) -> int:
        return len(self.legs)

    @property
    def bottleneck_cq(self) -> int:
        """Smallest leg. Legs need not be equal, so "basket size" is ambiguous;
        this is the quantity the thinnest leg could actually support."""
        return min((l.qty_cq for l in self.legs), default=0)

    @property
    def edge_bps(self) -> float:
        """Profit as basis points of deployed capital."""
        if self.capital_mu <= 0:
            return 0.0
        return 10_000.0 * self.profit_mu / self.capital_mu

    @property
    def profit_per_contract_mu(self) -> float:
        if self.qty_cq <= 0:
            return 0.0
        return self.profit_mu * CQ_PER_CONTRACT / self.qty_cq

    def leg_key(self) -> tuple:
        return tuple(sorted((l.ticker, l.buy_side) for l in self.legs))


# --------------------------------------------------------------------------
# ladder helpers
# --------------------------------------------------------------------------
def _breakpoints(books: Sequence[MarketBook], buy_side: int, cfg: SizingConfig) -> list[int]:
    """Cumulative-depth breakpoints: the only sizes where the optimum can change.

    Profit is piecewise linear in ``q`` between consecutive ladder exhaustion
    points, so evaluating the breakpoints is exact, not a grid approximation.
    """
    pts: set[int] = set()
    for bk in books:
        resting = NO if buy_side == YES else YES
        cum = 0
        for _price, size in bk.iter_levels(resting, descending=True):
            cum += size
            if cum >= cfg.min_qty_cq:
                pts.add(min(cum, cfg.max_qty_cq))
            if cum >= cfg.max_qty_cq:
                break
    pts.add(cfg.min_qty_cq)
    pts.discard(0)
    out = sorted(p for p in pts if cfg.min_qty_cq <= p <= cfg.max_qty_cq)
    if len(out) > cfg.max_breakpoints:
        # Keep the extremes and thin the middle; profit is monotone within a
        # linear piece so this only ever under-reports, never over-reports.
        step = len(out) / cfg.max_breakpoints
        idx = sorted({int(i * step) for i in range(cfg.max_breakpoints)} | {len(out) - 1})
        out = [out[i] for i in idx]
    return out


def _make_leg(
    book: MarketBook,
    buy_side: int,
    qty_cq: int,
    schedule: FeeSchedule,
    cfg: SizingConfig,
) -> Leg | None:
    walk: WalkResult = book.walk_buy(buy_side, qty_cq)
    if walk.filled_cq < qty_cq:
        return None                     # this leg cannot support the size
    fills = [(f.price_cc, f.qty_cq) for f in walk.fills]
    fee = fee_for_fills(schedule, fills, scope=cfg.fee_scope)
    return Leg(
        ticker=book.ticker,
        buy_side=buy_side,
        qty_cq=qty_cq,
        limit_price_cc=walk.worst_price_cc,
        cost_mu=walk.cost_mu,
        fee_mu=fee,
        fills=fills,
    )


# --------------------------------------------------------------------------
# Condition N1 -- overround, NO basket
# --------------------------------------------------------------------------
def _leg_value_curve(
    book: MarketBook,
    buy_side: int,
    schedule: FeeSchedule,
    cfg: SizingConfig,
) -> list[tuple[int, Leg, int]]:
    """``[(qty, Leg, contribution)]`` at each of this leg's own depth breakpoints.

    ``contribution`` is ``q - cost(q) - fee(q)`` in microdollars: what the leg is
    worth to a NO basket, before the shared ``-M`` term. It is concave in ``q``
    because the ladder walk gets progressively worse.
    """
    out: list[tuple[int, Leg, int]] = []
    resting = NO if buy_side == YES else YES
    cum = 0
    for _price, size in book.iter_levels(resting, descending=True):
        cum += size
        q = min(cum, cfg.max_qty_cq)
        if q < cfg.min_qty_cq:
            continue
        leg = _make_leg(book, buy_side, q, schedule, cfg)
        if leg is not None:
            out.append((q, leg, q * MU_PER_CQ - leg.total_mu))
        if cum >= cfg.max_qty_cq:
            break
    return out


def size_no_basket(
    event: Event,
    books: dict[str, MarketBook],
    cfg: SizingConfig | None = None,
    *,
    ts_ms: int = 0,
    eligible: Iterable[str] | None = None,
    schedule: FeeSchedule | None = None,
) -> Basket | None:
    """Best NO basket over a mutually-exclusive event (Condition N1).

    Requires only mutual exclusivity: if *no* leg resolves YES the basket pays
    more than the worst case, so non-exhaustiveness is a free option, not a risk.

    Optimises ``profit(M) = sum_i max_{q<=M} contribution_i(q) - M`` over the
    candidate largest-leg sizes ``M``. Because the true largest leg may be
    smaller than ``M``, the reported profit is a lower bound at every ``M`` and
    exact at the optimal one -- so this never over-reports.
    """
    if not event.mutually_exclusive:
        return None
    cfg = cfg or SizingConfig()
    sched = schedule or event.fee_schedule
    tickers = list(eligible) if eligible is not None else event.tickers
    candidates = [books[t] for t in tickers if t in books and books[t].best_yes_bid >= 0]
    if len(candidates) < 2:
        return None

    curves = [(bk, _leg_value_curve(bk, NO, sched, cfg)) for bk in candidates]
    curves = [(bk, c) for bk, c in curves if c]
    if len(curves) < 2:
        return None

    m_candidates = sorted({q for _bk, curve in curves for q, _leg, _v in curve})
    if len(m_candidates) > cfg.max_breakpoints:
        step = len(m_candidates) / cfg.max_breakpoints
        idx = sorted({int(i * step) for i in range(cfg.max_breakpoints)} | {len(m_candidates) - 1})
        m_candidates = [m_candidates[i] for i in idx]

    best: Basket | None = None
    for m in m_candidates:
        legs: list[Leg] = []
        for bk, curve in curves:
            # Best of this leg's own breakpoints at or below M ...
            pick: Leg | None = None
            pick_val = 0
            for q, leg, val in curve:
                if q > m:
                    break
                if val > pick_val:
                    pick_val, pick = val, leg
            # ... and M itself, which need not be one of them.
            if curve[-1][0] >= m:
                leg_at_m = _make_leg(bk, NO, m, sched, cfg)
                if leg_at_m is not None:
                    val_at_m = m * MU_PER_CQ - leg_at_m.total_mu
                    if val_at_m > pick_val:
                        pick_val, pick = val_at_m, leg_at_m
            if pick is not None and pick_val > 0:
                legs.append(pick)
        if len(legs) < 2:
            continue
        cost = sum(l.cost_mu for l in legs)
        fee = sum(l.fee_mu for l in legs)
        largest = max(l.qty_cq for l in legs)
        worst_payoff = (sum(l.qty_cq for l in legs) - largest) * MU_PER_CQ
        profit = worst_payoff - cost - fee - cfg.epsilon_mu
        if profit <= 0:
            continue
        if best is None or profit > best.profit_mu:
            detail = f"buy NO on {len(legs)}/{event.n_legs} legs"
            if len({l.qty_cq for l in legs}) > 1:
                detail += " (unequal sizes: thin legs held at their own depth)"
            best = Basket(
                condition="N1",
                event_ticker=event.event_ticker,
                qty_cq=largest,
                legs=legs,
                cost_mu=cost,
                fee_mu=fee,
                worst_payoff_mu=worst_payoff,
                profit_mu=profit,
                ts_ms=ts_ms,
                capital_mu=cost + fee,
                detail=detail,
            )
    return best


# --------------------------------------------------------------------------
# Condition N2 -- underround, YES basket
# --------------------------------------------------------------------------
def size_yes_basket(
    event: Event,
    books: dict[str, MarketBook],
    cfg: SizingConfig | None = None,
    *,
    ts_ms: int = 0,
    schedule: FeeSchedule | None = None,
) -> Basket | None:
    """Best YES basket over a proven partition (Condition N2).

    Gated hard on the exhaustiveness certificate: without it an unlisted outcome
    can win and the whole basket expires worthless. Every leg must participate --
    dropping one reopens the same hole.
    """
    if not (event.mutually_exclusive and event.exhaustive):
        return None
    cfg = cfg or SizingConfig()
    sched = schedule or event.fee_schedule
    candidates = [books[t] for t in event.tickers if t in books]
    if len(candidates) != event.n_legs or any(b.best_no_bid < 0 for b in candidates):
        return None

    best: Basket | None = None
    for q in _breakpoints(candidates, YES, cfg):
        legs: list[Leg] = []
        for bk in candidates:
            leg = _make_leg(bk, YES, q, sched, cfg)
            if leg is None:
                legs = []
                break                   # every leg is mandatory
            legs.append(leg)
        if not legs:
            continue
        cost = sum(l.cost_mu for l in legs)
        fee = sum(l.fee_mu for l in legs)
        worst_payoff = q * MU_PER_CQ    # exactly one leg pays, on every outcome
        profit = worst_payoff - cost - fee - cfg.epsilon_mu
        if profit <= 0:
            continue
        if best is None or profit > best.profit_mu:
            best = Basket(
                condition="N2",
                event_ticker=event.event_ticker,
                qty_cq=q,
                legs=legs,
                cost_mu=cost,
                fee_mu=fee,
                worst_payoff_mu=worst_payoff,
                profit_mu=profit,
                ts_ms=ts_ms,
                capital_mu=cost + fee,
                detail=f"buy YES on all {len(legs)} legs of a proven partition",
            )
    return best


# --------------------------------------------------------------------------
# Condition N3 -- nested strike ladder
# --------------------------------------------------------------------------
def size_nested_pair(
    event: Event,
    books: dict[str, MarketBook],
    inner: str,
    outer: str,
    cfg: SizingConfig | None = None,
    *,
    ts_ms: int = 0,
    schedule: FeeSchedule | None = None,
) -> Basket | None:
    """Buy YES on the superset ``outer`` and NO on the subset ``inner``.

    Because ``outcome(inner)`` is contained in ``outcome(outer)``, ``X_inner = 1``
    implies ``X_outer = 1``, so the pair pays at least $1 in every state.
    """
    cfg = cfg or SizingConfig()
    sched = schedule or event.fee_schedule
    if inner not in books or outer not in books:
        return None
    b_in, b_out = books[inner], books[outer]
    if b_in.best_yes_bid < 0 or b_out.best_no_bid < 0:
        return None

    best: Basket | None = None
    for q in _breakpoints([b_in, b_out], YES, cfg) + _breakpoints([b_in, b_out], NO, cfg):
        leg_out = _make_leg(b_out, YES, q, sched, cfg)
        leg_in = _make_leg(b_in, NO, q, sched, cfg)
        if leg_out is None or leg_in is None:
            continue
        legs = [leg_out, leg_in]
        cost = leg_out.cost_mu + leg_in.cost_mu
        fee = leg_out.fee_mu + leg_in.fee_mu
        worst_payoff = q * MU_PER_CQ
        profit = worst_payoff - cost - fee - cfg.epsilon_mu
        if profit <= 0:
            continue
        if best is None or profit > best.profit_mu:
            best = Basket(
                condition="N3",
                event_ticker=event.event_ticker,
                qty_cq=q,
                legs=legs,
                cost_mu=cost,
                fee_mu=fee,
                worst_payoff_mu=worst_payoff,
                profit_mu=profit,
                ts_ms=ts_ms,
                capital_mu=cost + fee,
                detail=f"YES {outer} (superset) + NO {inner} (subset)",
            )
    return best


# --------------------------------------------------------------------------
# L0 -- the naive screener's number
# --------------------------------------------------------------------------
def l0_edge_mu(event: Event, books: dict[str, MarketBook], condition: str) -> int:
    """Frictionless top-of-book edge for one basket: no fees, no depth limit.

    This is the upper bound a retail screener reports. It exists here purely so
    the study can quote the ratio between it and reality.
    """
    if condition == "N1":
        bids = [books[t].best_yes_bid for t in event.tickers if t in books]
        bids = [b for b in bids if b >= 0]
        if len(bids) < 2:
            return 0
        excess_cc = sum(bids) - NOTIONAL_CC
        return max(0, excess_cc) * CQ_PER_CONTRACT
    if condition == "N2":
        asks = []
        for t in event.tickers:
            bk = books.get(t)
            if bk is None or bk.best_no_bid < 0:
                return 0
            asks.append(complement_cc(bk.best_no_bid))
        if len(asks) != event.n_legs:
            return 0
        deficit_cc = NOTIONAL_CC - sum(asks)
        return max(0, deficit_cc) * CQ_PER_CONTRACT
    return 0


# --------------------------------------------------------------------------
# edge-versus-size curve (E4)
# --------------------------------------------------------------------------
def edge_curve(
    event: Event,
    books: dict[str, MarketBook],
    condition: str,
    cfg: SizingConfig | None = None,
) -> list[tuple[int, int]]:
    """``[(qty_cq, worst_case_profit_mu)]`` across every attainable size.

    The marginal basket is always the worst one, so this curve is the honest
    picture that a single top-of-book number hides.
    """
    cfg = cfg or SizingConfig()
    sched = event.fee_schedule
    out: list[tuple[int, int]] = []
    if condition == "N1":
        books_l = [books[t] for t in event.tickers if t in books and books[t].best_yes_bid >= 0]
        side = NO
    elif condition == "N2":
        books_l = [books[t] for t in event.tickers if t in books]
        side = YES
    else:
        return out
    if not books_l:
        return out
    for q in _breakpoints(books_l, side, cfg):
        legs = [l for l in (_make_leg(bk, side, q, sched, cfg) for bk in books_l) if l is not None]
        if condition == "N2" and len(legs) != event.n_legs:
            continue
        if condition == "N1":
            legs = [l for l in legs if q * MU_PER_CQ - l.total_mu > 0]
            if len(legs) < 2:
                continue
            payoff = (len(legs) - 1) * q * MU_PER_CQ
        else:
            payoff = q * MU_PER_CQ
        profit = payoff - sum(l.cost_mu for l in legs) - sum(l.fee_mu for l in legs)
        out.append((q, profit))
    return out


def min_viable_size(curve: Sequence[tuple[int, int]]) -> int | None:
    """Smallest size at which the basket clears its own fees (hypothesis H6)."""
    for q, profit in curve:
        if profit > 0:
            return q
    return None


#: A fee-free schedule used by the rejection funnel to isolate the depth filter
#: from the fee filter: whatever survives this but dies with real fees was
#: killed by fees, not by liquidity.
ZERO_FEE = FeeSchedule(series="__zero__", taker_multiplier=Fraction(0), maker_multiplier=Fraction(0))
