"""Synthetic L2 tape generator.

Produces a message stream in exactly the format :mod:`kima.recorder` writes, so
the replay engine cannot tell the two apart. Tapes are stamped
``source="synthetic"`` and every report carries that provenance forward.

Mechanics, and why they are not rigged
--------------------------------------
Each market has a market maker quoting a two-sided ladder around its own view of
fair value, with a half-spread and geometrically decaying depth. Fair values
within an event are *coherent*: for a mutually exclusive family they always sum
to the family's true total mass.

Dislocations therefore never arise from a fair-value inconsistency. They arise
from **heterogeneous reaction lag**: when an information shock moves probability
mass between legs, each maker requotes after its own delay, so for a few hundred
milliseconds the tape shows legs that have already moved up alongside legs that
have not yet moved down. That is the same mechanism as reality, and it means the
structural predictions in the plan are emergent properties of the simulation
rather than assumptions baked into it:

* the hurdle a basket must clear is ``sum_i 2*half_spread_i``, which grows with
  leg count, so wide ladders should show *fewer* violations (H8);
* a shock must move more probability mass than that hurdle to create one at all;
* the violation dies as soon as the slowest relevant maker requotes, so episode
  duration tracks the lag distribution (H4).

The generator also reproduces three failure modes the engine must survive:
sequence gaps, pre-close spread blow-out, and post-close phantom liquidity where
makers pull one side and leave stale bids resting.
"""

from __future__ import annotations

import heapq
import math
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..book import NO, YES
from ..events import Event
from ..tape import TapeWriter, delta_record, snapshot_record, trade_record
from ..ticks import PriceGrid
from ..units import NOTIONAL_CC
from .universe import DEFAULT_FAMILIES, FamilySpec, build_universe

SIDE_NAME = ("yes", "no")


@dataclass
class SimConfig:
    seed: int = 20260826
    duration_ms: int = 6 * 3_600_000
    start_ts_ms: int = 1_767_225_600_000        # 2026-01-01T00:00:00Z
    events_per_family: int = 1
    families: tuple[FamilySpec, ...] = DEFAULT_FAMILIES
    #: Probability that a given delta is lost in transit, forcing a resync.
    seq_gap_rate: float = 0.00015
    #: Feed transport delay applied to local receipt timestamps, in ms.
    network_jitter_ms: tuple[int, int] = (4, 38)
    #: Fraction of the run after which markets close, so the tape contains a
    #: post-close window full of phantom liquidity for the filters to reject.
    close_at_fraction: float = 0.92
    #: Multiplier applied to half-spread through the pre-close window.
    pre_close_widening: float = 4.0
    snapshot_interval_ms: int = 900_000          # periodic snapshot for diffing


# --------------------------------------------------------------------------
# per-market maker
# --------------------------------------------------------------------------
class MarketSim:
    __slots__ = (
        "ticker", "spec", "grid", "fair_cc", "quoted_cc", "lag_ms",
        "levels", "seq", "closed", "pulled_side", "rng", "half_spread_cc",
    )

    def __init__(self, ticker: str, spec: FamilySpec, grid: PriceGrid, rng: random.Random):
        self.ticker = ticker
        self.spec = spec
        self.grid = grid
        self.rng = rng
        self.fair_cc = NOTIONAL_CC // 2
        self.quoted_cc = self.fair_cc
        # Heterogeneous reaction lag is the entire source of arbitrage here.
        self.lag_ms = rng.randint(*spec.lag_ms)
        self.half_spread_cc = spec.half_spread_cc
        self.levels: tuple[dict[int, int], dict[int, int]] = ({}, {})
        self.seq = 0
        self.closed = False
        self.pulled_side: int | None = None

    def _step_at(self, price_cc: int) -> int:
        return self.grid.segment_for(max(0, min(NOTIONAL_CC, price_cc))).step_cc

    def target_levels(self, widen: float = 1.0) -> tuple[dict[int, int], dict[int, int]]:
        """Ladder the maker wants resting, given its current quoted fair value."""
        half = max(1, int(self.half_spread_cc * widen))
        spec = self.spec
        out: list[dict[int, int]] = [{}, {}]
        for side in (YES, NO):
            centre = self.quoted_cc if side == YES else NOTIONAL_CC - self.quoted_cc
            top = centre - half
            step = self._step_at(top)
            top = self.grid.round_down(max(step, min(NOTIONAL_CC - step, top)))
            if top <= 0:
                continue
            if self.closed and self.pulled_side == side:
                continue                      # maker walked away from this side
            size = spec.top_depth_cq
            price = top
            for k in range(spec.n_price_levels):
                if price <= 0:
                    break
                jitter = 0.8 + 0.4 * self.rng.random()
                qty = int(size * (spec.depth_decay ** k) * jitter)
                if self.closed:
                    qty //= 4                 # thin, stale, and not really there
                if qty > 0:
                    out[side][price] = qty
                step = self._step_at(price)
                price -= step
        return out[0], out[1]

    def diff_to(self, target: tuple[dict[int, int], dict[int, int]]) -> list[tuple[int, int, int]]:
        """``[(side, price_cc, delta_cq)]`` moving the resting book to ``target``."""
        changes: list[tuple[int, int, int]] = []
        for side in (YES, NO):
            cur, tgt = self.levels[side], target[side]
            for price in set(cur) | set(tgt):
                d = tgt.get(price, 0) - cur.get(price, 0)
                if d:
                    changes.append((side, price, d))
        # Removals before additions. A real matching engine can never publish a
        # crossed book, so a requote that adds the new top before pulling the old
        # one would emit a state the exchange could not produce -- and invariant
        # N0 would rightly reject the tape. After every removal the book is a
        # subset of the (uncrossed) old book; every addition moves it toward the
        # (uncrossed) new book, so no intermediate state is ever crossed.
        changes.sort(key=lambda c: (c[2] > 0, c[0], -c[1]))
        return changes

    def apply(self, side: int, price_cc: int, delta_cq: int) -> None:
        book = self.levels[side]
        new = book.get(price_cc, 0) + delta_cq
        if new <= 0:
            book.pop(price_cc, None)
        else:
            book[price_cc] = new

    def snapshot(self) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
        return (
            sorted(self.levels[YES].items()),
            sorted(self.levels[NO].items()),
        )

    def best(self, side: int) -> int:
        return max(self.levels[side], default=-1)


# --------------------------------------------------------------------------
# per-event fair-value process
# --------------------------------------------------------------------------
class EventSim:
    def __init__(self, event: Event, spec: FamilySpec, rng: random.Random, grid: PriceGrid):
        self.event = event
        self.spec = spec
        self.rng = rng
        self.markets = [MarketSim(m.ticker, spec, grid, rng) for m in event.markets]
        self.by_ticker = {m.ticker: m for m in self.markets}
        # Non-exhaustive families keep genuine mass outside the listed legs, so
        # their YES prices can legitimately sum below $1 with no arbitrage.
        self.total_mass = 1.0
        if spec.kind == "award":
            self.total_mass = 1.0 - (0.10 + 0.15 * rng.random())
        self.p: list[float] = self._initial_p()
        self._push_fair()

    def _initial_p(self) -> list[float]:
        n = self.event.n_legs
        if self.spec.kind == "threshold_ladder":
            return self._threshold_p()
        raw = [self.rng.gammavariate(1.4, 1.0) + 1e-6 for _ in range(n)]
        s = sum(raw)
        return [self.total_mass * r / s for r in raw]

    def _threshold_p(self) -> list[float]:
        """Survival probabilities of a latent normal: strictly monotone in strike."""
        mu = 2.6 + 0.4 * self.rng.random()
        sigma = 0.22 + 0.1 * self.rng.random()
        out = []
        for m in self.event.markets:
            strike = float(m.floor_strike if m.floor_strike is not None else 0)
            z = (strike - mu) / sigma
            out.append(0.5 * math.erfc(z / math.sqrt(2.0)))
        return out

    def _push_fair(self) -> None:
        for p, ms in zip(self.p, self.markets):
            ms.fair_cc = max(20, min(NOTIONAL_CC - 20, int(round(p * NOTIONAL_CC))))

    def settle(self, rng: random.Random) -> dict[str, str]:
        """Draw a terminal outcome consistent with the final fair values.

        Used only for the ex-post integrity check: every basket the simulator
        called risk-free is joined to the actual outcome and its realised payoff
        must meet the guaranteed floor. A single counterexample would mean the
        mutual-exclusivity assumption or the outcome partition was wrong.
        """
        n = self.event.n_legs
        if self.spec.kind == "threshold_ladder":
            # Nested survival probabilities: cell k is [s_k, s_{k+1}), and every
            # threshold at or below k settles YES.
            surv = sorted(self.p, reverse=True)
            cells = [1.0 - surv[0]] + [
                max(0.0, surv[i] - (surv[i + 1] if i + 1 < n else 0.0)) for i in range(n)
            ]
            k = _weighted_choice(cells, rng) - 1
            return {
                m.ticker: ("yes" if i <= k else "no")
                for i, m in enumerate(self.event.markets)
            }
        weights = list(self.p) + [max(0.0, 1.0 - sum(self.p))]
        k = _weighted_choice(weights, rng)
        return {
            m.ticker: ("yes" if i == k else "no")
            for i, m in enumerate(self.event.markets)
        }

    def shock(self, magnitude_cc: int) -> None:
        """Move probability mass between legs, preserving the family's total."""
        sigma = 2.0 * magnitude_cc / NOTIONAL_CC
        if self.spec.kind == "threshold_ladder":
            # A shock to the latent variable shifts the whole survival curve.
            shift = self.rng.gauss(0.0, sigma)
            self.p = [min(0.995, max(0.005, p * math.exp(shift * (1.0 - p)))) for p in self.p]
            self.p.sort(reverse=True)
        else:
            w = [max(1e-9, p * math.exp(self.rng.gauss(0.0, sigma))) for p in self.p]
            s = sum(w)
            self.p = [self.total_mass * x / s for x in w]
        self._push_fair()


# --------------------------------------------------------------------------
# generator
# --------------------------------------------------------------------------
def _weighted_choice(weights: list[float], rng: random.Random) -> int:
    total = sum(w for w in weights if w > 0)
    if total <= 0:
        return 0
    x = rng.random() * total
    acc = 0.0
    for i, w in enumerate(weights):
        if w <= 0:
            continue
        acc += w
        if x <= acc:
            return i
    return len(weights) - 1


JUMP, DRIFT, REQUOTE, TRADE_EV, CLOSE, SNAP = range(6)


class TapeGenerator:
    """Event-driven simulation writing a synthetic tape."""

    def __init__(self, cfg: SimConfig | None = None):
        self.cfg = cfg or SimConfig()
        self.rng = random.Random(self.cfg.seed)
        self.events, self.spec_by_event = build_universe(
            self.cfg.families, self.cfg.events_per_family
        )
        self.sims: dict[str, EventSim] = {}
        for ev in self.events:
            spec = self.spec_by_event[ev.event_ticker]
            self.sims[ev.event_ticker] = EventSim(ev, spec, self.rng, spec.price_grid)
        self.close_ts = self.cfg.start_ts_ms + int(self.cfg.duration_ms * self.cfg.close_at_fraction)
        for ev in self.events:
            for m in ev.markets:
                m.close_ts_ms = self.close_ts
        self._heap: list[tuple[int, int, int, Any]] = []
        self._counter = 0
        #: Sequence numbers are scoped to a subscription, as on the wire. One
        #: subscription per event family keeps a lost message from invalidating
        #: every book at once -- which is exactly why batch size matters when
        #: subscribing for real.
        self._seq_by_sid: dict[int, int] = {}
        #: Receipt times are monotonic: a connection delivers in order.
        self._last_recv_ns = 0
        self._sid_of: dict[str, int] = {
            m.ticker: i for i, ev in enumerate(self.events) for m in ev.markets
        }
        self.stats = {"deltas": 0, "snapshots": 0, "trades": 0, "gaps": 0, "shocks": 0}
        #: (ts_ms, event_ticker) for every information shock. This is the
        #: synthetic analogue of an economic calendar, and it is what makes
        #: the macro-event study (E11) checkable rather than eyeballed.
        self.shock_log: list[tuple[int, str]] = []

    # -- scheduling --------------------------------------------------------
    def _schedule(self, ts: int, kind: int, arg: Any) -> None:
        if ts <= self.cfg.start_ts_ms + self.cfg.duration_ms:
            self._counter += 1
            heapq.heappush(self._heap, (ts, self._counter, kind, arg))

    def _exp_gap_ms(self, rate_hz: float) -> int:
        if rate_hz <= 0:
            return 10**12
        return max(1, int(self.rng.expovariate(rate_hz) * 1000))

    def _widen(self, ts: int, spec: FamilySpec) -> float:
        """Pre-close spread blow-out, ramped rather than stepped."""
        pre_start = self.close_ts - int(self.cfg.duration_ms * spec.pre_close_fraction)
        if ts < pre_start:
            return 1.0
        if ts >= self.close_ts:
            return self.cfg.pre_close_widening
        frac = (ts - pre_start) / max(1, self.close_ts - pre_start)
        return 1.0 + (self.cfg.pre_close_widening - 1.0) * frac

    # -- emission ----------------------------------------------------------
    def _recv_ns(self, ts_ms: int) -> int:
        """Local receipt time, monotonic by construction.

        A connection delivers in order, so receipt timestamps never go
        backwards -- the real tape shows exactly that. Drawing an independent
        jitter per message would let a later message be stamped earlier, which
        is not a thing TCP can do, and it makes receipt order disagree with
        sequence order. The engine orders on this clock, so that disagreement
        would manufacture sequence gaps out of nothing.
        """
        lo, hi = self.cfg.network_jitter_ms
        ns = (ts_ms + self.rng.randint(lo, hi)) * 1_000_000
        if ns <= self._last_recv_ns:
            ns = self._last_recv_ns + 1_000        # 1 microsecond apart
        self._last_recv_ns = ns
        return ns

    def _emit_requote(self, ts: int, esim: EventSim, ms: MarketSim, out: list[dict]) -> None:
        widen = self._widen(ts, esim.spec)
        ms.quoted_cc = ms.fair_cc
        target = ms.target_levels(widen)
        for side, price, delta in ms.diff_to(target):
            ms.apply(side, price, delta)
            sid = self._sid_of[ms.ticker]
            ms.seq = self._seq_by_sid[sid] = self._seq_by_sid.get(sid, 0) + 1
            # A lost message leaves a hole in seq; the replay engine must notice
            # and refuse to trade the book until a snapshot repairs it.
            if self.rng.random() < self.cfg.seq_gap_rate:
                self.stats["gaps"] += 1
                self._schedule(ts + 180, SNAP, (esim.event.event_ticker, ms.ticker))
                continue
            out.append(delta_record(ms.ticker, price, delta, SIDE_NAME[side],
                                    ms.seq, ts, self._recv_ns(ts), sid))
            self.stats["deltas"] += 1

    def _emit_snapshot(self, ts: int, ms: MarketSim, out: list[dict]) -> None:
        yes, no = ms.snapshot()
        sid = self._sid_of[ms.ticker]
        ms.seq = self._seq_by_sid[sid] = self._seq_by_sid.get(sid, 0) + 1
        out.append(snapshot_record(ms.ticker, yes, no, ms.seq, ts,
                                   self._recv_ns(ts), sid))
        self.stats["snapshots"] += 1

    def _emit_trade(self, ts: int, esim: EventSim, ms: MarketSim, out: list[dict]) -> None:
        """A noise taker eats part of one side, which the maker later replaces."""
        side = YES if self.rng.random() < 0.5 else NO
        best = ms.best(side)
        if best < 0:
            return
        resting = ms.levels[side][best]
        take = max(1, int(resting * (0.2 + 0.7 * self.rng.random())))
        take = min(take, resting)
        ms.apply(side, best, -take)
        sid = self._sid_of[ms.ticker]
        ms.seq = self._seq_by_sid[sid] = self._seq_by_sid.get(sid, 0) + 1
        out.append(delta_record(ms.ticker, best, -take, SIDE_NAME[side], ms.seq, ts,
                                self._recv_ns(ts), sid))
        out.append(trade_record(ms.ticker, best, take, SIDE_NAME[1 - side], ts, self._recv_ns(ts)))
        self.stats["deltas"] += 1
        self.stats["trades"] += 1
        # The maker notices and refills after its own reaction lag.
        self._schedule(ts + ms.lag_ms // 2, REQUOTE, (esim.event.event_ticker, ms.ticker))

    # -- main loop ---------------------------------------------------------
    def run(self) -> Iterator[dict]:
        cfg = self.cfg
        t0 = cfg.start_ts_ms
        out: list[dict] = []

        for ev in self.events:
            esim = self.sims[ev.event_ticker]
            for ms in esim.markets:
                ms.quoted_cc = ms.fair_cc
                target = ms.target_levels(1.0)
                for side, price, delta in ms.diff_to(target):
                    ms.apply(side, price, delta)
                self._emit_snapshot(t0, ms, out)
            spec = esim.spec
            self._schedule(t0 + self._exp_gap_ms(spec.jumps_per_hour / 3600.0), JUMP, ev.event_ticker)
            for ms in esim.markets:
                self._schedule(t0 + self._exp_gap_ms(spec.drift_hz), DRIFT, (ev.event_ticker, ms.ticker))
                self._schedule(t0 + self._exp_gap_ms(spec.trade_hz), TRADE_EV, (ev.event_ticker, ms.ticker))
            self._schedule(self.close_ts, CLOSE, ev.event_ticker)
        self._schedule(t0 + cfg.snapshot_interval_ms, SNAP, None)
        yield from out
        out.clear()

        while self._heap:
            ts, _, kind, arg = heapq.heappop(self._heap)

            if kind == JUMP:
                esim = self.sims[arg]
                esim.shock(esim.spec.jump_size_cc)
                self.stats["shocks"] += 1
                self.shock_log.append((ts, arg))
                for ms in esim.markets:
                    delay = ms.lag_ms + self.rng.randint(0, ms.lag_ms // 2)
                    self._schedule(ts + delay, REQUOTE, (arg, ms.ticker))
                self._schedule(ts + self._exp_gap_ms(esim.spec.jumps_per_hour / 3600.0), JUMP, arg)

            elif kind == DRIFT:
                ev_t, mk_t = arg
                esim = self.sims[ev_t]
                esim.shock(esim.spec.jump_size_cc // 6)
                ms = esim.by_ticker[mk_t]
                self._schedule(ts + ms.lag_ms // 2, REQUOTE, arg)
                self._schedule(ts + self._exp_gap_ms(esim.spec.drift_hz), DRIFT, arg)

            elif kind == REQUOTE:
                ev_t, mk_t = arg
                esim = self.sims[ev_t]
                self._emit_requote(ts, esim, esim.by_ticker[mk_t], out)

            elif kind == TRADE_EV:
                ev_t, mk_t = arg
                esim = self.sims[ev_t]
                self._emit_trade(ts, esim, esim.by_ticker[mk_t], out)
                self._schedule(ts + self._exp_gap_ms(esim.spec.trade_hz), TRADE_EV, arg)

            elif kind == CLOSE:
                esim = self.sims[arg]
                for ms in esim.markets:
                    ms.closed = True
                    # Most makers pull one side entirely and leave the other
                    # resting and stale: phantom liquidity, not executable size.
                    ms.pulled_side = self.rng.choice([YES, NO]) if self.rng.random() < 0.6 else None
                    self._schedule(ts + self.rng.randint(50, 4_000), REQUOTE, (arg, ms.ticker))
                out.append({
                    "type": "status", "e": arg, "ts": ts, "rn": self._recv_ns(ts),
                    "status": "closed",
                })

            elif kind == SNAP:
                if arg is None:
                    for esim in self.sims.values():
                        for ms in esim.markets:
                            self._emit_snapshot(ts, ms, out)
                    self._schedule(ts + cfg.snapshot_interval_ms, SNAP, None)
                else:
                    ev_t, mk_t = arg
                    self._emit_snapshot(ts, self.sims[ev_t].by_ticker[mk_t], out)

            if out:
                yield from out
                out.clear()

    # -- convenience -------------------------------------------------------
    def write(self, path: str | Path) -> dict:
        note = (
            f"SYNTHETIC tape, seed={self.cfg.seed}, "
            f"{self.cfg.duration_ms / 3_600_000:.2f}h, "
            f"{len(self.events)} events. Not Kalshi data."
        )
        started = time.time()
        with TapeWriter(path, source="synthetic", note=note, overwrite=True) as w:
            for rec in self.run():
                w.write(rec)
        meta_path = self.write_universe(path)
        return {
            **self.stats,
            "events": len(self.events),
            "markets": sum(e.n_legs for e in self.events),
            "wall_seconds": round(time.time() - started, 2),
            "paths": [str(p) for p in w.paths],
            "universe": str(meta_path),
        }

    def write_universe(self, tape_path: str | Path) -> Path:
        """Persist the universe metadata and settlement outcomes beside the tape.

        Live recording gets the same file from the REST metadata cache, so the
        backtester has one code path regardless of provenance.
        """
        from ..events import event_to_dict
        from ..exhaustive import certificates
        from ..tape import save_json
        from .universe import CURATED_EXHAUSTIVE

        settle_ts = self.cfg.start_ts_ms + self.cfg.duration_ms
        for ev in self.events:
            results = self.sims[ev.event_ticker].settle(self.rng)
            for m in ev.markets:
                m.result = results[m.ticker]
                m.settlement_ts_ms = settle_ts
        certs = certificates(self.events, curated_exhaustive=CURATED_EXHAUSTIVE)
        out = Path(str(tape_path).replace(".jsonl.gz", "") + ".universe.json")
        save_json(
            {
                "source": "synthetic",
                "seed": self.cfg.seed,
                "start_ts_ms": self.cfg.start_ts_ms,
                "duration_ms": self.cfg.duration_ms,
                "events": [event_to_dict(e) for e in self.events],
                "certificates": {k: c.to_dict() for k, c in certs.items()},
                "shocks": [{"ts_ms": t, "event_ticker": e} for t, e in self.shock_log],
            },
            out,
        )
        return out


def generate(path: str | Path, cfg: SimConfig | None = None) -> dict:
    return TapeGenerator(cfg).write(path)
