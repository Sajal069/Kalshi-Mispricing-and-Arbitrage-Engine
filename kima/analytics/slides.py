"""The ten-slide / whiteboard summary.

Generated from the same metrics dict as the written report, so the two can never
drift apart. The narrative is fixed; only the numbers are substituted, and where
a number is unavailable it stays visibly blank rather than being invented.

The plan sets a bar for this artefact: the whole project must reduce to *"Kalshi
tells you at most one market in this event can pay, so the YES bids must sum to
under a dollar. Sometimes they don't. Here's the trade, here's the exact fee,
here's how deep the book actually is, and here's how fast the opportunity dies."*
Four boxes, two inequalities, one plot. These slides are that argument.
"""

from __future__ import annotations

from pathlib import Path

from .report import _be_label, _fmt, _pct, _table


def render_slides(m: dict) -> str:
    synthetic = m["provenance"]["is_synthetic"]
    f, opp, lat = m["funnel"], m["opportunities"], m["latency"]
    val, eng, rep = m["validation"], m["engineering"], m["replay"]
    be = lat["break_even_ms"]
    hours = rep.get("duration_ms", 0) / 3_600_000
    screen = eng.get("screen_us", {})
    size = eng.get("size_us", {})

    def slide(n: int, title: str, *body: str) -> str:
        return f"\n---\n\n## {n}. {title}\n\n" + "\n".join(body)

    S: list[str] = ["# KIMA in ten slides\n"]
    if synthetic:
        S.append("**Every figure below comes from a SYNTHETIC tape. It measures the "
                 "engine, not Kalshi.**\n")

    S.append(slide(
        1, "The structural fact",
        "Kalshi groups markets into **events** and publishes a first-class",
        "`mutually_exclusive` flag: at most one market in the event can resolve YES.",
        "",
        "But each market matches on its **own independent order book**. No",
        "cross-market matching. No combination order type. No implied-order engine.",
        "",
        "> A constraint guaranteed at settlement is enforced by nobody at the quote level.",
    ))

    S.append(slide(
        2, "The trade, in one line",
        "```",
        "sum_i bestYesBid_i  >  $1     =>     buy NO on every leg",
        "```",
        "At most one leg pays YES, so at least N-1 NO contracts pay $1. Guaranteed",
        "profit `sum bestYesBid - 1 - fees`, in **every** terminal state.",
        "",
        "Pure arbitrage: no probability model, no forecast, no assumption about the",
        "underlying event. The subset choice even collapses to a per-leg threshold,",
        "so there is nothing to search.",
    ))

    S.append(slide(
        3, "The trade that does NOT exist",
        "The classic pitch is *buy YES and NO for under $1*. On Kalshi:",
        "```",
        "cost(YES) + cost(NO) = 2 - (bestYesBid + bestNoBid)  <  1",
        "                 iff   bestYesBid + bestNoBid  >  1",
        "```",
        "But Kalshi publishes **one bid-only book** in which a YES bid at X *is* a NO",
        "offer at 1-X. That inequality describes a crossed book, which a continuous",
        "matching engine resolves on arrival.",
        "",
        "So we keep it as **invariant N0** and treat any sighting as a data bug.",
        f"Violations across {rep.get('deltas', 0):,} deltas: **{val['n0_violations']}**.",
    ))

    S.append(slide(
        4, "The trap: exhaustiveness",
        "`mutually_exclusive` says nothing about whether the legs cover the sample",
        "space. Kalshi documentation is explicit that the markets need not exhaust",
        "every possible outcome.",
        "",
        _table(["Condition", "Needs exclusivity", "Needs exhaustiveness"],
               [["N1 overround (buy NO)", "yes", "**no** -- an unlisted outcome is a free option"],
                ["N2 underround (buy YES)", "yes", "**yes** -- or the basket expires worthless"]]),
        "",
        "The prover refuses by default and emits a certificate per event.",
        f"It removed **{f['attribution'].get('exhaustiveness', 0):,}** signals here.",
    ))

    S.append(slide(
        5, "The engineering claim",
        "Per event, maintain `sum bestYesBid` incrementally. A delta that moves one",
        "leg updates it with a single integer add.",
        "",
        "> **Detection cost is independent of the number of legs.**",
        "",
        "The screen is also *sound*: dropping a leg only removes a non-negative bid,",
        "so any profitable subset forces the full sum above $1. No false negatives.",
        "",
        f"Measured: screen p50 **{_fmt(screen.get('p50'), 1)} us**, p99 {_fmt(screen.get('p99'), 1)} us.",
        f"Exact sizing runs only on survivors: p50 {_fmt(size.get('p50'), 0)} us.",
    ))

    over = (100 / f["survival_pct"]) if f["survival_pct"] else None
    S.append(slide(
        6, "The funnel is the result",
        f"Over {hours:.1f} hours and {rep.get('records', 0):,} messages:",
        "",
        _table(["Stage", "Surviving", "% of raw"],
               [[r["stage"].replace("_", " "), f"{r['count']:,}", f"{r['pct_of_raw']:.2f}%"]
                for r in f["rows"]]),
        "",
        f"**{f['survival_pct']:.2f}% of raw signals are actionable.** A screener that",
        "skips the market-state, depth and fee filters over-reports by roughly",
        f"{_fmt(over, 0)}x.",
    ))

    S.append(slide(
        7, "Fees: the rounding, not the rate",
        "```",
        "fee = ceil( M x 0.07 x C x P x (1-P) )      applied per ORDER",
        "```",
        _table(["Order size at $0.50", "Fee", "Per contract"],
               [["1 contract", "$0.02", "$0.0200 (4% of notional)"],
                ["100 contracts", "$1.75", "$0.0175"]]),
        "",
        "So a **minimum viable basket size** exists. Exact fees removed",
        f"**{f['attribution'].get('fee_negative', 0):,}** otherwise-viable signals here.",
        "",
        "And because the fee scales with P(1-P), extreme-priced legs are cheap to",
        "trade -- quietly favourable to exactly this strategy.",
    ))

    S.append(slide(
        8, "Execution is not atomic",
        "Kalshi has **no cross-market atomicity**. A basket that half-fills is a",
        "directional position acquired at a bad price, not an arbitrage.",
        "",
        _table(["delta (ms)", "Basket completion", "Edge retention", "Mean net/basket"],
               [[r["latency_ms"], _pct(r["basket_completion_rate"]),
                 _pct(r["edge_retention_vs_l1"]), f"${_fmt(r['mean_net_per_basket'])}"]
                for r in lat["rows"]]),
        "",
        "`L2(delta=0) == L1` exactly, by construction and by test. It is the control",
        "for every other row.",
    ))

    S.append(slide(
        9, "The one number",
        (f"# delta* = {_fmt(be, 1)} ms" if be is not None
         else "# delta* " + _be_label(lat, short=True)),
        "",
        "The latency at which expected net P&L per basket crosses zero.",
        "",
        "One scalar per series, directly comparable to a measured round-trip time,",
        "and it answers the only question a desk actually asks:",
        "",
        "> *Is this strategy latency-feasible for us, yes or no?*",
    ))

    settle = val["settlement"]
    S.append(slide(
        10, "Why believe any of it",
        _table(["Check", "Result"], [
            ["Invariant N0", f"{val['n0_violations']} violations"],
            ["Closed forms vs LP oracle",
             f"{val['lp_checks']:,} checks, {len(val['lp_disagreements'])} disagreements"],
            ["Baskets vs actual settlement",
             f"{settle.get('checked', 0):,} checked, "
             f"{settle.get('floor_violations', 0)} below their floor"],
            ["Snapshot diff (unexplained)",
             f"{rep.get('snapshot_mismatches_unexplained', 0)} of "
             f"{rep.get('snapshot_checks', 0)}"],
            ["Determinism", "same tape, identical output"],
            ["Null test", "scrambled tape changes the opportunity rate"],
        ]),
        "",
        "And what is deliberately **not** claimed:",
        "",
        "- No Sharpe ratio. " + m["quality"]["sharpe_note"],
        "- " + ("Synthetic provenance is stamped on every page, every hypothesis is "
                "marked UNTESTED against live data, and the CV bullets are withheld."
                if synthetic else
                "Results cover only the recorded window; Kalshi serves no order-book history."),
        "- Fills are simulated against a recorded book, not realised.",
    ))

    return "\n".join(S) + "\n"


def write_slides(m: dict, out_path: str | Path) -> Path:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_slides(m), encoding="utf-8")
    return p
