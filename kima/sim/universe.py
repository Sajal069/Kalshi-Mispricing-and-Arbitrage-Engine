"""Synthetic event families mirroring the universe table in Research.md.

Six archetypes, each chosen to exercise a specific part of the engine:

===================  ============================================================
``crypto_range``     Exhaustive contiguous price buckets, **zero fee**. The
                     fee-free control group that isolates microstructure from
                     fee effects (experiment E8).
``fed_ladder``       Exhaustive rate-range partition, standard fee, sharp
                     scheduled shock. The natural home of the latency question.
``temperature``      Exhaustive daily buckets, short capital lockup, many legs.
                     Drives the leg-count scaling test (E9).
``game``             Two-outcome, highest message rate. The latency stress test.
                     Exhaustive in truth but not in strike metadata, so it also
                     exercises the curated-allowlist path.
``award``            Mutually exclusive but **non-exhaustive** nominee list.
                     The negative control: the engine must refuse N2 here.
``threshold_ladder`` Nested ``greater_or_equal`` strikes, so the subset lattice
                     is non-trivial and Condition N3 has something to find.
===================  ============================================================

The award family matters more than it looks: without a family the engine is
*supposed* to refuse, "we handle non-exhaustiveness correctly" is an assertion
rather than a demonstration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from ..events import Event, Market, region_from_strike
from ..fees import FeeSchedule, schedule_for
from ..ticks import KNOWN_STRUCTURES, PriceGrid

#: Series the operator has audited by hand as genuinely exhaustive even though
#: strike metadata cannot express it. Kept tiny and explicit on purpose.
CURATED_EXHAUSTIVE = frozenset({"KXGAME"})


@dataclass
class FamilySpec:
    """Everything the generator needs to instantiate one event family."""

    series: str
    kind: str
    n_legs: int
    #: Half-spread in centicents; the per-leg hurdle a basket must clear.
    half_spread_cc: int = 100
    #: Top-of-book resting size in centi-contracts.
    top_depth_cq: int = 5_000
    depth_decay: float = 0.62
    n_price_levels: int = 5
    #: Market-maker reaction lag range in ms. Heterogeneity here is the sole
    #: source of transient no-arbitrage violations.
    lag_ms: tuple[int, int] = (120, 900)
    #: Expected information shocks per hour, and their magnitude in centicents.
    jumps_per_hour: float = 6.0
    jump_size_cc: int = 900
    #: Background quote-drift and noise-trade rates, per market per second.
    drift_hz: float = 0.35
    trade_hz: float = 0.12
    grid: str = "one_cent"
    zero_fee: bool = False
    #: Fraction of the event lifetime treated as the pre-close window, where
    #: makers widen and phantom liquidity appears.
    pre_close_fraction: float = 0.08
    labels: tuple[str, ...] = ()
    strike_step: Fraction = field(default_factory=lambda: Fraction(1))
    strike_origin: Fraction = field(default_factory=lambda: Fraction(0))
    #: Underlying tick separating adjacent inclusive buckets.
    underlying_tick: Fraction = field(default_factory=lambda: Fraction(1, 100))

    @property
    def price_grid(self) -> PriceGrid:
        return KNOWN_STRUCTURES.get(self.grid, PriceGrid.penny())

    @property
    def fee_schedule(self) -> FeeSchedule:
        if self.zero_fee:
            return FeeSchedule(series=self.series, taker_multiplier=Fraction(0),
                               maker_multiplier=Fraction(0))
        return schedule_for(self.series)


DEFAULT_FAMILIES: tuple[FamilySpec, ...] = (
    FamilySpec(
        series="KXBTCY", kind="crypto_range", n_legs=6,
        half_spread_cc=90, top_depth_cq=3_000, lag_ms=(200, 1_400),
        jumps_per_hour=5.0, jump_size_cc=1_100, drift_hz=0.30, trade_hz=0.06,
        grid="center_deci_edge_centi_cent", zero_fee=True,
        strike_step=Fraction(10_000), strike_origin=Fraction(80_000),
        underlying_tick=Fraction(1),
    ),
    FamilySpec(
        series="KXFED", kind="fed_ladder", n_legs=5,
        half_spread_cc=100, top_depth_cq=8_000, lag_ms=(90, 700),
        jumps_per_hour=8.0, jump_size_cc=1_300, drift_hz=0.40, trade_hz=0.20,
        grid="one_cent",
        strike_step=Fraction(25, 100), strike_origin=Fraction(350, 100),
        underlying_tick=Fraction(1, 100),
    ),
    FamilySpec(
        series="KXHIGHNY", kind="temperature", n_legs=8,
        half_spread_cc=150, top_depth_cq=2_500, lag_ms=(250, 1_600),
        jumps_per_hour=3.5, jump_size_cc=800, drift_hz=0.22, trade_hz=0.05,
        grid="one_cent",
        strike_step=Fraction(2), strike_origin=Fraction(60),
        underlying_tick=Fraction(1),
    ),
    FamilySpec(
        series="KXGAME", kind="game", n_legs=2,
        half_spread_cc=100, top_depth_cq=12_000, lag_ms=(40, 350),
        jumps_per_hour=90.0, jump_size_cc=1_600, drift_hz=1.6, trade_hz=0.9,
        grid="one_cent",
        labels=("Home team wins", "Away team wins"),
    ),
    FamilySpec(
        series="KXAWARD", kind="award", n_legs=6,
        half_spread_cc=180, top_depth_cq=1_200, lag_ms=(400, 2_500),
        jumps_per_hour=1.5, jump_size_cc=700, drift_hz=0.12, trade_hz=0.03,
        grid="one_cent",
        labels=("Nominee A", "Nominee B", "Nominee C", "Nominee D", "Nominee E", "Nominee F"),
    ),
    FamilySpec(
        series="KXCPILADDER", kind="threshold_ladder", n_legs=5,
        half_spread_cc=110, top_depth_cq=4_000, lag_ms=(120, 1_000),
        jumps_per_hour=6.0, jump_size_cc=1_000, drift_hz=0.30, trade_hz=0.10,
        grid="one_cent",
        strike_step=Fraction(1, 10), strike_origin=Fraction(24, 10),
        underlying_tick=Fraction(1, 100),
    ),
)


def _bucket_markets(spec: FamilySpec, event_ticker: str) -> list[Market]:
    """Contiguous inclusive buckets that cover the whole real line.

    The lowest leg is ``less_or_equal``, the highest ``greater``, and the middle
    legs are ``between``. Adjacent buckets are separated by exactly one
    underlying tick, which the exhaustiveness prover recognises as contiguity.
    """
    out: list[Market] = []
    tick = spec.underlying_tick
    edges = [spec.strike_origin + i * spec.strike_step for i in range(spec.n_legs)]
    for i in range(spec.n_legs):
        if i == 0:
            st, floor, cap = "less_or_equal", None, edges[0]
            sub = f"below {float(edges[0]):g}"
        elif i == spec.n_legs - 1:
            st, floor, cap = "greater", edges[i - 1], None
            sub = f"above {float(edges[i - 1]):g}"
        else:
            st = "between"
            floor, cap = edges[i - 1] + tick, edges[i]
            sub = f"{float(floor):g} to {float(cap):g}"
        m = Market(
            ticker=f"{event_ticker}-B{i}",
            event_ticker=event_ticker,
            series_ticker=spec.series,
            status="active",
            strike_type=st,
            floor_strike=floor,
            cap_strike=cap,
            yes_sub_title=sub,
            grid=spec.price_grid,
            region=region_from_strike(st, floor, cap, label=sub),
        )
        out.append(m)
    return out


def _threshold_markets(spec: FamilySpec, event_ticker: str) -> list[Market]:
    """Nested ``greater_or_equal`` thresholds: outcome sets are strictly nested.

    ``{X >= 2.8}`` is contained in ``{X >= 2.7}``, so every ordered pair is an
    N3 candidate. Note these are *not* mutually exclusive, which is exactly the
    point -- N3 needs no such flag.
    """
    out: list[Market] = []
    for i in range(spec.n_legs):
        strike = spec.strike_origin + i * spec.strike_step
        sub = f"at or above {float(strike):g}"
        out.append(
            Market(
                ticker=f"{event_ticker}-T{i}",
                event_ticker=event_ticker,
                series_ticker=spec.series,
                status="active",
                strike_type="greater_or_equal",
                floor_strike=strike,
                cap_strike=None,
                yes_sub_title=sub,
                grid=spec.price_grid,
                region=region_from_strike("greater_or_equal", strike, None, label=sub),
            )
        )
    return out


def _categorical_markets(spec: FamilySpec, event_ticker: str) -> list[Market]:
    labels = spec.labels or tuple(f"Outcome {i}" for i in range(spec.n_legs))
    out: list[Market] = []
    for i, label in enumerate(labels[: spec.n_legs]):
        out.append(
            Market(
                ticker=f"{event_ticker}-{chr(ord('A') + i)}",
                event_ticker=event_ticker,
                series_ticker=spec.series,
                status="active",
                #: No numeric strike exists for a named outcome. This is not the
                #: same as "custom", which denotes geometry we refuse to infer.
                strike_type=None,
                yes_sub_title=label,
                grid=spec.price_grid,
                region=region_from_strike(None, None, None, label=label),
            )
        )
    return out


def build_event(spec: FamilySpec, index: int = 0) -> Event:
    event_ticker = f"{spec.series}-{index:03d}"
    if spec.kind in ("crypto_range", "fed_ladder", "temperature"):
        markets = _bucket_markets(spec, event_ticker)
        mutually_exclusive = True
    elif spec.kind == "threshold_ladder":
        markets = _threshold_markets(spec, event_ticker)
        mutually_exclusive = False       # nested thresholds are not exclusive
    else:
        markets = _categorical_markets(spec, event_ticker)
        mutually_exclusive = True
    return Event(
        event_ticker=event_ticker,
        series_ticker=spec.series,
        title=f"{spec.series} synthetic event {index}",
        mutually_exclusive=mutually_exclusive,
        markets=markets,
        fee_schedule=spec.fee_schedule,
    )


def build_universe(
    specs: tuple[FamilySpec, ...] = DEFAULT_FAMILIES,
    events_per_family: int = 1,
) -> tuple[list[Event], dict[str, FamilySpec]]:
    events: list[Event] = []
    by_event: dict[str, FamilySpec] = {}
    for spec in specs:
        for i in range(events_per_family):
            ev = build_event(spec, i)
            events.append(ev)
            by_event[ev.event_ticker] = spec
    return events, by_event
