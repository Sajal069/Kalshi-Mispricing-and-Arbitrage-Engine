"""Shared fixtures: hand-built books and events with known-good arithmetic.

Everything here is constructed so the expected answer can be worked out on
paper. That matters more than usual in this project: the whole claim is that the
engine computes an *exact* guaranteed profit, so the tests have to pin exact
integers rather than assert that something is roughly positive.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from kima.book import NO, YES, MarketBook
from kima.events import Event, Market, region_from_strike
from kima.fees import FeeSchedule
from kima.units import NOTIONAL_CC, complement_cc


def make_book(
    ticker: str,
    yes_bid_cc: int,
    no_bid_cc: int,
    depth_cq: int = 10_000,
    levels: int = 4,
    step_cc: int = 100,
) -> MarketBook:
    """A two-sided book with a flat ladder on each side.

    ``yes_bid_cc + no_bid_cc`` must not exceed $1 or the book would be crossed,
    which the exchange cannot publish and invariant N0 rejects.
    """
    assert yes_bid_cc + no_bid_cc <= NOTIONAL_CC, "constructed a crossed book"
    bk = MarketBook(ticker)
    yes = [(yes_bid_cc - i * step_cc, depth_cq) for i in range(levels) if yes_bid_cc - i * step_cc > 0]
    no = [(no_bid_cc - i * step_cc, depth_cq) for i in range(levels) if no_bid_cc - i * step_cc > 0]
    bk.apply_snapshot(yes, no, seq=1, ts_ms=1_000)
    return bk


def make_event(
    ticker: str,
    n_legs: int,
    *,
    mutually_exclusive: bool = True,
    exhaustive: bool = False,
    zero_fee: bool = False,
    categorical: bool = True,
    field_market: bool = False,
) -> Event:
    markets = []
    for i in range(n_legs):
        label = "Another outcome" if (field_market and i == n_legs - 1) else f"Outcome {i}"
        markets.append(
            Market(
                ticker=f"{ticker}-{i}",
                event_ticker=ticker,
                series_ticker=ticker,
                status="active",
                strike_type=None,
                yes_sub_title=label,
                region=region_from_strike(None, None, None, label=label),
            )
        )
    sched = FeeSchedule(
        series=ticker,
        taker_multiplier=Fraction(0) if zero_fee else Fraction(1),
        maker_multiplier=Fraction(0),
    )
    ev = Event(
        event_ticker=ticker,
        series_ticker=ticker,
        mutually_exclusive=mutually_exclusive,
        markets=markets,
        fee_schedule=sched,
    )
    ev.exhaustive = exhaustive
    return ev


def bucket_event(
    ticker: str,
    edges: list[Fraction],
    *,
    tick: Fraction = Fraction(1, 100),
    zero_fee: bool = False,
    drop_leg: int | None = None,
) -> Event:
    """Contiguous inclusive buckets covering the real line.

    ``drop_leg`` removes one bucket, which punches a hole in the cover and must
    make the exhaustiveness prover refuse.
    """
    markets = []
    n = len(edges) + 1
    for i in range(n):
        if i == 0:
            st, floor, cap = "less_or_equal", None, edges[0]
        elif i == n - 1:
            st, floor, cap = "greater", edges[-1], None
        else:
            st, floor, cap = "between", edges[i - 1] + tick, edges[i]
        if drop_leg is not None and i == drop_leg:
            continue
        markets.append(
            Market(
                ticker=f"{ticker}-B{i}",
                event_ticker=ticker,
                series_ticker=ticker,
                status="active",
                strike_type=st,
                floor_strike=floor,
                cap_strike=cap,
                yes_sub_title=f"bucket {i}",
                region=region_from_strike(st, floor, cap),
            )
        )
    return Event(
        event_ticker=ticker,
        series_ticker=ticker,
        mutually_exclusive=True,
        markets=markets,
        fee_schedule=FeeSchedule(
            series=ticker,
            taker_multiplier=Fraction(0) if zero_fee else Fraction(1),
        ),
    )


@pytest.fixture
def overround_books():
    """Three legs whose YES bids sum to $1.20 -- a $0.20 gross overround.

    Buying NO on all three costs 3 * $0.60 = $1.80 and pays at least $2.00,
    because at most one leg can settle YES.
    """
    return {
        f"OVER-{i}": make_book(f"OVER-{i}", yes_bid_cc=4_000, no_bid_cc=5_500)
        for i in range(3)
    }


@pytest.fixture
def underround_books():
    """Three legs whose YES asks sum to $0.90 -- a $0.10 gross underround.

    NO bids of $0.70 imply YES asks of $0.30 each. Exactly one leg pays $1.
    """
    return {
        f"UNDER-{i}": make_book(f"UNDER-{i}", yes_bid_cc=2_500, no_bid_cc=7_000)
        for i in range(3)
    }
