"""Tape storage: the append-only record of everything the engine saw.

Two tiers, per the data plan, and the separation is deliberate:

* **raw** -- gzipped JSONL of exactly what arrived, one record per line, never
  discarded and never analysed directly. This is the forensic artefact that
  makes the study reproducible.
* **normalised** -- a columnar table of decoded deltas for analysis.

Kalshi serves no historical order-book data at all, so this file is the only
copy of the microstructure that will ever exist for the recorded window. Every
design choice here favours durability over speed: line-oriented, append-only,
flushed on write, and readable with nothing more exotic than ``gzip``.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

TAPE_VERSION = 1

# Record types
SNAPSHOT = "snapshot"
DELTA = "delta"
TRADE = "trade"
TICKER = "ticker"
META = "meta"
GAP = "gap"
RECONNECT = "reconnect"
HEARTBEAT = "heartbeat"


@dataclass
class TapeHeader:
    version: int = TAPE_VERSION
    created_ms: int = 0
    source: str = "unknown"        # "kalshi-live" | "kalshi-demo" | "synthetic"
    note: str = ""

    def to_record(self) -> dict:
        d = asdict(self)
        d["type"] = META
        return d


class TapeWriter:
    """Append-only gzipped JSONL sink with size-based rotation."""

    def __init__(
        self,
        path: str | os.PathLike,
        *,
        source: str = "unknown",
        note: str = "",
        rotate_bytes: int = 512 * 1024 * 1024,
        compresslevel: int = 4,
        overwrite: bool = False,
    ):
        self.base = Path(path)
        self.base.parent.mkdir(parents=True, exist_ok=True)
        self.source = source
        self.note = note
        self.rotate_bytes = rotate_bytes
        self.compresslevel = compresslevel
        # Append is the right default for a live recorder -- a restart must never
        # truncate a tape, because Kalshi serves no order-book history and the
        # lost window is unrecoverable. Regenerating a synthetic tape is the one
        # case where truncation is intended, so it has to be asked for.
        self.overwrite = overwrite
        self.part = 0
        self.records = 0
        self._fh: gzip.GzipFile | None = None
        self._open()

    def _path_for(self, part: int) -> Path:
        if part == 0:
            return self.base
        stem = self.base.name
        if stem.endswith(".jsonl.gz"):
            stem = stem[: -len(".jsonl.gz")]
        return self.base.with_name(f"{stem}.part{part:03d}.jsonl.gz")

    def _open(self) -> None:
        p = self._path_for(self.part)
        mode = "wt" if self.overwrite else "at"
        self._fh = gzip.open(p, mode, encoding="utf-8", compresslevel=self.compresslevel)
        header = TapeHeader(
            created_ms=int(time.time() * 1000), source=self.source, note=self.note
        )
        self._fh.write(json.dumps(header.to_record(), separators=(",", ":")) + "\n")

    def write(self, record: dict[str, Any]) -> None:
        assert self._fh is not None
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.records += 1
        if self.records % 2048 == 0:
            self._fh.flush()
            if self._path_for(self.part).stat().st_size >= self.rotate_bytes:
                self.rotate()

    def write_many(self, records: Iterable[dict[str, Any]]) -> None:
        for r in records:
            self.write(r)

    def rotate(self) -> None:
        self.close()
        self.part += 1
        self._open()

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "TapeWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def paths(self) -> list[Path]:
        return [self._path_for(i) for i in range(self.part + 1)]


def _open_maybe_gzip(path: Path) -> io.TextIOBase:
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")  # type: ignore[return-value]
    return open(path, "r", encoding="utf-8")


def read_tape(path: str | os.PathLike) -> Iterator[dict[str, Any]]:
    """Yield every record from a tape file or a directory of tape parts."""
    p = Path(path)
    files: list[Path]
    if p.is_dir():
        files = sorted(p.glob("*.jsonl.gz")) + sorted(p.glob("*.jsonl"))
    else:
        files = [p]
        stem = p.name[: -len(".jsonl.gz")] if p.name.endswith(".jsonl.gz") else p.stem
        files += sorted(p.parent.glob(f"{stem}.part*.jsonl.gz"))
    seen: set[Path] = set()
    for f in files:
        if f in seen or not f.exists():
            continue
        seen.add(f)
        with _open_maybe_gzip(f) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)


def tape_header(path: str | os.PathLike) -> TapeHeader | None:
    for rec in read_tape(path):
        if rec.get("type") == META:
            return TapeHeader(
                version=rec.get("version", 0),
                created_ms=rec.get("created_ms", 0),
                source=rec.get("source", "unknown"),
                note=rec.get("note", ""),
            )
        break
    return None


# --------------------------------------------------------------------------
# record constructors -- one place where the wire schema is defined
# --------------------------------------------------------------------------
def snapshot_record(
    ticker: str, yes: list[tuple[int, int]], no: list[tuple[int, int]],
    seq: int, ts_ms: int, recv_ns: int, sid: int = 0,
) -> dict:
    """``sid`` is the subscription the message arrived on.

    Sequence numbers are scoped to a subscription rather than to a market, so
    the replay cannot check continuity without knowing which subscription a
    record belongs to.
    """
    return {
        "type": SNAPSHOT, "m": ticker, "seq": seq, "ts": ts_ms, "rn": recv_ns,
        "sid": sid, "y": yes, "n": no,
    }


def delta_record(
    ticker: str, price_cc: int, delta_cq: int, side: str,
    seq: int, ts_ms: int, recv_ns: int, sid: int = 0,
) -> dict:
    return {
        "type": DELTA, "m": ticker, "seq": seq, "ts": ts_ms, "rn": recv_ns,
        "sid": sid, "p": price_cc, "d": delta_cq, "s": side,
    }


def trade_record(ticker: str, price_cc: int, qty_cq: int, taker_side: str, ts_ms: int, recv_ns: int) -> dict:
    return {
        "type": TRADE, "m": ticker, "ts": ts_ms, "rn": recv_ns,
        "p": price_cc, "q": qty_cq, "s": taker_side,
    }


def gap_record(ticker: str, expected: int, got: int, ts_ms: int,
               recv_ns: int, sid: int = 0) -> dict:
    """A detected sequence gap.

    Carries ``seq`` deliberately: the replay orders records by ``(ts, seq)``, and
    a gap marker without one sorts to the front of its millisecond. It would then
    mark the book untrusted *before* the still-valid deltas that preceded the
    loss, silently discarding good state.
    """
    return {
        "type": GAP, "m": ticker, "seq": got, "expected": expected, "got": got,
        "ts": ts_ms, "rn": recv_ns, "sid": sid,
    }


def reconnect_record(ts_ms: int, recv_ns: int, reason: str = "") -> dict:
    """Marks a feed restart.

    Subscription sequence numbers restart with the subscription, so without this
    the replay sees seq jump backwards and books a phantom gap. It is also the
    honest record of a window in which messages were certainly missed.
    """
    return {"type": RECONNECT, "ts": ts_ms, "rn": recv_ns, "reason": reason}


# --------------------------------------------------------------------------
# normalised export
# --------------------------------------------------------------------------
def export_parquet(tape_path: str | os.PathLike, out_path: str | os.PathLike) -> int:
    """Decode a raw tape into a columnar table for analysis. Returns row count."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pyarrow is required for parquet export") from exc

    cols: dict[str, list] = {k: [] for k in ("ts", "recv_ns", "seq", "ticker", "price_cc", "delta_cq", "side")}
    n = 0
    for rec in read_tape(tape_path):
        if rec.get("type") != DELTA:
            continue
        cols["ts"].append(rec["ts"])
        cols["recv_ns"].append(rec.get("rn", 0))
        cols["seq"].append(rec.get("seq", 0))
        cols["ticker"].append(rec["m"])
        cols["price_cc"].append(rec["p"])
        cols["delta_cq"].append(rec["d"])
        cols["side"].append(rec["s"])
        n += 1
    table = pa.table(cols)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out_path, compression="zstd")
    return n


def save_json(obj: Any, path: str | os.PathLike) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def load_json(path: str | os.PathLike) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
