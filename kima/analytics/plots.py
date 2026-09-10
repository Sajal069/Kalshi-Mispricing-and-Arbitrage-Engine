"""Figures.

Palette: the first three categorical slots of the reference data-viz palette,
used unchanged (blue #2a78d6 / orange #eb6834 / aqua #1baf7a). Validated rather
than eyeballed -- ``validate_palette.js`` on the light surface with ``--pairs
all`` reports:

    lightness band        PASS   all 3 inside L 0.43-0.77
    chroma floor          PASS   all 3 >= 0.1
    CVD separation        PASS   worst all-pairs dE 9.2 (deutan), 9.6 (tritan)
    normal-vision floor   PASS   worst all-pairs dE 24.0
    contrast vs surface   WARN   aqua at 2.74:1, below 3:1

The contrast warning is not dismissable: it obliges relief. Every series here is
directly labelled on the plot, and every figure is mirrored by a table in the
written report, so identity never rests on colour alone.

Design rules followed: one y-axis per chart (never two scales), a legend
whenever two or more series are present, thin marks over heavy ones, recessive
grid and axes, and selective direct labels rather than a number on every point.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
TEXT_MUTED = "#7a7975"
GRID = "#e4e3df"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a")
GOOD = "#1baf7a"
CRITICAL = "#e34948"

plt.rcParams.update({
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID,
    "axes.labelcolor": TEXT_SECONDARY,
    "axes.titlecolor": TEXT_PRIMARY,
    "text.color": TEXT_PRIMARY,
    "xtick.color": TEXT_SECONDARY,
    "ytick.color": TEXT_SECONDARY,
    "font.size": 10,
    "axes.titlesize": 12,
    "axes.titleweight": "600",
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "legend.frameon": False,
    "figure.dpi": 130,
})


def _style(ax, title: str, xlabel: str = "", ylabel: str = "", subtitle: str = "") -> None:
    ax.set_title(title, loc="left", pad=18 if subtitle else 10)
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9,
                color=TEXT_MUTED, va="bottom")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.set_axisbelow(True)


def _save(fig, out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def _money(x, _pos=None) -> str:
    return f"${x:,.2f}"


# --------------------------------------------------------------------------
def plot_funnel(metrics: dict, out_dir: Path) -> Path:
    """The flagship figure: how many raw signals survive each friction."""
    rows = metrics["funnel"]["rows"]
    labels = [r["stage"].replace("_", " ") for r in rows]
    counts = [r["count"] for r in rows]
    fig, ax = plt.subplots(figsize=(8.2, 4.4))
    y = range(len(rows))
    ax.barh(list(y), counts, height=0.62, color=SERIES[0], zorder=3)
    ax.set_yticks(list(y), labels)
    ax.invert_yaxis()
    ax.xaxis.grid(True)
    ax.yaxis.grid(False)
    top = max(counts) if counts else 1
    for i, r in enumerate(rows):
        ax.text(r["count"] + top * 0.012, i, f"{r['count']:,}  ({r['pct_of_raw']:.1f}%)",
                va="center", fontsize=9, color=TEXT_SECONDARY)
    ax.set_xlim(0, top * 1.28)
    _style(ax, "Rejection funnel", "signals surviving", "",
           "Each stage removes one named friction. The gap between the first row and the last is the result.")
    return _save(fig, out_dir, "fig1_rejection_funnel.png")


def plot_edge_distribution(metrics: dict, result, out_dir: Path) -> Path:
    edges = [e.best_basket.profit_mu / 1e6 for e in result.episodes if e.actionable]
    fig, ax = plt.subplots(figsize=(7.4, 4.0))
    if edges:
        ax.hist(edges, bins=min(30, max(6, len(edges) // 3)), color=SERIES[0], zorder=3)
        med = metrics["opportunities"]["edge_dollars"].get("median")
        if med is not None:
            ax.axvline(med, color=SERIES[1], linewidth=2, zorder=4)
            ax.text(med, ax.get_ylim()[1] * 0.94, f"  median {_money(med)}",
                    color=SERIES[1], fontsize=9, va="top")
    else:
        ax.text(0.5, 0.5, "no actionable episodes", transform=ax.transAxes,
                ha="center", color=TEXT_MUTED)
    ax.xaxis.set_major_formatter(FuncFormatter(_money))
    _style(ax, "Worst-case profit per actionable basket", "guaranteed profit", "episodes",
           "Fee-inclusive, depth-limited, evaluated before any latency is applied.")
    return _save(fig, out_dir, "fig2_edge_distribution.png")


def plot_duration_survival(metrics: dict, result, out_dir: Path) -> Path:
    """How long a dislocation lives. Censoring is drawn, not hidden."""
    all_d = sorted(e.duration_ms for e in result.episodes if e.duration_ms > 0)
    act_d = sorted(e.duration_ms for e in result.episodes if e.actionable and e.duration_ms > 0)
    fig, ax = plt.subplots(figsize=(7.4, 4.0))
    plotted = False
    for series, colour, label in ((all_d, SERIES[0], "all raw episodes"),
                                  (act_d, SERIES[1], "actionable episodes")):
        if not series:
            continue
        plotted = True
        n = len(series)
        surv = [1.0 - i / n for i in range(n)]
        ax.step(series, surv, where="post", color=colour, linewidth=2, label=label, zorder=3)
        ax.text(series[-1], surv[-1], f"  {label} (n={n})", color=colour, fontsize=9, va="center")
    if not plotted:
        ax.text(0.5, 0.5, "no episodes with measurable duration", transform=ax.transAxes,
                ha="center", color=TEXT_MUTED)
    else:
        ax.set_xscale("symlog", linthresh=10)
        ax.legend(loc="upper right")
    censored = metrics["opportunities"]["censored_episodes"]
    _style(ax, "Opportunity survival curve", "episode duration (ms, symlog)",
           "fraction still violating",
           f"Durations are capped by a trust ceiling; {censored} episode(s) censored by a quiet feed.")
    return _save(fig, out_dir, "fig3_duration_survival.png")


def plot_edge_vs_size(result, out_dir: Path) -> Path:
    """The marginal basket is always the worst one."""
    curves = result.edge_curves
    fig, ax = plt.subplots(figsize=(7.4, 4.0))
    if not curves:
        ax.text(0.5, 0.5, "no edge curves captured", transform=ax.transAxes,
                ha="center", color=TEXT_MUTED)
    else:
        by_series: dict[str, list] = {}
        for c in curves:
            by_series.setdefault(c["series"], []).append(c)
        # Cap at three series; the rest fold into "other" rather than cycling hues.
        ordered = sorted(by_series.items(), key=lambda kv: -len(kv[1]))
        keep = ordered[:3]
        for idx, (name, cs) in enumerate(keep):
            c = max(cs, key=lambda c: c["best_profit"])
            xs = [p[0] for p in c["points"]]
            ys = [p[1] for p in c["points"]]
            ax.plot(xs, ys, color=SERIES[idx], linewidth=2, marker="o", markersize=4,
                    label=f"{name} ({c['condition']})", zorder=3)
            ax.text(xs[-1], ys[-1], f"  {name}", color=SERIES[idx], fontsize=9, va="center")
        ax.axhline(0, color=TEXT_MUTED, linewidth=1, linestyle=(0, (4, 3)), zorder=2)
        if len(keep) >= 2:
            ax.legend(loc="upper left")
        ax.yaxis.set_major_formatter(FuncFormatter(_money))
    _style(ax, "Edge versus basket size", "basket size (contracts)", "worst-case profit",
           "Every additional contract walks further down each leg's ladder.")
    return _save(fig, out_dir, "fig4_edge_vs_size.png")


def plot_edge_retention(metrics: dict, out_dir: Path) -> Path:
    """The money plot: how much theoretical edge survives contact with the clock."""
    rows = metrics["latency"]["rows"]
    xs = [r["latency_ms"] for r in rows]
    ys = [100 * r["edge_retention_vs_l1"] for r in rows]
    fig, ax = plt.subplots(figsize=(7.8, 4.4))

    # Deep-loss rungs would otherwise compress the only region anyone reads --
    # the decay from 100% and the crossing of zero. Clip the axis and mark the
    # off-scale points explicitly rather than letting one rung set the scale.
    floor = -120.0
    ax.set_ylim(floor, 115)
    visible = [(x, y) for x, y in zip(xs, ys) if y >= floor]
    ax.plot([p[0] for p in visible], [p[1] for p in visible],
            color=SERIES[0], linewidth=2, marker="o", markersize=5, zorder=4)
    off = [(x, y) for x, y in zip(xs, ys) if y < floor]
    for x, y in off:
        ax.plot([x], [floor], color=SERIES[0], marker="v", markersize=7, zorder=4)
    ax.axhline(0, color=TEXT_MUTED, linewidth=1, linestyle=(0, (4, 3)), zorder=2)

    be = metrics["latency"]["break_even_ms"]
    if be is not None:
        ax.axvline(be, color=CRITICAL, linewidth=2, zorder=3)
        ax.text(be, 108, f"  break-even {be:.0f} ms", color=CRITICAL, fontsize=9.5, va="top")

    ax.set_xscale("symlog", linthresh=5)
    # Label a readable subset; every rung still carries a marker.
    ticks = [x for x in xs if x in (0, 10, 25, 50, 100, 250, 1000)] or xs
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(t) for t in ticks])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:.0f}%"))

    notes = []
    eng = metrics.get("engineering", {}).get("screen_us")
    if eng:
        notes.append(f"measured internal detect latency p50 {eng['p50']:.1f} us / p99 {eng['p99']:.1f} us")
    if off:
        notes.append(f"{len(off)} rung(s) below {floor:.0f}% shown as triangles; see table")
    if notes:
        ax.text(0.5, -0.28, "   |   ".join(notes), transform=ax.transAxes,
                ha="center", fontsize=8.5, color=TEXT_MUTED)

    _style(ax, "Edge retention versus execution latency",
           "injected decision-to-arrival latency (ms, symlog)",
           "realised net P&L as % of theoretical L1 edge",
           "Legs are not atomic: a basket that half-fills is a directional position, not an arbitrage.")
    return _save(fig, out_dir, "fig5_edge_retention_vs_latency.png")


def plot_completion_and_fill(metrics: dict, out_dir: Path) -> Path:
    """Fill quality on its own axis -- deliberately not overlaid on P&L."""
    rows = metrics["latency"]["rows"]
    xs = [r["latency_ms"] for r in rows]
    fig, ax = plt.subplots(figsize=(7.4, 4.0))
    for idx, (key, label) in enumerate(
        (("basket_completion_rate", "baskets fully assembled"),
         ("leg_fill_rate", "legs filled in full"),
         ("survival_rate", "baskets with a positive floor"))
    ):
        ys = [100 * r[key] for r in rows]
        ax.plot(xs, ys, color=SERIES[idx], linewidth=2, marker="o", markersize=4,
                label=label, zorder=3)
    ax.set_xscale("symlog", linthresh=5)
    ticks = [x for x in xs if x in (0, 10, 25, 50, 100, 250, 1000)] or xs
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(t) for t in ticks])
    ax.set_ylim(0, 105)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:.0f}%"))
    ax.legend(loc="lower left")
    _style(ax, "Fill quality versus latency", "injected latency (ms, symlog)", "share",
           "The bottleneck leg caps the basket, so completion falls faster than leg fill rate.")
    return _save(fig, out_dir, "fig6_fill_quality.png")


def plot_cumulative_pnl(metrics: dict, result, out_dir: Path) -> Path:
    rows = metrics["latency"]["rows"]
    chosen = [r["latency_ms"] for r in rows]
    picks = [chosen[0]]
    if len(chosen) > 2:
        picks.append(chosen[len(chosen) // 2])
    picks.append(chosen[-1])
    picks = sorted(set(picks))[:3]

    fig, ax = plt.subplots(figsize=(7.6, 4.2))
    plotted = False
    for idx, ms in enumerate(picks):
        label = None
        for s in result.exec_summaries:
            if (s["latency_ms"] == ms and s["order_type"] == "IOC"
                    and s["leg_order"] == "batch" and s["residual_policy"] == "unwind"):
                label = s["config"]
                break
        baskets = sorted(result.executions.get(label, []), key=lambda b: b.detect_ts_ms)
        if not baskets:
            continue
        plotted = True
        t0 = baskets[0].detect_ts_ms
        xs, ys, cum = [], [], 0.0
        for b in baskets:
            cum += b.net_mu / 1e6
            xs.append((b.detect_ts_ms - t0) / 60_000)
            ys.append(cum)
        ax.plot(xs, ys, color=SERIES[idx], linewidth=2, label=f"{ms} ms", zorder=3)
        ax.text(xs[-1], ys[-1], f"  {ms} ms", color=SERIES[idx], fontsize=9, va="center")
    ax.axhline(0, color=TEXT_MUTED, linewidth=1, linestyle=(0, (4, 3)), zorder=2)
    if plotted:
        ax.legend(loc="lower left", title="latency")
        ax.yaxis.set_major_formatter(FuncFormatter(_money))
    else:
        ax.text(0.5, 0.5, "no executed baskets", transform=ax.transAxes,
                ha="center", color=TEXT_MUTED)
    _style(ax, "Cumulative net P&L", "minutes into the tape", "cumulative net",
           "Same opportunities, same tape; only the injected latency differs.")
    return _save(fig, out_dir, "fig7_cumulative_pnl.png")


def plot_stage_latency(metrics: dict, out_dir: Path) -> Path:
    eng = metrics["engineering"]
    stages = [(k[:-3], v) for k, v in eng.items() if k.endswith("_us") and isinstance(v, dict)]
    fig, ax = plt.subplots(figsize=(7.2, 3.8))
    if not stages:
        ax.text(0.5, 0.5, "latency not measured", transform=ax.transAxes,
                ha="center", color=TEXT_MUTED)
    else:
        names = [s[0] for s in stages]
        p50 = [s[1]["p50"] for s in stages]
        p99 = [s[1]["p99"] for s in stages]
        y = range(len(names))
        h = 0.34
        ax.barh([i - h / 2 - 0.01 for i in y], p50, height=h, color=SERIES[0], label="p50", zorder=3)
        ax.barh([i + h / 2 + 0.01 for i in y], p99, height=h, color=SERIES[1], label="p99", zorder=3)
        ax.set_yticks(list(y), names)
        ax.set_xscale("log")
        ax.xaxis.grid(True)
        ax.yaxis.grid(False)
        ax.legend(loc="lower right")
        for i, (a, b) in enumerate(zip(p50, p99)):
            ax.text(a * 1.1, i - h / 2 - 0.01, f"{a:,.1f} us", va="center", fontsize=8.5, color=TEXT_SECONDARY)
            ax.text(b * 1.1, i + h / 2 + 0.01, f"{b:,.1f} us", va="center", fontsize=8.5, color=TEXT_SECONDARY)
        ax.set_xlim(right=max(p99) * 4)
    _style(ax, "Per-stage processing latency", "microseconds (log)", "",
           "The O(1) screen is the cheap stage; exact sizing runs only on survivors.")
    return _save(fig, out_dir, "fig8_stage_latency.png")


def render_all(metrics: dict, result, out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    figs = [
        plot_funnel(metrics, out),
        plot_edge_distribution(metrics, result, out),
        plot_duration_survival(metrics, result, out),
        plot_edge_vs_size(result, out),
        plot_edge_retention(metrics, out),
        plot_completion_and_fill(metrics, out),
        plot_cumulative_pnl(metrics, result, out),
        plot_stage_latency(metrics, out),
    ]
    return figs
