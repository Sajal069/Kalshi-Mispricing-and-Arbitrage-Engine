"""Per-market price grids.

Kalshi tick sizes are **not** always 1c.  Each market carries a
``price_ranges`` array of ``{start, end, step}`` segments, and grids are often
*tapered* -- finer ticks below $0.10 and above $0.90.  Documented structures run
from $0.01 down to $0.0001.  Any engine that assumes a penny grid is wrong, so
the grid is a first-class object here and every price the engine emits is
validated against it.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Iterable, Sequence

from .units import NOTIONAL_CC, dollars_to_cc


@dataclass(frozen=True)
class GridSegment:
    """Half-open ``[start_cc, end_cc)`` region with a uniform ``step_cc``."""

    start_cc: int
    end_cc: int
    step_cc: int

    def __post_init__(self) -> None:
        if self.step_cc <= 0:
            raise ValueError("step must be positive")
        if self.end_cc <= self.start_cc:
            raise ValueError("empty grid segment")


@dataclass(frozen=True)
class PriceGrid:
    """An ordered, gap-free set of :class:`GridSegment` covering [0, 1]."""

    segments: tuple[GridSegment, ...]
    structure: str = "unknown"

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError("price grid needs at least one segment")
        prev_end = None
        for seg in self.segments:
            if prev_end is not None and seg.start_cc != prev_end:
                raise ValueError(f"price grid has a gap/overlap at {seg.start_cc}")
            prev_end = seg.end_cc
        object.__setattr__(self, "_starts", tuple(s.start_cc for s in self.segments))

    # -- construction ------------------------------------------------------
    @classmethod
    def uniform(cls, step_cc: int, structure: str = "uniform") -> "PriceGrid":
        return cls((GridSegment(0, NOTIONAL_CC + step_cc, step_cc),), structure)

    @classmethod
    def penny(cls) -> "PriceGrid":
        return cls.uniform(100, "one_cent")

    @classmethod
    def from_api(cls, price_ranges: Sequence[dict], structure: str = "unknown") -> "PriceGrid":
        """Build from the API's ``price_ranges`` payload (dollar strings)."""
        segs = []
        for r in price_ranges:
            segs.append(
                GridSegment(
                    dollars_to_cc(r["start"]),
                    dollars_to_cc(r["end"]),
                    dollars_to_cc(r["step"]),
                )
            )
        segs.sort(key=lambda s: s.start_cc)
        # The API describes closed ranges; splice them into half-open cover.
        spliced: list[GridSegment] = []
        for i, seg in enumerate(segs):
            end = segs[i + 1].start_cc if i + 1 < len(segs) else max(seg.end_cc, NOTIONAL_CC + seg.step_cc)
            spliced.append(GridSegment(seg.start_cc, end, seg.step_cc))
        if spliced and spliced[0].start_cc != 0:
            spliced.insert(0, GridSegment(0, spliced[0].start_cc, spliced[0].step_cc))
        return cls(tuple(spliced), structure)

    # -- queries -----------------------------------------------------------
    def segment_for(self, price_cc: int) -> GridSegment:
        starts: tuple[int, ...] = getattr(self, "_starts")
        idx = bisect_right(starts, price_cc) - 1
        if idx < 0:
            idx = 0
        return self.segments[min(idx, len(self.segments) - 1)]

    @property
    def min_step_cc(self) -> int:
        return min(s.step_cc for s in self.segments)

    def is_on_grid(self, price_cc: int) -> bool:
        if not 0 <= price_cc <= NOTIONAL_CC:
            return False
        seg = self.segment_for(price_cc)
        return (price_cc - seg.start_cc) % seg.step_cc == 0

    def round_down(self, price_cc: int) -> int:
        seg = self.segment_for(price_cc)
        return seg.start_cc + ((price_cc - seg.start_cc) // seg.step_cc) * seg.step_cc

    def round_up(self, price_cc: int) -> int:
        down = self.round_down(price_cc)
        if down == price_cc:
            return price_cc
        seg = self.segment_for(price_cc)
        return min(down + seg.step_cc, NOTIONAL_CC)

    def levels(self) -> Iterable[int]:
        """Every tradable price on the grid, ascending."""
        for seg in self.segments:
            p = seg.start_cc
            while p < seg.end_cc and p <= NOTIONAL_CC:
                yield p
                p += seg.step_cc

    def n_levels(self) -> int:
        return sum(1 for _ in self.levels())


#: Documented ``price_level_structure`` labels -> grid.  Tapered structures use
#: finer ticks in the tails, which is exactly where multi-leg baskets live.
KNOWN_STRUCTURES: dict[str, PriceGrid] = {
    "one_cent": PriceGrid.penny(),
    "one_deci_cent": PriceGrid.uniform(10, "one_deci_cent"),
    "one_centi_cent": PriceGrid.uniform(1, "one_centi_cent"),
    "center_deci_edge_centi_cent": PriceGrid(
        (
            GridSegment(0, 1_000, 1),          # < $0.10 : centicent ticks
            GridSegment(1_000, 9_000, 10),     # $0.10-$0.90 : decicent ticks
            GridSegment(9_000, NOTIONAL_CC + 1, 1),  # > $0.90 : centicent ticks
        ),
        "center_deci_edge_centi_cent",
    ),
}


def grid_from_metadata(structure: str | None, price_ranges: Sequence[dict] | None) -> PriceGrid:
    """``price_ranges`` is authoritative; ``price_level_structure`` is a label."""
    if price_ranges:
        return PriceGrid.from_api(price_ranges, structure or "unknown")
    if structure and structure in KNOWN_STRUCTURES:
        return KNOWN_STRUCTURES[structure]
    return PriceGrid.penny()
