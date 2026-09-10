"""Evaluation metrics.

Only metrics obtainable from the recorded data are computed. Where a metric is
*not* obtainable it is named and excluded rather than approximated -- see
``NOT_OBTAINABLE``.

One deliberate omission is worth stating: **no Sharpe ratio**. For a
hold-to-settlement arbitrage with lumpy, non-i.i.d. returns and a likely small
episode count, a Sharpe ratio is close to meaningless. Reporting interval
estimates and refusing to report a Sharpe is the more honest choice, and
:func:`strategy_quality` says so explicitly in its output.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from typing import Any, Iterable, Sequence

from ..backtest import FUNNEL_STAGES, BacktestResult
from ..detector import Episode
from ..execution import ExecutedBasket
from ..units import CQ_PER_CONTRACT

NOT_OBTAINABLE = (
    "true per-order queue dynamics (the feed is level-aggregated)",
    "counterfactual market impact of our own hypothetical fills",
    "order-book history prior to the recording window",
    "realised live P&L (no live or demo phase was run)",
)

#: Minimum episodes before an inferential statistic is worth quoting at all.
MIN_N_FOR_INFERENCE = 30


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _pct(values: Sequence[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = q * (len(s) - 1)
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def describe(values: Sequence[float], name: str = "") -> dict:
    if not values:
        return {"n": 0, "name": name}
    return {
        "name": name,
        "n": len(values),
        "mean": statistics.fmean(values),
        "median": _pct(values, 0.5),
        "p10": _pct(values, 0.10),
        "p90": _pct(values, 0.90),
        "min": min(values),
        "max": max(values),
        "total": sum(values),
    }


def bootstrap_ci(
    values: Sequence[float], *, iters: int = 2_000, alpha: float = 0.05, seed: int = 7
) -> tuple[float, float] | None:
    """Percentile bootstrap for the mean. Returns ``None`` when n is too small.

    Refusing to produce an interval from a handful of episodes is the point:
    a confidence interval built on n=4 invites more confidence than the data
    can support.
    """
    n = len(values)
    if n < 8:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        means.append(statistics.fmean(rng.choices(values, k=n)))
    return (_pct(means, alpha / 2), _pct(means, 1 - alpha / 2))


def _baseline_configs(result: BacktestResult) -> list[dict]:
    """The pure latency ladder: IOC, batched, unwind. One variable at a time."""
    return sorted(
        (
            s for s in result.exec_summaries
            if s["order_type"] == "IOC" and s["leg_order"] == "batch"
            and s["residual_policy"] == "unwind"
            and not s.get("rate_limited_config")
        ),
        key=lambda s: s["latency_ms"],
    )


# --------------------------------------------------------------------------
# E2/E3 -- the rejection funnel
# --------------------------------------------------------------------------
def rejection_funnel(result: BacktestResult) -> dict:
    """The flagship table: how many raw signals survive each friction."""
    raw = result.funnel.get("raw_signals", 0) or 1
    rows = []
    prev = None
    for stage in FUNNEL_STAGES:
        n = result.funnel.get(stage, 0)
        rows.append(
            {
                "stage": stage,
                "count": n,
                "pct_of_raw": 100.0 * n / raw,
                "removed": (prev - n) if prev is not None else 0,
            }
        )
        prev = n
    return {
        "rows": rows,
        "raw_signals": result.funnel.get("raw_signals", 0),
        "actionable": result.funnel.get("risk_ok", 0),
        "survival_pct": 100.0 * result.funnel.get("risk_ok", 0) / raw,
        "attribution": dict(result.risk.get("rejections", {})),
        "by_condition": result.funnel_by_condition,
    }


# --------------------------------------------------------------------------
# E1 / opportunity shape
# --------------------------------------------------------------------------
def opportunity_stats(result: BacktestResult) -> dict:
    eps = result.episodes
    actionable = [e for e in eps if e.actionable]
    hours = max(result.replay.get("duration_ms", 0) / 3_600_000, 1e-9)

    edges = [e.best_basket.profit_mu / 1e6 for e in actionable]
    bps = [e.best_basket.edge_bps for e in actionable]
    durations = [e.duration_ms for e in eps if e.duration_ms > 0]
    act_durations = [e.duration_ms for e in actionable if e.duration_ms > 0]
    sizes = [e.best_basket.bottleneck_cq / CQ_PER_CONTRACT for e in actionable]

    by_series: dict[str, dict] = defaultdict(lambda: {"episodes": 0, "actionable": 0, "edge": 0.0})
    for e in eps:
        row = by_series[e.series_ticker or "?"]
        row["episodes"] += 1
        if e.actionable:
            row["actionable"] += 1
            row["edge"] += e.best_basket.profit_mu / 1e6

    by_phase: dict[str, int] = defaultdict(int)
    for e in eps:
        by_phase[e.phase] += 1

    by_legs: dict[int, dict] = defaultdict(lambda: {"episodes": 0, "actionable": 0})
    for e in eps:
        by_legs[e.n_legs]["episodes"] += 1
        if e.actionable:
            by_legs[e.n_legs]["actionable"] += 1

    by_condition: dict[str, int] = defaultdict(int)
    for e in eps:
        by_condition[e.condition] += 1

    violating_ms = sum(e.duration_ms for e in eps)
    return {
        "episodes_total": len(eps),
        "episodes_actionable": len(actionable),
        "episodes_per_hour": len(eps) / hours,
        "actionable_per_hour": len(actionable) / hours,
        "censored_episodes": sum(1 for e in eps if e.censored),
        "pct_time_violating": 100.0 * violating_ms / max(result.replay.get("duration_ms", 1), 1),
        "edge_dollars": describe(edges, "edge per basket ($)"),
        "edge_bps": describe(bps, "edge (bps of capital)"),
        "duration_ms_all": describe(durations, "episode duration (ms)"),
        "duration_ms_actionable": describe(act_durations, "actionable duration (ms)"),
        "executable_size_contracts": describe(sizes, "bottleneck size (contracts)"),
        "by_series": {k: dict(v) for k, v in sorted(by_series.items())},
        "by_phase": dict(by_phase),
        "by_leg_count": {k: dict(v) for k, v in sorted(by_legs.items())},
        "by_condition": dict(by_condition),
    }


# --------------------------------------------------------------------------
# E5 -- the latency sweep
# --------------------------------------------------------------------------
def latency_curve(result: BacktestResult) -> dict:
    rows = []
    for s in _baseline_configs(result):
        baskets = result.executions.get(s["config"], [])
        nets = [b.net_mu / 1e6 for b in baskets]
        survived = sum(1 for b in baskets if b.worst_payoff_mu > 0)
        rows.append(
            {
                "latency_ms": s["latency_ms"],
                "baskets": s["baskets"],
                "survival_rate": survived / len(baskets) if baskets else 0.0,
                "basket_completion_rate": s["basket_completion_rate"],
                "leg_fill_rate": s["leg_fill_rate"],
                "edge_retention_vs_l1": s["edge_retention_vs_l1"],
                "edge_retention_vs_l0": s["edge_retention_vs_l0"],
                "net_pnl": s["net_pnl"],
                "mean_net_per_basket": statistics.fmean(nets) if nets else 0.0,
                "ci_net_per_basket": bootstrap_ci(nets),
                "residual_contracts": s["residual_contracts"],
                "unwind_cost": s["unwind_cost"],
                "rate_limited": s["rate_limited"],
            }
        )
    return {
        "rows": rows,
        "break_even_ms": break_even_latency(rows),
        "break_even_label": break_even_bound(rows),
        "break_even_short": break_even_bound(rows, short=True),
    }


def break_even_latency(rows: Sequence[dict]) -> float | None:
    """The latency at which expected net P&L per basket crosses zero.

    Linear interpolation between the bracketing rungs. Returns ``None`` when the
    curve never crosses -- either always profitable or never profitable -- which
    is a meaningful answer, not a failure.
    """
    pts = [(r["latency_ms"], r["mean_net_per_basket"]) for r in rows]
    pts.sort()
    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if v0 > 0 >= v1:
            if v0 == v1:
                return float(d1)
            return d0 + (d1 - d0) * (v0 / (v0 - v1))
    return None


def break_even_bound(rows: Sequence[dict], short: bool = False) -> str:
    """Describe the break-even latency, including when it lies off the ladder.

    ``break_even_latency`` returns ``None`` both when the curve never becomes
    profitable and when it never stops being profitable. Those are opposite
    findings and should not print the same way: a monotone curve that is still
    positive at the last rung tells us delta* is *greater than* that rung, which
    is information, not an absence of it.
    """
    pts = sorted((r["latency_ms"], r["mean_net_per_basket"]) for r in rows)
    if not pts:
        return "n/a"
    if not any(r.get("baskets") for r in rows):
        # No basket was ever executed, so every rung averages zero. That is the
        # absence of a measurement, not a measurement of zero.
        return "undefined (no baskets executed)"
    be = break_even_latency(rows)
    if be is not None:
        return f"{be:.1f} ms"
    if pts[-1][1] > 0:
        return (f"> {pts[-1][0]:,} ms" if short
                else f"> {pts[-1][0]:,} ms (still profitable at the widest rung tested)")
    if pts[0][1] <= 0:
        return (f"< {pts[0][0]:,} ms" if short
                else f"< {pts[0][0]:,} ms (already unprofitable at the narrowest rung)")
    return "not resolved"


def break_even_by_series(result: BacktestResult) -> dict:
    """Per-series break-even latency.

    Reported separately because pooling hides the thing worth knowing: bursty,
    high-message-rate markets should die far faster than sparse scheduled ones.
    """
    out: dict[str, Any] = {}
    series = {b.series_ticker for baskets in result.executions.values() for b in baskets}
    for ser in sorted(s for s in series if s):
        rows = []
        for s in _baseline_configs(result):
            nets = [
                b.net_mu / 1e6
                for b in result.executions.get(s["config"], [])
                if b.series_ticker == ser
            ]
            if not nets:
                continue
            rows.append(
                {
                    "latency_ms": s["latency_ms"],
                    "n": len(nets),
                    "mean_net_per_basket": statistics.fmean(nets),
                    "ci": bootstrap_ci(nets),
                }
            )
        if rows:
            out[ser] = {
                "rows": rows,
                "break_even_ms": break_even_latency(rows),
                "break_even_label": break_even_bound(rows),
                "break_even_short": break_even_bound(rows, short=True),
                "n_baskets": rows[0]["n"],
                "inference_supported": rows[0]["n"] >= MIN_N_FOR_INFERENCE,
            }
    return out


# --------------------------------------------------------------------------
# E6 / E7 -- policy comparisons
# --------------------------------------------------------------------------
def policy_comparison(result: BacktestResult, baseline_ms: int) -> dict:
    at = [s for s in result.exec_summaries if s["latency_ms"] == baseline_ms]
    return {
        "latency_ms": baseline_ms,
        "order_type": [
            {k: s[k] for k in ("order_type", "net_pnl", "basket_completion_rate",
                               "leg_fill_rate", "residual_contracts")}
            for s in at if s["leg_order"] == "batch" and s["residual_policy"] == "unwind"
        ],
        "leg_order": [
            {k: s[k] for k in ("leg_order", "net_pnl", "basket_completion_rate",
                               "residual_contracts")}
            for s in at if s["order_type"] == "IOC" and s["residual_policy"] == "unwind"
        ],
        "residual_policy": [
            {k: s.get(k) for k in ("residual_policy", "net_pnl", "settled_pnl",
                                   "unwind_cost")}
            for s in at if s["order_type"] == "IOC" and s["leg_order"] == "batch"
        ],
    }


# --------------------------------------------------------------------------
# E8 / E9 -- fee regime and leg-count scaling
# --------------------------------------------------------------------------
def fee_regime_comparison(result: BacktestResult, baseline_ms: int) -> dict:
    """Isolates the fee effect from the microstructure effect.

    The zero-fee series is a near-laboratory control: same detector, same
    execution model, same latency, no fee. Whatever gap remains is
    microstructure.
    """
    zero_fee_series = {e.series_ticker for e in result.events if e.fee_schedule.is_zero_fee}
    cfg = next(
        (s["config"] for s in _baseline_configs(result) if s["latency_ms"] == baseline_ms),
        None,
    )
    if cfg is None:
        return {}
    groups: dict[str, list[ExecutedBasket]] = {"zero_fee": [], "standard_fee": []}
    for b in result.executions.get(cfg, []):
        groups["zero_fee" if b.series_ticker in zero_fee_series else "standard_fee"].append(b)
    out = {"latency_ms": baseline_ms, "zero_fee_series": sorted(zero_fee_series)}
    for name, bs in groups.items():
        out[name] = {
            "baskets": len(bs),
            "net_pnl": sum(b.net_mu for b in bs) / 1e6,
            "fees_paid": sum(b.fee_mu + b.unwind_fee_mu for b in bs) / 1e6,
            "complete_rate": (sum(1 for b in bs if b.complete) / len(bs)) if bs else 0.0,
            "mean_net_per_basket": statistics.fmean([b.net_mu / 1e6 for b in bs]) if bs else 0.0,
        }
    return out


def leg_count_scaling(result: BacktestResult) -> dict:
    """Tests whether the aggregate spread hurdle grows with leg count.

    An N-leg basket crosses N spreads, so the hurdle grows roughly linearly in N
    while the mispricing that creates an opportunity does not obviously do so.
    """
    stats = opportunity_stats(result)["by_leg_count"]
    hours = max(result.replay.get("duration_ms", 0) / 3_600_000, 1e-9)
    return {
        "rows": [
            {
                "n_legs": n,
                "episodes": v["episodes"],
                "actionable": v["actionable"],
                "actionable_per_hour": v["actionable"] / hours,
                "actionable_rate": v["actionable"] / v["episodes"] if v["episodes"] else 0.0,
            }
            for n, v in sorted(stats.items())
        ]
    }


# --------------------------------------------------------------------------
# E10 / E12 -- phase contamination and legging adverse selection
# --------------------------------------------------------------------------
def phase_analysis(result: BacktestResult) -> dict:
    """Quantifies phantom-liquidity contamination rather than merely excluding it."""
    by_phase = opportunity_stats(result)["by_phase"]
    total = sum(by_phase.values()) or 1
    return {
        "counts": by_phase,
        "pct": {k: 100.0 * v / total for k, v in by_phase.items()},
        "excluded_pct": 100.0 * sum(v for k, v in by_phase.items() if k != "active") / total,
    }


def legging_adverse_selection(result: BacktestResult) -> dict:
    """E[edge | full fill] versus E[edge | partial fill], per latency rung.

    The winner's-curse prediction: the leg that fails to fill is precisely the
    one that just moved against the basket, so realised edge conditional on a
    partial fill should be strictly worse -- and the gap should widen with
    latency.
    """
    rows = []
    for s in _baseline_configs(result):
        baskets = result.executions.get(s["config"], [])
        full = [b.net_mu / 1e6 for b in baskets if b.complete]
        part = [b.net_mu / 1e6 for b in baskets if not b.complete and b.any_fill]
        if not full and not part:
            continue
        rows.append(
            {
                "latency_ms": s["latency_ms"],
                "n_full": len(full),
                "n_partial": len(part),
                "mean_full": statistics.fmean(full) if full else None,
                "mean_partial": statistics.fmean(part) if part else None,
                "gap": (statistics.fmean(full) - statistics.fmean(part))
                if (full and part) else None,
            }
        )
    supported = [r for r in rows if r["gap"] is not None]
    return {
        "rows": rows,
        "hypothesis_supported": all(r["gap"] > 0 for r in supported) if supported else None,
        "widens_with_latency": (
            supported[-1]["gap"] > supported[0]["gap"] if len(supported) >= 2 else None
        ),
    }


# --------------------------------------------------------------------------
# E11 -- macro-event study
# --------------------------------------------------------------------------
def macro_event_study(
    result: BacktestResult, shocks: Sequence[dict], window_ms: int = 5_000
) -> dict:
    """Do scheduled information shocks actually generate dislocations?

    Compares the episode rate inside a window following each shock against the
    rate outside every such window. The comparison is per-event, because a shock
    to one family says nothing about another, and the windows are unioned before
    computing exposure so overlapping shocks are not double counted.

    On a live tape ``shocks`` would come from an economic calendar (FOMC, CPI,
    payrolls). On a synthetic tape the generator logs them exactly, which is what
    makes this checkable rather than merely suggestive.
    """
    if not shocks:
        return {"note": "no event calendar supplied; E11 not run"}
    by_event: dict[str, list[int]] = defaultdict(list)
    for s in shocks:
        by_event[s["event_ticker"]].append(int(s["ts_ms"]))

    in_win = out_win = 0
    in_actionable = out_actionable = 0
    for ep in result.episodes:
        times = by_event.get(ep.event_ticker)
        hit = bool(times) and any(0 <= ep.start_ts_ms - t <= window_ms for t in times)
        if hit:
            in_win += 1
            in_actionable += int(ep.actionable)
        else:
            out_win += 1
            out_actionable += int(ep.actionable)

    total_ms = max(result.replay.get("duration_ms", 0), 1)
    covered = 0
    for times in by_event.values():
        merged: list[list[int]] = []
        for a, b in sorted((t, t + window_ms) for t in times):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        covered += sum(b - a for a, b in merged)
    n_events = max(len(by_event), 1)
    in_frac = min(0.999, covered / (total_ms * n_events))

    in_rate = in_win / max(in_frac, 1e-9)
    out_rate = out_win / max(1 - in_frac, 1e-9)
    return {
        "window_ms": window_ms,
        "shocks": len(shocks),
        "time_share_in_window": in_frac,
        "episodes_in_window": in_win,
        "episodes_outside": out_win,
        "actionable_in_window": in_actionable,
        "actionable_outside": out_actionable,
        "rate_ratio": (in_rate / out_rate) if out_rate else None,
        "interpretation": (
            "A ratio above 1 means dislocations cluster after information shocks, "
            "which is the mechanism the whole latency experiment assumes."
        ),
    }


# --------------------------------------------------------------------------
# E13 -- capital
# --------------------------------------------------------------------------
def capital_study(
    result: BacktestResult,
    baseline_ms: int,
    *,
    hold_days: float = 1.0,
    budget_dollars: float = 25_000.0,
) -> dict:
    """Turns cents per basket into an economically meaningful number.

    With no settlement fee, holding to expiry is free, so the correct metric is
    return per dollar-*day* of capital, not return per trade.

    Capital is reported here rather than enforced upstream. Within a tape this
    short nothing settles, so capital only accumulates; capping it during the
    replay would silently truncate the latency experiment to the opening slice of
    the tape. Instead we report the peak requirement if every opportunity were
    taken, and how many of them a stated budget could actually have funded.
    """
    cfg = next(
        (s["config"] for s in _baseline_configs(result) if s["latency_ms"] == baseline_ms),
        None,
    )
    baskets = sorted(result.executions.get(cfg, []), key=lambda b: b.detect_ts_ms) if cfg else []
    capital = sum(b.capital_mu for b in baskets) / 1e6
    net = sum(b.net_mu for b in baskets) / 1e6
    roc = (net / capital) if capital else 0.0
    per_dollar_day = roc / hold_days if hold_days else 0.0

    # How far a fixed budget goes, taking opportunities in the order they arrive.
    budget_mu = int(budget_dollars * 1_000_000)
    spent = 0
    funded = 0
    funded_net = 0
    for b in baskets:
        if spent + b.capital_mu > budget_mu:
            continue
        spent += b.capital_mu
        funded += 1
        funded_net += b.net_mu

    return {
        "latency_ms": baseline_ms,
        "capital_deployed": capital,
        "net_pnl": net,
        "return_on_capital_bps": roc * 10_000,
        "assumed_hold_days": hold_days,
        "return_per_dollar_day_bps": per_dollar_day * 10_000,
        "annualised_pct": ((1 + per_dollar_day) ** 365 - 1) * 100 if abs(per_dollar_day) < 1 else None,
        "budget_dollars": budget_dollars,
        "baskets_total": len(baskets),
        "baskets_fundable": funded,
        "budget_utilisation": spent / budget_mu if budget_mu else 0.0,
        "net_within_budget": funded_net / 1e6,
        "note": (
            "Positions are cash-collateralised and held to settlement, so capital "
            "accumulates across the tape and never releases. Collateral return "
            "(netting_enabled) would reduce the posted amount for a hedged "
            "mutually-exclusive basket; its exact accounting is not verified here, "
            "so these figures assume no capital relief."
        ),
    }


# --------------------------------------------------------------------------
# E14 -- engineering
# --------------------------------------------------------------------------
def engineering_stats(result: BacktestResult) -> dict:
    out: dict[str, Any] = {
        "records": result.replay.get("records", 0),
        "deltas": result.replay.get("deltas", 0),
        "wall_seconds": result.wall_seconds,
        "throughput_records_per_s": (
            result.replay.get("records", 0) / result.wall_seconds if result.wall_seconds else 0.0
        ),
        "sequence_gaps": result.replay.get("sequence_gaps", 0),
        "snapshot_mismatch_rate": result.replay.get("snapshot_mismatch_rate", 0.0),
        "untrusted_ms": result.replay.get("untrusted_ms", 0),
    }
    for stage, samples in result.stage_latency_ns.items():
        if not samples:
            continue
        us = [s / 1_000 for s in samples]
        out[f"{stage}_us"] = {
            "n": len(us),
            "p50": _pct(us, 0.50),
            "p99": _pct(us, 0.99),
            "mean": statistics.fmean(us),
        }
    return out


def strategy_quality(result: BacktestResult, baseline_ms: int) -> dict:
    cfg = next(
        (s["config"] for s in _baseline_configs(result) if s["latency_ms"] == baseline_ms),
        None,
    )
    baskets = result.executions.get(cfg, []) if cfg else []
    nets = [b.net_mu / 1e6 for b in baskets]
    wins = [n for n in nets if n > 0]
    losses = [-n for n in nets if n < 0]
    return {
        "latency_ms": baseline_ms,
        "n": len(nets),
        "hit_rate": len(wins) / len(nets) if nets else 0.0,
        "profit_factor": (sum(wins) / sum(losses)) if losses else None,
        "max_drawdown": _max_drawdown(nets),
        "sharpe": None,
        "sharpe_note": (
            "Deliberately not reported: returns are lumpy, non-i.i.d. and "
            f"n={len(nets)} is too small for the statistic to mean anything."
        ),
        "inference_supported": len(nets) >= MIN_N_FOR_INFERENCE,
    }


def _max_drawdown(nets: Sequence[float]) -> float:
    peak = cum = 0.0
    dd = 0.0
    for n in nets:
        cum += n
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


# --------------------------------------------------------------------------
def compute_metrics(
    result: BacktestResult, baseline_ms: int = 100, shocks: Sequence[dict] | None = None
) -> dict:
    """Every experiment in one dict. Pure function of the backtest result."""
    return {
        "provenance": {
            "source": result.source,
            "tape_note": result.tape_note,
            "is_synthetic": result.source.startswith("synthetic"),
        },
        "replay": result.replay,
        "universe": result.universe_profile,
        "funnel": rejection_funnel(result),
        "opportunities": opportunity_stats(result),
        "latency": latency_curve(result),
        "latency_by_series": break_even_by_series(result),
        "policies": policy_comparison(result, baseline_ms),
        "fee_regime": fee_regime_comparison(result, baseline_ms),
        "leg_count": leg_count_scaling(result),
        "phases": phase_analysis(result),
        "macro_events": macro_event_study(result, shocks or []),
        "adverse_selection": legging_adverse_selection(result),
        "capital": capital_study(result, baseline_ms),
        "engineering": engineering_stats(result),
        "quality": strategy_quality(result, baseline_ms),
        "validation": {
            "lp_checks": result.lp_checks,
            "lp_disagreements": result.lp_disagreements,
            "lp_max_gap_dollars": result.lp_max_gap,
            "settlement": result.settlement_check,
            "n0_violations": result.replay.get("n0_violations", 0),
        },
        "edge_curves": result.edge_curves,
        "not_obtainable": list(NOT_OBTAINABLE),
    }
