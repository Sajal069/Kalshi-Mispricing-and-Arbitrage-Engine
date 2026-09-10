"""Integer unit system for KIMA.

Design rule (Research.md, "Low-Latency Architecture"): *never* use floats in the
book or in money arithmetic.  Every price, quantity and cash amount below is an
exact Python ``int``.

Three units
-----------
``cc``   -- **centicent**.  Price unit.  1 dollar = 10_000 cc, so the finest
            documented Kalshi grid ($0.0001) is exactly 1 cc.  A binary
            contract's notional is therefore ``NOTIONAL_CC = 10_000``.
``cq``   -- **centi-contract**.  Quantity unit.  1 contract = 100 cq, matching
            the documented minimum fractional-contract granularity of 0.01
            contracts.
``mu``   -- **microdollar**.  Money unit, 1 dollar = 1_000_000 mu.  Chosen so
            that ``cost_mu = price_cc * qty_cq`` is exact with no division:
            (p/10_000 dollars) * (q/100 contracts) = p*q/1_000_000 dollars.

All conversions to float live at the reporting boundary only.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

# --------------------------------------------------------------------------
# scale constants
# --------------------------------------------------------------------------
CC_PER_DOLLAR: Final[int] = 10_000
CQ_PER_CONTRACT: Final[int] = 100
MU_PER_DOLLAR: Final[int] = 1_000_000
MU_PER_CENT: Final[int] = 10_000
MU_PER_CENTICENT: Final[int] = 100

#: Settlement value of one YES (or NO) contract, in price units.
NOTIONAL_CC: Final[int] = CC_PER_DOLLAR
#: Settlement value of one contract, in money units.
NOTIONAL_MU: Final[int] = MU_PER_DOLLAR


# --------------------------------------------------------------------------
# price
# --------------------------------------------------------------------------
def dollars_to_cc(value: str | float | Decimal) -> int:
    """Parse a dollar price (Kalshi sends fixed-point *strings*) into centicents.

    Exact for anything on a documented grid; raises if the value carries more
    than four decimal places, which would mean our unit choice is wrong rather
    than that the value should be silently rounded.
    """
    d = Decimal(str(value))
    scaled = d * CC_PER_DOLLAR
    if scaled != scaled.to_integral_value():
        raise ValueError(f"price {value!r} is finer than the centicent grid")
    return int(scaled)


def cc_to_dollars(cc: int) -> Decimal:
    return Decimal(cc) / CC_PER_DOLLAR


def cc_to_float(cc: int) -> float:
    """Reporting boundary only."""
    return cc / CC_PER_DOLLAR


def complement_cc(cc: int) -> int:
    """The documented YES/NO reciprocity: a YES bid at X is a NO ask at 1-X."""
    return NOTIONAL_CC - cc


# --------------------------------------------------------------------------
# quantity
# --------------------------------------------------------------------------
def contracts_to_cq(value: str | float | Decimal) -> int:
    d = Decimal(str(value))
    scaled = d * CQ_PER_CONTRACT
    if scaled != scaled.to_integral_value():
        raise ValueError(f"quantity {value!r} is finer than 0.01 contracts")
    return int(scaled)


def cq_to_contracts(cq: int) -> Decimal:
    return Decimal(cq) / CQ_PER_CONTRACT


def cq_to_float(cq: int) -> float:
    return cq / CQ_PER_CONTRACT


# --------------------------------------------------------------------------
# money
# --------------------------------------------------------------------------
def cost_mu(price_cc: int, qty_cq: int) -> int:
    """Exact notional cost of ``qty_cq`` centi-contracts at ``price_cc``."""
    return price_cc * qty_cq


def mu_to_dollars(mu: int) -> Decimal:
    return Decimal(mu) / MU_PER_DOLLAR


def mu_to_float(mu: int) -> float:
    return mu / MU_PER_DOLLAR


def dollars_to_mu(value: str | float | Decimal) -> int:
    d = Decimal(str(value)) * MU_PER_DOLLAR
    return int(d.to_integral_value(rounding="ROUND_HALF_EVEN"))


#: Money value of one winning centi-contract (exact: 1_000_000 / 100).
MU_PER_CQ: Final[int] = MU_PER_DOLLAR // CQ_PER_CONTRACT


def payoff_mu(qty_cq: int) -> int:
    """Settlement payoff of ``qty_cq`` winning centi-contracts."""
    return qty_cq * MU_PER_CQ
