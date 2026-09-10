"""Written report generation.

Two rules govern everything in this module.

**No fabricated numbers.** Every figure printed comes from the metrics dict,
which comes from a deterministic replay of an actual tape. Where a quantity was
not measured, the report says so rather than estimating it.

**Provenance travels with the numbers.** A report built from a synthetic tape is
stamped as such at the top, in every section header that quotes a P&L, and in
the CV bullets. A reader must not be able to mistake a simulation result for a
measurement of Kalshi.

Each hypothesis from the plan is marked supported / refuted / untested against
the data actually collected -- including the ones that turned out wrong, which is
the part that makes the rest credible.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SYNTHETIC_BANNER = (
    "> **PROVENANCE: SYNTHETIC TAPE.** These numbers were produced by replaying a\n"
    "> simulated order-book tape, not a recording of Kalshi. They measure the\n"
    "> *engine* -- its correctness, its determinism, and the shape of its latency\n"
    "> response -- and say nothing about how much arbitrage exists on the real\n"
    "> exchange. Kalshi publishes no historical order-book data, so producing the\n"
    "> real-world version of this table requires running `kima record` for the\n"
    "> stated window first. Every hypothesis below is therefore marked UNTESTED\n"
    "> against live data regardless of what the simulation showed.\n"
)

LIVE_BANNER = (
    "> **PROVENANCE: SELF-RECORDED LIVE TAPE.** No public Level 2 dataset exists\n"
    "> for Kalshi and the API serves no order-book history, so this study covers\n"
    "> only the window recorded below and cannot speak to other periods or regimes.\n"
)


def _fmt(x: Any, nd: int = 4, dash: str = "n/a") -> str:
    if x is None:
        return dash
    if isinstance(x, float):
        if x != x:  # NaN
            return dash
        return f"{x:,.{nd}f}"
    if isinstance(x, int):
        return f"{x:,}"
    return str(x)


def _pct(x: float | None, nd: int = 1) -> str:
    return "n/a" if x is None else f"{100 * x:.{nd}f}%"


def _be_label(section: dict, short: bool = False) -> str:
    """Break-even latency as text, derived from the rows if not precomputed."""
    key = "break_even_short" if short else "break_even_label"
    label = section.get(key)
    if label:
        return label
    from .metrics import break_even_bound
    return break_even_bound(section.get("rows", []), short=short)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


# --------------------------------------------------------------------------
def hypothesis_verdicts(m: dict) -> list[tuple[str, str, str, str]]:
    """(id, claim, verdict, evidence) for H1-H12.

    ``UNTESTED`` on synthetic data is not a cop-out: a hypothesis about Kalshi
    cannot be settled by a simulation whose parameters we chose.
    """
    synthetic = m["provenance"]["is_synthetic"]
    f = m["funnel"]
    opp = m["opportunities"]
    lat = m["latency"]
    fee = m.get("fee_regime", {})
    legs = m.get("leg_count", {}).get("rows", [])
    adv = m.get("adverse_selection", {})
    phases = m.get("phases", {})

    def verdict(engine_says: bool | None) -> str:
        if synthetic:
            return "UNTESTED (live)"
        if engine_says is None:
            return "UNTESTED"
        return "SUPPORTED" if engine_says else "REFUTED"

    survival = f["survival_pct"]
    dur = opp["duration_ms_actionable"]
    med_dur = dur.get("median")
    sizes = opp["executable_size_contracts"]
    be = lat["break_even_ms"]

    zero = fee.get("zero_fee", {})
    std = fee.get("standard_fee", {})
    zero_better = (
        zero.get("mean_net_per_basket", 0) > std.get("mean_net_per_basket", 0)
        if zero and std else None
    )
    n1 = sum(v for k, v in opp["by_condition"].items() if k == "N1")
    n2 = sum(v for k, v in opp["by_condition"].items() if k == "N2")

    leg_decline = None
    if len(legs) >= 2:
        rates = [(r["n_legs"], r["actionable_rate"]) for r in legs if r["episodes"] > 0]
        if len(rates) >= 2:
            leg_decline = rates[0][1] > rates[-1][1]

    return [
        ("H1", "Raw L0 counts are large and mostly illusory",
         verdict(survival < 50 and f["raw_signals"] > 0),
         f"{f['raw_signals']:,} raw signals; {survival:.1f}% survive every filter"),
        ("H2", "Actionable episodes are sparse after the full funnel",
         verdict(opp["episodes_actionable"] < opp["episodes_total"]),
         f"{opp['episodes_actionable']} actionable of {opp['episodes_total']} episodes"),
        ("H3", "Depth, not edge size, is the binding constraint",
         verdict(None),
         f"median bottleneck size {_fmt(sizes.get('median'), 1)} contracts; "
         f"depth filter removed {f['attribution'].get('min_size', 0):,} signals"),
        ("H4", "Median episode duration is seconds, not minutes",
         verdict(med_dur is not None and med_dur < 60_000),
         f"median actionable duration {_fmt(med_dur, 0)} ms"),
        ("H5", "Break-even latency is above zero but below a retail round trip",
         verdict(be is not None and 0 < be < 100),
         f"break-even delta* {_be_label(lat, short=True)}"),
        ("H6", "Fee rounding creates a minimum viable basket size",
         verdict(f["attribution"].get("fee_negative", 0) > 0),
         f"exact fees removed {f['attribution'].get('fee_negative', 0):,} otherwise-viable signals"),
        ("H7", "Zero-fee series retain more edge, but not proportionally",
         verdict(zero_better),
         f"zero-fee mean net/basket {_fmt(zero.get('mean_net_per_basket'))} vs "
         f"standard {_fmt(std.get('mean_net_per_basket'))}"),
        ("H8", "Opportunity frequency declines with leg count",
         verdict(leg_decline),
         f"actionable rate by leg count: "
         + ", ".join(f"N={r['n_legs']}:{r['actionable_rate']:.2f}" for r in legs)),
        ("H9", "E[edge | full fill] > E[edge | partial fill], widening with latency",
         verdict(adv.get("hypothesis_supported")),
         f"gap positive at every rung: {adv.get('hypothesis_supported')}; "
         f"widens with latency: {adv.get('widens_with_latency')}"),
        ("H10", "Condition N1 fires more often than N2",
         verdict(n1 > n2 if (n1 or n2) else None),
         f"N1 episodes {n1}, N2 episodes {n2}"),
        ("H11", "Rate limits bind before latency during simultaneous dislocations",
         verdict(None),
         f"rate-limited submissions at baseline: "
         f"{sum(r['rate_limited'] for r in lat['rows'])}"),
        ("H12", "Net P&L at realistic latency is small in dollars, positive in bps",
         verdict(None),
         f"net at baseline {_fmt(m['capital']['net_pnl'])} on "
         f"{_fmt(m['capital']['capital_deployed'])} of capital"),
    ]


# --------------------------------------------------------------------------
def render_report(m: dict, figures: Sequence[Path] | None = None,
                  title: str = "KIMA -- Kalshi Intra-Event Mispricing & Arbitrage Engine") -> str:
    synthetic = m["provenance"]["is_synthetic"]
    f, opp, lat = m["funnel"], m["opportunities"], m["latency"]
    rep, val, eng = m["replay"], m["validation"], m["engineering"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hours = rep.get("duration_ms", 0) / 3_600_000

    L: list[str] = []
    add = L.append

    add(f"# {title}\n")
    add(f"*Generated {now}. Every number below is produced by a single deterministic "
        f"replay of the tape named in Provenance; none is hand-entered.*\n")
    add(SYNTHETIC_BANNER if synthetic else LIVE_BANNER)

    # -- research question ------------------------------------------------
    add("\n## The question\n")
    add("> What fraction of theoretically risk-free intra-event arbitrage edge on "
        "Kalshi survives exact fees, finite order-book depth, non-atomic multi-leg "
        "execution, and realistic round-trip latency?\n")
    add("Kalshi publishes an event-level `mutually_exclusive` flag, and the exchange "
        "guarantees at settlement that at most one constituent market resolves YES. "
        "But each market matches in isolation -- there is no cross-market matching, no "
        "combination order type, no implied-order engine. A constraint that must hold "
        "at settlement is enforced by nobody at the quote level. That gap is the "
        "entire subject of this study.\n")

    # -- headline ---------------------------------------------------------
    be = lat["break_even_ms"]
    add("\n## Headline result\n")
    add(_table(
        ["Quantity", "Value"],
        [
            ["Raw L0 signals", f"{f['raw_signals']:,}"],
            ["Actionable after every filter", f"{f['actionable']:,} ({f['survival_pct']:.2f}% of raw)"],
            ["Episodes (grouped, credited once each)", f"{opp['episodes_total']:,}"],
            ["Actionable episodes", f"{opp['episodes_actionable']:,}"],
            ["Median edge per actionable basket", f"${_fmt(opp['edge_dollars'].get('median'))}"],
            ["Median episode duration", f"{_fmt(opp['duration_ms_actionable'].get('median'), 0)} ms"],
            ["**Break-even latency delta\\***", f"**{_be_label(lat, short=True)}**"],
            ["Edge retention at 100 ms", _pct(next((r['edge_retention_vs_l1'] for r in lat['rows'] if r['latency_ms'] == 100), None))],
            ["Tape duration (recorded)", f"{hours:.2f} h"],
            ["Order-book messages processed", f"{rep.get('records', 0):,}"],
        ] + ([
            ["Wall-clock span (incl. gaps between sessions)",
             f"{rep.get('span_ms', 0) / 3_600_000:.2f} h across "
             f"{rep.get('idle_gaps', 0)} idle gap(s)"],
        ] if rep.get("idle_gaps") else []) + [
        ] + ([
            ["Measured feed delay (p50 / p99)",
             f"{rep['feed_delay_ms_p50']:,} / {rep['feed_delay_ms_p99']:,} ms"],
        ] if rep.get("feed_delay_samples") else []) + [
        ],
    ))
    add("")
    if be is not None:
        add(f"**delta\\* = {be:.1f} ms.** Below that, the strategy is latency-feasible; "
            f"above it, expected net P&L per basket is negative. This is the one scalar "
            f"the whole project exists to produce.\n")
        p50 = rep.get("feed_delay_ms_p50")
        if p50:
            ratio = p50 / be if be else float("inf")
            verdict = "NOT latency-feasible" if p50 > be else "latency-feasible"
            add(f"Set against it, the **measured feed delay alone is {p50:,} ms** -- "
                f"{ratio:.0f}x the break-even latency. That is one leg only: market "
                f"data reaching this machine. A round trip adds the outbound order hop "
                f"on top, so this is a lower bound on the true handicap. On this tape "
                f"the strategy is therefore **{verdict}** from this connection, and "
                f"closing that gap is a question of colocation rather than of code.\n")
    else:
        executed = sum(r.get("baskets", 0) for r in lat["rows"])
        if not executed:
            add("**delta\\* is undefined: no basket survived the filters, so nothing "
                "was executed at any latency.** The latency experiment needs an "
                "opportunity to decay before it can measure decay. Read the rejection "
                "funnel above instead -- it says which friction removed everything, "
                "and that is the result this tape supports.\n")
        else:
            add(f"**delta\\* {_be_label(lat)}.** Expected net P&L per basket does not "
                "cross zero anywhere on the ladder the plan specifies (0-2000 ms), so "
                "the pooled figure is a bound rather than a point estimate. Per-series "
                "figures are reported below; pooling was always the weaker reading, "
                "since a bursty two-outcome game market and a scheduled rate ladder "
                "have no business sharing a break-even latency.\n")

    # -- universe (E1) ----------------------------------------------------
    uni = m.get("universe", {})
    if uni.get("by_series"):
        from ..profiling import render_universe_table
        add("\n## The universe (E1)\n")
        add("An N-leg basket has to cross N spreads, so the sum of a family's "
            "spreads bounds what it can produce before any arbitrage claim is "
            "made. Reporting it first means the results below read as *given "
            "these spreads, here is what survived* rather than as an "
            "unexplained hit rate.\n")
        add(render_universe_table(uni))
        add(f"\n*Top-of-book sampled every {uni['sample_every_n_updates']} updates "
            "per market. Spread is the implied YES spread, "
            "`(1 - bestNoBid) - bestYesBid`.*\n")

    # -- funnel -----------------------------------------------------------
    add("\n## The rejection funnel\n")
    add("The funnel *is* the result. Filters are applied in a fixed order so each "
        "stage is attributable to exactly one friction.\n")
    add(_table(
        ["Stage", "Surviving", "% of raw", "Removed here"],
        [[r["stage"].replace("_", " "), f"{r['count']:,}", f"{r['pct_of_raw']:.2f}%",
          f"{r['removed']:,}" if r["removed"] else "--"] for r in f["rows"]],
    ))
    add("")
    if f["attribution"]:
        add("Attribution of every rejection:\n")
        add(_table(["Reason", "Count"],
                   [[k.replace("_", " "), f"{v:,}"]
                    for k, v in sorted(f["attribution"].items(), key=lambda kv: -kv[1])]))
        add("")

    # -- latency ----------------------------------------------------------
    add("\n## The latency experiment\n")
    add("Each rung re-runs the identical opportunities against the book as it actually "
        "existed `delta` milliseconds later, reconstructed from the tape. Legs match "
        "independently: Kalshi has no cross-market atomicity, so a basket that "
        "half-fills is a directional position, not an arbitrage.\n")
    add(_table(
        ["delta (ms)", "Baskets", "Complete", "Leg fill", "Retention vs L1",
         "Net P&L", "Mean net/basket", "95% CI"],
        [[r["latency_ms"], r["baskets"], _pct(r["basket_completion_rate"]),
          _pct(r["leg_fill_rate"]), _pct(r["edge_retention_vs_l1"]),
          f"${_fmt(r['net_pnl'], 2)}", f"${_fmt(r['mean_net_per_basket'])}",
          (f"[{r['ci_net_per_basket'][0]:.4f}, {r['ci_net_per_basket'][1]:.4f}]"
           if r["ci_net_per_basket"] else "n too small")]
         for r in lat["rows"]],
    ))
    add("")
    add("`L2(delta=0)` reproduces `L1` exactly by construction, and that identity is "
        "asserted in the test suite -- it is the control for every other row.\n")

    by_series = m.get("latency_by_series", {})
    if by_series:
        add("### Break-even by series\n")
        add("Pooling hides the thing worth knowing: bursty markets should die faster "
            "than sparse scheduled ones.\n")
        add(_table(
            ["Series", "Baskets", "delta\\* (ms)", "Inference supported?"],
            [[k, v["n_baskets"], _be_label(v, short=True),
              "yes" if v["inference_supported"] else f"no (n={v['n_baskets']})"]
             for k, v in by_series.items()],
        ))
        add("")

    # -- microstructure ---------------------------------------------------
    add("\n## What actually kills the trade\n")
    fee = m.get("fee_regime", {})
    if fee.get("zero_fee") and fee.get("standard_fee"):
        add("### Fees versus microstructure (E8)\n")
        add("The zero-fee series is the closest thing to a control available: "
            "identical detector, identical execution model, identical latency, no "
            "fee.\n")
        add("It is **not** a clean one, and the difference should not be read as a "
            "pure fee effect. The zero-fee family also has its own spread, depth, "
            "tick grid and maker reaction speed, so the two rows differ in more "
            "than their fee schedule -- compare them against the universe table "
            "above before drawing a conclusion. A genuinely clean version needs "
            "two series that differ *only* in fee multiplier.\n")
        add(_table(
            ["Regime", "Baskets", "Net P&L", "Fees paid", "Complete rate", "Mean net/basket"],
            [[name.replace("_", " "), v["baskets"], f"${_fmt(v['net_pnl'], 2)}",
              f"${_fmt(v['fees_paid'], 2)}", _pct(v["complete_rate"]),
              f"${_fmt(v['mean_net_per_basket'])}"]
             for name, v in (("zero_fee", fee["zero_fee"]), ("standard_fee", fee["standard_fee"]))],
        ))
        add("")

    legs = m.get("leg_count", {}).get("rows", [])
    if legs:
        add("### Leg-count scaling (E9)\n")
        add("An N-leg basket crosses N spreads, so the hurdle grows roughly linearly "
            "in N while the mispricing that creates an opportunity does not.\n")
        add(_table(["Legs", "Episodes", "Actionable", "Actionable rate", "Per hour"],
                   [[r["n_legs"], r["episodes"], r["actionable"],
                     _pct(r["actionable_rate"]), f"{r['actionable_per_hour']:.2f}"]
                    for r in legs]))
        add("")

    ph = m.get("phases", {})
    if ph.get("counts"):
        add("### Event-phase contamination (E10)\n")
        add(_table(["Phase", "Episodes", "Share"],
                   [[k, v, f"{ph['pct'][k]:.1f}%"] for k, v in sorted(ph["counts"].items())]))
        excl = ph["excluded_pct"]
        if excl >= 1.0:
            add(f"\n{excl:.1f}% of raw episodes fall outside the active window and are "
                "excluded from the headline numbers. A screener that skips this step "
                "over-reports opportunities by that margin.\n")
        else:
            add(f"\nOnly {excl:.2f}% of raw episodes fall outside the active window, so "
                "on this tape the phase filter removes almost nothing. That is a "
                "property of the recording, not evidence the filter is unnecessary: "
                "the post-close window here is short and the quote behaviour in it is "
                "simulated. The comparable Polymarket study found the majority of raw "
                "signals were post-game artefacts, so this is one of the results most "
                "in need of a real tape before it means anything.\n")

    macro = m.get("macro_events", {})
    if macro.get("shocks"):
        add("### Macro-event study (E11)\n")
        add("Episodes starting within {} ms of an information shock, against "
            "every other moment.\n".format(macro["window_ms"]))
        add(_table(["Window", "Episodes", "Actionable", "Share of time"],
                   [["post-shock", macro["episodes_in_window"], macro["actionable_in_window"],
                     _pct(macro["time_share_in_window"])],
                    ["baseline", macro["episodes_outside"], macro["actionable_outside"],
                     _pct(1 - macro["time_share_in_window"])]]))
        add("\nTime-normalised rate ratio: **{}x**. ".format(_fmt(macro["rate_ratio"], 2))
            + macro["interpretation"] + "\n")

    adv = m.get("adverse_selection", {})
    if adv.get("rows"):
        add("### Legging adverse selection (E12)\n")
        add("The winner's curse prediction: the leg that fails to fill is precisely "
            "the one that just moved against the basket.\n")
        add(_table(["delta (ms)", "n full", "n partial", "Mean net (full fill)",
                    "Mean net (partial fill)", "Gap"],
                   [[r["latency_ms"], r["n_full"], r["n_partial"],
                     f"${_fmt(r['mean_full'])}", f"${_fmt(r['mean_partial'])}",
                     f"${_fmt(r['gap'])}"] for r in adv["rows"]]))
        add("")

    pol = m.get("policies", {})
    if pol.get("order_type"):
        add("### Execution policy (E6, E7)\n")
        add(f"All at delta = {pol['latency_ms']} ms.\n")
        add(_table(["Order type", "Net P&L", "Basket completion", "Leg fill", "Residual (contracts)"],
                   [[r["order_type"], f"${_fmt(r['net_pnl'], 2)}",
                     _pct(r["basket_completion_rate"]), _pct(r["leg_fill_rate"]),
                     _fmt(r["residual_contracts"], 1)] for r in pol["order_type"]]))
        add("")
        add(_table(["Leg ordering", "Net P&L", "Basket completion", "Residual"],
                   [[r["leg_order"], f"${_fmt(r['net_pnl'], 2)}",
                     _pct(r["basket_completion_rate"]), _fmt(r["residual_contracts"], 1)]
                    for r in pol["leg_order"]]))
        nets = {round(r["net_pnl"], 6) for r in pol["leg_order"]}
        if len(nets) == 1:
            add("\nThe three orderings are indistinguishable here. Sequential legs do "
                "meet later books -- that is asserted in the test suite -- so this says "
                "the inter-leg gap is small relative to how fast these books move, not "
                "that ordering cannot matter. Widen `inter_leg_ms` or record a faster "
                "market to give the comparison something to find.\n")
        else:
            add("")
        add(_table(["Residual policy", "Net (worst case)", "Net (marked to settlement)", "Unwind cost"],
                   [[r["residual_policy"], f"${_fmt(r['net_pnl'], 2)}",
                     f"${_fmt(r.get('settled_pnl'), 2)}", f"${_fmt(r['unwind_cost'], 2)}"]
                    for r in pol["residual_policy"]]))
        add("")

    # -- capital ----------------------------------------------------------
    cap = m["capital"]
    add("\n## Capital\n")
    add("These positions are cash-collateralised and there is no settlement fee, so "
        "holding to expiry is free and the correct metric is return per dollar-*day*, "
        "not return per trade.\n")
    add(_table(["Quantity", "Value"], [
        ["Peak capital if every opportunity were taken", f"${_fmt(cap['capital_deployed'], 2)}"],
        ["Net P&L", f"${_fmt(cap['net_pnl'], 2)}"],
        ["Return on capital", f"{_fmt(cap['return_on_capital_bps'], 1)} bps"],
        ["Return per dollar-day", f"{_fmt(cap['return_per_dollar_day_bps'], 1)} bps"],
        [f"Baskets fundable on a ${cap['budget_dollars']:,.0f} budget",
         f"{cap['baskets_fundable']:,} of {cap['baskets_total']:,}"],
        ["Net P&L within that budget", f"${_fmt(cap['net_within_budget'], 2)}"],
    ]))
    add(f"\n*{cap['note']}*\n")

    # -- validation -------------------------------------------------------
    add("\n## Validation\n")
    add("These checks decide whether anything above is worth reading.\n")
    settle = val["settlement"]
    add(_table(["Check", "Result"], [
        ["Invariant N0 (no crossed book) across the tape",
         f"{val['n0_violations']} violation(s)"],
        ["Sequence gaps detected and resynced", f"{rep.get('sequence_gaps', 0):,}"],
        ["Snapshot diff vs locally maintained book (unexplained)",
         f"{rep.get('snapshot_mismatches_unexplained', 0)} of {rep.get('snapshot_checks', 0)} checks"],
        ["Snapshot diff explained by a known gap",
         f"{rep.get('snapshot_mismatches_after_gap', 0)}"],
        ["Closed forms vs LP oracle",
         f"{val['lp_checks']:,} checks, {len(val['lp_disagreements'])} disagreement(s), "
         f"max gap ${_fmt(val['lp_max_gap_dollars'], 6)}"],
        ["Baskets joined to actual settlement",
         f"{settle.get('checked', 0):,}"],
        ["**Baskets settling below their guaranteed floor**",
         f"**{settle.get('floor_violations', 0)}**"],
        ["Book untrusted (sequence gap unresolved)", f"{rep.get('untrusted_ms', 0):,} ms"],
    ]))
    add("")
    add("The LP gap is bounded by fee rounding: the LP prices fees linearly while the "
        "closed forms apply the published per-order ceiling, so the LP is expected to "
        "sit marginally above. A closed form *exceeding* the LP would be a bug and is "
        "asserted against.\n")
    if val["lp_disagreements"]:
        add("Disagreements found:\n")
        for d in val["lp_disagreements"][:10]:
            add(f"- {d}")
        add("")

    # -- engineering ------------------------------------------------------
    add("\n## Engineering\n")
    add(_table(["Metric", "Value"], [
        ["Records replayed", f"{eng['records']:,}"],
        ["Deltas applied", f"{eng['deltas']:,}"],
        ["Replay throughput", f"{eng['throughput_records_per_s']:,.0f} records/s"],
        ["Wall time", f"{eng['wall_seconds']:.1f} s"],
    ] + [
        [f"{k[:-3]} latency p50 / p99", f"{v['p50']:,.1f} us / {v['p99']:,.1f} us"]
        for k, v in eng.items() if k.endswith("_us") and isinstance(v, dict)
    ]))
    add("\nThe screen is O(1) per message: each event keeps an incrementally "
        "maintained sum of best bids, so a delta that moves one leg updates the "
        "sufficient statistic with a single integer add. **Detection cost does not "
        "grow with the number of legs.** Exact sizing -- the expensive stage -- runs "
        "only on the survivors of that screen.\n")

    # -- hypotheses -------------------------------------------------------
    add("\n## Hypotheses, marked\n")
    add("Stated in advance, and reported here whether or not they held.\n")
    add(_table(["ID", "Hypothesis", "Verdict", "Evidence"],
               [[h, c, v, e] for h, c, v, e in hypothesis_verdicts(m)]))
    add("")

    # -- figures ----------------------------------------------------------
    if figures:
        add("\n## Figures\n")
        for p in figures:
            name = Path(p).stem.replace("_", " ")
            add(f"![{name}]({Path(p).name})\n")

    # -- limitations ------------------------------------------------------
    add("\n## Limitations\n")
    lims = [
        "**Self-recorded data only.** Kalshi serves no order-book history, so the "
        "study covers only the recorded window and cannot claim anything about other "
        "periods, regimes or seasons.",
        "**Simulated, not realised, execution.** Fills are modelled against the "
        "recorded book. Real fills face queue dynamics, competing takers and "
        "exchange-side behaviour we cannot observe.",
        "**No own-impact modelling.** Our hypothetical orders do not perturb the "
        "tape. Acceptable at small size; false at scale.",
        "**Level-aggregated feed.** No per-order visibility, so cancellation dynamics "
        "and true queue position are approximated.",
        "**Latency is injected, not experienced.** The sweep is a controlled "
        "counterfactual, not an end-to-end measurement of our own stack.",
        "**Fee-schedule drift.** Multipliers vary by series and event and the schedule "
        "is versioned; results are conditional on the configuration in force.",
        "**Exhaustiveness is inferred, not certified.** The prover reads strike "
        "metadata; `functional`/`custom`/`structured` strikes are excluded rather "
        "than guessed at, and curated allowlist entries are human assertions.",
        "**Collateral-return accounting is assumed, not verified.** Return-on-capital "
        "figures assume no capital relief.",
    ]
    if m["quality"]["n"] < 30:
        lims.insert(1, f"**Small sample.** Only {m['quality']['n']} baskets at the "
                       "baseline rung, so the statistics here are descriptive rather "
                       "than inferential. Confidence intervals are omitted where n is "
                       "too small to support them.")
    if synthetic:
        lims.insert(0, "**The tape is synthetic.** The dislocation rate, depth and "
                       "reaction-lag distribution are parameters we chose. Nothing "
                       "here measures how much arbitrage exists on Kalshi.")
    for i, l in enumerate(lims, 1):
        add(f"{i}. {l}")
    add("")
    add("Deliberately **not** reported: a Sharpe ratio. "
        + m["quality"]["sharpe_note"] + "\n")
    add("Not obtainable from this data at all:\n")
    for n in m["not_obtainable"]:
        add(f"- {n}")
    add("")

    # -- CV bullets -------------------------------------------------------
    add("\n## CV bullets\n")
    # A live tape is necessary but not sufficient. The plan asks for two weeks
    # spanning a scheduled macro release, because a claim like "X% of edge
    # survives" needs enough episodes to mean anything. Quoting it from a short
    # pilot would not be fabrication, but it would be the same failure the
    # discipline exists to prevent: a number more confident than its evidence.
    MIN_HOURS_FOR_CLAIMS = 24.0
    MIN_EPISODES_FOR_CLAIMS = 30
    too_short = hours < MIN_HOURS_FOR_CLAIMS
    too_few = opp["episodes_actionable"] < MIN_EPISODES_FOR_CLAIMS
    if not synthetic and (too_short or too_few):
        reasons = []
        if too_short:
            reasons.append(f"the tape covers {hours:.1f} h, under the "
                           f"{MIN_HOURS_FOR_CLAIMS:.0f} h minimum")
        if too_few:
            reasons.append(f"only {opp['episodes_actionable']} actionable episodes, "
                           f"under {MIN_EPISODES_FOR_CLAIMS}")
        add(f"*Withheld: {'; '.join(reasons)}. The recording is real, so these are "
            "computable -- but a headline percentage from a pilot this size would "
            "claim more than the data supports. Record longer and regenerate.*\n")
    elif synthetic:
        add("*Withheld. These templates must be filled from a live recording; "
            "populating them from synthetic data would be fabrication.*\n")
        add("```\n"
            "Kalshi Intra-Event Arbitrage Engine -- recorded ___M full-depth order-book\n"
            "messages across ___ Kalshi markets (no public L2 dataset exists),\n"
            "reconstructed books from an incremental delta feed with sequence-gap\n"
            "recovery, and detected multi-outcome no-arbitrage violations in O(1) per\n"
            "message via an incrementally maintained event-level sufficient statistic.\n"
            "```\n")
    else:
        add(f"- Built an event-driven prediction-market arbitrage engine in Python: "
            f"recorded {rep.get('records', 0):,} full-depth order-book messages, "
            f"reconstructed books from an incremental delta feed with sequence-gap "
            f"recovery ({rep.get('sequence_gaps', 0)} gaps, "
            f"{rep.get('snapshot_mismatches_unexplained', 0)} unexplained snapshot "
            f"mismatches), and detected multi-outcome no-arbitrage violations in "
            f"**O(1) per message** (screen p50 "
            f"{eng.get('screen_us', {}).get('p50', float('nan')):.1f} us).")
        add(f"- Quantified arbitrage decay under execution latency: of "
            f"{f['raw_signals']:,} theoretical signals, {f['survival_pct']:.2f}% "
            f"survived exact Kalshi fees and order-book depth, establishing a "
            f"break-even execution latency of {_fmt(be, 1)} ms.")
        add(f"- Modelled non-atomic multi-leg execution including partial fills, "
            f"per-leg adverse selection and residual unwind cost; validated every "
            f"'risk-free' basket against actual settlement outcomes "
            f"({settle.get('checked', 0):,} checked, "
            f"{settle.get('floor_violations', 0)} floor violations).")
        add("- Formulated multi-outcome no-arbitrage detection as a linear program "
            f"over the outcome partition induced by contract strikes, and validated "
            f"the O(1) closed-form detector against the LP optimum across "
            f"{val['lp_checks']:,} evaluations.")
    add("")
    return "\n".join(L)


def write_report(m: dict, out_path: str | Path, figures: Sequence[Path] | None = None) -> Path:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_report(m, figures), encoding="utf-8")
    return p
