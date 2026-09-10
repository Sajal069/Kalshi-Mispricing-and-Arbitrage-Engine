"""Contract metadata and strike geometry.

The engine never reads rules text. Everything it knows about *what a market
means* is derived mechanically from ``strike_type``, ``floor_strike`` and
``cap_strike``, which is what makes the subset lattice and the exhaustiveness
proof machine-checkable rather than a judgement call.

Markets whose ``strike_type`` is ``functional``, ``custom`` or ``structured``
are represented as :class:`Opaque` and are *excluded* from automatic inference
rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Literal, Optional, Sequence

from .fees import FeeSchedule, schedule_for
from .ticks import PriceGrid, grid_from_metadata

NEG_INF = Fraction(-10**18)
POS_INF = Fraction(10**18)

MarketStatus = Literal[
    "initialized", "inactive", "active", "closed", "determined",
    "disputed", "amended", "finalized", "paused",
]

TRADABLE_STATUSES = frozenset({"active"})

#: Strike types whose outcome region can be derived mechanically.
NUMERIC_STRIKE_TYPES = frozenset(
    {"greater", "greater_or_equal", "less", "less_or_equal", "between"}
)
OPAQUE_STRIKE_TYPES = frozenset({"functional", "custom", "structured"})


# --------------------------------------------------------------------------
# outcome regions
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Interval:
    """A region of the real line: the set of underlying values settling YES."""

    lo: Fraction
    hi: Fraction
    lo_closed: bool
    hi_closed: bool

    def contains_point(self, x: Fraction) -> bool:
        if x < self.lo or (x == self.lo and not self.lo_closed):
            return False
        if x > self.hi or (x == self.hi and not self.hi_closed):
            return False
        return True

    def is_empty(self) -> bool:
        if self.lo > self.hi:
            return True
        return self.lo == self.hi and not (self.lo_closed and self.hi_closed)

    def issubset(self, other: "Interval") -> bool:
        lo_ok = (other.lo < self.lo) or (
            other.lo == self.lo and (other.lo_closed or not self.lo_closed)
        )
        hi_ok = (other.hi > self.hi) or (
            other.hi == self.hi and (other.hi_closed or not self.hi_closed)
        )
        return self.is_empty() or (lo_ok and hi_ok)

    def overlaps(self, other: "Interval") -> bool:
        if self.is_empty() or other.is_empty():
            return False
        lo = max(self.lo, other.lo)
        hi = min(self.hi, other.hi)
        if lo > hi:
            return False
        if lo == hi:
            lo_closed = (self.lo_closed if self.lo == lo else True) and (
                other.lo_closed if other.lo == lo else True
            )
            hi_closed = (self.hi_closed if self.hi == hi else True) and (
                other.hi_closed if other.hi == hi else True
            )
            return lo_closed and hi_closed
        return True

    def __str__(self) -> str:
        lb = "[" if self.lo_closed else "("
        rb = "]" if self.hi_closed else ")"
        lo = "-inf" if self.lo <= NEG_INF else f"{float(self.lo):g}"
        hi = "+inf" if self.hi >= POS_INF else f"{float(self.hi):g}"
        return f"{lb}{lo}, {hi}{rb}"


@dataclass(frozen=True)
class Categorical:
    """A single named outcome (award winner, nominee, team)."""

    label: str

    def issubset(self, other: "Categorical") -> bool:
        return self.label == other.label

    def overlaps(self, other: "Categorical") -> bool:
        return self.label == other.label

    def __str__(self) -> str:
        return "{" + self.label + "}"


@dataclass(frozen=True)
class Opaque:
    """Region we refuse to infer. Blocks the market from geometric reasoning."""

    reason: str

    def issubset(self, other: object) -> bool:
        return False

    def overlaps(self, other: object) -> bool:
        return True  # conservative: assume it could collide with anything

    def __str__(self) -> str:
        return "<opaque: " + self.reason + ">"


Region = Interval | Categorical | Opaque


def region_from_strike(
    strike_type: str | None,
    floor_strike: Optional[Fraction | float | str],
    cap_strike: Optional[Fraction | float | str],
    *,
    label: str | None = None,
) -> Region:
    """Map documented strike metadata onto an outcome region."""
    st = (strike_type or "").lower()
    f = Fraction(str(floor_strike)) if floor_strike is not None else None
    c = Fraction(str(cap_strike)) if cap_strike is not None else None

    if st in OPAQUE_STRIKE_TYPES:
        return Opaque("strike_type=" + st)
    if st == "greater":
        if f is None:
            return Opaque("greater without floor_strike")
        return Interval(f, POS_INF, False, True)
    if st == "greater_or_equal":
        if f is None:
            return Opaque("greater_or_equal without floor_strike")
        return Interval(f, POS_INF, True, True)
    if st == "less":
        if c is None:
            return Opaque("less without cap_strike")
        return Interval(NEG_INF, c, True, False)
    if st == "less_or_equal":
        if c is None:
            return Opaque("less_or_equal without cap_strike")
        return Interval(NEG_INF, c, True, True)
    if st == "between":
        if f is None or c is None:
            return Opaque("between without both strikes")
        # Kalshi bucket markets publish inclusive endpoints; adjacency between
        # neighbouring buckets is resolved by the exhaustiveness prover.
        return Interval(f, c, True, True)
    if label is not None:
        return Categorical(label)
    return Opaque("unmapped strike_type=" + repr(st))


# --------------------------------------------------------------------------
# markets and events
# --------------------------------------------------------------------------
@dataclass
class Market:
    ticker: str
    event_ticker: str
    series_ticker: str = ""
    status: str = "active"
    strike_type: str | None = None
    floor_strike: Optional[Fraction] = None
    cap_strike: Optional[Fraction] = None
    yes_sub_title: str = ""
    close_ts_ms: int = 0
    settlement_timer_seconds: int = 0
    grid: PriceGrid = field(default_factory=PriceGrid.penny)
    region: Region = field(default_factory=lambda: Opaque("uninitialised"))
    #: Settlement outcome: "yes", "no", or "" while unsettled.
    result: str = ""
    settlement_ts_ms: int = 0

    @property
    def is_tradable(self) -> bool:
        return self.status in TRADABLE_STATUSES

    @property
    def is_opaque(self) -> bool:
        return isinstance(self.region, Opaque)

    @classmethod
    def from_api(cls, payload: dict, grid: PriceGrid | None = None) -> "Market":
        floor = payload.get("floor_strike")
        cap = payload.get("cap_strike")
        region = region_from_strike(
            payload.get("strike_type"),
            floor,
            cap,
            label=payload.get("yes_sub_title") or payload.get("ticker"),
        )
        return cls(
            ticker=payload["ticker"],
            event_ticker=payload.get("event_ticker", ""),
            series_ticker=payload.get("series_ticker", ""),
            status=payload.get("status", "active"),
            strike_type=payload.get("strike_type"),
            floor_strike=Fraction(str(floor)) if floor is not None else None,
            cap_strike=Fraction(str(cap)) if cap is not None else None,
            yes_sub_title=payload.get("yes_sub_title", ""),
            close_ts_ms=int(payload.get("close_ts_ms", 0) or 0),
            settlement_timer_seconds=int(payload.get("settlement_timer_seconds", 0) or 0),
            grid=grid or grid_from_metadata(
                payload.get("price_level_structure"), payload.get("price_ranges")
            ),
            region=region,
            result=payload.get("result", "") or "",
        )


@dataclass
class Event:
    event_ticker: str
    series_ticker: str = ""
    title: str = ""
    mutually_exclusive: bool = False
    markets: list[Market] = field(default_factory=list)
    fee_schedule: FeeSchedule = field(default_factory=FeeSchedule)
    #: Set by the exhaustiveness prover; never assumed.
    exhaustive: bool = False
    exhaustiveness_reason: str = "not proven"

    @property
    def tickers(self) -> list[str]:
        return [m.ticker for m in self.markets]

    def market(self, ticker: str) -> Market:
        for m in self.markets:
            if m.ticker == ticker:
                return m
        raise KeyError(ticker)

    @property
    def n_legs(self) -> int:
        return len(self.markets)

    @property
    def all_tradable(self) -> bool:
        return all(m.is_tradable for m in self.markets)

    @classmethod
    def from_api(cls, payload: dict, markets: Sequence[dict] | None = None) -> "Event":
        series = payload.get("series_ticker", "")
        mkts = [Market.from_api(m) for m in (markets or payload.get("markets", []))]
        for m in mkts:
            if not m.event_ticker:
                m.event_ticker = payload["event_ticker"]
            if not m.series_ticker:
                m.series_ticker = series
        return cls(
            event_ticker=payload["event_ticker"],
            series_ticker=series,
            title=payload.get("title", ""),
            mutually_exclusive=bool(payload.get("mutually_exclusive", False)),
            markets=mkts,
            fee_schedule=schedule_for(series or "DEFAULT"),
        )


def market_to_dict(m: Market) -> dict:
    return {
        "ticker": m.ticker,
        "event_ticker": m.event_ticker,
        "series_ticker": m.series_ticker,
        "status": m.status,
        "strike_type": m.strike_type,
        "floor_strike": str(m.floor_strike) if m.floor_strike is not None else None,
        "cap_strike": str(m.cap_strike) if m.cap_strike is not None else None,
        "yes_sub_title": m.yes_sub_title,
        "close_ts_ms": m.close_ts_ms,
        "settlement_timer_seconds": m.settlement_timer_seconds,
        "result": m.result,
        "settlement_ts_ms": m.settlement_ts_ms,
        "price_level_structure": m.grid.structure,
    }


def market_from_dict(d: dict) -> Market:
    floor = d.get("floor_strike")
    cap = d.get("cap_strike")
    region = region_from_strike(
        d.get("strike_type"), floor, cap, label=d.get("yes_sub_title") or d["ticker"]
    )
    return Market(
        ticker=d["ticker"],
        event_ticker=d.get("event_ticker", ""),
        series_ticker=d.get("series_ticker", ""),
        status=d.get("status", "active"),
        strike_type=d.get("strike_type"),
        floor_strike=Fraction(floor) if floor is not None else None,
        cap_strike=Fraction(cap) if cap is not None else None,
        yes_sub_title=d.get("yes_sub_title", ""),
        close_ts_ms=int(d.get("close_ts_ms", 0) or 0),
        settlement_timer_seconds=int(d.get("settlement_timer_seconds", 0) or 0),
        grid=grid_from_metadata(d.get("price_level_structure"), None),
        region=region,
        result=d.get("result", "") or "",
        settlement_ts_ms=int(d.get("settlement_ts_ms", 0) or 0),
    )


def event_to_dict(ev: Event) -> dict:
    return {
        "event_ticker": ev.event_ticker,
        "series_ticker": ev.series_ticker,
        "title": ev.title,
        "mutually_exclusive": ev.mutually_exclusive,
        "exhaustive": ev.exhaustive,
        "exhaustiveness_reason": ev.exhaustiveness_reason,
        "fee": {
            "series": ev.fee_schedule.series,
            "taker_multiplier": str(ev.fee_schedule.taker_multiplier),
            "maker_multiplier": str(ev.fee_schedule.maker_multiplier),
            "rounding": ev.fee_schedule.rounding,
        },
        "markets": [market_to_dict(m) for m in ev.markets],
    }


def event_from_dict(d: dict) -> Event:
    fee = d.get("fee", {})
    sched = FeeSchedule(
        series=fee.get("series", d.get("series_ticker", "DEFAULT")),
        taker_multiplier=Fraction(fee.get("taker_multiplier", "1")),
        maker_multiplier=Fraction(fee.get("maker_multiplier", "0")),
        rounding=fee.get("rounding", "cent"),
    )
    ev = Event(
        event_ticker=d["event_ticker"],
        series_ticker=d.get("series_ticker", ""),
        title=d.get("title", ""),
        mutually_exclusive=bool(d.get("mutually_exclusive", False)),
        markets=[market_from_dict(m) for m in d.get("markets", [])],
        fee_schedule=sched,
    )
    ev.exhaustive = bool(d.get("exhaustive", False))
    ev.exhaustiveness_reason = d.get("exhaustiveness_reason", "not proven")
    return ev


def subset_pairs(event: Event) -> list[tuple[str, str]]:
    """All ordered pairs ``(a, b)`` with ``outcome(a)`` a subset of ``outcome(b)``.

    These generate Condition N3 (monotone strike-ladder) checks. Opaque markets
    never participate, and identical regions are skipped because the relation is
    then symmetric and carries no arbitrage content.
    """
    out: list[tuple[str, str]] = []
    usable = [m for m in event.markets if not m.is_opaque]
    for a in usable:
        for b in usable:
            if a.ticker == b.ticker:
                continue
            if type(a.region) is not type(b.region):
                continue
            if a.region == b.region:
                continue
            if a.region.issubset(b.region):  # type: ignore[arg-type]
                out.append((a.ticker, b.ticker))
    return out
