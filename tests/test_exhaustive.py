"""The exhaustiveness prover -- the module that prevents the expensive mistake."""

from fractions import Fraction

from kima.events import Market, region_from_strike
from kima.exhaustive import Cell, outcome_partition, prove_exhaustive
from tests.conftest import bucket_event, make_event

E = [Fraction(3), Fraction("3.25"), Fraction("3.5")]


class TestIntervalCover:
    def test_contiguous_buckets_are_exhaustive(self):
        ev = bucket_event("F", E)
        cert = prove_exhaustive(ev)
        assert cert.exhaustive
        assert cert.method == "interval-cover"
        # Adjacent inclusive buckets are one underlying tick apart; the prover
        # must recognise that as contiguity, not as a hole.
        assert cert.underlying_tick == Fraction(1, 100)

    def test_a_missing_bucket_is_refused(self):
        ev = bucket_event("F", E, drop_leg=2)
        cert = prove_exhaustive(ev)
        assert not cert.exhaustive
        assert cert.gaps

    def test_overlapping_legs_contradict_the_exclusivity_flag(self):
        ev = bucket_event("F", E)
        ev.markets[1].floor_strike = Fraction(0)
        ev.markets[1].region = region_from_strike("between", Fraction(0), Fraction("3.25"))
        cert = prove_exhaustive(ev)
        assert not cert.exhaustive
        assert cert.overlaps

    def test_uncovered_tails_are_refused(self):
        ev = bucket_event("F", E)
        # Replace the open-ended top bucket with a bounded one, leaving the
        # right tail uncovered.
        ev.markets[-1].strike_type = "between"
        ev.markets[-1].cap_strike = Fraction(4)
        ev.markets[-1].region = region_from_strike("between", Fraction("3.51"), Fraction(4))
        cert = prove_exhaustive(ev)
        assert not cert.exhaustive
        assert any("right tail" in g for g in cert.gaps)


class TestCategorical:
    def test_nominee_list_without_a_field_market_is_refused(self):
        """The negative control: an unlisted outcome can win, so the YES basket
        can expire worthless. This is the single most expensive mistake."""
        ev = make_event("A", 5, field_market=False)
        cert = prove_exhaustive(ev)
        assert not cert.exhaustive
        assert "field market" in cert.reason

    def test_a_residual_field_market_closes_the_partition(self):
        ev = make_event("A", 5, field_market=True)
        cert = prove_exhaustive(ev)
        assert cert.exhaustive

    def test_duplicate_labels_contradict_exclusivity(self):
        ev = make_event("A", 3)
        ev.markets[1].region = ev.markets[0].region
        ev.markets[1].yes_sub_title = ev.markets[0].yes_sub_title
        cert = prove_exhaustive(ev)
        assert not cert.exhaustive
        assert cert.overlaps


class TestRefusals:
    def test_not_mutually_exclusive_means_n2_inapplicable(self):
        ev = make_event("E", 3, mutually_exclusive=False)
        cert = prove_exhaustive(ev)
        assert not cert.exhaustive
        assert cert.method == "flag"

    def test_opaque_strikes_are_excluded_not_guessed(self):
        ev = bucket_event("F", E)
        ev.markets[0].strike_type = "functional"
        ev.markets[0].region = region_from_strike("functional", None, None)
        cert = prove_exhaustive(ev)
        assert not cert.exhaustive
        assert cert.method == "opaque-exclusion"

    def test_curated_allowlist_is_stamped_visibly(self):
        """A human assertion is allowed, but never silently."""
        ev = make_event("G", 2)
        ev.series_ticker = "KXGAME"
        cert = prove_exhaustive(ev, curated_exhaustive={"KXGAME"})
        assert cert.exhaustive
        assert cert.method == "curated"
        assert "human-audited" in cert.reason

    def test_certificate_renders_for_human_review(self):
        text = prove_exhaustive(bucket_event("F", E)).render()
        assert "EXHAUSTIVE" in text and "method:" in text and "reason:" in text


class TestPartition:
    def test_exclusive_event_gives_one_cell_per_leg(self):
        ev = bucket_event("F", E)
        ev.exhaustive = True
        cells = outcome_partition(ev)
        assert len(cells) == ev.n_legs
        assert all(len(c.yes_tickers) == 1 for c in cells)

    def test_non_exhaustive_event_gets_an_outside_cell(self):
        ev = make_event("A", 4, exhaustive=False)
        cells = outcome_partition(ev)
        assert len(cells) == 5
        assert any(c.is_outside for c in cells)

    def test_unreachable_tick_gaps_are_not_enumerated(self):
        """A proven cover still has geometric holes between inclusive buckets.
        Enumerating them would invent a state where the YES basket pays nothing."""
        ev = bucket_event("F", E)
        ev.mutually_exclusive = False        # force the geometry branch
        ev.exhaustive = True
        cells = outcome_partition(ev)
        assert not any(c.is_outside for c in cells)

    def test_refuses_when_no_reasoning_is_possible(self):
        ev = make_event("X", 2, mutually_exclusive=False)
        for m in ev.markets:
            m.strike_type = "custom"
            m.region = region_from_strike("custom", None, None)
        assert outcome_partition(ev) == []

    def test_nested_thresholds_partition_into_n_plus_one_cells(self):
        from kima.events import Event
        mk = lambda i, s: Market(
            ticker=f"T-{i}", event_ticker="T", strike_type="greater_or_equal",
            floor_strike=s, region=region_from_strike("greater_or_equal", s, None),
        )
        ev = Event(
            event_ticker="T", mutually_exclusive=False,
            markets=[mk(i, Fraction(i)) for i in range(4)],
        )
        cells = outcome_partition(ev)
        # X < 0, and then each of [0,1), [1,2), [2,3), [3, inf)
        assert len(cells) == 5
        sizes = sorted(len(c.yes_tickers) for c in cells)
        assert sizes == [0, 1, 2, 3, 4]
