"""O(1) opportunity detection plus episode grouping.

The screen is the cheapest part of the whole system and deliberately so. Per
event we keep two incrementally maintained sufficient statistics::

    S_yes = sum_i bestYesBid_i        -> Condition N1 (overround)
    S_no  = sum_i bestNoBid_i         -> Condition N2 (underround)

A delta that moves leg ``i``'s best bid updates the relevant sum by a single
integer add, so **detection cost does not grow with the number of legs**.

Soundness of the screen matters more than its speed, so it is worth stating why
``S_yes > $1`` is safe. The best subset ``S*`` maximises
``sum_{i in S} b_i - 1 - fees``, and dropping a leg only ever removes a
non-negative ``b_i``, so ``sum_{S*} b_i <= S_yes``. Any subset that could be
profitable therefore forces ``S_yes > $1``: the screen has no false negatives.
Stage 2 (:mod:`kima.sizing`) then does the expensive exact work on the survivors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .book import MarketBook
from .events import Event, subset_pairs
from .sizing import Basket, SizingConfig, l0_edge_mu, size_no_basket, size_nested_pair, size_yes_basket
from .units import NOTIONAL_CC

CONDITIONS = ("N1", "N2", "N3")


@dataclass
class DetectorConfig:
    """Screen thresholds and episode-grouping policy."""

    #: Stage-1 tolerance in centicents. Zero means screen on the raw inequality
    #: and let stage 2 apply exact fees; a positive value trades recall for speed.
    screen_tol_cc: int = 0
    #: Enable each closed form independently (used by the rejection funnel).
    enable_n1: bool = True
    enable_n2: bool = True
    enable_n3: bool = True
    #: Emit N2 signals even on events with no exhaustiveness certificate, so the
    #: rejection funnel can *quantify* the gate instead of the opportunities
    #: simply never appearing. The downstream filter still refuses them. A live
    #: hot path should set this False and gate at the screen.
    screen_n2_without_proof: bool = True
    #: Gap after which an episode is closed rather than assumed to persist.
    #: Protects against recording a feed outage as a long-lived opportunity.
    trust_ceiling_ms: int = 2_000
    sizing: SizingConfig = field(default_factory=SizingConfig)


@dataclass
class Signal:
    """A stage-1 hit, before any depth, fee or state filtering."""

    ts_ms: int
    event_ticker: str
    condition: str
    statistic_cc: int
    threshold_cc: int
    pair: tuple[str, str] | None = None

    @property
    def excess_cc(self) -> int:
        return self.statistic_cc - self.threshold_cc


class EventDetector:
    """Incremental screen for one event."""

    __slots__ = (
        "event", "books", "cfg", "tickers", "_yes", "_no",
        "sum_yes_cc", "sum_no_cc", "n_with_no_bid", "nested", "_nested_index",
        "screens", "signals_emitted",
    )

    def __init__(self, event: Event, books: dict[str, MarketBook], cfg: DetectorConfig | None = None):
        self.event = event
        self.books = books
        self.cfg = cfg or DetectorConfig()
        self.tickers = [t for t in event.tickers if t in books]
        self._yes: dict[str, int] = {t: 0 for t in self.tickers}
        self._no: dict[str, int] = {t: 0 for t in self.tickers}
        self.sum_yes_cc = 0
        self.sum_no_cc = 0
        self.n_with_no_bid = 0
        self.nested = subset_pairs(event) if self.cfg.enable_n3 else []
        self._nested_index: dict[str, list[int]] = {}
        for i, (a, b) in enumerate(self.nested):
            self._nested_index.setdefault(a, []).append(i)
            self._nested_index.setdefault(b, []).append(i)
        self.screens = 0
        self.signals_emitted = 0

    # -- incremental state -------------------------------------------------
    def refresh(self, ticker: str) -> None:
        """Fold one leg's new top-of-book into the event statistics. O(1)."""
        bk = self.books.get(ticker)
        if bk is None or ticker not in self._yes:
            return
        y = bk.best_yes_bid
        n = bk.best_no_bid
        y = 0 if y < 0 else y
        prev_y = self._yes[ticker]
        if y != prev_y:
            self.sum_yes_cc += y - prev_y
            self._yes[ticker] = y
        had = self._no[ticker] > 0
        n_val = 0 if n < 0 else n
        prev_n = self._no[ticker]
        if n_val != prev_n:
            self.sum_no_cc += n_val - prev_n
            self._no[ticker] = n_val
            has = n_val > 0
            if has and not had:
                self.n_with_no_bid += 1
            elif had and not has:
                self.n_with_no_bid -= 1

    def refresh_all(self) -> None:
        for t in self.tickers:
            self.refresh(t)

    # -- the screen --------------------------------------------------------
    def screen(self, ts_ms: int, ticker: str | None = None) -> list[Signal]:
        """Stage-1 test. Returns raw signals; no filtering of any kind applied."""
        self.screens += 1
        out: list[Signal] = []
        tol = self.cfg.screen_tol_cc

        if self.cfg.enable_n1 and self.event.mutually_exclusive:
            thr = NOTIONAL_CC + tol
            if self.sum_yes_cc > thr:
                out.append(Signal(ts_ms, self.event.event_ticker, "N1", self.sum_yes_cc, NOTIONAL_CC))

        if (
            self.cfg.enable_n2
            and self.event.mutually_exclusive
            and (self.event.exhaustive or self.cfg.screen_n2_without_proof)
            and self.n_with_no_bid == len(self.tickers)
            and len(self.tickers) == self.event.n_legs
        ):
            # sum(yesAsk) < 1  <=>  sum(noBid) > (N-1)
            thr = (len(self.tickers) - 1) * NOTIONAL_CC
            if self.sum_no_cc > thr + tol:
                out.append(Signal(ts_ms, self.event.event_ticker, "N2", self.sum_no_cc, thr))

        if self.cfg.enable_n3 and self.nested:
            idxs = self._nested_index.get(ticker, []) if ticker else range(len(self.nested))
            for i in idxs:
                inner, outer = self.nested[i]
                b_inner = self._yes.get(inner, 0)
                beta_outer = self._no.get(outer, 0)
                if b_inner <= 0 or beta_outer <= 0:
                    continue
                # bestYesBid(inner) > bestYesAsk(outer) = 1 - bestNoBid(outer)
                stat = b_inner + beta_outer
                if stat > NOTIONAL_CC + tol:
                    out.append(
                        Signal(ts_ms, self.event.event_ticker, "N3", stat, NOTIONAL_CC, (inner, outer))
                    )
        self.signals_emitted += len(out)
        return out

    # -- stage 2 -----------------------------------------------------------
    def evaluate(self, signal: Signal) -> Basket | None:
        """Exact, depth-limited, fee-inclusive evaluation of a stage-1 signal."""
        cfg = self.cfg.sizing
        if signal.condition == "N1":
            return size_no_basket(self.event, self.books, cfg, ts_ms=signal.ts_ms)
        if signal.condition == "N2":
            return size_yes_basket(self.event, self.books, cfg, ts_ms=signal.ts_ms)
        if signal.condition == "N3" and signal.pair:
            inner, outer = signal.pair
            return size_nested_pair(self.event, self.books, inner, outer, cfg, ts_ms=signal.ts_ms)
        return None

    def l0_edge(self, condition: str) -> int:
        return l0_edge_mu(self.event, self.books, condition)

    def top_of_book_key(self) -> tuple:
        """Fingerprint of the state a basket would be derived from.

        A dislocation arrives as many deltas, and re-running the exact sizer on
        every one of them is the dominant cost on a multi-million-message tape.
        The key covers both touch prices and both touch sizes for every leg, so
        it changes whenever anything that moves the *first* unit of the basket
        moves. Depth changes strictly below the touch can be missed; that is the
        documented trade-off of top-of-book duplicate suppression, and it errs
        toward evaluating fewer opportunities, never more.
        """
        books = self.books
        out = []
        for t in self.tickers:
            bk = books.get(t)
            if bk is None:
                out.append((-1, 0, -1, 0))
                continue
            y, n = bk.best_yes_bid, bk.best_no_bid
            out.append((y, bk.size_at(0, y) if y >= 0 else 0,
                        n, bk.size_at(1, n) if n >= 0 else 0))
        return tuple(out)

    # -- validation --------------------------------------------------------
    def assert_consistency(self) -> None:
        """N1 and N2 cannot both fire: ``a_i >= b_i`` makes them contradictory."""
        n1 = self.sum_yes_cc > NOTIONAL_CC
        n2 = (
            self.n_with_no_bid == len(self.tickers)
            and self.sum_no_cc > (len(self.tickers) - 1) * NOTIONAL_CC
        )
        if n1 and n2:
            raise AssertionError(
                f"{self.event.event_ticker}: N1 and N2 fired simultaneously "
                f"(sum b = {self.sum_yes_cc}, sum beta = {self.sum_no_cc}); book state is corrupt"
            )


# --------------------------------------------------------------------------
# episodes
# --------------------------------------------------------------------------
@dataclass
class Episode:
    """Consecutive violating states, credited **once**.

    Summing profit across every snapshot of a persistent dislocation would
    assume liquidity regenerates instantly and inflates results by orders of
    magnitude. We take the single best realisable basket in the window instead.
    """

    event_ticker: str
    condition: str
    start_ts_ms: int
    end_ts_ms: int
    n_states: int = 0
    best_basket: Basket | None = None
    best_l0_mu: int = 0
    max_excess_cc: int = 0
    censored: bool = False
    phase: str = "unknown"
    series_ticker: str = ""
    n_legs: int = 0

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ts_ms - self.start_ts_ms)

    @property
    def actionable(self) -> bool:
        return self.best_basket is not None and self.best_basket.profit_mu > 0


class EpisodeGrouper:
    """Groups per-message signals into episodes with one-shot profit crediting."""

    def __init__(self, trust_ceiling_ms: int = 2_000):
        self.trust_ceiling_ms = trust_ceiling_ms
        self._open: dict[tuple[str, str, tuple | None], Episode] = {}
        self._last_seen: dict[tuple[str, str, tuple | None], int] = {}
        self.closed: list[Episode] = []

    def observe(
        self,
        ts_ms: int,
        signal: Signal,
        basket: Basket | None,
        l0_mu: int = 0,
        phase: str = "unknown",
        series_ticker: str = "",
        n_legs: int = 0,
    ) -> None:
        key = (signal.event_ticker, signal.condition, signal.pair)
        last = self._last_seen.get(key)
        ep = self._open.get(key)
        if ep is not None and last is not None and ts_ms - last > self.trust_ceiling_ms:
            # The feed went quiet for longer than we trust; close and censor.
            ep.censored = True
            ep.end_ts_ms = last + self.trust_ceiling_ms
            self.closed.append(ep)
            ep = None
        if ep is None:
            ep = Episode(
                event_ticker=signal.event_ticker,
                condition=signal.condition,
                start_ts_ms=ts_ms,
                end_ts_ms=ts_ms,
                phase=phase,
                series_ticker=series_ticker,
                n_legs=n_legs,
            )
            self._open[key] = ep
        ep.end_ts_ms = ts_ms
        ep.n_states += 1
        ep.max_excess_cc = max(ep.max_excess_cc, signal.excess_cc)
        ep.best_l0_mu = max(ep.best_l0_mu, l0_mu)
        if basket is not None and (ep.best_basket is None or basket.profit_mu > ep.best_basket.profit_mu):
            ep.best_basket = basket
        self._last_seen[key] = ts_ms

    def resolve(self, ts_ms: int, event_ticker: str, condition: str, pair: tuple | None = None) -> None:
        """Mark a key as no longer violating, closing any open episode."""
        key = (event_ticker, condition, pair)
        ep = self._open.pop(key, None)
        if ep is not None:
            ep.end_ts_ms = max(ep.end_ts_ms, ts_ms)
            self.closed.append(ep)

    def resolve_all_for(self, ts_ms: int, event_ticker: str, active: Iterable[tuple[str, tuple | None]]) -> None:
        """Close every open episode for an event that is not in ``active``."""
        active_set = set(active)
        for key in [k for k in self._open if k[0] == event_ticker]:
            if (key[1], key[2]) not in active_set:
                self.resolve(ts_ms, key[0], key[1], key[2])

    def flush(self, ts_ms: int | None = None) -> list[Episode]:
        for key in list(self._open):
            ep = self._open.pop(key)
            if ts_ms is not None:
                ep.end_ts_ms = max(ep.end_ts_ms, min(ts_ms, ep.end_ts_ms + self.trust_ceiling_ms))
            self.closed.append(ep)
        return self.closed

    @property
    def episodes(self) -> Sequence[Episode]:
        return self.closed
