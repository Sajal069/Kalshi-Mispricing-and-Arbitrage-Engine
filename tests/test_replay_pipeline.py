"""Replay correctness and end-to-end pipeline invariants.

These run against a short synthetic tape generated in a temp directory, so the
suite stays hermetic and fast while still exercising the real code path from
tape bytes to P&L.
"""

from pathlib import Path

import pytest

from kima.backtest import BacktestConfig, load_universe, run_backtest
from kima.book import YES
from kima.execution import ExecConfig
from kima.replay import ReplayConfig, ReplayEngine, ordered_records
from kima.sim.synthetic import SimConfig, generate
from kima.tape import delta_record, read_tape, snapshot_record
from kima.validate import shuffle_tape, summarise_for_determinism


@pytest.fixture(scope="module")
def tape(tmp_path_factory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("tape")
    p = d / "t.jsonl.gz"
    generate(p, SimConfig(duration_ms=8 * 60_000, seed=99))
    return p, d / "t.universe.json"


class TestOrdering:
    def test_records_are_emitted_in_timestamp_order(self):
        recs = [
            {"type": "delta", "ts": 300, "seq": 3, "m": "A"},
            {"type": "delta", "ts": 100, "seq": 1, "m": "A"},
            {"type": "delta", "ts": 200, "seq": 2, "m": "A"},
        ]
        out = list(ordered_records(recs, window=8))
        assert [r["ts"] for r in out] == [100, 200, 300]

    def test_same_timestamp_breaks_ties_on_sequence(self):
        recs = [
            {"type": "delta", "ts": 100, "seq": 7, "m": "A"},
            {"type": "delta", "ts": 100, "seq": 5, "m": "A"},
        ]
        assert [r["seq"] for r in ordered_records(recs, window=8)] == [5, 7]


class TestSequenceGaps:
    def _engine(self):
        from tests.conftest import make_event
        ev = make_event("E", 2)
        return ReplayEngine([ev], ReplayConfig(snapshot_diff=False)), ev

    def test_a_gap_marks_the_book_untrusted(self, tmp_path):
        eng, ev = self._engine()
        p = tmp_path / "g.jsonl.gz"
        from kima.tape import TapeWriter
        with TapeWriter(p, overwrite=True) as w:
            w.write(snapshot_record("E-0", [(4_000, 100)], [(5_000, 100)], 1, 1_000, 0))
            w.write(delta_record("E-0", 3_900, 50, "yes", 2, 1_100, 0))
            w.write(delta_record("E-0", 3_800, 50, "yes", 9, 1_200, 0))   # gap
            w.write(delta_record("E-0", 3_700, 50, "yes", 10, 1_300, 0))
        stats = eng.run(p)
        assert stats.gaps == 1
        assert not eng.book("E-0").trusted
        # The post-gap delta must NOT have been applied to a corrupt book.
        assert eng.book("E-0").size_at(YES, 3_700) == 0

    def test_a_snapshot_restores_trust(self, tmp_path):
        eng, ev = self._engine()
        p = tmp_path / "g2.jsonl.gz"
        from kima.tape import TapeWriter
        with TapeWriter(p, overwrite=True) as w:
            w.write(snapshot_record("E-0", [(4_000, 100)], [(5_000, 100)], 1, 1_000, 0))
            w.write(delta_record("E-0", 3_800, 50, "yes", 9, 1_200, 0))   # gap
            w.write(snapshot_record("E-0", [(4_100, 70)], [(5_100, 70)], 20, 1_400, 0))
            w.write(delta_record("E-0", 4_000, 30, "yes", 21, 1_500, 0))
        eng.run(p)
        bk = eng.book("E-0")
        assert bk.trusted
        assert bk.best_yes_bid == 4_100
        assert bk.size_at(YES, 4_000) == 30


class TestBookReconstruction:
    def test_n0_holds_across_the_whole_tape(self, tape):
        tape_path, uni = tape
        events, _ = load_universe(uni)
        stats = ReplayEngine(events, ReplayConfig(strict_n0=True)).run(tape_path)
        assert stats.n0_violations == 0
        assert stats.deltas > 1_000

    def test_no_unexplained_snapshot_mismatches(self, tape):
        """Target zero. A mismatch after a known gap proves the gap detector
        works; a mismatch without one would be a reconstruction bug."""
        tape_path, uni = tape
        events, _ = load_universe(uni)
        stats = ReplayEngine(events, ReplayConfig()).run(tape_path)
        assert stats.snapshot_checks > 0
        assert stats.snapshot_diffs == 0


class TestPipelineInvariants:
    @staticmethod
    @pytest.fixture(scope="class")
    def result(tape):
        tape_path, uni = tape
        cfg = BacktestConfig()
        cfg.lp_check_every = 3
        return run_backtest(tape_path, uni, cfg)

    def test_funnel_is_monotonically_narrowing(self, result):
        from kima.backtest import FUNNEL_STAGES
        counts = [result.funnel[s] for s in FUNNEL_STAGES]
        assert counts == sorted(counts, reverse=True)
        assert counts[0] > 0

    def test_provenance_is_carried_through(self, result):
        assert result.source == "synthetic"
        assert "SYNTHETIC" in result.tape_note

    def test_lp_never_contradicts_the_closed_forms(self, result):
        assert result.lp_checks > 0
        assert result.lp_disagreements == []

    def test_no_basket_settles_below_its_guaranteed_floor(self, result):
        """The ex-post integrity check. One counterexample would mean the
        mutual-exclusivity assumption or the outcome partition was wrong."""
        assert result.settlement_check["checked"] > 0
        assert result.settlement_check["floor_violations"] == 0

    def test_zero_latency_matches_the_frictionless_theory(self, result):
        zero = next(s for s in result.exec_summaries if s["latency_ms"] == 0
                    and s["order_type"] == "IOC" and s["leg_order"] == "batch"
                    and s["residual_policy"] == "unwind")
        assert zero["edge_retention_vs_l1"] == pytest.approx(1.0, abs=1e-9)
        assert zero["complete_baskets"] == zero["baskets"]

    def test_edge_retention_decays_with_latency(self, result):
        rungs = {
            s["latency_ms"]: s["edge_retention_vs_l1"]
            for s in result.exec_summaries
            if s["order_type"] == "IOC" and s["leg_order"] == "batch"
            and s["residual_policy"] == "unwind"
        }
        assert rungs[0] > rungs[max(rungs)]

    def test_n2_is_never_fired_on_a_non_exhaustive_event(self, result):
        """The negative control. The award family is mutually exclusive but its
        nominee list does not cover the sample space."""
        non_exhaustive = {e.event_ticker for e in result.events if not e.exhaustive}
        assert non_exhaustive
        for baskets in result.executions.values():
            for b in baskets:
                assert not (b.condition == "N2" and b.event_ticker in non_exhaustive)

    def test_determinism(self, tape):
        tape_path, uni = tape
        cfg = BacktestConfig()
        cfg.lp_check_every = 0
        a = run_backtest(tape_path, uni, cfg)
        b = run_backtest(tape_path, uni, cfg)
        assert summarise_for_determinism(a) == summarise_for_determinism(b)


class TestNullTest:
    def test_scrambling_changes_the_opportunity_rate(self, tape, tmp_path):
        """A detector that finds the same number of arbitrages in scrambled data
        is finding noise. Each market keeps its own dynamics; only cross-market
        coherence is destroyed."""
        tape_path, uni = tape
        shuffled = tmp_path / "shuf.jsonl.gz"
        info = shuffle_tape(tape_path, shuffled, seed=5)
        assert info["records"] > 0

        cfg = BacktestConfig()
        cfg.lp_check_every = 0
        real = run_backtest(tape_path, uni, cfg)
        null = run_backtest(shuffled, uni, cfg)
        # The same records, reordered -- the shuffle must not lose any. Applied
        # deltas legitimately differ: the real tape carries sequence gaps that
        # block application, while the shuffled copy is renumbered contiguously.
        assert real.replay["records"] == null.replay["records"]
        assert real.funnel["raw_signals"] != null.funnel["raw_signals"]


class TestDuplicateSuppression:
    """The same dislocation arrives as many deltas.

    Suppression happens *before* the exact sizer rather than after it: re-sizing
    an unchanged book is the dominant cost on a large tape, and the result is
    identical by construction. The saved work must not change any number.
    """

    def test_suppression_is_recorded_and_saves_work(self, tape):
        tape_path, uni = tape
        cfg = BacktestConfig()
        cfg.lp_check_every = 0
        from kima.backtest import ReplayEngine, Strategy, build_exec_configs, load_universe
        from kima.execution import ExecutionSimulator

        events, _ = load_universe(uni)
        engine = ReplayEngine(events, cfg.replay)
        sims = [ExecutionSimulator(c) for c in build_exec_configs(cfg)]
        strat = Strategy(events, engine, cfg, sims)
        engine.run(tape_path, [strat])
        assert strat.stage2_skipped > 0
        assert strat.risk.rejections["duplicate"] == strat.stage2_skipped

    def test_every_funnel_signal_is_accounted_for(self, tape):
        """Signals leaving a stage must equal signals entering the next one plus
        the rejections attributed at that stage."""
        tape_path, uni = tape
        cfg = BacktestConfig()
        cfg.lp_check_every = 0
        res = run_backtest(tape_path, uni, cfg)
        rej = res.risk["rejections"]
        f = res.funnel
        assert f["raw_signals"] - f["book_trusted"] == rej.get("untrusted_book", 0)
        assert f["book_trusted"] - f["market_state_active"] == rej.get("market_state", 0)
        assert f["market_state_active"] - f["not_stale"] == rej.get("stale_data", 0)
        assert f["not_stale"] - f["exhaustiveness_ok"] == rej.get("exhaustiveness", 0)


class TestMixedClockOrdering:
    """A live tape carries two clocks in one field.

    Deltas have an exchange timestamp; snapshots have none, so the recorder
    stamps them with receipt time. On the real feed the exchange clock trails
    receipt by 200-570ms, so ordering on that field puts deltas *before* the
    snapshots that must precede them -- and every book stays uninitialised.

    Observed on a real 30-minute capture: 33,762 records, 0 deltas applied.
    """

    def _tape(self, tmp_path):
        from kima.tape import TapeWriter
        p = tmp_path / "mixed.jsonl.gz"
        recv = 1_787_826_608_253
        feed_delay = 570
        with TapeWriter(p, overwrite=True) as w:
            # Snapshot: no exchange time available, so ts == receipt.
            w.write(snapshot_record("E-0", [(4_000, 100)], [(5_000, 100)],
                                    seq=1, ts_ms=recv, recv_ns=recv * 1_000_000))
            # Delta: exchange time, which is *earlier* than the snapshot's ts
            # even though it genuinely arrived afterwards.
            w.write(delta_record("E-0", 3_900, 50, "yes", seq=2,
                                 ts_ms=recv - feed_delay,
                                 recv_ns=(recv + 10) * 1_000_000))
        return p

    def test_receipt_order_is_preserved(self, tmp_path):
        from kima.replay import clock_ms
        recs = list(read_tape(self._tape(tmp_path)))
        recs = [r for r in recs if r.get("type") in ("snapshot", "delta")]
        # The exchange-time field would order these backwards...
        assert recs[1]["ts"] < recs[0]["ts"]
        # ...while the engine clock keeps them in the order they arrived.
        assert clock_ms(recs[0]) < clock_ms(recs[1])
        emitted = [r["type"] for r in ordered_records(recs, window=8)]
        assert emitted == ["snapshot", "delta"]

    def test_the_delta_actually_applies(self, tmp_path):
        from tests.conftest import make_event
        engine = ReplayEngine([make_event("E", 2)], ReplayConfig(snapshot_diff=False))
        stats = engine.run(self._tape(tmp_path))
        assert stats.deltas == 1, "the delta was dropped, as in the live failure"
        assert stats.gaps == 0
        bk = engine.book("E-0")
        assert bk.trusted
        assert bk.size_at(YES, 3_900) == 50
