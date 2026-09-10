"""Deterministic replay: tape in, book states out.

Three correctness properties this module is responsible for, all of them load
bearing:

**Strict ordering.** Records are emitted in ``(ts, seq, ticker)`` order through a
bounded reorder window, so the same tape always produces byte-identical output.
Determinism is not a nicety here -- it is what makes the latency sweep a
controlled experiment rather than a set of unrelated runs.

**No look-ahead, ever.** Each leg's state is whatever its last *observed* update
said, forward-filled. The detector never sees a message with ``ts > t``. The
execution simulator is the only component allowed to look forward, it does so by
exactly ``delta``, and it does it by *waiting* in the same forward pass rather
than by peeking -- see :mod:`kima.execution`.

**Sequence-gap safety.** A gap means the local book is corrupt. The book is
marked untrusted immediately and stays that way until a snapshot repairs it.
Trading a corrupt book is worse than missing the trade, so untrusted books are
excluded from detection entirely and the excluded interval is reported.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol, Sequence

from .book import NO, YES, BookIntegrityError, MarketBook, side_index
from .events import Event
from .tape import DELTA, GAP, META, RECONNECT, SNAPSHOT, TRADE, read_tape
from .ticks import PriceGrid

PHASES = ("pre_open", "active", "pre_close", "post_close", "untrusted")


@dataclass
class ReplayConfig:
    #: Records held in the reorder heap before emission. Bounded so memory is
    #: constant on a multi-gigabyte tape.
    reorder_window: int = 4_096
    #: Halt the replay when invariant N0 fails. On by default: a crossed book
    #: means our delta application is wrong, and continuing would launder a bug
    #: into a "finding".
    strict_n0: bool = True
    #: Window before close_time in which quotes are treated as unreliable.
    pre_close_window_ms: int = 5 * 60_000
    #: Feed-liveness guard: no leg of the event has updated for this long, so
    #: the connection is suspect. This is the plan's tau.
    stale_feed_ms: int = 500
    #: Per-leg abandonment guard, deliberately generous.
    #:
    #: A resting book does not go stale because it is quiet: Kalshi does not
    #: cancel orders for inactivity, and sequence continuity already proves we
    #: have missed nothing. In a 28-leg bucket ladder the far-out-of-the-money
    #: legs routinely sit still for minutes, so a tight guard rejects sound books.
    #:
    #: Measured on 8.6h of live tape, this threshold removed:
    #:      60s -> 85% of raw signals, 27 actionable
    #:     300s -> 60%,                51 actionable
    #:    3600s ->  0%,                56 actionable
    #: The conclusion is insensitive to it -- fees dominate at every setting --
    #: but 60s was discarding sound books for no reason. What this guard is
    #: actually for is the one failure sequence numbers cannot reveal: a single
    #: market going silent while the rest of the subscription keeps flowing.
    stale_leg_ms: int = 3_600_000
    #: Compare periodic exchange snapshots against the locally maintained book.
    snapshot_diff: bool = True
    #: Silence longer than this is treated as "not recording" rather than as an
    #: observed quiet period. A tape stitched from several sessions otherwise
    #: counts the hours between them as coverage.
    coverage_gap_ms: int = 60_000


@dataclass
class ReplayStats:
    records: int = 0
    deltas: int = 0
    snapshots: int = 0
    trades: int = 0
    gaps: int = 0
    reconnects: int = 0
    n0_violations: int = 0
    out_of_order: int = 0
    snapshot_diffs: int = 0
    #: Mismatches explained by a known sequence gap since the last snapshot.
    #: These prove the *gap detector* works; they are not reconstruction errors.
    snapshot_diffs_after_gap: int = 0
    snapshot_checks: int = 0
    untrusted_ms: int = 0
    first_ts_ms: int = 0
    last_ts_ms: int = 0
    #: Time actually covered by messages, excluding stretches with none.
    coverage_ms: int = 0
    idle_gaps: int = 0
    #: Exchange-to-local delay samples, in ms, from records carrying both clocks.
    #: This is the number delta* has to be compared against.
    feed_delay_ms: list[int] = field(default_factory=list)
    ticks_by_market: dict[str, int] = field(default_factory=dict)

    @property
    def span_ms(self) -> int:
        """Wall-clock from first message to last, idle stretches included."""
        return max(0, self.last_ts_ms - self.first_ts_ms)

    @property
    def duration_ms(self) -> int:
        """Time actually recorded. This is the denominator for every rate.

        Falls back to the span for tapes written before coverage was tracked.
        """
        return self.coverage_ms or self.span_ms

    @property
    def snapshot_mismatch_rate(self) -> float:
        """Unexplained mismatch rate -- the number the plan targets at zero.

        A snapshot that disagrees with our book *after a known message loss* is
        evidence the gap detector works, not evidence the delta application is
        wrong, so it is counted separately.
        """
        return self.snapshot_diffs / self.snapshot_checks if self.snapshot_checks else 0.0

    def feed_delay_summary(self) -> dict:
        """Percentiles of exchange-to-local delay, or an explicit absence.

        Snapshots carry no exchange timestamp, so only deltas contribute. When
        nothing does, the figures are omitted rather than reported as zero.
        """
        if not self.feed_delay_ms:
            return {"feed_delay_samples": 0}
        s = sorted(self.feed_delay_ms)
        pick = lambda q: s[min(len(s) - 1, int(q * len(s)))]
        return {
            "feed_delay_samples": len(s),
            "feed_delay_ms_p50": pick(0.50),
            "feed_delay_ms_p99": pick(0.99),
        }

    def to_dict(self) -> dict:
        return {
            "records": self.records,
            "deltas": self.deltas,
            "snapshots": self.snapshots,
            "trades": self.trades,
            "sequence_gaps": self.gaps,
            "reconnects": self.reconnects,
            "n0_violations": self.n0_violations,
            "out_of_order_records": self.out_of_order,
            "snapshot_checks": self.snapshot_checks,
            "snapshot_mismatches_unexplained": self.snapshot_diffs,
            "snapshot_mismatches_after_gap": self.snapshot_diffs_after_gap,
            "snapshot_mismatch_rate": self.snapshot_mismatch_rate,
            "untrusted_ms": self.untrusted_ms,
            "duration_ms": self.duration_ms,
            "span_ms": self.span_ms,
            "idle_gaps": self.idle_gaps,
            **self.feed_delay_summary(),
            "first_ts_ms": self.first_ts_ms,
            "last_ts_ms": self.last_ts_ms,
        }


class Listener(Protocol):
    """Anything that wants to observe the replay as it advances."""

    def on_tick(self, engine: "ReplayEngine", ts_ms: int, ticker: str) -> None: ...

    def on_time(self, engine: "ReplayEngine", ts_ms: int) -> None: ...

    def on_finish(self, engine: "ReplayEngine", ts_ms: int) -> None: ...


def clock_ms(rec: dict) -> int:
    """The engine's clock for a record: local receipt time.

    Not the exchange timestamp. A live tape carries exchange time on deltas and
    nothing on snapshots, so the recorder stamps snapshots with receipt time --
    two clocks in one field, separated by the feed delay. Ordering on that mix
    puts deltas ahead of the snapshots that must precede them, which leaves every
    book uninitialised and every later message looking like a gap.

    Receipt time is observed for every record, so it is the only clock that can
    order all of them consistently. Exchange time survives in ``ts`` for
    measuring feed delay, which is what it is actually good for.
    """
    rn = rec.get("rn")
    if rn:
        return rn // 1_000_000
    return rec.get("ts", 0)


def ordered_records(
    records: Iterable[dict], window: int = 4_096, stats: ReplayStats | None = None
) -> Iterator[dict]:
    """Emit records in receipt order through a bounded heap.

    The tape is written in arrival order, which is *almost* receipt-time order; a
    small window removes the jitter without loading the whole tape into memory.
    """
    heap: list[tuple[int, int, str, int, dict]] = []
    counter = 0
    last_emitted = -1
    for rec in records:
        ts = clock_ms(rec)
        counter += 1
        heapq.heappush(heap, (ts, rec.get("seq", 0), rec.get("m", rec.get("e", "")), counter, rec))
        if len(heap) > window:
            ts_out, _, _, _, out = heapq.heappop(heap)
            if stats is not None and ts_out < last_emitted:
                stats.out_of_order += 1
            last_emitted = max(last_emitted, ts_out)
            yield out
    while heap:
        ts_out, _, _, _, out = heapq.heappop(heap)
        if stats is not None and ts_out < last_emitted:
            stats.out_of_order += 1
        last_emitted = max(last_emitted, ts_out)
        yield out


class ReplayEngine:
    """Owns the books, the clock, and the trust state of every market."""

    def __init__(
        self,
        events: Sequence[Event],
        cfg: ReplayConfig | None = None,
    ):
        self.cfg = cfg or ReplayConfig()
        self.events = list(events)
        self.event_by_ticker: dict[str, Event] = {}
        self.books: dict[str, MarketBook] = {}
        self.market_meta: dict[str, Any] = {}
        for ev in self.events:
            for m in ev.markets:
                self.books[m.ticker] = MarketBook(m.ticker, m.grid)
                self.event_by_ticker[m.ticker] = ev
                self.market_meta[m.ticker] = m
        self.stats = ReplayStats()
        self.now_ms: int = 0
        #: Next expected sequence number, keyed by subscription id. Kalshi
        #: scopes `seq` to a subscription, not to a market, so continuity is a
        #: property of the stream rather than of any one book.
        self._expected_seq: dict[int, int] = {}
        #: Markets observed on each subscription, so a gap can invalidate every
        #: book the lost messages might have belonged to.
        self._sid_tickers: dict[int, set[str]] = {}
        self._untrusted_since: dict[str, int] = {}
        self._gap_since_snapshot: set[str] = set()
        self._event_status: dict[str, str] = {ev.event_ticker: "active" for ev in self.events}

    # -- helpers -----------------------------------------------------------
    def book(self, ticker: str) -> MarketBook:
        return self.books[ticker]

    def event_of(self, ticker: str) -> Event:
        return self.event_by_ticker[ticker]

    def books_for(self, event: Event) -> dict[str, MarketBook]:
        return {t: self.books[t] for t in event.tickers if t in self.books}

    def is_trusted(self, event: Event) -> bool:
        return all(self.books[t].trusted for t in event.tickers if t in self.books)

    def is_stale(self, event: Event, ts_ms: int) -> bool:
        newest = 0
        for t in event.tickers:
            bk = self.books.get(t)
            if bk is None or bk.last_ts_ms == 0:
                return True                      # never seen: no book to trade
            if ts_ms - bk.last_ts_ms > self.cfg.stale_leg_ms:
                return True                      # this leg has gone dark
            newest = max(newest, bk.last_ts_ms)
        return ts_ms - newest > self.cfg.stale_feed_ms

    def phase(self, event: Event, ts_ms: int) -> str:
        """Lifecycle phase, used to quarantine phantom-liquidity signals."""
        if self._event_status.get(event.event_ticker) not in ("active", ""):
            return "post_close"
        closes = [m.close_ts_ms for m in event.markets if m.close_ts_ms]
        if not closes:
            return "active"
        close = min(closes)
        if ts_ms >= close:
            return "post_close"
        if ts_ms >= close - self.cfg.pre_close_window_ms:
            return "pre_close"
        return "active"

    def tradable(self, event: Event, ts_ms: int) -> bool:
        """Every gate that must pass before an opportunity can be actionable."""
        if not self.is_trusted(event):
            return False
        if self.phase(event, ts_ms) != "active":
            return False
        if self.is_stale(event, ts_ms):
            return False
        return all(m.is_tradable for m in event.markets)

    # -- record handling ---------------------------------------------------
    def _mark_untrusted(self, ticker: str, ts_ms: int, reason: str) -> None:
        bk = self.books[ticker]
        if bk.trusted:
            self._untrusted_since[ticker] = ts_ms
        bk.mark_untrusted(reason)

    def _restore_trust(self, ticker: str, ts_ms: int) -> None:
        since = self._untrusted_since.pop(ticker, None)
        if since is not None:
            self.stats.untrusted_ms += max(0, ts_ms - since)

    def _note_gap(self, sid: int, ticker: str, ts_ms: int, expected: int, got: int) -> None:
        """A lost message taints every market on its subscription.

        When the subscription membership is not yet known, taint everything:
        the lost message could have belonged to a market we have not seen, and
        assuming otherwise is how a delta gets applied across a hole.
        """
        self.stats.gaps += 1
        known = self._sid_tickers.get(sid)
        affected = known if known else set(self.books)
        for t in set(affected) | {ticker}:
            if t in self.books:
                self._gap_since_snapshot.add(t)
                self._mark_untrusted(
                    t, ts_ms, f"seq gap on sid {sid}: expected {expected}, got {got}")

    def _apply_snapshot(self, rec: dict) -> str | None:
        ticker = rec["m"]
        bk = self.books.get(ticker)
        if bk is None:
            return None
        yes = [(int(p), int(q)) for p, q in rec.get("y", [])]
        no = [(int(p), int(q)) for p, q in rec.get("n", [])]
        # A snapshot carries its own seq, so a discontinuity here reveals message
        # loss that no delta has surfaced yet. Without this check a gap followed
        # immediately by a snapshot would be misfiled as a reconstruction error.
        sid = rec.get("sid", 0)
        self._sid_tickers.setdefault(sid, set()).add(ticker)
        expected = self._expected_seq.get(sid)
        snap_seq = rec.get("seq", 0)
        if expected is not None and snap_seq != expected:
            self._note_gap(sid, ticker, clock_ms(rec), expected, snap_seq)
        # Diff whenever there is a prior book to compare against, not only when
        # it is trusted. Sequence numbers are scoped to a subscription, so a
        # single lost message untrusts every market on it -- gating the check on
        # trust meant almost no snapshot was ever compared, and the strongest
        # evidence that delta application is correct quietly disappeared.
        #
        # Trust still decides how a mismatch is *classified*: only a book with no
        # known loss behind it can produce an unexplained mismatch.
        if self.cfg.snapshot_diff and bk.seq:
            self.stats.snapshot_checks += 1
            if bk.snapshot_levels(YES) != sorted(yes) or bk.snapshot_levels(NO) != sorted(no):
                if bk.trusted and ticker not in self._gap_since_snapshot:
                    self.stats.snapshot_diffs += 1
                else:
                    self.stats.snapshot_diffs_after_gap += 1
        bk.apply_snapshot(yes, no, rec.get("seq", 0), clock_ms(rec), rec.get("rn", 0))
        self._expected_seq[sid] = rec.get("seq", 0) + 1
        self._gap_since_snapshot.discard(ticker)
        self._restore_trust(ticker, rec["ts"])
        self.stats.snapshots += 1
        return ticker

    def _apply_delta(self, rec: dict) -> str | None:
        ticker = rec["m"]
        bk = self.books.get(ticker)
        if bk is None:
            return None
        seq = rec.get("seq", 0)
        sid = rec.get("sid", 0)
        self._sid_tickers.setdefault(sid, set()).add(ticker)
        expected = self._expected_seq.get(sid)
        if expected is not None and seq != expected:
            self._note_gap(sid, ticker, clock_ms(rec), expected, seq)
            self._expected_seq[sid] = seq + 1
            return None
        self._expected_seq[sid] = seq + 1
        if not bk.trusted:
            return None                      # awaiting a snapshot to repair state
        try:
            bk.apply_delta(side_index(rec["s"]), int(rec["p"]), int(rec["d"]), seq,
                           clock_ms(rec), rec.get("rn", 0))
        except BookIntegrityError:
            self.stats.n0_violations += 1
            self._mark_untrusted(ticker, clock_ms(rec), "negative resting size")
            if self.cfg.strict_n0:
                raise
            return None
        self.stats.deltas += 1
        if bk.n0_violated():
            self.stats.n0_violations += 1
            if self.cfg.strict_n0:
                bk.check_n0()
            self._mark_untrusted(ticker, clock_ms(rec), "N0 violated")
            return None
        return ticker

    # -- main loop ---------------------------------------------------------
    def run(
        self,
        tape_path: str | Path,
        listeners: Sequence[Listener] = (),
        *,
        progress: Callable[[int], None] | None = None,
    ) -> ReplayStats:
        stats = self.stats
        source = ordered_records(read_tape(tape_path), self.cfg.reorder_window, stats)
        for rec in source:
            kind = rec.get("type")
            if kind == META:
                continue
            ts = clock_ms(rec)
            exch = rec.get("ts")
            if exch and rec.get("rn"):
                delay = ts - exch
                if 0 <= delay < 60_000:
                    stats.feed_delay_ms.append(delay)
            if ts:
                self.now_ms = ts
                if not stats.first_ts_ms:
                    stats.first_ts_ms = ts
                else:
                    step = ts - stats.last_ts_ms
                    if 0 <= step <= self.cfg.coverage_gap_ms:
                        stats.coverage_ms += step
                    elif step > self.cfg.coverage_gap_ms:
                        # A stretch with no messages is not an observation of a
                        # quiet market; it is an absence of observation.
                        stats.idle_gaps += 1
                stats.last_ts_ms = ts
            stats.records += 1

            touched: str | None = None
            if kind == DELTA:
                touched = self._apply_delta(rec)
            elif kind == SNAPSHOT:
                touched = self._apply_snapshot(rec)
            elif kind == TRADE:
                stats.trades += 1
            elif kind == GAP:
                # The recorder detected this hole and wrote it down. Honour its
                # blast radius, then adopt its sequence position so the next
                # message is not counted as a second, phantom gap.
                sid = rec.get("sid", 0)
                self._note_gap(sid, rec["m"], ts,
                               rec.get("expected", 0), rec.get("got", 0))
                # Drop the expectation rather than guessing the next number. The
                # marker says the stream is broken here; the following message
                # re-establishes the position without inventing a second gap.
                self._expected_seq.pop(sid, None)
            elif kind == RECONNECT:
                # The feed restarted, so sequence numbers restart too. Reset the
                # expectations rather than reading the discontinuity as loss --
                # but every book is genuinely stale until its snapshot arrives.
                stats.reconnects += 1
                self._expected_seq.clear()
                for sid, tickers in self._sid_tickers.items():
                    for t in tickers:
                        if t in self.books:
                            self._gap_since_snapshot.add(t)
                            self._mark_untrusted(t, ts, "feed reconnected")
                self._sid_tickers.clear()
            elif kind == "status":
                self._event_status[rec["e"]] = rec.get("status", "closed")
                ev = next((e for e in self.events if e.event_ticker == rec["e"]), None)
                if ev is not None:
                    for m in ev.markets:
                        m.status = rec.get("status", "closed")

            for lis in listeners:
                lis.on_time(self, ts)
            if touched is not None:
                stats.ticks_by_market[touched] = stats.ticks_by_market.get(touched, 0) + 1
                for lis in listeners:
                    lis.on_tick(self, ts, touched)
            if progress is not None and stats.records % 250_000 == 0:
                progress(stats.records)

        for ticker in list(self._untrusted_since):
            self._restore_trust(ticker, stats.last_ts_ms)
        for lis in listeners:
            lis.on_finish(self, stats.last_ts_ms)
        return stats


class NullListener:
    """Base class so listeners only implement the hooks they care about."""

    def on_tick(self, engine: ReplayEngine, ts_ms: int, ticker: str) -> None:
        pass

    def on_time(self, engine: ReplayEngine, ts_ms: int) -> None:
        pass

    def on_finish(self, engine: ReplayEngine, ts_ms: int) -> None:
        pass
