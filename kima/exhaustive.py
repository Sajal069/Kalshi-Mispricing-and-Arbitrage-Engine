"""Exhaustiveness prover and outcome-partition builder.

This module exists to prevent the single most expensive mistake available in
this strategy: trading Condition N2 (the YES basket) on an event that is
mutually exclusive but *not* exhaustive. Kalshi documents that "the markets do
not need to exhaust every possible outcome", so ``mutually_exclusive = true``
says nothing at all about whether the legs partition the sample space.

The prover therefore refuses by default and emits a signed-in-prose
:class:`Certificate` for every event explaining exactly why it did or did not
conclude exhaustiveness. Certificates are meant to be spot-checked by hand.

It also builds the finite outcome partition used by the LP oracle: the cells
``w_1 .. w_K`` induced by the arrangement of all legs' outcome regions, plus an
explicit ``outside`` cell whenever exhaustiveness is not proven.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable, Sequence

from .events import NEG_INF, POS_INF, Categorical, Event, Interval, Opaque, Region

#: Sub-titles that denote a residual "none of the listed outcomes" market.
FIELD_LABELS = (
    "another", "other", "someone else", "field", "any other",
    "none of the above", "all others", "other candidate",
)


@dataclass
class Certificate:
    """Machine-checkable record of an exhaustiveness decision."""

    event_ticker: str
    exhaustive: bool
    reason: str
    method: str = "none"
    n_legs: int = 0
    n_opaque: int = 0
    gaps: list[str] = field(default_factory=list)
    overlaps: list[str] = field(default_factory=list)
    underlying_tick: Fraction | None = None

    def to_dict(self) -> dict:
        return {
            "event_ticker": self.event_ticker,
            "exhaustive": self.exhaustive,
            "reason": self.reason,
            "method": self.method,
            "n_legs": self.n_legs,
            "n_opaque": self.n_opaque,
            "gaps": self.gaps,
            "overlaps": self.overlaps,
            "underlying_tick": str(self.underlying_tick) if self.underlying_tick is not None else None,
        }

    def render(self) -> str:
        head = "EXHAUSTIVE" if self.exhaustive else "NOT EXHAUSTIVE"
        lines = [
            f"[{head}] {self.event_ticker}  ({self.n_legs} legs, {self.n_opaque} opaque)",
            f"  method: {self.method}",
            f"  reason: {self.reason}",
        ]
        for g in self.gaps:
            lines.append(f"  gap:     {g}")
        for o in self.overlaps:
            lines.append(f"  overlap: {o}")
        return "\n".join(lines)


def _looks_like_field_market(label: str) -> bool:
    low = label.lower()
    return any(tok in low for tok in FIELD_LABELS)


def _infer_underlying_tick(intervals: Sequence[Interval]) -> Fraction | None:
    """Smallest positive gap between adjacent bucket boundaries.

    Kalshi publishes bucket endpoints inclusively (e.g. ``[3.00, 3.24]`` then
    ``[3.25, 3.49]``), so a "gap" of exactly one underlying tick is not a hole in
    the sample space -- the underlying cannot land strictly between them. We
    infer that tick from the geometry itself rather than hardcoding it, and we
    record it on the certificate so the assumption is visible.
    """
    finite = sorted(
        (iv for iv in intervals if iv.lo > NEG_INF and iv.hi < POS_INF),
        key=lambda iv: iv.lo,
    )
    gaps: list[Fraction] = []
    for a, b in zip(finite, finite[1:]):
        d = b.lo - a.hi
        if d > 0:
            gaps.append(d)
    if not gaps:
        return None
    return min(gaps)


def prove_exhaustive(
    event: Event,
    *,
    allow_field_market: bool = True,
    curated_exhaustive: frozenset[str] | set[str] | None = None,
) -> Certificate:
    """Decide, from strike geometry alone, whether the legs partition Omega.

    ``curated_exhaustive`` is a human-audited allowlist of series tickers (e.g. a
    two-outcome game series with no draw) where exhaustiveness is real but is not
    expressible in strike metadata. Entries are honoured only for events that are
    already flagged mutually exclusive, and the resulting certificate is stamped
    ``method="curated"`` so the assertion stays visible for review rather than
    disappearing into a boolean.
    """
    cert = Certificate(
        event_ticker=event.event_ticker,
        exhaustive=False,
        reason="not proven",
        n_legs=event.n_legs,
        n_opaque=sum(1 for m in event.markets if m.is_opaque),
    )

    if not event.mutually_exclusive:
        cert.reason = "event is not flagged mutually_exclusive; N2 is inapplicable"
        cert.method = "flag"
        return cert
    if event.n_legs == 0:
        cert.reason = "event has no markets"
        return cert
    if curated_exhaustive and event.series_ticker in curated_exhaustive:
        cert.exhaustive = True
        cert.method = "curated"
        cert.reason = (
            f"series {event.series_ticker} is on the human-audited exhaustive "
            "allowlist; strike metadata alone does not prove this"
        )
        return cert
    if cert.n_opaque:
        opaque = [m.ticker for m in event.markets if m.is_opaque]
        cert.reason = (
            f"{cert.n_opaque} leg(s) have functional/custom/structured strikes "
            f"({', '.join(opaque[:3])}); geometry cannot be inferred"
        )
        cert.method = "opaque-exclusion"
        return cert

    regions = [m.region for m in event.markets]
    kinds = {type(r) for r in regions}
    if len(kinds) > 1:
        cert.reason = "event mixes interval and categorical legs; no uniform geometry"
        cert.method = "mixed"
        return cert

    if kinds == {Categorical}:
        return _prove_categorical(event, cert, allow_field_market=allow_field_market)
    return _prove_intervals(event, cert)


def _prove_categorical(event: Event, cert: Certificate, *, allow_field_market: bool) -> Certificate:
    cert.method = "categorical"
    labels = [m.region.label for m in event.markets]  # type: ignore[union-attr]
    dupes = {l for l in labels if labels.count(l) > 1}
    if dupes:
        cert.overlaps = [f"duplicate outcome label {l!r}" for l in sorted(dupes)]
        cert.reason = "duplicate categorical labels contradict mutual exclusivity"
        return cert
    field_markets = [m.ticker for m in event.markets if _looks_like_field_market(
        m.yes_sub_title or m.region.label  # type: ignore[union-attr]
    )]
    if allow_field_market and field_markets:
        cert.exhaustive = True
        cert.reason = (
            f"a residual field market is present ({field_markets[0]}), so the listed "
            "outcomes plus the field cover the sample space"
        )
        return cert
    cert.reason = (
        "categorical outcome list with no residual field market; an unlisted "
        "outcome can win, so the YES basket can expire worthless"
    )
    return cert


def _prove_intervals(event: Event, cert: Certificate) -> Certificate:
    cert.method = "interval-cover"
    ivs: list[tuple[str, Interval]] = [
        (m.ticker, m.region) for m in event.markets  # type: ignore[misc]
    ]
    ivs.sort(key=lambda t: (t[1].lo, t[1].hi))

    tick = _infer_underlying_tick([iv for _, iv in ivs])
    cert.underlying_tick = tick
    tol = tick if tick is not None else Fraction(0)

    # Overlap check: mutual exclusivity is contractual, so geometric overlap is
    # a metadata inconsistency worth surfacing loudly.
    for i in range(len(ivs)):
        for j in range(i + 1, len(ivs)):
            if ivs[i][1].overlaps(ivs[j][1]):
                cert.overlaps.append(
                    f"{ivs[i][0]} {ivs[i][1]} overlaps {ivs[j][0]} {ivs[j][1]}"
                )
    if cert.overlaps:
        cert.reason = "legs overlap geometrically despite the mutually_exclusive flag"
        return cert

    # Cover check: left tail, interior gaps, right tail.
    if ivs[0][1].lo > NEG_INF:
        cert.gaps.append(f"uncovered left tail below {float(ivs[0][1].lo):g}")
    reach_hi = ivs[0][1].hi
    reach_closed = ivs[0][1].hi_closed
    for ticker, iv in ivs[1:]:
        step = iv.lo - reach_hi
        contiguous = step <= 0 or (step <= tol and reach_closed and iv.lo_closed)
        if not contiguous:
            cert.gaps.append(
                f"gap between {float(reach_hi):g} and {float(iv.lo):g} before {ticker}"
            )
        if iv.hi > reach_hi:
            reach_hi, reach_closed = iv.hi, iv.hi_closed
    if reach_hi < POS_INF:
        cert.gaps.append(f"uncovered right tail above {float(reach_hi):g}")

    if cert.gaps:
        cert.reason = (
            f"{len(cert.gaps)} uncovered region(s) in the strike ladder; "
            "the underlying can settle outside every listed bucket"
        )
        return cert

    cert.exhaustive = True
    tick_note = f" (adjacent buckets separated by one underlying tick of {tick})" if tick else ""
    cert.reason = (
        f"the {event.n_legs} bucket(s) form a contiguous, non-overlapping cover of "
        f"the real line{tick_note}"
    )
    return cert


# --------------------------------------------------------------------------
# outcome partition (for the LP oracle and worst-case enumeration)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Cell:
    """One elementary outcome cell: a set of tickers that settle YES together."""

    name: str
    yes_tickers: frozenset[str]

    @property
    def is_outside(self) -> bool:
        return len(self.yes_tickers) == 0


def outcome_partition(event: Event) -> list[Cell]:
    """Finite partition of Omega induced by the legs of ``event``.

    Guarantees:

    * every cell is elementary -- no leg boundary passes through its interior,
      so leg membership is constant on it;
    * an ``outside`` cell (no leg pays) is included whenever the legs do not
      provably cover the space, which is what makes worst-case enumeration
      honest for non-exhaustive events;
    * the function returns an **empty list** rather than a guess when it cannot
      justify a partition. Callers must treat that as "no reasoning possible",
      never as "no constraints". An empty partition silently interpreted as a
      single cell in which nothing settles YES would make every NO leg look
      guaranteed and manufacture arbitrage out of nothing.

    Note the ordering of the two branches. ``mutually_exclusive`` is a
    contractual fact published by the exchange, so when it holds the partition
    follows immediately -- one cell per leg -- with no appeal to strike geometry
    at all. Geometry is only needed for events that are *not* flagged exclusive,
    such as nested threshold ladders.
    """
    usable = [m for m in event.markets if not m.is_opaque]

    if event.mutually_exclusive and event.markets:
        cells = [Cell(m.ticker, frozenset({m.ticker})) for m in event.markets]
        if not event.exhaustive:
            cells.append(Cell("outside", frozenset()))
        return cells

    if not usable or len(usable) != len(event.markets):
        # Some leg has functional/custom/structured strikes and the event is not
        # flagged mutually exclusive, so there is nothing we can soundly assume.
        return []

    if all(isinstance(m.region, Categorical) for m in usable):
        cells = [
            Cell(m.region.label, frozenset({m.ticker}))  # type: ignore[union-attr]
            for m in usable
        ]
        if not event.exhaustive:
            cells.append(Cell("outside", frozenset()))
        return cells

    intervals = [(m.ticker, m.region) for m in usable if isinstance(m.region, Interval)]
    if len(intervals) != len(usable):
        return []
    points: set[Fraction] = {NEG_INF, POS_INF}
    for _, iv in intervals:
        points.add(iv.lo)
        points.add(iv.hi)
    ordered = sorted(points)

    probes: list[tuple[str, Fraction]] = []
    for i, p in enumerate(ordered):
        probes.append((f"x={float(p):g}", p))
        if i + 1 < len(ordered):
            mid = (p + ordered[i + 1]) / 2
            probes.append((f"({float(p):g},{float(ordered[i + 1]):g})", mid))

    cells: list[Cell] = []
    seen: set[frozenset[str]] = set()
    for name, x in probes:
        members = frozenset(t for t, iv in intervals if iv.contains_point(x))
        if members in seen:
            continue
        # A proven-exhaustive ladder still shows geometric holes: Kalshi bucket
        # endpoints are inclusive, so (e_i, e_i + tick) sits between neighbours.
        # The prover has already established the underlying cannot land there,
        # so those cells are unreachable and must not be enumerated -- keeping
        # them would invent a state in which the YES basket pays nothing.
        if not members and event.exhaustive:
            continue
        seen.add(members)
        cells.append(Cell(name if members else "outside", members))
    if not event.exhaustive and not any(c.is_outside for c in cells):
        cells.append(Cell("outside", frozenset()))
    return cells


def certificates(events: Iterable[Event], **kwargs) -> dict[str, Certificate]:
    """Prove every event and stamp the result back onto the :class:`Event`."""
    out: dict[str, Certificate] = {}
    for ev in events:
        cert = prove_exhaustive(ev, **kwargs)
        ev.exhaustive = cert.exhaustive
        ev.exhaustiveness_reason = cert.reason
        out[ev.event_ticker] = cert
    return out
