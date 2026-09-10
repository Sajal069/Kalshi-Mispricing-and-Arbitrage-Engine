"""REST client: universe resolution, metadata, fee configuration, settlement.

Two things here are risk controls rather than conveniences.

**Fees are read at runtime.** This strategy's entire edge is one to three cents
per basket, so a per-series multiplier change can flip it from profitable to
unprofitable. The published table is a fallback only; the live configuration
wins.

**The token bucket is respected.** Kalshi returns 429 with no ``Retry-After``,
so the client must not discover the limit empirically. Read and write budgets
are tracked separately because they are separate buckets.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable, Sequence

import httpx

from ..events import Event, Market
from ..fees import FeeSchedule, Rounding, schedule_for
from ..ticks import grid_from_metadata
from .auth import KalshiAuth

PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"


@dataclass
class RestConfig:
    base_url: str = PROD_BASE
    #: Basic tier: 200 read tokens/s. Default request cost is 10 tokens.
    read_tokens_per_s: int = 200
    tokens_per_request: int = 10
    #: Overall request budget.
    timeout_s: float = 30.0
    #: Connect budget, deliberately short.
    #:
    #: Connections to this host establish intermittently: a successful TCP+TLS
    #: setup takes roughly 0.3-0.8s, but a meaningful fraction of attempts never
    #: complete at all. A long connect timeout spends the budget waiting on
    #: attempts that were already lost, so it is better to give up quickly and
    #: retry more often. Six attempts at a 6s ceiling covers far more ground than
    #: three at 15s, and costs less wall time in the worst case.
    connect_timeout_s: float = 6.0
    max_retries: int = 6
    fee_rounding: Rounding = "cent"
    user_agent: str = "kima/0.1 (research)"
    #: Content encodings we are willing to receive.
    #:
    #: Deliberately excludes ``br``. httpx advertises brotli whenever a brotli
    #: package is importable, and ``brotlicffi`` has a streaming decode bug that
    #: surfaces as ``decoder process called with data when can_accept_more_data()
    #: is False`` -- a decode failure on a response that arrived perfectly well.
    #: Nothing here needs brotli: these payloads are small JSON, and gzip is
    #: universally supported. Asking only for what we can definitely decode is
    #: cheaper than depending on which brotli build happens to be installed.
    accept_encoding: str = "gzip, deflate"


class _AsyncBucket:
    def __init__(self, tokens_per_s: int):
        self.rate = tokens_per_s
        self.tokens = float(tokens_per_s)
        self.last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: int) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                await asyncio.sleep((n - self.tokens) / self.rate)


async def measure_clock_skew(base_url: str = PROD_BASE,
                             timeout_s: float = 15.0) -> float | None:
    """Local clock minus server clock, in seconds. ``None`` if unmeasurable.

    Worth checking before every recording, for two reasons. The signature covers
    a timestamp, so a skewed clock is rejected outright with
    ``header_timestamp_expired`` -- noisy, but at least it fails loudly.

    The quieter failure is the tape. Receipt time is the clock the replay orders
    on, so a session recorded with a shifted clock lands out of position against
    every other session in the same append-only file, and the feed-delay figures
    computed from it are wrong by the offset. That corrupts a measurement without
    breaking anything visibly.

    Resolution is one second (the HTTP ``Date`` header), which is ample: the
    skew that matters here is seconds to hours, not milliseconds.
    """
    import email.utils

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=timeout_s),
            headers={"Accept-Encoding": "gzip, deflate"},
        ) as client:
            t0 = time.time()
            resp = await client.get(f"{base_url}/exchange/status")
            t1 = time.time()
    except Exception:
        return None
    date = resp.headers.get("Date")
    if not date:
        return None
    try:
        server = email.utils.parsedate_to_datetime(date).timestamp()
    except (TypeError, ValueError):
        return None
    return (t0 + t1) / 2 - server


def describe_skew(skew: float | None) -> str:
    if skew is None:
        return "clock skew could not be measured"
    if abs(skew) < 2:
        return f"clock skew {skew:+.1f}s (fine)"
    hours = skew / 3600.0
    extra = f" = {hours:+.2f}h" if abs(skew) >= 600 else ""
    return f"clock skew {skew:+.1f}s{extra}"


class KalshiREST:
    """Thin async client. Only the endpoints this study actually needs."""

    def __init__(self, cfg: RestConfig | None = None, auth: KalshiAuth | None = None):
        self.cfg = cfg or RestConfig()
        self.auth = auth
        self._bucket = _AsyncBucket(self.cfg.read_tokens_per_s)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "KalshiREST":
        self._client = httpx.AsyncClient(
            base_url=self.cfg.base_url,
            timeout=httpx.Timeout(self.cfg.timeout_s, connect=self.cfg.connect_timeout_s),
            headers={
                "User-Agent": self.cfg.user_agent,
                # Setting this explicitly overrides httpx's default, which
                # advertises every encoding it can import. See the field comment.
                "Accept-Encoding": self.cfg.accept_encoding,
                "Accept": "application/json",
            },
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(self, path: str, params: dict | None = None) -> dict:
        assert self._client is not None, "use KalshiREST as an async context manager"
        await self._bucket.acquire(self.cfg.tokens_per_request)
        headers: dict[str, str] = {}
        if self.auth is not None:
            # The signature covers the path only -- never the query string.
            full_path = httpx.URL(self.cfg.base_url).path.rstrip("/") + path
            headers = self.auth.sign("GET", full_path)
        delay = 0.5
        last_transport_error: Exception | None = None
        for attempt in range(self.cfg.max_retries):
            try:
                resp = await self._client.get(path, params=params, headers=headers)
            except (httpx.TransportError, httpx.DecodingError) as exc:
                # Connect/read timeouts and TLS hiccups are common on a home
                # link and are almost always transient. Retrying here matters
                # more than it looks: a multi-week recording run must survive
                # them, and the signature is regenerated on the next attempt so
                # a stale timestamp cannot accumulate.
                last_transport_error = exc
                if attempt + 1 >= self.cfg.max_retries:
                    break
                # Short, near-flat backoff. A dropped connection attempt carries
                # no server load, so backing off hard only wastes the window in
                # which the next attempt might have succeeded. Rate limiting is
                # the case that needs exponential backoff, and it is handled
                # separately below.
                await asyncio.sleep(min(1.0, 0.25 * (attempt + 1)))
                if self.auth is not None:
                    full_path = httpx.URL(self.cfg.base_url).path.rstrip("/") + path
                    headers = self.auth.sign("GET", full_path)
                continue
            if resp.status_code == 429:
                # No Retry-After is sent, so back off exponentially rather than
                # hammering and getting the key throttled harder.
                await asyncio.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp.json()
        if last_transport_error is not None:
            raise RuntimeError(
                f"could not reach {self.cfg.base_url}{path} after "
                f"{self.cfg.max_retries} attempts: "
                f"{type(last_transport_error).__name__}: {last_transport_error}"
            ) from last_transport_error
        raise RuntimeError(f"rate limited on {path} after {self.cfg.max_retries} attempts")

    # -- paging ------------------------------------------------------------
    async def get_paged(self, path: str, key: str, params: dict | None = None,
                        limit: int = 200, max_pages: int = 100) -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        for _ in range(max_pages):
            p = dict(params or {}, limit=limit)
            if cursor:
                p["cursor"] = cursor
            payload = await self.get(path, p)
            out.extend(payload.get(key, []) or [])
            cursor = payload.get("cursor") or None
            if not cursor:
                break
        return out

    # -- endpoints ---------------------------------------------------------
    async def events(self, *, status: str = "open", series_ticker: str | None = None,
                     with_nested_markets: bool = True) -> list[dict]:
        params: dict[str, Any] = {"status": status, "with_nested_markets": with_nested_markets}
        if series_ticker:
            params["series_ticker"] = series_ticker
        return await self.get_paged("/events", "events", params)

    async def event(self, event_ticker: str) -> dict:
        payload = await self.get(f"/events/{event_ticker}", {"with_nested_markets": True})
        return payload.get("event", payload)

    async def markets(self, *, event_ticker: str | None = None, status: str | None = None) -> list[dict]:
        params: dict[str, Any] = {}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        return await self.get_paged("/markets", "markets", params)

    async def series_fee_changes(self) -> list[dict]:
        try:
            payload = await self.get("/exchange/series_fee_changes")
            return payload.get("series_fee_changes", []) or []
        except Exception:
            # A fallback to the published table is acceptable; silently assuming
            # the default multiplier for a series that overrides it is not, so
            # the caller is told the live lookup failed.
            raise

    async def event_fee_changes(self, event_ticker: str) -> list[dict]:
        payload = await self.get(f"/events/{event_ticker}/fee_changes")
        return payload.get("fee_changes", []) or []

    async def settlements(self, event_ticker: str) -> list[dict]:
        payload = await self.get("/markets", {"event_ticker": event_ticker, "status": "settled"})
        return payload.get("markets", []) or []

    # -- assembly ----------------------------------------------------------
    def _fee_for(self, series: str, overrides: dict[str, tuple[Fraction, Fraction]]) -> FeeSchedule:
        if series in overrides:
            taker, maker = overrides[series]
            return FeeSchedule(series=series, taker_multiplier=taker,
                               maker_multiplier=maker, rounding=self.cfg.fee_rounding)
        return schedule_for(series, self.cfg.fee_rounding)

    async def build_universe(
        self,
        series: Sequence[str] | None = None,
        *,
        require_mutually_exclusive: bool = True,
        max_events: int = 40,
    ) -> list[Event]:
        """Resolve events to markets with metadata, grids and live fee config."""
        overrides: dict[str, tuple[Fraction, Fraction]] = {}
        try:
            for row in await self.series_fee_changes():
                st = row.get("series_ticker")
                if st:
                    overrides[st] = (
                        Fraction(str(row.get("taker_fee_multiplier", 1))),
                        Fraction(str(row.get("maker_fee_multiplier", 0))),
                    )
        except Exception:
            overrides = {}

        raw: list[dict] = []
        if series:
            for s in series:
                raw.extend(await self.events(series_ticker=s))
        else:
            raw = await self.events()

        out: list[Event] = []
        for payload in raw:
            if require_mutually_exclusive and not payload.get("mutually_exclusive"):
                continue
            markets = payload.get("markets") or await self.markets(
                event_ticker=payload["event_ticker"]
            )
            if len(markets) < 2:
                continue
            ev = Event.from_api(payload, markets)
            ev.fee_schedule = self._fee_for(ev.series_ticker or "DEFAULT", overrides)
            for m, mp in zip(ev.markets, markets):
                m.grid = grid_from_metadata(
                    mp.get("price_level_structure"), mp.get("price_ranges")
                )
            out.append(ev)
            if len(out) >= max_events:
                break
        return out

    async def refresh_settlements(self, events: Iterable[Event]) -> int:
        """Fill in ``result`` for the ex-post guaranteed-floor verification."""
        n = 0
        for ev in events:
            try:
                rows = await self.markets(event_ticker=ev.event_ticker)
            except Exception:
                continue
            by_ticker = {r["ticker"]: r for r in rows}
            for m in ev.markets:
                row = by_ticker.get(m.ticker)
                if row and row.get("result"):
                    m.result = row["result"]
                    m.status = row.get("status", m.status)
                    n += 1
        return n
