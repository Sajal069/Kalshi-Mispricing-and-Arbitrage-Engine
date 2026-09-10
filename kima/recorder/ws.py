"""WebSocket recorder for the ``orderbook_delta`` channel.

The contract with the replay engine is that a recorded tape can be replayed into
a byte-identical book. Three things make that true, and each is a place where a
naive recorder quietly loses fidelity:

**Sequence tracking with resync.** Every message carries a monotonic ``seq``. A
gap means the local book is corrupt. We record the gap explicitly, mark the
market untrusted, and request a fresh snapshot via ``update_subscription`` with
``get_snapshot`` rather than papering over it. A gap that is recorded is
recoverable; a gap that is silently skipped corrupts every downstream number.

**Dual timestamping.** Exchange ``ts_ms`` and local receipt time are both
recorded. Their difference estimates feed latency plus clock skew, and its
*variance* is the useful signal.

**Append-only, flushed writes.** A restart must never truncate a tape. The
window it covers is unrecoverable -- Kalshi serves no order-book history.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..events import Event
from ..tape import (TapeWriter, delta_record, gap_record, reconnect_record,
                    snapshot_record)
from ..units import contracts_to_cq, dollars_to_cc
from .auth import KalshiAuth

PROD_WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"
DEMO_WS = "wss://demo-api.kalshi.co/trade-api/ws/v2"


@dataclass
class RecorderConfig:
    ws_url: str = PROD_WS
    #: Tickers per subscribe command; Kalshi accepts batches.
    subscribe_batch: int = 100
    #: Reconnect backoff bounds, in seconds.
    backoff_min_s: float = 1.0
    backoff_max_s: float = 60.0
    #: Last-resort liveness backstop, NOT the primary one.
    #:
    #: Silence is not death. An illiquid market can go many minutes without a
    #: single book update, and tearing the connection down for that costs a
    #: resubscribe, a fresh set of snapshots, and a sequence gap on every market
    #: -- which is exactly what a 45s timeout produced in testing: seven
    #: reconnects and zero deltas in a five-minute run. The websockets library
    #: already runs ping/pong (ping_interval=20, ping_timeout=20) and raises on a
    #: genuinely dead peer within ~40s, so this only needs to catch the
    #: pathological case where the socket is open but the protocol has stalled.
    idle_timeout_s: float = 900.0
    #: Opening-handshake budget. Generous on purpose: an aborted handshake
    #: costs a resubscribe and a fresh snapshot for every market.
    open_timeout_s: float = 30.0
    #: Print the first N raw messages, to inspect the wire schema.
    dump_messages: int = 0
    #: Periodically re-request snapshots so the replay has diff anchors.
    snapshot_interval_s: float = 900.0
    #: Stop if free disk falls below this, rather than dying mid-write.
    min_free_disk_mb: int = 512
    heartbeat_s: float = 60.0
    source: str = "kalshi-live"


@dataclass
class RecorderStats:
    messages: int = 0
    deltas: int = 0
    snapshots: int = 0
    trades: int = 0
    gaps: int = 0
    reconnects: int = 0
    started_ms: int = 0
    last_message_ms: int = 0
    #: Exchange-to-local delay samples, in ms. Variance is the interesting part.
    feed_delay_ms: list[int] = field(default_factory=list)
    #: How many messages actually carried an exchange timestamp. When this is
    #: zero the feed-delay figures are meaningless and must not be quoted.
    exchange_ts_messages: int = 0
    #: Book updates per market. A microstructure study needs markets that
    #: actually move; this is how you find out before committing two weeks.
    deltas_by_market: dict = field(default_factory=dict)

    @property
    def uptime_s(self) -> float:
        """Seconds of observed feed. Zero until a message has actually arrived.

        Guarded rather than computed blindly: a zero here divides into any rate
        derived from it, and a rate of 1e12 updates per hour is worse than no
        rate at all -- it looks like a measurement.
        """
        if not self.started_ms or not self.last_message_ms:
            return 0.0
        return max(0.0, (self.last_message_ms - self.started_ms) / 1000.0)

    def to_dict(self) -> dict:
        d = {
            "messages": self.messages,
            "deltas": self.deltas,
            "snapshots": self.snapshots,
            "trades": self.trades,
            "sequence_gaps": self.gaps,
            "reconnects": self.reconnects,
            "uptime_s": self.uptime_s,
        }
        d["messages_with_exchange_ts"] = self.exchange_ts_messages
        active = [t for t, n in self.deltas_by_market.items() if n]
        d["markets_with_updates"] = len(active)
        hours = self.uptime_s / 3600.0
        # No elapsed time means no rate. Reporting None is honest; reporting a
        # number derived from a near-zero denominator is not.
        d["deltas_per_market_hour"] = (
            round(self.deltas / max(len(active), 1) / hours, 1)
            if hours > 0 and active else None
        )
        if self.feed_delay_ms:
            s = sorted(self.feed_delay_ms)
            d["feed_delay_ms_p50"] = s[len(s) // 2]
            d["feed_delay_ms_p99"] = s[min(len(s) - 1, int(len(s) * 0.99))]
        return d


class OrderbookRecorder:
    """Subscribes to ``orderbook_delta`` and writes an append-only tape."""

    def __init__(
        self,
        tickers: Sequence[str],
        tape_path: str | Path,
        cfg: RecorderConfig | None = None,
        auth: KalshiAuth | None = None,
    ):
        self.tickers = list(tickers)
        self.cfg = cfg or RecorderConfig()
        self.auth = auth
        self.tape_path = Path(tape_path)
        self.stats = RecorderStats()
        #: Next expected sequence number, per subscription id.
        self._expected_seq: dict[int, int] = {}
        #: Which markets have been seen on each subscription. A gap
        #: invalidates all of them, because the lost messages could have
        #: belonged to any one.
        self._sid_tickers: dict[int, set[str]] = {}
        self._writer: TapeWriter | None = None
        self._cmd_id = 0
        self._stop = asyncio.Event()
        self._sids: list[int] = []
        self._sid_for_ticker: dict[str, int] = {}
        self._pending_sub: dict[int, list[str]] = {}
        self._dump_remaining = int(cfg.dump_messages) if cfg else 0

    # -- lifecycle ---------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def open_sink(self, note: str = "") -> TapeWriter:
        """Open the append-only tape. Idempotent.

        Separate from :meth:`run` so the decode path can be exercised without a
        socket, and so a caller that forgets to open one fails loudly rather than
        recording nothing while reporting healthy statistics.
        """
        if self._writer is None:
            appending = self.tape_path.exists()
            self._writer = TapeWriter(
                self.tape_path,
                source=self.cfg.source,
                note=note or f"live capture of {len(self.tickers)} markets",
            )
            if appending:
                # A new process re-subscribes, so sequence numbering restarts.
                # Without a marker the replay reads that as message loss and
                # untrusts every book until snapshots arrive. Over a long run
                # driven by a restart loop that is a lot of phantom gaps.
                now_ns = time.time_ns()
                self._writer.write(reconnect_record(
                    int(now_ns / 1_000_000), now_ns, "recorder session start"))
        return self._writer

    def close_sink(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    async def run(self, max_seconds: float | None = None) -> RecorderStats:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("the websockets package is required to record") from exc

        self.open_sink()
        self.stats.started_ms = int(time.time() * 1000)
        deadline = (time.monotonic() + max_seconds) if max_seconds else None
        backoff = self.cfg.backoff_min_s
        try:
            while not self._stop.is_set():
                if deadline and time.monotonic() >= deadline:
                    break
                try:
                    # Connection-level auth is required even for public data.
                    headers = self.auth.sign("GET", "/trade-api/ws/v2") if self.auth else {}
                    async with websockets.connect(
                        self.cfg.ws_url,
                        additional_headers=headers,
                        ping_interval=20,
                        ping_timeout=20,
                        # Handshakes to this host are intermittently slow; the
                        # 10s default aborts connections that would have
                        # completed, and each abort costs a full resubscribe.
                        open_timeout=self.cfg.open_timeout_s,
                        close_timeout=5,
                        max_size=8 * 1024 * 1024,
                    ) as ws:
                        backoff = self.cfg.backoff_min_s
                        await self._subscribe(ws)
                        await self._pump(ws, deadline)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.stats.reconnects += 1
                    self._log(f"connection lost ({type(exc).__name__}: {exc}); "
                              f"reconnecting in {backoff:.1f}s")
                    await asyncio.sleep(backoff + random.uniform(0, backoff * 0.25))
                    backoff = min(self.cfg.backoff_max_s, backoff * 2)
                    # Every market is untrusted after a reconnect until a fresh
                    # snapshot arrives, so drop the sequence expectations -- and
                    # tell the tape, because the replay cannot infer it.
                    self._expected_seq.clear()
                    self._sid_tickers.clear()
                    now_ns = time.time_ns()
                    self._write(reconnect_record(
                        int(now_ns / 1_000_000), now_ns,
                        f"{type(exc).__name__}: {exc}"))
        finally:
            self.close_sink()
        return self.stats

    async def _subscribe(self, ws: Any) -> None:
        self._sids.clear()
        self._sid_for_ticker.clear()
        for i in range(0, len(self.tickers), self.cfg.subscribe_batch):
            batch = self.tickers[i : i + self.cfg.subscribe_batch]
            self._cmd_id += 1
            self._pending_sub[self._cmd_id] = list(batch)
            await ws.send(json.dumps({
                "id": self._cmd_id,
                "cmd": "subscribe",
                "params": {"channels": ["orderbook_delta"], "market_tickers": batch},
            }))

    async def request_snapshot(self, ws: Any, tickers: Iterable[str]) -> None:
        """Ask the exchange to resend full books, repairing a corrupt local state.

        ``update_subscription`` is scoped to a single subscription, and the
        exchange rejects the command outright without one -- the error reads
        "Exactly one subscription ID is required". Subscribing in batches means
        there is more than one live subscription, so the request has to be split
        per subscription rather than sent once for every ticker.
        """
        wanted = list(tickers)
        if not self._sids:
            # No confirmed subscription to scope the request to. Never silent:
            # a resync we failed to request is a book that stays untrusted, and
            # the replay would show it as an unexplained dead market.
            self._log(f"cannot resync {', '.join(wanted[:3])}: no subscription id yet")
            return
        by_sid: dict[int, list[str]] = {}
        for t in wanted:
            sid = self._sid_for_ticker.get(t)
            if sid is None:
                sid = self._sids[0]
            by_sid.setdefault(sid, []).append(t)
        for sid, group in by_sid.items():
            self._cmd_id += 1
            await ws.send(json.dumps({
                "id": self._cmd_id,
                "cmd": "update_subscription",
                "params": {
                    "sids": [sid],
                    "action": "get_snapshot",
                    "market_tickers": group,
                },
            }))

    async def _pump(self, ws: Any, deadline: float | None) -> None:
        while not self._stop.is_set():
            if deadline and time.monotonic() >= deadline:
                return
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.cfg.idle_timeout_s)
            except asyncio.TimeoutError:
                # Quiet, not dead: ping/pong is what proves liveness. Log it so a
                # genuinely stalled feed stays visible, then keep waiting.
                self._log(f"no messages for {self.cfg.idle_timeout_s:.0f}s "
                          f"(ping/pong still healthy); continuing")
                continue
            recv_ns = time.time_ns()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await self._handle(ws, msg, recv_ns)

    # -- message handling --------------------------------------------------
    async def _handle(self, ws: Any, msg: dict, recv_ns: int) -> None:
        kind = msg.get("type")
        self.stats.messages += 1
        if self._dump_remaining > 0 and kind not in ("pong",):
            self._dump_remaining -= 1
            self._log(f"RAW {json.dumps(msg)[:600]}")
        if kind in ("subscribed", "ok", "error", "pong"):
            if kind == "subscribed":
                sub = msg.get("msg") or {}
                sid = sub.get("sid")
                if sid is not None:
                    if sid not in self._sids:
                        self._sids.append(sid)
                    for t in self._pending_sub.pop(msg.get("id"), []):
                        self._sid_for_ticker[t] = sid
            elif kind == "error":
                self._log(f"exchange error: {msg}")
            return
        payload = msg.get("msg") or {}
        ticker = payload.get("market_ticker")
        if not ticker:
            return
        # Sequence numbers live at the top level and are scoped to the
        # subscription, not the market.
        sid = msg.get("sid", payload.get("sid", 0))
        seq = msg.get("seq", payload.get("seq", 0))

        # Exchange timestamps are not always present on these messages -- the
        # orderbook_snapshot payload carries none at all. Fall back to local
        # receipt time, but count how often we had a real one so the report can
        # say honestly whether feed-delay figures mean anything.
        exch_ts = payload.get("ts_ms") or msg.get("ts_ms") or msg.get("ts")
        if exch_ts:
            self.stats.exchange_ts_messages += 1
            ts_ms = int(exch_ts)
            delay = int(recv_ns / 1_000_000) - ts_ms
            if -5_000 < delay < 60_000:
                self.stats.feed_delay_ms.append(delay)
        else:
            ts_ms = int(recv_ns / 1_000_000)
        self.stats.last_message_ms = ts_ms

        if kind in ("orderbook_snapshot", "orderbook_delta"):
            known = self._sid_tickers.setdefault(sid, set())
            expected = self._expected_seq.get(sid)
            if expected is not None and seq != expected:
                # Lost messages could have belonged to any market on this
                # subscription, so every one of them is now suspect.
                self.stats.gaps += 1
                self._write(gap_record(ticker, expected, seq, ts_ms, recv_ns, sid))
                self._expected_seq[sid] = seq + 1
                await self.request_snapshot(ws, sorted(known) or [ticker])
                if kind == "orderbook_delta":
                    return
            else:
                self._expected_seq[sid] = seq + 1
            known.add(ticker)

        if kind == "orderbook_snapshot":
            self._write_snapshot(ticker, payload, seq, ts_ms, recv_ns, sid)
            return

        if kind == "orderbook_delta":
            self._write_delta(ticker, payload, seq, ts_ms, recv_ns, sid)
            return

        if kind == "trade":
            from ..tape import trade_record
            self._write(trade_record(
                ticker,
                dollars_to_cc(payload.get("yes_price_dollars", "0")),
                contracts_to_cq(payload.get("count", 0)),
                payload.get("taker_side", ""),
                ts_ms,
                recv_ns,
            ))
            self.stats.trades += 1

    def _write_snapshot(self, ticker: str, payload: dict, seq: int, ts_ms: int,
                        recv_ns: int, sid: int = 0) -> None:
        # A market with resting orders on only one side omits the other key
        # entirely rather than sending an empty list.
        yes = [(dollars_to_cc(p), contracts_to_cq(q))
               for p, q in payload.get("yes_dollars_fp") or []]
        no = [(dollars_to_cc(p), contracts_to_cq(q))
              for p, q in payload.get("no_dollars_fp") or []]
        self._write(snapshot_record(ticker, yes, no, seq, ts_ms, recv_ns, sid))
        self.stats.snapshots += 1

    def _write_delta(self, ticker: str, payload: dict, seq: int, ts_ms: int,
                     recv_ns: int, sid: int = 0) -> None:
        price = payload.get("price_dollars", payload.get("price"))
        delta = payload.get("delta_fp", payload.get("delta"))
        if price is None or delta is None:
            self._log(f"delta with unexpected shape, skipped: {sorted(payload)}")
            return
        self._write(delta_record(
            ticker,
            dollars_to_cc(price),
            contracts_to_cq(delta),
            payload.get("side", "yes"),
            seq,
            ts_ms,
            recv_ns,
            sid,
        ))
        self.stats.deltas += 1
        self.stats.deltas_by_market[ticker] = self.stats.deltas_by_market.get(ticker, 0) + 1

    def _write(self, record: dict) -> None:
        if self._writer is None:
            # Silently dropping here would lose an unrecoverable window of tape
            # while the statistics still looked healthy.
            raise RuntimeError("recorder has no open tape sink; call open_sink() first")
        self._writer.write(record)

    def _log(self, message: str) -> None:
        print(f"[recorder {time.strftime('%H:%M:%S')}] {message}", flush=True)


async def record(
    events: Sequence[Event],
    tape_path: str | Path,
    cfg: RecorderConfig | None = None,
    auth: KalshiAuth | None = None,
    max_seconds: float | None = None,
) -> RecorderStats:
    tickers = [m.ticker for ev in events for m in ev.markets]
    rec = OrderbookRecorder(tickers, tape_path, cfg, auth)
    return await rec.run(max_seconds)
