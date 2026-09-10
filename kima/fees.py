"""Exact Kalshi fee arithmetic.

Published schedule (eff. 7 July 2026)::

    taker: fees = round_up( M * 0.07   * C * P * (1-P) )
    maker: fees = round_up( M * 0.0175 * C * P * (1-P) )

with ``P`` the price in dollars, ``C`` the contract count and ``M`` a
per-series multiplier (taker M defaults to 1, maker M defaults to 0).

Two properties of this formula drive the whole study and are therefore
implemented exactly rather than approximately:

1. ``round_up`` applies to the **order**, not the contract, so per-contract fee
   is a decreasing step function of size.  There is consequently a *minimum
   viable basket size* below which no arbitrage clears its own fees.
2. The rounding granularity is ambiguous in the published text (the prose says
   "rounded to a centicent", the accompanying table shows whole cents).  We do
   not guess: :class:`FeeSchedule` carries a ``rounding`` field, the default is
   the conservative whole-cent reading, and :func:`calibrate_rounding` recovers
   the true rule from observed ``average_fee_paid`` values.

All arithmetic is exact rational (``fractions.Fraction``) over integer units
from :mod:`kima.units`; no float ever touches a fee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable, Literal, Mapping

from .units import MU_PER_CENT, MU_PER_CENTICENT, NOTIONAL_CC

Rounding = Literal["cent", "centicent", "none"]

#: Fee rates as exact rationals.
TAKER_RATE = Fraction(7, 100)
MAKER_RATE = Fraction(175, 10_000)

_ROUNDING_MU: Mapping[str, int] = {
    "cent": MU_PER_CENT,
    "centicent": MU_PER_CENTICENT,
    "none": 1,
}


def _ceil_to(value: Fraction, granularity_mu: int) -> int:
    """Round ``value`` (microdollars, exact) *up* to the next granularity step."""
    if granularity_mu <= 1:
        return -((-value.numerator) // value.denominator)  # ceil of a Fraction
    steps = value / granularity_mu
    ceil_steps = -((-steps.numerator) // steps.denominator)
    return ceil_steps * granularity_mu


def raw_fee_mu(rate: Fraction, multiplier: Fraction, qty_cq: int, price_cc: int) -> Fraction:
    """Un-rounded fee in microdollars.

    Derivation: ``C = qty_cq/100``, ``P = price_cc/10_000``, so
    ``M*r*C*P*(1-P)`` dollars equals ``M*r*qty_cq*price_cc*(10_000-price_cc) /
    10**10`` dollars, i.e. ``.../10**4`` microdollars.
    """
    if price_cc < 0 or price_cc > NOTIONAL_CC:
        raise ValueError(f"price {price_cc} outside [0, {NOTIONAL_CC}]")
    if qty_cq < 0:
        raise ValueError("negative quantity")
    return rate * multiplier * qty_cq * price_cc * (NOTIONAL_CC - price_cc) / 10_000


@dataclass(frozen=True)
class FeeSchedule:
    """Per-series fee configuration.

    ``taker_multiplier`` / ``maker_multiplier`` are the ``M`` values.  They MUST
    be sourced from the API at runtime (``fee_type_override`` /
    ``fee_multiplier_override`` / ``GET /exchange/series_fee_changes``) rather
    than hardcoded -- a multiplier change can flip this strategy's sign.
    """

    series: str = "DEFAULT"
    taker_multiplier: Fraction = Fraction(1)
    maker_multiplier: Fraction = Fraction(0)
    rounding: Rounding = "cent"

    def _granularity(self) -> int:
        try:
            return _ROUNDING_MU[self.rounding]
        except KeyError:  # pragma: no cover - guarded by Literal
            raise ValueError(f"unknown rounding rule {self.rounding!r}") from None

    def taker_fee_mu(self, qty_cq: int, price_cc: int) -> int:
        raw = raw_fee_mu(TAKER_RATE, self.taker_multiplier, qty_cq, price_cc)
        return _ceil_to(raw, self._granularity())

    def maker_fee_mu(self, qty_cq: int, price_cc: int) -> int:
        raw = raw_fee_mu(MAKER_RATE, self.maker_multiplier, qty_cq, price_cc)
        return _ceil_to(raw, self._granularity())

    def fee_mu(self, qty_cq: int, price_cc: int, *, maker: bool = False) -> int:
        return self.maker_fee_mu(qty_cq, price_cc) if maker else self.taker_fee_mu(qty_cq, price_cc)

    @property
    def is_zero_fee(self) -> bool:
        return self.taker_multiplier == 0 and self.maker_multiplier == 0

    def marginal_fee_mu(self, qty_cq: int, price_cc: int, *, maker: bool = False) -> Fraction:
        """Un-rounded per-centi-contract fee, for use as a *linear* leg cost.

        The LP and the two-stage screen need a linear fee term; the ceiling is
        then applied per order as a post-solve correction (Research.md,
        "The unifying LP").
        """
        if qty_cq <= 0:
            return Fraction(0)
        rate = MAKER_RATE if maker else TAKER_RATE
        mult = self.maker_multiplier if maker else self.taker_multiplier
        return raw_fee_mu(rate, mult, qty_cq, price_cc) / qty_cq


#: Published non-standard multipliers.  Used ONLY as a fallback when the API is
#: unreachable; :mod:`kima.recorder.rest` overrides these at runtime.
PUBLISHED_OVERRIDES: Mapping[str, tuple[Fraction, Fraction]] = {
    # series: (taker M, maker M)
    "KXBTCY": (Fraction(0), Fraction(0)),
    "KXETHY": (Fraction(0), Fraction(0)),
    "KXMVE": (Fraction(1), Fraction(2)),
}


def schedule_for(series: str, rounding: Rounding = "cent") -> FeeSchedule:
    taker, maker = PUBLISHED_OVERRIDES.get(series.upper(), (Fraction(1), Fraction(0)))
    return FeeSchedule(series=series, taker_multiplier=taker, maker_multiplier=maker, rounding=rounding)


@dataclass
class FeeCalibration:
    """Result of comparing our fee model against exchange-reported fees."""

    observations: int = 0
    mismatches: int = 0
    best_rounding: Rounding = "cent"
    per_rule_mismatches: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.observations > 0 and self.mismatches == 0


def calibrate_rounding(
    observations: Iterable[tuple[int, int, Fraction, int]],
) -> FeeCalibration:
    """Recover the true rounding rule from observed fills.

    ``observations`` yields ``(qty_cq, price_cc, taker_multiplier, observed_fee_mu)``
    tuples -- in practice from ``average_fee_paid`` on demo-environment orders.
    Whichever candidate rule explains every observation wins; ties resolve to the
    finest rule, because over-charging ourselves is the safe direction.
    """
    cal = FeeCalibration()
    counts: dict[str, int] = {}
    total = 0
    for qty_cq, price_cc, mult, observed in observations:
        total += 1
        for rule in ("none", "centicent", "cent"):
            sched = FeeSchedule(taker_multiplier=mult, rounding=rule)  # type: ignore[arg-type]
            if sched.taker_fee_mu(qty_cq, price_cc) != observed:
                counts[rule] = counts.get(rule, 0) + 1
            else:
                counts.setdefault(rule, 0)
    cal.observations = total
    cal.per_rule_mismatches = counts
    if counts:
        cal.best_rounding = min(  # type: ignore[assignment]
            ("none", "centicent", "cent"), key=lambda r: (counts.get(r, 0),)
        )
        cal.mismatches = counts.get(cal.best_rounding, 0)
    return cal


def fee_for_fills(
    schedule: FeeSchedule,
    fills: Iterable[tuple[int, int]],
    *,
    maker: bool = False,
    scope: str = "order",
) -> int:
    """Fee for an order that filled across several price levels.

    ``fills`` yields ``(price_cc, qty_cq)`` pairs.

    The published formula takes a single price ``P``, which leaves the
    multi-level case underdetermined. Two readings are supported:

    ``scope="order"``   sum the exact rational fee across fills, then apply the
                        ceiling once for the order. This is the natural reading
                        of "round up the order fee".
    ``scope="fill"``    round each fill up separately. Strictly more expensive,
                        so it is the conservative bound.

    Neither is guessed at in production: :func:`calibrate_rounding` pins the rule
    against exchange-reported fees.
    """
    rate = MAKER_RATE if maker else TAKER_RATE
    mult = schedule.maker_multiplier if maker else schedule.taker_multiplier
    gran = schedule._granularity()
    if scope == "fill":
        return sum(_ceil_to(raw_fee_mu(rate, mult, q, p), gran) for p, q in fills)
    total = Fraction(0)
    for price_cc, qty_cq in fills:
        total += raw_fee_mu(rate, mult, qty_cq, price_cc)
    return _ceil_to(total, gran)
