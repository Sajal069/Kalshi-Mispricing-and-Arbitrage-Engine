"""Backtest orchestration: one deterministic pass, every experiment.

The whole latency sweep runs in a **single** replay. That is possible because
the simulator does not model our own market impact, so each latency rung is an
independent counterfactual over the same tape and can carry its own pending
queue and accounting. One pass, ten rungs, plus the policy variants.

The rejection funnel is the headline output and is built here. Filters are
applied in a fixed order so the table reads as a narrowing pipeline, and each
stage is attributed to exactly one friction:

    raw L0 signals
      -> book trusted (no unresolved sequence gap)
      -> market state active (not pre-close, not post-close)
      -> not stale
      -> exhaustiveness proven (N2 only)
      -> survives real ladder depth
      -> survives exact fees
      -> survives risk caps
      = actionable

The gap between the first line and the last is the result.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .detector import CONDITIONS, DetectorConfig, Episode, EpisodeGrouper, EventDetector, Signal
from .events import Event, event_from_dict
from .execution import LATENCY_RUNGS_MS, ExecConfig, ExecutedBasket, ExecutionSimulator
from .exhaustive import prove_exhaustive
from .lp import LP_TOL, cross_check
from .profiling import UniverseProfiler
from .replay import NullListener, ReplayConfig, ReplayEngine
from .risk import RiskConfig, RiskEngine, book_state_hash
from .sizing import ZERO_FEE, Basket, SizingConfig, edge_curve, min_viable_size
from .tape import load_json, tape_header
from .units import CQ_PER_CONTRACT, MU_PER_CQ

FUNNEL_STAGES = (
    "raw_signals",
    "book_trusted",
    "market_state_active",
    "not_stale",
    "exhaustiveness_ok",
    "not_duplicate",
    "depth_ok",
    "fee_ok",
    "risk_ok",
)


@dataclass
class BacktestConfig:
    replay: ReplayConfig = field(default_factory=ReplayConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    #: Inventory accumulation is off by default. See RiskConfig.accumulate_positions:
    #: a cap that binds partway through the tape would bias every number after it.
    risk: RiskConfig = field(default_factory=lambda: RiskConfig(accumulate_positions=False))
    latency_rungs_ms: tuple[int, ...] = LATENCY_RUNGS_MS
    #: Latency at which the policy comparisons (E6/E7) are run.
    baseline_latency_ms: int = 100
    compare_policies: bool = True
    #: Cross-check the closed forms against the LP on 1 in N evaluated baskets.
    lp_check_every: int = 40
    #: Capture an edge-versus-size curve on 1 in N episodes (E4).
    edge_curve_every: int = 25
    measure_latency: bool = True
    max_latency_samples: int = 20_000
    #: Experiment E1. Sampling keeps the cost well below the detector's.
    profile_universe: bool = True
    profile_sample_every: int = 25


@dataclass
class BacktestResult:
    source: str = "unknown"
    tape_note: str = ""
    replay: dict = field(default_factory=dict)
    funnel: dict[str, int] = field(default_factory=dict)
    funnel_by_condition: dict[str, dict[str, int]] = field(default_factory=dict)
    episodes: list[Episode] = field(default_factory=list)
    executions: dict[str, list[ExecutedBasket]] = field(default_factory=dict)
    exec_summaries: list[dict] = field(default_factory=list)
    risk: dict = field(default_factory=dict)
    lp_checks: int = 0
    lp_disagreements: list[str] = field(default_factory=list)
    lp_max_gap: float = 0.0
    edge_curves: list[dict] = field(default_factory=list)
    stage_latency_ns: dict[str, list[int]] = field(default_factory=dict)
    settlement_check: dict = field(default_factory=dict)
    #: Experiment E1: spreads, depth and update rates per series.
    universe_profile: dict = field(default_factory=dict)
    wall_seconds: float = 0.0
    events: list[Event] = field(default_factory=list)

    @property
    def actionable_episodes(self) -> list[Episode]:
        return [e for e in self.episodes if e.actionable]


class Strategy(NullListener):
    """Detection, gating and submission, driven by the replay clock."""

    def __init__(
        self,
        events: Sequence[Event],
        engine: ReplayEngine,
        cfg: BacktestConfig,
        exec_sims: Sequence[ExecutionSimulator],
    ):
        self.cfg = cfg
        self.engine = engine
        self.exec_sims = list(exec_sims)
        self.detectors: dict[str, EventDetector] = {
            ev.event_ticker: EventDetector(ev, engine.books_for(ev), cfg.detector)
            for ev in events
        }
        self.events = {ev.event_ticker: ev for ev in events}
        self.grouper = EpisodeGrouper(cfg.detector.trust_ceiling_ms)
        self.risk = RiskEngine(cfg.risk)
        self.funnel: dict[str, int] = {k: 0 for k in FUNNEL_STAGES}
        self.funnel_by_condition: dict[str, dict[str, int]] = {
            c: {k: 0 for k in FUNNEL_STAGES} for c in CONDITIONS
        }
        self.lp_checks = 0
        self.lp_disagreements: list[str] = []
        self.lp_max_gap = 0.0
        self.edge_curves: list[dict] = []
        self.stage_latency_ns: dict[str, list[int]] = {"screen": [], "size": []}
        self._eval_counter = 0
        self._episode_counter = 0
        self._last_key: dict[tuple, tuple] = {}
        self._last_basket: dict[tuple, Basket | None] = {}
        self.stage2_skipped = 0

    # -- helpers -----------------------------------------------------------
    def _count(self, stage: str, condition: str) -> None:
        self.funnel[stage] += 1
        self.funnel_by_condition[condition][stage] += 1

    # -- replay hooks ------------------------------------------------------
    def on_time(self, engine: ReplayEngine, ts_ms: int) -> None:
        # Called once per message per simulator, so on a multi-million-message
        # tape this is tens of millions of calls. Skip the ones with nothing
        # queued rather than paying the call overhead.
        for sim in self.exec_sims:
            if sim.has_pending:
                sim.advance(ts_ms, engine.books)

    def on_tick(self, engine: ReplayEngine, ts_ms: int, ticker: str) -> None:
        event = engine.event_of(ticker)
        det = self.detectors.get(event.event_ticker)
        if det is None:
            return
        det.refresh(ticker)

        measure = self.cfg.measure_latency and len(self.stage_latency_ns["screen"]) < self.cfg.max_latency_samples
        if measure:
            t0 = time.perf_counter_ns()
            signals = det.screen(ts_ms)
            self.stage_latency_ns["screen"].append(time.perf_counter_ns() - t0)
        else:
            signals = det.screen(ts_ms)

        # Close any episode whose condition stopped firing on this tick.
        active = {(s.condition, s.pair) for s in signals}
        self.grouper.resolve_all_for(ts_ms, event.event_ticker, active)
        if not signals:
            return

        det.assert_consistency()
        for sig in signals:
            self._handle(engine, event, det, sig, ts_ms)

    def on_finish(self, engine: ReplayEngine, ts_ms: int) -> None:
        for sim in self.exec_sims:
            sim.flush(ts_ms + 10_000, engine.books)
        self.grouper.flush(ts_ms)

    # -- the funnel --------------------------------------------------------
    def _handle(
        self, engine: ReplayEngine, event: Event, det: EventDetector, sig: Signal, ts_ms: int
    ) -> None:
        cond = sig.condition
        self._count("raw_signals", cond)
        phase = engine.phase(event, ts_ms)
        l0 = det.l0_edge(cond)

        # Every raw signal is recorded as an episode, tagged with its phase, so
        # post-close phantom liquidity can be quantified rather than merely
        # excluded (experiment E10).
        self.grouper.observe(
            ts_ms, sig, None, l0, phase, event.series_ticker, event.n_legs
        )

        if not engine.is_trusted(event):
            self.risk.note_rejection("untrusted_book")
            return
        self._count("book_trusted", cond)

        if phase != "active":
            self.risk.note_rejection("market_state")
            return
        self._count("market_state_active", cond)

        if engine.is_stale(event, ts_ms):
            self.risk.note_rejection("stale_data")
            return
        self._count("not_stale", cond)

        if cond == "N2" and not event.exhaustive:
            self.risk.note_rejection("exhaustiveness")
            return
        self._count("exhaustiveness_ok", cond)

        # Duplicate suppression, applied *before* the expensive stage rather than
        # after it. Re-sizing an unchanged book on every inbound delta is the
        # dominant cost on a large tape, and the result is identical by
        # construction.
        dedupe_key = (event.event_ticker, cond, sig.pair)
        tob = det.top_of_book_key()
        if self._last_key.get(dedupe_key) == tob:
            self.stage2_skipped += 1
            cached = self._last_basket.get(dedupe_key)
            # The signal is real; we simply decline to act on it twice. Attribute
            # it to duplicate suppression so the funnel still balances.
            # Filtered here, so it does not count toward any later stage.
            # The episode still records the cached basket: the opportunity was
            # real and persisted, we simply decline to fire on it twice.
            self.risk.note_rejection("duplicate")
            if cached is not None:
                self.grouper.observe(ts_ms, sig, cached, l0, phase,
                                     event.series_ticker, event.n_legs)
            return
        self._last_key[dedupe_key] = tob
        self._count("not_duplicate", cond)

        measure = self.cfg.measure_latency and len(self.stage_latency_ns["size"]) < self.cfg.max_latency_samples
        if measure:
            t0 = time.perf_counter_ns()
            basket = self._size(det, sig)
            self.stage_latency_ns["size"].append(time.perf_counter_ns() - t0)
        else:
            basket = self._size(det, sig)
        self._last_basket[dedupe_key] = basket

        if basket is None:
            # Price the same basket fee-free to attribute the rejection: if it
            # survives without fees, fees killed it; if not, depth did. This runs
            # only on failures, which keeps the common path at one sizing pass.
            free = self._size(det, sig, schedule=ZERO_FEE)
            if free is None:
                self.risk.note_rejection("min_size")
                return
            self._count("depth_ok", cond)
            self._fee_rejected(cond)
            return
        self._count("depth_ok", cond)
        self._count("fee_ok", cond)

        self._maybe_cross_check(event, engine, basket)
        self._maybe_edge_curve(event, engine, sig, basket, ts_ms)

        reason = self.risk.check(basket, ts_ms)
        if reason is not None:
            return
        self._count("risk_ok", cond)

        # Book the capital. These positions are fully cash-collateralised and are
        # held to settlement, which for a tape this short never arrives -- so
        # capital accumulates and the caps genuinely bind. That is not an
        # artefact: capital intensity, not edge size, is the real economic
        # constraint on this strategy, and a backtest that lets the same dollar
        # fund unlimited simultaneous baskets is measuring a fantasy.
        self.risk.on_fill(
            basket.event_ticker,
            {l.ticker: l.qty_cq for l in basket.legs},
            basket.capital_mu,
        )

        self.grouper.observe(ts_ms, sig, basket, l0, phase, event.series_ticker, event.n_legs)
        state_hash = book_state_hash(basket)
        for sim in self.exec_sims:
            sim.submit(basket, event, ts_ms, l0_mu=l0, state_hash=state_hash)
            # A zero-latency order must match the very book state it was sized
            # against. Without this it would wait for the next inbound message
            # and silently become a positive-latency run, breaking the control.
            sim.advance(ts_ms, engine.books)

    def _fee_rejected(self, cond: str) -> None:
        self.risk.note_rejection("fee_negative")

    def _size(self, det: EventDetector, sig: Signal, schedule=None) -> Basket | None:
        from .sizing import size_nested_pair, size_no_basket, size_yes_basket

        cfg = self.cfg.sizing
        ev = det.event
        if sig.condition == "N1":
            return size_no_basket(ev, det.books, cfg, ts_ms=sig.ts_ms, schedule=schedule)
        if sig.condition == "N2":
            return size_yes_basket(ev, det.books, cfg, ts_ms=sig.ts_ms, schedule=schedule)
        if sig.condition == "N3" and sig.pair:
            inner, outer = sig.pair
            return size_nested_pair(ev, det.books, inner, outer, cfg, ts_ms=sig.ts_ms, schedule=schedule)
        return None

    # -- validation --------------------------------------------------------
    def _maybe_cross_check(self, event: Event, engine: ReplayEngine, basket: Basket) -> None:
        self._eval_counter += 1
        if self.cfg.lp_check_every <= 0 or self._eval_counter % self.cfg.lp_check_every:
            return
        self.lp_checks += 1
        ok, t_star, msg = cross_check(
            event,
            engine.books_for(event),
            basket.profit_mu,
            max_contracts=self.cfg.sizing.max_qty_cq / CQ_PER_CONTRACT,
        )
        gap = t_star - basket.profit_mu / 1e6
        self.lp_max_gap = max(self.lp_max_gap, abs(gap))
        if not ok:
            self.lp_disagreements.append(f"{event.event_ticker} {basket.condition}: {msg}")

    def _maybe_edge_curve(
        self, event: Event, engine: ReplayEngine, sig: Signal, basket: Basket, ts_ms: int
    ) -> None:
        self._episode_counter += 1
        if self.cfg.edge_curve_every <= 0 or self._episode_counter % self.cfg.edge_curve_every:
            return
        if sig.condition not in ("N1", "N2"):
            return
        curve = edge_curve(event, engine.books_for(event), sig.condition, self.cfg.sizing)
        if not curve:
            return
        mvs = min_viable_size(curve)
        self.edge_curves.append(
            {
                "event_ticker": event.event_ticker,
                "series": event.series_ticker,
                "condition": sig.condition,
                "ts_ms": ts_ms,
                "zero_fee": event.fee_schedule.is_zero_fee,
                "n_legs": event.n_legs,
                "points": [(q / CQ_PER_CONTRACT, p / 1e6) for q, p in curve],
                "min_viable_contracts": (mvs / CQ_PER_CONTRACT) if mvs else None,
                "best_profit": max(p for _, p in curve) / 1e6,
                "top_of_book_profit": curve[0][1] / 1e6,
            }
        )


# --------------------------------------------------------------------------
def build_exec_configs(cfg: BacktestConfig) -> list[ExecConfig]:
    """The latency ladder, plus the policy variants at the baseline rung."""
    # The ladder varies latency and nothing else. In particular the write budget
    # is off: modelled as a delay it would push a delta=0 basket past the instant
    # it was priced, and L2(0) must reproduce L1 exactly for the other rungs to
    # mean anything. Its cost is measured separately below.
    out = [
        ExecConfig(latency_ms=d, order_type="IOC", leg_order="batch",
                   residual_policy="unwind", enforce_rate_limit=False)
        for d in cfg.latency_rungs_ms
    ]
    if cfg.compare_policies:
        b = cfg.baseline_latency_ms
        out += [
            ExecConfig(latency_ms=b, order_type="FOK", leg_order="batch",
                       residual_policy="unwind", enforce_rate_limit=False),
            ExecConfig(latency_ms=b, order_type="IOC", leg_order="thinnest_first",
                       residual_policy="unwind", enforce_rate_limit=False),
            ExecConfig(latency_ms=b, order_type="IOC", leg_order="cheapest_first",
                       residual_policy="unwind", enforce_rate_limit=False),
            ExecConfig(latency_ms=b, order_type="IOC", leg_order="batch",
                       residual_policy="hold", enforce_rate_limit=False),
            # H11: does the write budget bind before the network does? Same rung,
            # same policy, only the rate limit differs.
            ExecConfig(latency_ms=b, order_type="IOC", leg_order="batch",
                       residual_policy="unwind", enforce_rate_limit=True),
        ]
    # Deduplicate labels while preserving order.
    seen: set[str] = set()
    uniq: list[ExecConfig] = []
    for c in out:
        if c.label not in seen:
            seen.add(c.label)
            uniq.append(c)
    return uniq


def load_universe(path: str | Path) -> tuple[list[Event], dict]:
    payload = load_json(path)
    events = [event_from_dict(d) for d in payload.get("events", [])]
    return events, payload


def verify_settlement(result: BacktestResult, events: Sequence[Event]) -> dict:
    """Ex-post integrity check against actual settlement outcomes.

    Two jobs, and they are different.

    The **integrity check**: for every basket the engine called risk-free, join
    to the real results and confirm the realised payoff of the *retained*
    position met the guaranteed floor. A single basket settling below its floor
    would mean the mutual-exclusivity assumption or the outcome partition was
    wrong -- which would be the most important finding in the study, so it is
    checked rather than assumed.

    The **hold-policy valuation**: a position held to settlement is marked to the
    actual outcome, not to its worst case. Worst-case marking is the right
    convention for deciding whether to trade; it is the wrong one for reporting
    what a hold-to-settlement desk would actually have earned.
    """
    results_by_ticker: dict[str, str] = {}
    for ev in events:
        for m in ev.markets:
            if m.result:
                results_by_ticker[m.ticker] = m.result
    if not results_by_ticker:
        return {"checked": 0, "note": "no settlement outcomes available"}

    checked = violations = 0
    worst_gap = 0
    offenders: list[str] = []
    for label, baskets in result.executions.items():
        for eb in baskets:
            if not eb.any_fill:
                continue
            realised = 0
            known = True
            for f in eb.fills:
                if f.kept_cq <= 0:
                    continue
                res = results_by_ticker.get(f.ticker)
                if res is None:
                    known = False
                    break
                pays = (res == "yes") if f.buy_side == 0 else (res == "no")
                if pays:
                    realised += f.kept_cq * MU_PER_CQ
            if not known:
                continue
            eb.settled_payoff_mu = realised
            eb.settled_net_mu = (
                realised - eb.cost_mu - eb.fee_mu + eb.unwind_proceeds_mu - eb.unwind_fee_mu
            )
            if eb.worst_payoff_mu <= 0:
                continue
            checked += 1
            if realised < eb.worst_payoff_mu:
                violations += 1
                worst_gap = max(worst_gap, eb.worst_payoff_mu - realised)
                if len(offenders) < 5:
                    offenders.append(f"{label} {eb.event_ticker} {eb.condition} @{eb.detect_ts_ms}")
    return {
        "checked": checked,
        "floor_violations": violations,
        "worst_shortfall": worst_gap / 1e6,
        "offenders": offenders,
    }


def run_backtest(
    tape_path: str | Path,
    universe_path: str | Path,
    cfg: BacktestConfig | None = None,
    *,
    progress: bool = False,
) -> BacktestResult:
    cfg = cfg or BacktestConfig()
    started = time.time()
    events, payload = load_universe(universe_path)
    # Re-prove exhaustiveness from metadata rather than trusting the stored flag:
    # the certificate is cheap and the failure mode it guards is not.
    for ev in events:
        stored = ev.exhaustive
        cert = prove_exhaustive(ev)
        if not cert.exhaustive and stored:
            # Only a curated allowlist entry can justify the difference.
            cert = prove_exhaustive(
                ev, curated_exhaustive=frozenset(payload.get("curated_exhaustive", [ev.series_ticker]))
                if payload.get("certificates", {}).get(ev.event_ticker, {}).get("method") == "curated"
                else None,
            )
        ev.exhaustive = cert.exhaustive
        ev.exhaustiveness_reason = cert.reason

    engine = ReplayEngine(events, cfg.replay)
    exec_cfgs = build_exec_configs(cfg)
    sims = [ExecutionSimulator(c) for c in exec_cfgs]
    strat = Strategy(events, engine, cfg, sims)
    profiler = UniverseProfiler(cfg.profile_sample_every) if cfg.profile_universe else None

    hdr = tape_header(tape_path)
    stats = engine.run(
        tape_path,
        [strat] + ([profiler] if profiler else []),
        progress=(lambda n: print(f"    {n:,} records", flush=True)) if progress else None,
    )

    res = BacktestResult(
        source=hdr.source if hdr else "unknown",
        tape_note=hdr.note if hdr else "",
        replay=stats.to_dict(),
        funnel=strat.funnel,
        funnel_by_condition=strat.funnel_by_condition,
        episodes=list(strat.grouper.closed),
        executions={s.cfg.label: s.results for s in sims},
        exec_summaries=[s.summary() for s in sims],
        risk=strat.risk.summary(),
        lp_checks=strat.lp_checks,
        lp_disagreements=strat.lp_disagreements,
        lp_max_gap=strat.lp_max_gap,
        edge_curves=strat.edge_curves,
        stage_latency_ns=strat.stage_latency_ns,
        universe_profile=profiler.summary() if profiler else {},
        wall_seconds=round(time.time() - started, 2),
        events=events,
    )
    res.settlement_check = verify_settlement(res, events)
    # Settled P&L is only meaningful once the join above has run.
    for summary, sim in zip(res.exec_summaries, sims):
        settled = [b.settled_net_mu for b in sim.results if b.settled_net_mu is not None]
        summary["settled_pnl"] = sum(settled) / 1e6 if settled else None
    return res
