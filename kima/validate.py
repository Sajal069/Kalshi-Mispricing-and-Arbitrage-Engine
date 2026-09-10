"""Validation harness: the checks that decide whether the numbers are credible.

Five checks, each aimed at a specific way this study could be quietly wrong.

``determinism``    Same tape, same output. Without it the latency sweep is not a
                   controlled experiment, just a set of unrelated runs.
``n0``             Invariant N0 across the whole tape. A crossed book means the
                   delta application is wrong, and every downstream number with it.
``snapshot_diff``  Locally maintained book versus exchange snapshots.
``lp_agreement``   The O(1) closed forms against the LP oracle.
``null_test``      The detector run on a deliberately scrambled tape.

The null test deserves a note. It shifts each market's timeline by an
independent random offset, preserving every market's own dynamics -- its spread,
its depth, its update rate -- while destroying the *cross-market coherence* that
makes an event's prices sum to one. A detector that reports the same opportunity
rate on scrambled data is measuring noise, not structure. The direction of the
change is itself informative and is reported rather than assumed.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tape import DELTA, META, SNAPSHOT, TapeWriter, read_tape


def shuffle_tape(
    src: str | Path,
    dst: str | Path,
    *,
    seed: int = 0,
    max_shift_ms: int | None = None,
) -> dict:
    """Write a scrambled copy: each market's timeline shifted independently.

    Per-market message order is preserved, so the replay reconstructs every
    individual book exactly as before. Only the alignment *between* markets is
    destroyed -- which is precisely the structure the arbitrage conditions
    depend on.

    The shift is applied to receipt time, because that is what the replay orders
    on. Shifting the exchange timestamp instead leaves the ordering untouched
    while still rewriting the file, and the disagreement interleaves a market
    with itself.
    """
    rng = random.Random(seed)
    records = [r for r in read_tape(src) if r.get("type") != META]
    if not records:
        return {"records": 0}
    # Shift the clock the replay orders on -- receipt time -- not the exchange
    # timestamp. Moving `ts` alone changes nothing the engine reads, while the
    # tape still gets written in that order, and the two disagreeing is enough
    # to interleave a market with itself and corrupt its book.
    rn_values = [r["rn"] for r in records if r.get("rn")]
    if not rn_values:
        return {"records": 0, "note": "tape carries no receipt timestamps"}
    span_ms = (max(rn_values) - min(rn_values)) // 1_000_000
    shift_cap = max_shift_ms if max_shift_ms is not None else max(span_ms, 1)

    shifts: dict[str, int] = {}
    for r in records:
        key = r.get("m") or r.get("e") or ""
        if key not in shifts:
            shifts[key] = rng.randint(-shift_cap, shift_cap)
    for r in records:
        shift_ms = shifts[r.get("m") or r.get("e") or ""]
        if r.get("rn"):
            r["rn"] = r["rn"] + shift_ms * 1_000_000
        if "ts" in r:
            r["ts"] = r["ts"] + shift_ms

    # Re-sort globally on the engine clock, keeping each market's own order
    # intact: a market shifted as a block never reorders against itself.
    per_market: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        per_market[r.get("m") or r.get("e") or ""].append(r)
    merged: list[dict] = []
    for key, rs in per_market.items():
        merged.extend(rs)
    merged.sort(key=lambda r: (r.get("rn", 0), r.get("seq", 0)))

    # Sequence numbers are scoped to a subscription and must stay contiguous in
    # arrival order. Shifting each market independently reorders that stream, so
    # renumber per subscription afterwards -- otherwise every message reads as a
    # gap, the replay trusts nothing, and the null test measures our own
    # bookkeeping instead of the detector.
    next_seq: dict[int, int] = {}
    for r in merged:
        if r.get("type") not in ("snapshot", "delta"):
            continue
        sid = r.get("sid", 0)
        next_seq[sid] = next_seq.get(sid, 0) + 1
        r["seq"] = next_seq[sid]

    with TapeWriter(dst, source="synthetic-shuffled",
                    note="NULL TEST: per-market timelines randomly offset",
                    overwrite=True) as w:
        w.write_many(merged)
    return {"records": len(merged), "markets_shifted": len(shifts), "max_shift_ms": shift_cap}


@dataclass
class ValidationReport:
    checks: dict[str, dict] = field(default_factory=dict)

    def add(self, name: str, passed: bool, detail: str, **extra: Any) -> None:
        self.checks[name] = {"passed": passed, "detail": detail, **extra}

    @property
    def all_passed(self) -> bool:
        return all(c["passed"] for c in self.checks.values())

    def render(self) -> str:
        lines = []
        for name, c in self.checks.items():
            mark = "PASS" if c["passed"] else "FAIL"
            lines.append(f"  [{mark}] {name:22s} {c['detail']}")
        lines.append(f"  {'ALL CHECKS PASSED' if self.all_passed else 'VALIDATION FAILED'}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"all_passed": self.all_passed, "checks": self.checks}


def summarise_for_determinism(result: Any) -> tuple:
    """A compact fingerprint of a backtest, used to prove reruns are identical."""
    return (
        tuple(sorted(result.funnel.items())),
        len(result.episodes),
        tuple(sorted((e.event_ticker, e.condition, e.start_ts_ms, e.end_ts_ms,
                      e.n_states, e.max_excess_cc) for e in result.episodes)),
        tuple(
            (s["config"], round(s["net_pnl"], 9), s["baskets"], s["complete_baskets"])
            for s in result.exec_summaries
        ),
        result.replay["deltas"],
        result.replay["sequence_gaps"],
    )
