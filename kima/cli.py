"""Command-line interface.

    python -m kima simulate   # write a synthetic tape (no credentials needed)
    python -m kima record     # capture a live tape from Kalshi
    python -m kima universe   # resolve events and print exhaustiveness certificates
    python -m kima backtest   # replay, detect, execute, sweep latency
    python -m kima validate   # the credibility checks, including the null test
    python -m kima report     # metrics + figures + written report
    python -m kima all        # simulate -> backtest -> validate -> report

``python -m kima all`` is the reproduction command: one invocation regenerates
every number in the report from the raw tape.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .analytics.metrics import compute_metrics
from .analytics.plots import render_all
from .analytics.report import write_report
from .analytics.slides import write_slides
from .backtest import BacktestConfig, load_universe, run_backtest
from .detector import DetectorConfig
from .exhaustive import prove_exhaustive
from .replay import ReplayConfig
from .risk import RiskConfig
from .sizing import SizingConfig
from .tape import export_parquet, save_json, tape_header
from .validate import ValidationReport, shuffle_tape, summarise_for_determinism

DEFAULT_TAPE = "data/run/tape.jsonl.gz"


def _universe_path(tape: str) -> str:
    return str(tape).replace(".jsonl.gz", "") + ".universe.json"


def _shocks(tape: str) -> list[dict]:
    """The information-shock calendar, if the tape carries one (E11)."""
    try:
        from .tape import load_json
        return load_json(_universe_path(tape)).get("shocks", []) or []
    except Exception:
        return []


def _artefact_dir(args: argparse.Namespace) -> Path:
    """Where report artefacts go: beside the tape, unless told otherwise.

    A fixed default meant `report --tape data/live/...` wrote into `data/run`,
    overwriting a synthetic report with a live one and leaving a directory whose
    tape and metrics described different things. Deriving it from the tape keeps
    every artefact next to the data it came from.
    """
    if getattr(args, "out_dir", None):
        return Path(args.out_dir)
    return Path(args.tape).parent


def _banner(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


# --------------------------------------------------------------------------
def cmd_auth(args: argparse.Namespace) -> int:
    """Preflight the credentials before committing to a long recording.

    Checks the three things that go wrong, in the order they go wrong: the key
    file loads at all, a signature over ``timestamp + METHOD + path`` verifies
    locally, and the exchange actually accepts it. The third is the only one that
    proves anything, but the first two localise the failure when it does not --
    a signature computed over the query string returns a 401 that looks exactly
    like a bad key.
    """
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    from .recorder.auth import KalshiAuth
    from .recorder.rest import (DEMO_BASE, PROD_BASE, KalshiREST, RestConfig,
                               describe_skew, measure_clock_skew)

    _banner("Credential preflight")
    key_id = os.environ.get("KALSHI_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    print(f"  KALSHI_KEY_ID            {'set (' + key_id[:8] + '...)' if key_id else 'MISSING'}", flush=True)
    print(f"  KALSHI_PRIVATE_KEY_PATH  {key_path or 'MISSING'}", flush=True)
    if not key_id or not key_path:
        print("\n  Set both, then re-run. The recorder places no orders, but Kalshi "
              "requires connection-level auth even for public market data.",
              file=sys.stderr, flush=True)
        return 2
    if not Path(key_path).exists():
        print(f"\n  No such file: {key_path}", file=sys.stderr, flush=True)
        return 2

    try:
        auth = KalshiAuth.from_env()
    except Exception as exc:
        print(f"\n  Key failed to load -> {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        print("  PKCS#1 (BEGIN RSA PRIVATE KEY) and PKCS#8 (BEGIN PRIVATE KEY) are "
              "both fine. If the key is encrypted, set KALSHI_PRIVATE_KEY_PASSWORD.",
              file=sys.stderr, flush=True)
        return 2
    assert auth is not None
    bits = auth.private_key.key_size
    print(f"  key loaded               RSA-{bits}", flush=True)

    path = "/trade-api/v2/exchange/status"
    headers = auth.sign("GET", path + "?ignored=1", timestamp_ms=1_700_000_000_000)
    message = f"1700000000000GET{path}".encode()
    try:
        auth.private_key.public_key().verify(
            base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        print("  local signature          verifies over timestamp+METHOD+path "
              "(query string correctly excluded)", flush=True)
    except Exception as exc:
        print(f"  local signature          FAILED -> {exc}", file=sys.stderr, flush=True)
        return 1

    if args.offline:
        print("\n  Skipping the live check (--offline). Only the exchange accepting "
              "the signature actually proves the setup works.")
        return 0

    async def probe() -> int:
        import httpx

        base = DEMO_BASE if args.env == "demo" else PROD_BASE
        print(f"  endpoint                 {base}", flush=True)

        proxies = {k: v for k, v in os.environ.items()
                   if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
        if proxies:
            print(f"  proxy env                {', '.join(sorted(proxies))}", flush=True)

        cfg = RestConfig(base_url=base)
        # Deliberately *not* raise_for_status: the whole point is to tell
        # "the server refused us" apart from "we never reached the server",
        # and an exception path collapses those into one message.
        # /exchange/status answers 200 to anyone, so it proves connectivity but
        # says nothing about credentials. /portfolio/balance requires auth, which
        # is the whole question -- a probe that cannot fail on a bad key is not a
        # probe.
        probe_path = "/portfolio/balance"
        signed = auth.sign("GET", httpx.URL(base).path.rstrip("/") + probe_path)
        async with httpx.AsyncClient(
            base_url=base,
            timeout=httpx.Timeout(cfg.timeout_s, connect=cfg.connect_timeout_s),
            headers={"User-Agent": cfg.user_agent,
                     "Accept-Encoding": cfg.accept_encoding,
                     "Accept": "application/json"},
        ) as client:
            # Retry transport errors here too. This probe deliberately bypasses
            # KalshiREST so it can read the raw status code, which means it also
            # bypassed that client's retry loop -- and TLS handshakes to this host
            # are intermittently slow enough that a single attempt reports
            # "unreachable" for a link that is merely flaky.
            resp = None
            last: Exception | None = None
            attempts = cfg.max_retries
            for attempt in range(attempts):
                try:
                    resp = await client.get(probe_path, headers=signed)
                    if attempt:
                        print(f"  connected                 on attempt "
                              f"{attempt + 1} of {attempts}", flush=True)
                    break
                except httpx.TransportError as exc:
                    last = exc
                    if attempt + 1 < attempts:
                        print(f"  attempt {attempt + 1}/{attempts}               "
                              f"{type(exc).__name__}, retrying", flush=True)
                        await asyncio.sleep(min(1.0, 0.25 * (attempt + 1)))
                        signed = auth.sign(
                            "GET", httpx.URL(base).path.rstrip("/") + probe_path)
            if resp is None:
                exc = last if last is not None else RuntimeError("unreachable")
                print(f"  live call                UNREACHABLE -> "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                for line in (
                    "",
                    "  This is a network failure, not an auth failure -- the request",
                    "  never got a reply, so the key was never judged.",
                    f"  All {attempts} attempts failed.",
                    "",
                    "  Run the layered diagnostic:  python tools/netcheck.py",
                    "  Its stage 7 repeats the exact client this command builds, three",
                    "  times each. If that shows the endpoint answering intermittently,",
                    "  this host is simply refusing a share of connection attempts, and",
                    "  the answer is to retry rather than to change configuration.",
                ):
                    print(line, file=sys.stderr, flush=True)
                return 1

        body = resp.text[:160].replace("\n", " ")
        if resp.status_code == 200:
            print(f"  live call                OK (HTTP 200) -> {body}", flush=True)
            print("\n  Credentials work. Next: "
                  "python -m kima --tape data/live/tape.jsonl.gz record --minutes 5")
            return 0

        print(f"  live call                HTTP {resp.status_code} -> {body}",
              file=sys.stderr, flush=True)
        if resp.status_code in (401, 403):
            # A timestamp rejection has exactly one cause, so measure it rather
            # than listing possibilities.
            if "timestamp" in body.lower():
                skew = await measure_clock_skew(base)
                print("", file=sys.stderr, flush=True)
                print(f"  {describe_skew(skew)}", file=sys.stderr, flush=True)
                if skew is not None and abs(skew) >= 2:
                    for line in (
                        "  The signature covers a timestamp, and this machine "
                        "disagrees with",
                        "  the exchange by that much, so every request is stale on "
                        "arrival.",
                        "  Fix the clock, not the key:",
                        "      w32tm /resync            (run as Administrator)",
                        "  or Settings > Time & Language > Set time automatically.",
                        "",
                        "  Do not record until this is fixed: receipt time is the "
                        "clock the",
                        "  replay orders on, so a skewed session lands out of "
                        "position against",
                        "  the rest of the tape.",
                    ):
                        print(line, file=sys.stderr, flush=True)
                return 1
            other = "demo" if args.env == "prod" else "prod"
            print(f"\n  The server replied, so connectivity is fine and the key was "
                  f"rejected.\n"
                  f"    - Wrong environment? Try --env {other}; demo and production "
                  f"keys are not interchangeable.\n"
                  f"    - Clock skew: the signature covers a timestamp. Check your "
                  f"system clock.\n"
                  f"    - Key ID and private key must be from the same key pair.",
                  file=sys.stderr, flush=True)
        return 1

    return asyncio.run(probe())


def cmd_simulate(args: argparse.Namespace) -> int:
    from .sim.synthetic import SimConfig, generate

    cfg = SimConfig(
        seed=args.seed,
        duration_ms=int(args.hours * 3_600_000),
        events_per_family=args.events_per_family,
    )
    _banner(f"Simulating {args.hours:g}h of synthetic order-book data (seed {args.seed})")
    print("NOTE: synthetic data measures the engine, not Kalshi.")
    stats = generate(args.tape, cfg)
    for k, v in stats.items():
        print(f"  {k:16s} {v}")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    from .recorder.auth import KalshiAuth
    from .recorder.rest import DEMO_BASE, PROD_BASE, KalshiREST, RestConfig
    from .recorder.ws import DEMO_WS, PROD_WS, RecorderConfig, record
    from .events import event_to_dict
    from .exhaustive import certificates

    auth = KalshiAuth.from_env()
    if auth is None:
        print("No credentials found. Set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH.",
              file=sys.stderr, flush=True)
        print("Connection-level auth is required even for public market data.",
              file=sys.stderr, flush=True)
        return 2

    demo = args.env == "demo"
    rest_cfg = RestConfig(base_url=DEMO_BASE if demo else PROD_BASE)
    rec_cfg = RecorderConfig(
        ws_url=DEMO_WS if demo else PROD_WS,
        source="kalshi-demo" if demo else "kalshi-live",
        dump_messages=args.dump,
    )

    async def main() -> int:
        from .recorder.rest import describe_skew, measure_clock_skew

        skew = await measure_clock_skew(rest_cfg.base_url)
        print(f"  {describe_skew(skew)}", flush=True)
        if skew is not None and abs(skew) > args.max_skew_s:
            for line in (
                "",
                "  Refusing to record. Receipt time is the clock the replay orders",
                "  on, so a session captured with a shifted clock sorts out of",
                "  position against the rest of an append-only tape, and its",
                "  feed-delay figures are wrong by the offset.",
                "  Fix with:  w32tm /resync   (as Administrator)",
                "  Override with --max-skew-s if you know what you are doing.",
            ):
                print(line, file=sys.stderr, flush=True)
            return 2

        async with KalshiREST(rest_cfg, auth) as rest:
            # Reuse the universe `discover` chose, if there is one. Silently
            # resolving a *different* set of events here was a trap: you pick a
            # universe deliberately, then record something else entirely.
            existing = Path(_universe_path(args.tape))
            if existing.exists() and not args.series and not args.refresh_universe:
                events, _payload = load_universe(existing)
                _banner(f"Using the universe already at {existing}")
                print(f"  {len(events)} events, {sum(e.n_legs for e in events)} markets. "
                      "Pass --refresh-universe to re-resolve, or --series to change it.",
                      flush=True)
                certs = certificates(events)
            else:
                _banner("Resolving universe")
                events = await rest.build_universe(
                    args.series or None, max_events=args.max_events
                )
                certs = certificates(events)
                for c in certs.values():
                    print(c.render())
            if not events:
                print("No mutually exclusive events found.", file=sys.stderr, flush=True)
                return 2
            save_json(
                {
                    "source": rec_cfg.source,
                    "events": [event_to_dict(e) for e in events],
                    "certificates": {k: c.to_dict() for k, c in certs.items()},
                },
                _universe_path(args.tape),
            )
            n = sum(e.n_legs for e in events)
            _banner(f"Recording {n} markets across {len(events)} events "
                    f"for {args.minutes} minutes")
            stats = await record(events, args.tape, rec_cfg, auth,
                                 max_seconds=args.minutes * 60)
            print(json.dumps(stats.to_dict(), indent=2))

            # Judge the universe on what it actually delivered. A market that
            # never updates contributes nothing but a subscription slot, and a
            # universe of them yields a tape too sparse to study -- better to
            # learn that in five minutes than after two weeks.
            by_mkt = stats.deltas_by_market
            total = sum(e.n_legs for e in events)
            silent = total - len(by_mkt)
            # Silence is not itself a problem: a 28-leg ladder has many
            # far-out-of-the-money buckets that never trade. What matters is
            # whether an event still has the two quoting legs an N1 basket needs.
            tradable_events = [
                ev for ev in events
                if sum(1 for m in ev.markets if by_mkt.get(m.ticker)) >= 2
            ]
            _banner("Universe liveness")
            print(f"  {len(by_mkt)} of {total} markets produced a book update; "
                  f"{silent} were silent", flush=True)
            print(f"  {len(tradable_events)} of {len(events)} events had 2+ quoting "
                  "legs (the minimum for a basket)", flush=True)
            if by_mkt:
                top = sorted(by_mkt.items(), key=lambda kv: -kv[1])[:8]
                for t, n in top:
                    print(f"    {n:6d}  {t}", flush=True)
            rate = stats.to_dict().get("deltas_per_market_hour")
            print(f"  ~{rate if rate is not None else 'n/a'} updates per active "
                  "market per hour", flush=True)
            if not tradable_events or rate is None or rate < 60:
                for line in (
                    "",
                    "  This universe is too quiet for a microstructure study.",
                    "  Arbitrage needs markets that move. Pick liquid series:",
                    "      discover --series KXBTCY KXETHY KXHIGHNY KXGDPYEAR",
                    "  Delete the existing tape and .universe.json first, or pass",
                    "  --series straight to record.",
                ):
                    print(line, flush=True)

            _banner("Refreshing settlement outcomes")
            updated = await rest.refresh_settlements(events)
            save_json(
                {
                    "source": rec_cfg.source,
                    "events": [event_to_dict(e) for e in events],
                    "certificates": {k: c.to_dict() for k, c in certs.items()},
                },
                _universe_path(args.tape),
            )
            print(f"  {updated} markets carry a settlement result")
        return 0

    return asyncio.run(main())


def cmd_discover(args: argparse.Namespace) -> int:
    """Resolve a real universe from the REST API and write its certificates.

    Kalshi's REST market-data endpoints answer without credentials -- only the
    WebSocket demands connection-level auth. So this runs before, and
    independently of, any auth or recording setup, which makes it the right place
    to choose a universe: exhaustiveness is a property of the contracts, and it
    decides whether Condition N2 is available at all.
    """
    from .events import event_to_dict
    from .exhaustive import certificates
    from .recorder.auth import KalshiAuth
    from .recorder.rest import DEMO_BASE, PROD_BASE, KalshiREST, RestConfig

    cfg = RestConfig(base_url=DEMO_BASE if args.env == "demo" else PROD_BASE)
    auth = KalshiAuth.from_env()          # optional here

    async def main() -> int:
        async with KalshiREST(cfg, auth) as rest:
            _banner(f"Resolving universe from {cfg.base_url}"
                    + ("" if auth else "  (no credentials -- market data is public)"))
            events = await rest.build_universe(
                args.series or None, max_events=args.max_events
            )
        if not events:
            print("No mutually exclusive events found.", file=sys.stderr, flush=True)
            return 2
        certs = certificates(events)
        exh = sum(1 for e in events if e.exhaustive)
        print(f"  {len(events)} events, {sum(e.n_legs for e in events)} markets, "
              f"{exh} provably exhaustive\n", flush=True)
        for ev in events:
            c = certs[ev.event_ticker]
            grid = ev.markets[0].grid
            print(f"  [{'EXH' if c.exhaustive else '---'}] {ev.event_ticker:30s} "
                  f"legs={ev.n_legs:3d}  tick={grid.min_step_cc:4d}cc  "
                  f"fee taker M={ev.fee_schedule.taker_multiplier}")
            print(f"        {c.reason[:120]}")
        save_json(
            {
                "source": "kalshi-rest-discovery",
                "events": [event_to_dict(e) for e in events],
                "certificates": {k: c.to_dict() for k, c in certs.items()},
            },
            _universe_path(args.tape),
        )
        print(f"\n  written: {_universe_path(args.tape)}")
        print("  N1 (overround) works on every event above -- it needs only the\n"
              "  mutually_exclusive flag. N2 (underround) is available only on the\n"
              "  EXH rows; the engine refuses it elsewhere.")
        return 0

    return asyncio.run(main())


def cmd_universe(args: argparse.Namespace) -> int:
    events, _ = load_universe(_universe_path(args.tape))
    _banner(f"{len(events)} events")
    for ev in events:
        cert = prove_exhaustive(ev)
        print(cert.render())
        print(f"  fee: taker M={ev.fee_schedule.taker_multiplier} "
              f"maker M={ev.fee_schedule.maker_multiplier} "
              f"rounding={ev.fee_schedule.rounding}")
        for m in ev.markets:
            print(f"    {m.ticker:28s} {m.strike_type or '-':18s} {m.region} "
                  f"result={m.result or '-'}")
        print()
    return 0


def cmd_settle(args: argparse.Namespace) -> int:
    """Refresh settlement outcomes into an existing universe file.

    ``record`` fetches results when it exits, but an event that settles days
    later is still unresolved at that point. Without this the ex-post check --
    every "risk-free" basket joined to what actually happened -- silently has
    nothing to join against, which is the one integrity test worth most.
    """
    from .events import event_to_dict
    from .exhaustive import certificates
    from .recorder.auth import KalshiAuth
    from .recorder.rest import DEMO_BASE, PROD_BASE, KalshiREST, RestConfig

    auth = KalshiAuth.from_env()
    if auth is None:
        print("No credentials found. Set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH.",
              file=sys.stderr, flush=True)
        return 2

    path = _universe_path(args.tape)
    events, payload = load_universe(path)
    before = sum(1 for e in events for m in e.markets if m.result)

    async def main() -> int:
        cfg = RestConfig(base_url=DEMO_BASE if args.env == "demo" else PROD_BASE)
        async with KalshiREST(cfg, auth) as rest:
            _banner(f"Refreshing settlement for {len(events)} events")
            await rest.refresh_settlements(events)
        after = sum(1 for e in events for m in e.markets if m.result)
        certs = certificates(events)
        payload["events"] = [event_to_dict(e) for e in events]
        payload["certificates"] = {k: c.to_dict() for k, c in certs.items()}
        save_json(payload, path)
        print(f"  settled markets: {before} -> {after} of "
              f"{sum(e.n_legs for e in events)}")
        unresolved = [e.event_ticker for e in events
                      if not all(m.result for m in e.markets)]
        if unresolved:
            print(f"  still unresolved ({len(unresolved)}): "
                  f"{', '.join(unresolved[:5])}")
            print("  re-run once they settle; the ex-post floor check skips them.")
        return 0

    return asyncio.run(main())


def _config(args: argparse.Namespace) -> BacktestConfig:
    cfg = BacktestConfig(
        replay=ReplayConfig(strict_n0=not args.lenient,
                            stale_leg_ms=int(args.stale_leg_s * 1000)),
        detector=DetectorConfig(sizing=SizingConfig(min_qty_cq=int(args.min_contracts * 100))),
        sizing=SizingConfig(min_qty_cq=int(args.min_contracts * 100)),
        risk=RiskConfig(
            max_total_capital_mu=int(args.capital * 1_000_000),
            max_capital_per_event_mu=int(args.capital * 1_000_000 // 5),
            accumulate_positions=True,
        ) if args.capital else RiskConfig(accumulate_positions=False),
        baseline_latency_ms=args.baseline_ms,
    )
    cfg.lp_check_every = args.lp_every
    return cfg


def cmd_backtest(args: argparse.Namespace) -> int:
    hdr = tape_header(args.tape)
    _banner(f"Backtest over {args.tape}")
    if hdr:
        print(f"  provenance: {hdr.source} -- {hdr.note}")
    res = run_backtest(args.tape, _universe_path(args.tape), _config(args), progress=True)
    m = compute_metrics(res, args.baseline_ms, _shocks(args.tape))
    out_path = args.out or str(_artefact_dir(args) / "metrics.json")
    save_json(m, out_path)
    print(f"\n  raw signals      {res.funnel['raw_signals']:,}")
    print(f"  actionable       {res.funnel['risk_ok']:,}")
    print(f"  episodes         {len(res.episodes):,} "
          f"({len(res.actionable_episodes):,} actionable)")
    print(f"  break-even delta*  {m['latency']['break_even_ms']}")
    print(f"  metrics written  {out_path}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    _banner("Validation suite")
    cfg = _config(args)
    # The LP oracle is the expensive check: one linear program per basket. On a
    # multi-hour tape that is tens of thousands of solves for a claim a large
    # sample already settles, so honour --lp-every here rather than forcing 1.
    # Pass --lp-every 1 for an exhaustive pass on a short tape.
    rep = ValidationReport()

    res = run_backtest(args.tape, _universe_path(args.tape), cfg)
    rep.add("n0_invariant", res.replay["n0_violations"] == 0,
            f"{res.replay['n0_violations']} crossed-book violations across "
            f"{res.replay['deltas']:,} deltas")
    rep.add("snapshot_diff", res.replay["snapshot_mismatches_unexplained"] == 0,
            f"{res.replay['snapshot_mismatches_unexplained']} unexplained of "
            f"{res.replay['snapshot_checks']} checks "
            f"({res.replay['snapshot_mismatches_after_gap']} explained by known gaps)")
    rep.add("lp_agreement", not res.lp_disagreements,
            f"{res.lp_checks:,} checks (1 in {cfg.lp_check_every}), "
            f"{len(res.lp_disagreements)} disagreements, "
            f"max gap ${res.lp_max_gap:.6f}")
    settle = res.settlement_check
    rep.add("settlement_floor", settle.get("floor_violations", 0) == 0,
            f"{settle.get('checked', 0):,} baskets joined to actual outcomes, "
            f"{settle.get('floor_violations', 0)} below their guaranteed floor")

    zero = next((s for s in res.exec_summaries
                 if s["latency_ms"] == 0 and s["order_type"] == "IOC"
                 and s["leg_order"] == "batch" and s["residual_policy"] == "unwind"), None)
    ok = zero is not None and abs(zero["edge_retention_vs_l1"] - 1.0) < 1e-9
    rep.add("l2_zero_equals_l1", ok,
            f"retention at delta=0 is {zero['edge_retention_vs_l1']:.9f}" if zero else "missing")

    _banner("Determinism (same tape, twice)")
    cfg2 = _config(args)
    cfg2.lp_check_every = 0
    a = run_backtest(args.tape, _universe_path(args.tape), cfg2)
    b = run_backtest(args.tape, _universe_path(args.tape), cfg2)
    rep.add("determinism", summarise_for_determinism(a) == summarise_for_determinism(b),
            "two runs produced identical funnels, episodes and P&L")

    _banner("Null test (scrambled tape)")
    shuffled = str(args.tape).replace(".jsonl.gz", "") + ".shuffled.jsonl.gz"
    info = shuffle_tape(args.tape, shuffled, seed=args.seed)
    try:
        null = run_backtest(shuffled, _universe_path(args.tape), cfg2)
    finally:
        # Scratch, not an artefact: it is a full-size copy of the tape and is
        # regenerable from the seed. Leaving it behind doubles the disk cost of
        # every validation run.
        Path(shuffled).unlink(missing_ok=True)
    real_n = a.funnel["raw_signals"]
    null_n = null.funnel["raw_signals"]
    ratio = (null_n / real_n) if real_n else float("inf")
    rep.add("null_test", real_n != null_n,
            f"raw signals {real_n:,} real vs {null_n:,} scrambled "
            f"(ratio {ratio:.2f}x across {info['markets_shifted']} shifted markets)")

    print()
    print(rep.render())
    out_path = args.out or str(_artefact_dir(args) / "validation.json")
    save_json(rep.to_dict(), out_path)
    print(f"  written: {out_path}", flush=True)
    return 0 if rep.all_passed else 1


def cmd_report(args: argparse.Namespace) -> int:
    _banner("Report")
    res = run_backtest(args.tape, _universe_path(args.tape), _config(args), progress=True)
    m = compute_metrics(res, args.baseline_ms, _shocks(args.tape))
    out = _artefact_dir(args)
    figs = render_all(m, res, out / "figures")
    save_json(m, out / "metrics.json")
    path = write_report(m, out / "report.md", figs)
    slides = write_slides(m, out / "slides.md")
    print(f"  figures  {len(figs)} written to {out / 'figures'}")
    print(f"  slides   {slides}")
    print(f"  metrics  {out / 'metrics.json'}")
    print(f"  report   {path}")
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    if not Path(args.tape).exists() or args.regenerate:
        rc = cmd_simulate(args)
        if rc:
            return rc
    rc = cmd_report(args)
    if rc:
        return rc
    args.out = str(_artefact_dir(args) / "validation.json")
    return cmd_validate(args)


def cmd_export(args: argparse.Namespace) -> int:
    out_path = args.out or str(_artefact_dir(args) / "deltas.parquet")
    n = export_parquet(args.tape, out_path)
    print(f"  {n:,} delta rows -> {out_path}")
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kima",
        description="Kalshi Intra-Event Mispricing & Arbitrage Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--tape", default=DEFAULT_TAPE, help="path to the tape")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--baseline-ms", type=int, default=100,
                        help="latency at which policy comparisons are run")
        sp.add_argument("--min-contracts", type=float, default=1.0,
                        help="depth filter: discard baskets below this size")
        sp.add_argument("--lp-every", type=int, default=25,
                        help="cross-check the closed forms against the LP every N "
                             "baskets. 1 checks every basket (slow on a long tape)")
        sp.add_argument("--stale-leg-s", type=float, default=3600.0,
                        help="reject an event if any leg has been silent this "
                             "long. Generous by default: a resting book does not "
                             "expire because it is quiet")
        sp.add_argument("--lenient", action="store_true",
                        help="do not halt on an N0 violation (diagnostic only)")
        sp.add_argument("--seed", type=int, default=20260826)
        sp.add_argument("--capital", type=float, default=0.0,
                        help="hard cap on deployed capital in dollars. Default 0 "
                             "means uncapped: capital never releases within a short "
                             "tape, so a cap would truncate the latency experiment "
                             "rather than inform it. The capital study reports the "
                             "budget constraint separately.")

    s = sub.add_parser("simulate", help="write a synthetic tape")
    s.add_argument("--hours", type=float, default=6.0)
    s.add_argument("--events-per-family", type=int, default=1)
    s.add_argument("--seed", type=int, default=20260826)
    s.set_defaults(func=cmd_simulate)

    s = sub.add_parser("record", help="capture a live tape from Kalshi")
    s.add_argument("--minutes", type=float, default=60.0)
    # Production by default. This command only ever reads market data -- it
    # places no orders -- and the demo environment carries synthetic liquidity,
    # so a demo tape would be useless for a microstructure study.
    s.add_argument("--env", choices=("prod", "demo"), default="prod",
                   help="which Kalshi environment to record from. Default prod: "
                        "the recorder is read-only, and demo books are not real "
                        "liquidity. Use demo only to rehearse the auth path.")
    s.add_argument("--series", nargs="*", default=[])
    s.add_argument("--max-events", type=int, default=8)
    s.add_argument("--max-skew-s", type=float, default=5.0,
                   help="refuse to record if the local clock differs from the "
                        "exchange by more than this many seconds")
    s.add_argument("--refresh-universe", action="store_true",
                   help="re-resolve the universe from the API instead of reusing "
                        "the one already saved beside the tape")
    s.add_argument("--dump", type=int, default=0, metavar="N",
                   help="print the first N raw WebSocket messages, to inspect the "
                        "wire schema when fields are not decoding as expected")
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("discover",
                       help="resolve a real universe from the REST API (no auth needed)")
    s.add_argument("--env", choices=("prod", "demo"), default="prod")
    s.add_argument("--series", nargs="*", default=[])
    s.add_argument("--max-events", type=int, default=12)
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("universe", help="print the universe and its certificates")
    s.set_defaults(func=cmd_universe)

    s = sub.add_parser("auth", help="preflight your Kalshi credentials")
    s.add_argument("--env", choices=("prod", "demo"), default="prod")
    s.add_argument("--offline", action="store_true",
                   help="skip the live call and only check the key and signature")
    s.set_defaults(func=cmd_auth)

    s = sub.add_parser("settle", help="refresh settlement outcomes for a recorded tape")
    s.add_argument("--env", choices=("prod", "demo"), default="prod")
    s.set_defaults(func=cmd_settle)

    s = sub.add_parser("backtest", help="replay, detect, execute, sweep latency")
    common(s)
    s.add_argument("--out", default=None,
                   help="metrics output path; defaults to metrics.json beside the tape")
    s.set_defaults(func=cmd_backtest)

    s = sub.add_parser("validate", help="run the credibility checks")
    common(s)
    s.add_argument("--out", default=None,
                   help="validation output path; defaults to beside the tape")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("report", help="metrics, figures and the written report")
    common(s)
    s.add_argument("--out-dir", default=None,
                   help="where to write report artefacts. Defaults to the "
                        "directory holding the tape, so they cannot drift apart")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("all", help="simulate -> backtest -> validate -> report")
    common(s)
    s.add_argument("--hours", type=float, default=6.0)
    s.add_argument("--events-per-family", type=int, default=1)
    s.add_argument("--out-dir", default=None,
                   help="where to write report artefacts. Defaults to the "
                        "directory holding the tape, so they cannot drift apart")
    s.add_argument("--regenerate", action="store_true")
    s.set_defaults(func=cmd_all)

    s = sub.add_parser("export", help="normalise a tape to parquet")
    s.add_argument("--out", default=None,
                   help="parquet output path; defaults to deltas.parquet beside the tape")
    s.set_defaults(func=cmd_export)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
