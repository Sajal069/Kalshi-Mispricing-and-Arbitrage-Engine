"""Experiment E1: universe characterisation.

Descriptive statistics that justify the universe choice before any arbitrage
claim is made: how wide the spreads are, how deep the books are, how fast each
market updates, and how that varies through the day.

This matters for one specific reason. The hurdle an N-leg basket must clear is
roughly the sum of the legs' spreads, so a family's spread profile determines
whether it can *ever* produce an opportunity. Reporting it up front means the
later results can be read as "given these spreads, here is what survived"
rather than as an unexplained hit rate.

Sampling is deliberate: recording every top-of-book on every message would cost
more than the detector does. One sample every ``sample_every`` updates per
market is plenty for a distribution, and the sampling rate is reported so the
reader knows what they are looking at.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .book import NO, YES
from .replay import NullListener, ReplayEngine
from .units import CQ_PER_CONTRACT, cc_to_float


@dataclass
class MarketProfile:
    ticker: str
    series: str = ""
    updates: int = 0
    two_sided_samples: int = 0
    one_sided_samples: int = 0
    spreads_cc: list[int] = field(default_factory=list)
    top_depth_cq: list[int] = field(default_factory=list)
    ladder_depth_cq: list[int] = field(default_factory=list)
    levels: list[int] = field(default_factory=list)
    first_ts: int = 0
    last_ts: int = 0


class UniverseProfiler(NullListener):
    """Samples top-of-book quality as the replay advances."""

    def __init__(self, sample_every: int = 25, hour_buckets: bool = True):
        self.sample_every = max(1, sample_every)
        self.hour_buckets = hour_buckets
        self.markets: dict[str, MarketProfile] = {}
        self.updates_by_hour: dict[int, int] = defaultdict(int)
        self._counter: dict[str, int] = defaultdict(int)

    def on_tick(self, engine: ReplayEngine, ts_ms: int, ticker: str) -> None:
        prof = self.markets.get(ticker)
        if prof is None:
            ev = engine.event_of(ticker)
            prof = MarketProfile(ticker=ticker, series=ev.series_ticker, first_ts=ts_ms)
            self.markets[ticker] = prof
        prof.updates += 1
        prof.last_ts = ts_ms
        if self.hour_buckets:
            self.updates_by_hour[(ts_ms // 3_600_000) % 24] += 1

        self._counter[ticker] += 1
        if self._counter[ticker] % self.sample_every:
            return
        bk = engine.book(ticker)
        if not bk.trusted:
            return
        if not bk.is_two_sided:
            prof.one_sided_samples += 1
            return
        prof.two_sided_samples += 1
        prof.spreads_cc.append(bk.yes_spread_cc)
        _, top = bk.top_depth(NO)
        prof.top_depth_cq.append(top)
        prof.ladder_depth_cq.append(
            sum(size for _, size in bk.iter_levels(YES, descending=True))
        )
        prof.levels.append(bk.n_levels(YES) + bk.n_levels(NO))

    # -- reporting ---------------------------------------------------------
    def summary(self) -> dict:
        by_series: dict[str, list[MarketProfile]] = defaultdict(list)
        for p in self.markets.values():
            by_series[p.series or "?"].append(p)

        rows = []
        for series, profs in sorted(by_series.items()):
            spreads = [s for p in profs for s in p.spreads_cc if s >= 0]
            tops = [d for p in profs for d in p.top_depth_cq]
            ladders = [d for p in profs for d in p.ladder_depth_cq]
            span_ms = max((p.last_ts - p.first_ts) for p in profs) or 1
            updates = sum(p.updates for p in profs)
            one_sided = sum(p.one_sided_samples for p in profs)
            total_samples = one_sided + sum(p.two_sided_samples for p in profs)
            rows.append({
                "series": series,
                "markets": len(profs),
                "updates": updates,
                "updates_per_market_per_s": updates / len(profs) / (span_ms / 1000),
                "median_spread_cents": (statistics.median(spreads) / 100) if spreads else None,
                "p90_spread_cents": (
                    sorted(spreads)[int(0.9 * (len(spreads) - 1))] / 100 if spreads else None
                ),
                "median_top_depth_contracts": (
                    statistics.median(tops) / CQ_PER_CONTRACT if tops else None
                ),
                "median_ladder_depth_contracts": (
                    statistics.median(ladders) / CQ_PER_CONTRACT if ladders else None
                ),
                "one_sided_pct": 100.0 * one_sided / total_samples if total_samples else 0.0,
            })
        return {
            "sample_every_n_updates": self.sample_every,
            "by_series": rows,
            "updates_by_hour_utc": dict(sorted(self.updates_by_hour.items())),
            "note": (
                "Spreads are the implied YES spread (1 - bestNoBid) - bestYesBid. "
                "An N-leg basket must clear roughly the sum of its legs' spreads, "
                "so this table bounds what any family can produce."
            ),
        }


def render_universe_table(profile: dict) -> str:
    rows = profile.get("by_series", [])
    if not rows:
        return ""
    head = ["Series", "Markets", "Updates/mkt/s", "Median spread", "p90 spread",
            "Median top depth", "Median ladder depth", "One-sided"]
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for r in rows:
        def c(v, unit="", nd=2):
            return "n/a" if v is None else f"{v:,.{nd}f}{unit}"
        lines.append("| " + " | ".join([
            r["series"], str(r["markets"]),
            f"{r['updates_per_market_per_s']:.2f}",
            c(r["median_spread_cents"], "c"), c(r["p90_spread_cents"], "c"),
            c(r["median_top_depth_contracts"], "", 1),
            c(r["median_ladder_depth_contracts"], "", 1),
            f"{r['one_sided_pct']:.1f}%",
        ]) + " |")
    return "\n".join(lines)
