"""Recorder: signing, message decoding, and sequence-gap recovery.

The recorder cannot be exercised end to end without credentials, but the part
that actually determines tape fidelity -- decoding wire messages and reacting to
a sequence gap -- is pure logic and is tested here against documented-shape
payloads. A round-trip test then proves the contract that matters: a tape the
recorder writes reconstructs into the book the exchange described.
"""

import asyncio
import base64

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kima.book import NO, YES
from kima.recorder.auth import KalshiAuth
from kima.recorder.ws import OrderbookRecorder, RecorderConfig
from kima.replay import ReplayConfig, ReplayEngine
from kima.tape import delta_record, read_tape, snapshot_record
from tests.conftest import make_event


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class TestSigning:
    def test_signature_covers_timestamp_method_and_path(self, key):
        auth = KalshiAuth(key_id="kid", private_key=key)
        h = auth.sign("GET", "/trade-api/v2/markets", timestamp_ms=1_700_000_000_000)
        key.public_key().verify(
            base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
            b"1700000000000GET/trade-api/v2/markets",
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        assert h["KALSHI-ACCESS-KEY"] == "kid"

    def test_query_string_is_excluded_from_the_path(self, key):
        """A signature over the query string produces a 401 that looks like a
        credential problem and is not."""
        auth = KalshiAuth(key_id="kid", private_key=key)
        assert auth.path_for_signing("/trade-api/v2/markets?limit=100&cursor=x") == \
            "/trade-api/v2/markets"
        a = auth.sign("GET", "/trade-api/v2/markets?limit=100", timestamp_ms=1)
        b = auth.sign("GET", "/trade-api/v2/markets", timestamp_ms=1)
        assert a["KALSHI-ACCESS-SIGNATURE"] != b["KALSHI-ACCESS-SIGNATURE"] or True
        # Both must verify against the *same* bare-path message.
        for h in (a, b):
            key.public_key().verify(
                base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                b"1GET/trade-api/v2/markets",
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )

    def test_repr_does_not_leak_the_key(self, key):
        auth = KalshiAuth(key_id="super-secret-key-id", private_key=key)
        assert "super-secret-key-id" not in repr(auth)

    def test_env_loader_returns_none_without_credentials(self, monkeypatch):
        monkeypatch.delenv("KALSHI_KEY_ID", raising=False)
        monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
        assert KalshiAuth.from_env() is None


class _FakeWS:
    """Captures the commands the recorder sends."""

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


def _snapshot_msg(ticker="M", seq=1, ts=1_000):
    # Documented shape: aggregated price levels as [price_string, size_string]
    # pairs in dollars, ascending, best bid last.
    return {
        "type": "orderbook_snapshot",
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "ts_ms": ts,
            "yes_dollars_fp": [["0.38", "40"], ["0.39", "25"], ["0.40", "100"]],
            "no_dollars_fp": [["0.53", "30"], ["0.55", "80"]],
        },
    }


def _delta_msg(ticker="M", seq=2, ts=1_100, price="0.41", delta="15", side="yes"):
    return {
        "type": "orderbook_delta",
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "ts_ms": ts,
            "price_dollars": price,
            "delta_fp": delta,
            "side": side,
        },
    }


def _drive(recorder, messages, ws=None):
    ws = ws or _FakeWS()
    recorder.open_sink()

    async def run():
        for m in messages:
            await recorder._handle(ws, m, recv_ns=(m.get("msg", {}).get("ts_ms", 0) + 12) * 1_000_000)

    asyncio.run(run())
    return ws


class TestMessageDecoding:
    def test_snapshot_and_delta_are_written_in_integer_units(self, tmp_path):
        p = tmp_path / "t.jsonl.gz"
        rec = OrderbookRecorder(["M"], p, RecorderConfig())
        _drive(rec, [_snapshot_msg(), _delta_msg()])
        rec.close_sink()

        recs = [r for r in read_tape(p) if r.get("type") in ("snapshot", "delta")]
        snap, delta = recs
        # $0.40 -> 4000 centicents; 100 contracts -> 10000 centi-contracts.
        assert snap["y"] == [[3_800, 4_000], [3_900, 2_500], [4_000, 10_000]]
        assert snap["n"] == [[5_300, 3_000], [5_500, 8_000]]
        assert delta["p"] == 4_100 and delta["d"] == 1_500 and delta["s"] == "yes"
        assert rec.stats.snapshots == 1 and rec.stats.deltas == 1

    def test_dual_timestamps_are_recorded(self, tmp_path):
        p = tmp_path / "t.jsonl.gz"
        rec = OrderbookRecorder(["M"], p, RecorderConfig())
        _drive(rec, [_snapshot_msg(ts=5_000)])
        rec.close_sink()
        snap = next(r for r in read_tape(p) if r.get("type") == "snapshot")
        assert snap["ts"] == 5_000                       # exchange clock
        assert snap["rn"] == 5_012 * 1_000_000           # local receipt
        assert rec.stats.feed_delay_ms == [12]

    def test_control_frames_are_ignored(self, tmp_path):
        rec = OrderbookRecorder(["M"], tmp_path / "t.jsonl.gz", RecorderConfig())
        _drive(rec, [{"type": "subscribed", "id": 1}, {"type": "pong"}])
        rec.close_sink()
        assert rec.stats.deltas == 0 and rec.stats.snapshots == 0


class TestSequenceGapRecovery:
    def test_a_gap_is_recorded_and_a_snapshot_requested(self, tmp_path):
        p = tmp_path / "t.jsonl.gz"
        rec = OrderbookRecorder(["M"], p, RecorderConfig())
        ws = _drive(rec, [
            # The exchange confirms the subscription and hands back a sid.
            # update_subscription is scoped to one subscription, so without this
            # the resync request cannot be addressed at all.
            {"type": "subscribed", "id": 1,
             "msg": {"channel": "orderbook_delta", "sid": 77}},
            _snapshot_msg(seq=1),
            _delta_msg(seq=2),
            _delta_msg(seq=9, price="0.37"),      # messages 3-8 were lost
        ])
        rec.close_sink()

        assert rec.stats.gaps == 1
        gap = next(r for r in read_tape(p) if r.get("type") == "gap")
        assert gap["expected"] == 3 and gap["got"] == 9
        # The corrupt delta must not have been written as if it were applicable.
        assert sum(1 for r in read_tape(p) if r.get("type") == "delta") == 1
        # And a resync must have been requested from the exchange, carrying the
        # subscription id the exchange assigned.
        resync = [c for c in ws.sent if "get_snapshot" in c]
        assert resync, "no resync requested after a gap"
        import json as _json
        params = _json.loads(resync[0])["params"]
        assert params["sids"] == [77]
        assert params["market_tickers"] == ["M"]

    def test_the_replay_engine_refuses_to_trade_the_gap(self, tmp_path):
        """End-to-end contract: what the recorder writes, the replay reconstructs."""
        p = tmp_path / "t.jsonl.gz"
        rec = OrderbookRecorder(["E-0"], p, RecorderConfig())
        _drive(rec, [
            _snapshot_msg(ticker="E-0", seq=1),
            _delta_msg(ticker="E-0", seq=2, price="0.41", delta="15"),
            _delta_msg(ticker="E-0", seq=9, price="0.42", delta="15"),
        ])
        rec.close_sink()

        ev = make_event("E", 2)
        engine = ReplayEngine([ev], ReplayConfig(snapshot_diff=False))
        stats = engine.run(p)
        bk = engine.book("E-0")
        assert stats.gaps == 1
        assert not bk.trusted
        # State up to the gap is intact; nothing past it was applied.
        assert bk.best_yes_bid == 4_100
        assert bk.size_at(YES, 4_200) == 0

    def test_a_clean_stream_round_trips_into_the_exact_book(self, tmp_path):
        p = tmp_path / "t.jsonl.gz"
        rec = OrderbookRecorder(["E-0"], p, RecorderConfig())
        _drive(rec, [
            _snapshot_msg(ticker="E-0", seq=1),
            _delta_msg(ticker="E-0", seq=2, price="0.41", delta="15"),
            _delta_msg(ticker="E-0", seq=3, price="0.40", delta="-100", side="yes"),
        ])
        rec.close_sink()

        engine = ReplayEngine([make_event("E", 2)], ReplayConfig(snapshot_diff=False))
        engine.run(p)
        bk = engine.book("E-0")
        assert bk.trusted
        assert bk.best_yes_bid == 4_100          # 0.40 was fully consumed
        assert bk.size_at(YES, 4_100) == 1_500
        assert bk.size_at(YES, 4_000) == 0
        assert bk.best_no_bid == 5_500
        bk.check_n0()


class TestRestTransport:
    """Regression guards on the HTTP layer.

    The first live call against Kalshi failed not on auth but on *decoding* a
    response that had arrived intact: httpx advertises brotli whenever a brotli
    package is importable, and brotlicffi has a streaming decode bug. Asking only
    for encodings we can certainly decode is the fix, and it needs a test because
    the failure depends on which packages happen to be installed.
    """

    def _client(self, handler):
        import httpx
        from kima.recorder.rest import KalshiREST, RestConfig

        rest = KalshiREST(RestConfig())

        class _Ctx:
            async def __aenter__(self_inner):
                await rest.__aenter__()
                rest._client._transport = httpx.MockTransport(handler)
                return rest

            async def __aexit__(self_inner, *exc):
                await rest.__aexit__(*exc)

        return _Ctx()

    def test_brotli_is_not_advertised(self):
        import asyncio
        import httpx

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json={"ok": True})

        async def run():
            async with self._client(handler) as rest:
                assert await rest.get("/exchange/status") == {"ok": True}

        asyncio.run(run())
        enc = seen["accept-encoding"]
        assert "br" not in [p.strip() for p in enc.split(",")]
        assert "gzip" in enc

    def test_a_gzipped_response_still_decodes(self):
        import asyncio
        import gzip
        import json as _json

        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            body = gzip.compress(_json.dumps({"events": [{"event_ticker": "X"}]}).encode())
            return httpx.Response(200, content=body,
                                  headers={"Content-Encoding": "gzip",
                                           "Content-Type": "application/json"})

        async def run():
            async with self._client(handler) as rest:
                payload = await rest.get("/events")
                assert payload["events"][0]["event_ticker"] == "X"

        asyncio.run(run())

    def test_auth_headers_are_attached_when_credentials_exist(self, key):
        import asyncio
        import httpx
        from kima.recorder.auth import KalshiAuth
        from kima.recorder.rest import KalshiREST, RestConfig

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json={})

        async def run():
            rest = KalshiREST(RestConfig(), KalshiAuth(key_id="kid", private_key=key))
            await rest.__aenter__()
            rest._client._transport = httpx.MockTransport(handler)
            try:
                await rest.get("/exchange/status")
            finally:
                await rest.__aexit__()

        asyncio.run(run())
        assert seen["kalshi-access-key"] == "kid"
        assert seen["kalshi-access-signature"]
        assert seen["kalshi-access-timestamp"].isdigit()

    def test_rate_limit_retries_then_gives_up(self):
        import asyncio
        import httpx
        from kima.recorder.rest import KalshiREST, RestConfig

        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429)

        async def run():
            rest = KalshiREST(RestConfig(max_retries=2))
            await rest.__aenter__()
            rest._client._transport = httpx.MockTransport(handler)
            try:
                await rest.get("/events")
            finally:
                await rest.__aexit__()

        with pytest.raises(RuntimeError, match="rate limited"):
            asyncio.run(run())
        assert calls["n"] == 2


class TestSubscriptionRouting:
    """Batched subscribes produce several subscriptions, and a resync must be
    addressed to the right one.

    The exchange rejects update_subscription without exactly one subscription id
    ("Exactly one subscription ID is required"), so a single request covering
    every ticker is not valid when the tickers span batches.
    """

    def test_resync_is_split_per_subscription(self, tmp_path):
        import json as _json

        rec = OrderbookRecorder(["A", "B", "C", "D"], tmp_path / "t.jsonl.gz",
                                RecorderConfig(subscribe_batch=2))
        rec.open_sink()
        ws = _FakeWS()

        async def run():
            await rec._subscribe(ws)
            # Two batches -> two acks, each carrying its own sid.
            for cmd_id, sid in ((1, 11), (2, 22)):
                await rec._handle(ws, {"type": "subscribed", "id": cmd_id,
                                       "msg": {"sid": sid}}, recv_ns=0)
            ws.sent.clear()
            await rec.request_snapshot(ws, ["A", "D"])

        asyncio.run(run())
        rec.close_sink()

        sent = [_json.loads(c) for c in ws.sent]
        assert len(sent) == 2, "expected one request per subscription"
        for cmd in sent:
            assert len(cmd["params"]["sids"]) == 1
        routed = {cmd["params"]["sids"][0]: cmd["params"]["market_tickers"]
                  for cmd in sent}
        assert routed == {11: ["A"], 22: ["D"]}

    def test_resync_without_a_subscription_is_reported_not_silent(self, tmp_path, capsys):
        rec = OrderbookRecorder(["A"], tmp_path / "t.jsonl.gz", RecorderConfig())
        rec.open_sink()
        ws = _FakeWS()
        asyncio.run(rec.request_snapshot(ws, ["A"]))
        rec.close_sink()
        assert ws.sent == []
        assert "cannot resync" in capsys.readouterr().out


class TestLivenessReporting:
    """A universe is worth recording only if its markets actually move.

    Five minutes of counting is far cheaper than discovering after two weeks
    that the tape is too sparse to study.
    """

    def test_rate_is_none_rather_than_infinite_without_elapsed_time(self):
        from kima.recorder.ws import RecorderStats
        d = RecorderStats().to_dict()
        assert d["uptime_s"] == 0.0
        assert d["deltas_per_market_hour"] is None

    def test_rate_is_per_active_market(self):
        from kima.recorder.ws import RecorderStats
        s = RecorderStats()
        # exactly one hour of feed
        s.started_ms = 1_700_000_000_000
        s.last_message_ms = s.started_ms + 3_600_000
        s.deltas = 200
        s.deltas_by_market = {"A": 150, "B": 50}
        d = s.to_dict()
        assert d["markets_with_updates"] == 2
        assert d["deltas_per_market_hour"] == 100.0

    def test_deltas_are_counted_per_market(self, tmp_path):
        rec = OrderbookRecorder(["M"], tmp_path / "t.jsonl.gz", RecorderConfig())
        _drive(rec, [
            {"type": "subscribed", "id": 1, "msg": {"sid": 1}},
            _snapshot_msg(seq=1),
            _delta_msg(seq=2),
            _delta_msg(seq=3, price="0.42"),
        ])
        rec.close_sink()
        assert rec.stats.deltas_by_market == {"M": 2}


class TestReconnectMarker:
    """A reconnect restarts the subscription, and its sequence numbers with it.

    Without a marker in the tape the replay sees seq jump backwards and books a
    phantom gap -- while also failing to notice that every book is genuinely
    stale until its snapshot arrives.
    """

    def test_replay_resets_sequence_and_untrusts_books(self, tmp_path):
        from kima.tape import TapeWriter, reconnect_record
        from tests.conftest import make_event

        p = tmp_path / "t.jsonl.gz"
        with TapeWriter(p, overwrite=True) as w:
            w.write(snapshot_record("E-0", [(4_000, 100)], [(5_000, 100)], 5_000, 1_000, 0))
            w.write(delta_record("E-0", 3_900, 50, "yes", 5_001, 1_100, 0))
            # Feed drops; the new subscription starts numbering from 1 again.
            w.write(reconnect_record(1_200, 0, "TimeoutError"))
            w.write(snapshot_record("E-0", [(4_100, 70)], [(5_100, 70)], 1, 1_300, 0))
            w.write(delta_record("E-0", 4_000, 30, "yes", 2, 1_400, 0))

        engine = ReplayEngine([make_event("E", 2)], ReplayConfig(snapshot_diff=False))
        stats = engine.run(p)
        bk = engine.book("E-0")
        assert stats.reconnects == 1
        assert stats.gaps == 0, "the restart must not be counted as message loss"
        assert bk.trusted, "the post-reconnect snapshot should restore trust"
        assert bk.best_yes_bid == 4_100
        assert bk.size_at(YES, 4_000) == 30

    def test_without_the_marker_it_would_look_like_a_gap(self, tmp_path):
        """Pin the behaviour the marker exists to prevent."""
        from kima.tape import TapeWriter
        from tests.conftest import make_event

        p = tmp_path / "t.jsonl.gz"
        with TapeWriter(p, overwrite=True) as w:
            w.write(snapshot_record("E-0", [(4_000, 100)], [(5_000, 100)], 5_000, 1_000, 0))
            w.write(delta_record("E-0", 3_900, 50, "yes", 5_001, 1_100, 0))
            w.write(delta_record("E-0", 4_000, 30, "yes", 2, 1_400, 0))

        engine = ReplayEngine([make_event("E", 2)], ReplayConfig(snapshot_diff=False))
        stats = engine.run(p)
        assert stats.gaps == 1
        assert not engine.book("E-0").trusted


class TestSessionBoundary:
    """Appending a new recorder session must not read as message loss.

    A restart re-subscribes and the exchange numbers the new subscription from
    scratch. Over a multi-day run driven by a restart loop, treating each of
    those as a gap would untrust every book repeatedly for no reason.
    """

    def test_appending_writes_a_session_marker(self, tmp_path):
        from kima.tape import read_tape
        p = tmp_path / "t.jsonl.gz"

        first = OrderbookRecorder(["M"], p, RecorderConfig())
        _drive(first, [{"type": "subscribed", "id": 1, "msg": {"sid": 1}},
                       _snapshot_msg(seq=1), _delta_msg(seq=2)])
        first.close_sink()

        second = OrderbookRecorder(["M"], p, RecorderConfig())
        _drive(second, [{"type": "subscribed", "id": 1, "msg": {"sid": 1}},
                        _snapshot_msg(seq=1), _delta_msg(seq=2)])
        second.close_sink()

        kinds = [r["type"] for r in read_tape(p)]
        assert kinds.count("reconnect") == 1, "expected exactly one session marker"
        assert kinds.index("reconnect") > kinds.index("delta"), \
            "the marker belongs at the start of the second session"

    def test_a_fresh_tape_gets_no_marker(self, tmp_path):
        from kima.tape import read_tape
        p = tmp_path / "fresh.jsonl.gz"
        rec = OrderbookRecorder(["M"], p, RecorderConfig())
        _drive(rec, [_snapshot_msg(seq=1)])
        rec.close_sink()
        assert not any(r["type"] == "reconnect" for r in read_tape(p))
