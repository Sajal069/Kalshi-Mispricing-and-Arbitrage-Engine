"""Linear-programming oracle for worst-case-dominant baskets.

Conditions N1, N2 and N3 are all special cases of one program. Enumerate every
tradable **leg** ``j`` -- a (market, side, price-level) triple with per-contract
cost ``c_j`` and capacity ``u_j`` equal to that level's resting size -- and let
``A_kj = 1`` iff leg ``j`` pays $1 in outcome cell ``k``. Then::

    maximize    t
    subject to  (A x)_k - c^T x >= t      for every cell k
                0 <= x_j <= u_j

``t*`` is the guaranteed worst-case profit of the best basket, and ``t* > 0``
iff arbitrage exists. Depth enters natively as the box constraints, so walking
the ladder is not a separate heuristic bolted on afterwards.

The LP is an **offline oracle**, not the hot path. Its jobs are (i) to validate
the O(1) closed forms -- any disagreement is a bug in one of them, (ii) to
handle irregular events whose partition is not a clean bucket ladder, and (iii)
to state the problem in the form a desk would recognise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from scipy.optimize import linprog

from .book import NO, YES, MarketBook
from .events import Event
from .exhaustive import Cell, outcome_partition
from .fees import MAKER_RATE, TAKER_RATE, FeeSchedule
from .units import CQ_PER_CONTRACT, NOTIONAL_CC, cc_to_float

#: Tolerance for LP-vs-closed-form agreement, in dollars. The LP is solved in
#: floating point; the closed forms are exact integers.
LP_TOL = 1e-7


@dataclass
class LPLeg:
    ticker: str
    buy_side: int
    price_cc: int
    capacity_cq: int
    cost_per_contract: float     # price + linear fee term, in dollars

    @property
    def side_name(self) -> str:
        return "yes" if self.buy_side == YES else "no"


@dataclass
class LPSolution:
    t_star: float
    legs: list[LPLeg] = field(default_factory=list)
    x: list[float] = field(default_factory=list)     # contracts per leg
    cells: list[Cell] = field(default_factory=list)
    status: str = "ok"

    @property
    def has_arbitrage(self) -> bool:
        return self.t_star > LP_TOL

    def active_legs(self, eps: float = 1e-9) -> list[tuple[LPLeg, float]]:
        return [(leg, q) for leg, q in zip(self.legs, self.x) if q > eps]

    def describe(self) -> str:
        lines = [f"LP t* = ${self.t_star:.6f} over {len(self.cells)} outcome cells"]
        for leg, q in self.active_legs():
            lines.append(
                f"  buy {q:8.2f} {leg.side_name.upper():3s} {leg.ticker} "
                f"@ ${cc_to_float(leg.price_cc):.4f} (cap {leg.capacity_cq / CQ_PER_CONTRACT:.2f})"
            )
        return "\n".join(lines)


def enumerate_legs(
    event: Event,
    books: dict[str, MarketBook],
    schedule: FeeSchedule | None = None,
    *,
    max_levels_per_side: int = 25,
    maker: bool = False,
) -> list[LPLeg]:
    """Every marketable (market, side, price-level) triple, with its capacity.

    Buying a side consumes the opposite side's bid ladder, so a NO bid level at
    ``n`` is a YES-buy opportunity at ``1 - n``.
    """
    sched = schedule or event.fee_schedule
    rate = MAKER_RATE if maker else TAKER_RATE
    mult = sched.maker_multiplier if maker else sched.taker_multiplier
    fee_coef = float(rate) * float(mult)

    legs: list[LPLeg] = []
    for ticker in event.tickers:
        bk = books.get(ticker)
        if bk is None:
            continue
        for buy_side, resting_side in ((YES, NO), (NO, YES)):
            taken = 0
            for resting_price, size in bk.iter_levels(resting_side, descending=True):
                pay_cc = NOTIONAL_CC - resting_price
                p = cc_to_float(pay_cc)
                legs.append(
                    LPLeg(
                        ticker=ticker,
                        buy_side=buy_side,
                        price_cc=pay_cc,
                        capacity_cq=size,
                        cost_per_contract=p + fee_coef * p * (1.0 - p),
                    )
                )
                taken += 1
                if taken >= max_levels_per_side:
                    break
    return legs


def payoff_matrix(legs: Sequence[LPLeg], cells: Sequence[Cell]) -> np.ndarray:
    """``A[k, j] = 1`` iff leg ``j`` settles at $1 in cell ``k``."""
    A = np.zeros((len(cells), len(legs)), dtype=float)
    for k, cell in enumerate(cells):
        for j, leg in enumerate(legs):
            pays_yes = leg.ticker in cell.yes_tickers
            A[k, j] = 1.0 if (pays_yes if leg.buy_side == YES else not pays_yes) else 0.0
    return A


def solve(
    event: Event,
    books: dict[str, MarketBook],
    schedule: FeeSchedule | None = None,
    *,
    max_levels_per_side: int = 25,
    max_contracts: float = 1e6,
) -> LPSolution:
    """Solve the worst-case-dominant basket LP for one event."""
    cells = outcome_partition(event)
    legs = enumerate_legs(event, books, schedule, max_levels_per_side=max_levels_per_side)
    if not cells:
        # No justifiable outcome partition. Refuse rather than assume: an empty
        # partition would make unconstrained NO legs look risk-free.
        return LPSolution(0.0, legs, [0.0] * len(legs), [], "no-partition")
    if not legs:
        return LPSolution(0.0, legs, [0.0] * len(legs), list(cells), "empty-book")

    A = payoff_matrix(legs, cells)
    c = np.array([leg.cost_per_contract for leg in legs], dtype=float)
    n = len(legs)

    # Variables: [x_0 .. x_{n-1}, t].  Minimise -t.
    obj = np.zeros(n + 1)
    obj[-1] = -1.0
    # Constraint per cell:  -(A x)_k + c^T x + t <= 0
    rows = [np.hstack([-A + c[None, :], np.ones((len(cells), 1))])]
    b_ub = [np.zeros(len(cells))]

    # Per (market, side) position cap. Without this the LP is capped only per
    # price level, so it can assemble a position orders of magnitude larger than
    # the closed form was allowed to size -- and the comparison would measure
    # configuration rather than correctness.
    groups: dict[tuple[str, int], list[int]] = {}
    for j, leg in enumerate(legs):
        groups.setdefault((leg.ticker, leg.buy_side), []).append(j)
    if max_contracts < 1e6 and groups:
        cap = np.zeros((len(groups), n + 1))
        for r, idxs in enumerate(groups.values()):
            for j in idxs:
                cap[r, j] = 1.0
        rows.append(cap)
        b_ub.append(np.full(len(groups), float(max_contracts)))

    A_ub = np.vstack(rows)
    b_ub = np.concatenate(b_ub)
    bounds = [(0.0, min(leg.capacity_cq / CQ_PER_CONTRACT, max_contracts)) for leg in legs]
    bounds.append((None, None))

    res = linprog(obj, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
    if not res.success:
        return LPSolution(0.0, legs, [0.0] * n, list(cells), f"solver: {res.message}")
    return LPSolution(float(res.x[-1]), legs, [float(v) for v in res.x[:n]], list(cells), "ok")


def cross_check(
    event: Event,
    books: dict[str, MarketBook],
    closed_form_profit_mu: int,
    *,
    tol: float = 1e-6,
    max_contracts: float = 1e6,
) -> tuple[bool, float, str]:
    """Assert the LP dominates and (on clean partitions) matches the closed form.

    The LP optimises over the *full* cone of legs, so it is a relaxation of any
    single closed form: ``t* >= closed_form`` must always hold. A closed form
    that beats the LP is a bug in the closed form; an LP that beats it on a clean
    bucket partition means the closed form left money on the table.
    """
    # The LP must be given the *same* size cap the closed form was sized under,
    # otherwise it wins trivially by buying a larger basket and the comparison
    # measures configuration rather than correctness.
    sol = solve(event, books, max_contracts=max_contracts)
    cf = closed_form_profit_mu / 1_000_000.0
    if sol.t_star + tol < cf:
        return False, sol.t_star, (
            f"closed form claims ${cf:.6f} but LP optimum is ${sol.t_star:.6f}; "
            "the closed form is over-reporting"
        )
    return True, sol.t_star, "ok"
