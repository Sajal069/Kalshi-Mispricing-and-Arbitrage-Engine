"""Per-market order book.

Kalshi publishes a **bid-only, two-sided** book: YES bids and NO bids, no asks,
because a YES bid at ``X`` is definitionally a NO offer at ``1 - X``. The
documented identities we rely on everywhere::

    bestYesAsk = 1 - bestNoBid
    bestNoAsk  = 1 - bestYesBid

The practical consequence for execution is worth stating once, clearly:
**buying one side consumes the other side's bid ladder.** To buy NO you lift the
best NO ask, which is the resting best YES bid. So a NO-basket (Condition N1)
walks down the YES bid ladders, and a YES-basket (Condition N2) walks down the
NO bid ladders.

Implementation follows the plan: dense price-indexed arrays keyed by integer
centicent ticks, no floats, no hash lookups in the hot path, and an
incrementally maintained best-bid pointer so that a delta costs O(1) amortised.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field
from typing import Iterator, Sequence

from .ticks import PriceGrid
from .units import NOTIONAL_CC, complement_cc

YES = 0
NO = 1
SIDES = ("yes", "no")


def side_index(side: str) -> int:
    s = side.lower()
    if s == "yes":
        return YES
    if s == "no":
        return NO
    raise ValueError(f"unknown side {side!r}")


class BookIntegrityError(AssertionError):
    """Raised when invariant N0 fails: the local book is crossed."""


@dataclass
class Fill:
    price_cc: int
    qty_cq: int

    @property
    def cost_mu(self) -> int:
        return self.price_cc * self.qty_cq


@dataclass
class WalkResult:
    """Outcome of walking a ladder for a marketable buy order."""

    fills: list[Fill] = field(default_factory=list)
    filled_cq: int = 0
    cost_mu: int = 0
    #: Worst price touched, i.e. the limit an IOC order would need to reach.
    worst_price_cc: int = 0

    @property
    def is_empty(self) -> bool:
        return self.filled_cq == 0

    @property
    def avg_price_cc(self) -> int:
        if self.filled_cq == 0:
            return 0
        return self.cost_mu // self.filled_cq


class MarketBook:
    """Dense, price-indexed ladders for one market.

    ``levels[side][price_cc]`` holds resting size in centi-contracts.
    """

    __slots__ = (
        "ticker", "grid", "_levels", "_best", "_active", "_step",
        "seq", "trusted", "last_ts_ms", "last_recv_ns", "untrusted_reason",
    )

    def __init__(self, ticker: str, grid: PriceGrid | None = None) -> None:
        self.ticker = ticker
        self.grid = grid or PriceGrid.penny()
        size = NOTIONAL_CC + 1
        self._levels = (array("q", bytes(8 * size)), array("q", bytes(8 * size)))
        self._best = [-1, -1]
        self._active = [0, 0]
        # Scanning stride when the best level empties. Safe for any grid because
        # a uniform grid has all levels on multiples of its step.
        self._step = self.grid.min_step_cc if len(self.grid.segments) == 1 else 1
        self.seq: int = 0
        self.trusted: bool = False
        self.untrusted_reason: str = "no snapshot yet"
        self.last_ts_ms: int = 0
        self.last_recv_ns: int = 0

    # -- state ------------------------------------------------------------
    def reset(self, reason: str = "resync") -> None:
        for s in (YES, NO):
            if self._active[s]:
                lv = self._levels[s]
                for i in range(len(lv)):
                    lv[i] = 0
            self._best[s] = -1
            self._active[s] = 0
        self.trusted = False
        self.untrusted_reason = reason

    def mark_untrusted(self, reason: str) -> None:
        self.trusted = False
        self.untrusted_reason = reason

    # -- mutation ---------------------------------------------------------
    def apply_snapshot(
        self,
        yes_levels: Sequence[tuple[int, int]],
        no_levels: Sequence[tuple[int, int]],
        seq: int = 0,
        ts_ms: int = 0,
        recv_ns: int = 0,
    ) -> None:
        self.reset("applying snapshot")
        for side, levels in ((YES, yes_levels), (NO, no_levels)):
            lv = self._levels[side]
            best = -1
            active = 0
            for price_cc, size_cq in levels:
                if size_cq <= 0:
                    continue
                lv[price_cc] = size_cq
                active += 1
                if price_cc > best:
                    best = price_cc
            self._best[side] = best
            self._active[side] = active
        self.seq = seq
        self.last_ts_ms = ts_ms
        self.last_recv_ns = recv_ns
        self.trusted = True
        self.untrusted_reason = ""

    def apply_delta(
        self,
        side: int,
        price_cc: int,
        delta_cq: int,
        seq: int = 0,
        ts_ms: int = 0,
        recv_ns: int = 0,
    ) -> None:
        """Apply one incremental level change. O(1) except when the top empties."""
        lv = self._levels[side]
        before = lv[price_cc]
        after = before + delta_cq
        if after < 0:
            # Negative resting size is impossible; the local book is corrupt.
            raise BookIntegrityError(
                f"{self.ticker}: level {price_cc} on side {SIDES[side]} went "
                f"negative ({before} {delta_cq:+d})"
            )
        lv[price_cc] = after
        if before == 0 and after > 0:
            self._active[side] += 1
            if price_cc > self._best[side]:
                self._best[side] = price_cc
        elif before > 0 and after == 0:
            self._active[side] -= 1
            if price_cc == self._best[side]:
                self._rescan_best(side)
        if seq:
            self.seq = seq
        if ts_ms:
            self.last_ts_ms = ts_ms
        if recv_ns:
            self.last_recv_ns = recv_ns

    def _rescan_best(self, side: int) -> None:
        if self._active[side] == 0:
            self._best[side] = -1
            return
        lv = self._levels[side]
        p = self._best[side] - self._step
        while p >= 0:
            if lv[p]:
                self._best[side] = p
                return
            p -= self._step
        # Stride missed it (tapered grid); fall back to a dense scan.
        p = self._best[side] - 1
        while p >= 0:
            if lv[p]:
                self._best[side] = p
                return
            p -= 1
        self._best[side] = -1

    # -- queries ----------------------------------------------------------
    def size_at(self, side: int, price_cc: int) -> int:
        return self._levels[side][price_cc]

    def best_bid(self, side: int) -> int:
        """Best bid price on ``side``, or -1 if that side is empty."""
        return self._best[side]

    @property
    def best_yes_bid(self) -> int:
        return self._best[YES]

    @property
    def best_no_bid(self) -> int:
        return self._best[NO]

    @property
    def best_yes_ask(self) -> int:
        """Implied: 1 - bestNoBid. Returns -1 when no NO bid rests."""
        b = self._best[NO]
        return -1 if b < 0 else complement_cc(b)

    @property
    def best_no_ask(self) -> int:
        b = self._best[YES]
        return -1 if b < 0 else complement_cc(b)

    @property
    def yes_spread_cc(self) -> int:
        a, b = self.best_yes_ask, self.best_yes_bid
        return -1 if (a < 0 or b < 0) else a - b

    @property
    def is_two_sided(self) -> bool:
        return self._best[YES] >= 0 and self._best[NO] >= 0

    def n_levels(self, side: int) -> int:
        return self._active[side]

    def iter_levels(self, side: int, descending: bool = True) -> Iterator[tuple[int, int]]:
        lv = self._levels[side]
        if descending:
            p = self._best[side]
            while p >= 0:
                if lv[p]:
                    yield p, lv[p]
                p -= 1
        else:
            for p in range(len(lv)):
                if lv[p]:
                    yield p, lv[p]

    def snapshot_levels(self, side: int) -> list[tuple[int, int]]:
        return list(self.iter_levels(side, descending=False))

    # -- invariant --------------------------------------------------------
    def check_n0(self) -> None:
        """Invariant N0: ``bestYesBid + bestNoBid <= $1``.

        A violation is a *crossed book*, which a continuous matching engine
        cannot publish. Per the plan this is treated as a data-integrity bug,
        never as a trading signal.
        """
        y, n = self._best[YES], self._best[NO]
        if y >= 0 and n >= 0 and y + n > NOTIONAL_CC:
            raise BookIntegrityError(
                f"N0 violated on {self.ticker}: bestYesBid={y} + bestNoBid={n} "
                f"= {y + n} > {NOTIONAL_CC} (crossed book)"
            )

    def n0_violated(self) -> bool:
        y, n = self._best[YES], self._best[NO]
        return y >= 0 and n >= 0 and y + n > NOTIONAL_CC

    # -- execution --------------------------------------------------------
    def walk_buy(self, buy_side: int, qty_cq: int, limit_price_cc: int | None = None) -> WalkResult:
        """Walk the ladder for a marketable buy of ``buy_side``.

        Buying YES consumes the NO bid ladder and vice versa. ``limit_price_cc``
        is expressed in the price of the side being bought; levels worse than
        the limit are not taken (IOC semantics).
        """
        res = WalkResult()
        if qty_cq <= 0:
            return res
        resting_side = NO if buy_side == YES else YES
        remaining = qty_cq
        for resting_price, size in self.iter_levels(resting_side, descending=True):
            pay = complement_cc(resting_price)
            if limit_price_cc is not None and pay > limit_price_cc:
                break
            take = size if size < remaining else remaining
            if take <= 0:
                continue
            res.fills.append(Fill(pay, take))
            res.filled_cq += take
            res.cost_mu += pay * take
            res.worst_price_cc = pay
            remaining -= take
            if remaining == 0:
                break
        return res

    def walk_sell(self, sell_side: int, qty_cq: int, limit_price_cc: int | None = None) -> WalkResult:
        """Walk the ladder to *close* a long position by crossing back.

        Selling a side you hold consumes that same side's bid ladder -- the exact
        mirror of :meth:`walk_buy`. This is how a naked residual leg is unwound,
        and it pays a second taker fee, which is charged by the caller.
        """
        res = WalkResult()
        if qty_cq <= 0:
            return res
        remaining = qty_cq
        for price, size in self.iter_levels(sell_side, descending=True):
            if limit_price_cc is not None and price < limit_price_cc:
                break
            take = size if size < remaining else remaining
            if take <= 0:
                continue
            res.fills.append(Fill(price, take))
            res.filled_cq += take
            res.cost_mu += price * take          # proceeds, not cost
            res.worst_price_cc = price
            remaining -= take
            if remaining == 0:
                break
        return res

    def depth_at_or_better(self, buy_side: int, limit_price_cc: int) -> int:
        """Total size buyable at or below ``limit_price_cc`` on ``buy_side``."""
        resting_side = NO if buy_side == YES else YES
        total = 0
        for resting_price, size in self.iter_levels(resting_side, descending=True):
            if complement_cc(resting_price) > limit_price_cc:
                break
            total += size
        return total

    def top_depth(self, buy_side: int) -> tuple[int, int]:
        """(best price to buy ``buy_side``, size available at that price)."""
        resting_side = NO if buy_side == YES else YES
        best = self._best[resting_side]
        if best < 0:
            return -1, 0
        return complement_cc(best), self._levels[resting_side][best]

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<MarketBook {self.ticker} yesbid={self.best_yes_bid} "
            f"nobid={self.best_no_bid} seq={self.seq} "
            f"{'ok' if self.trusted else 'UNTRUSTED'}>"
        )


class BookSet:
    """All books for one universe, keyed by ticker, with slot ids for the hot path."""

    def __init__(self) -> None:
        self.books: list[MarketBook] = []
        self.slot: dict[str, int] = {}

    def add(self, ticker: str, grid: PriceGrid | None = None) -> int:
        if ticker in self.slot:
            return self.slot[ticker]
        idx = len(self.books)
        self.books.append(MarketBook(ticker, grid))
        self.slot[ticker] = idx
        return idx

    def get(self, ticker: str) -> MarketBook:
        return self.books[self.slot[ticker]]

    def __contains__(self, ticker: object) -> bool:
        return ticker in self.slot

    def __len__(self) -> int:
        return len(self.books)

    def __iter__(self) -> Iterator[MarketBook]:
        return iter(self.books)
