# Project Title

**KIMA — Kalshi Intra-Event Mispricing & Arbitrage Engine**
_A latency-aware study of multi-outcome no-arbitrage violations in a regulated binary event-contract exchange._

---

## Epistemic labelling convention used throughout

Because the brief explicitly asks for this separation, every non-obvious claim below carries one of four tags:

| Tag               | Meaning                                                                                             |
| ----------------- | --------------------------------------------------------------------------------------------------- |
| **[FACT]**        | Documented in Kalshi's official API documentation / fee schedule / help centre, cited inline.       |
| **[FINDING]**     | Reported in academic or third-party empirical research, cited inline.                               |
| **[ENGINEERING]** | A design assumption we adopt for the build. Defensible, but not verified — must be measured.        |
| **[HYPOTHESIS]**  | Our own prediction about what the data will show. **Unverified.** The project exists to test these. |

Nothing in this document is a measured result. Every number that appears is either (a) taken from a cited source, or (b) an illustrative worked example using arithmetic on documented formulas. **No P&L, opportunity count, or latency figure for this project has been produced yet.**

---

# Executive Summary

Kalshi is a CFTC-regulated exchange for binary event contracts. Its order book has an unusual property: it publishes **only bids**, on two sides (YES and NO), because a YES bid at price _X_ is definitionally a NO offer at _$1 − X_ **[FACT](https://docs.kalshi.com/getting_started/orderbook_responses)**. This single-book design means the naïve "YES + NO ≠ $1" arbitrage that people expect to find **cannot persist**: the matching engine would have crossed it. Any project premised on that discrepancy is dead on arrival, and saying so precisely is itself a good interview answer.

The real structural inefficiency lives one level up, at the **event**. A Kalshi _event_ groups several markets, and the API exposes a first-class boolean `mutually_exclusive` — "if true, only one market in this event can resolve to 'yes'" **[FACT](https://docs.kalshi.com/api-reference/events/get-event)**. Those markets trade on **completely independent order books with no cross-market matching**. That is the crack: a constraint that must hold at settlement (`Σ Xᵢ ≤ 1`) is enforced by _nobody_ at the quote level.

From that one fact, two exact no-arbitrage inequalities follow, both expressible purely in top-of-book quantities:

- **Overround (needs only mutual exclusivity):** if `Σᵢ bestYesBidᵢ > $1`, then buying NO in every market locks in a guaranteed profit of `Σᵢ bestYesBidᵢ − 1` per basket, in _every_ terminal state.
- **Underround (needs mutual exclusivity **and** exhaustiveness):** if `Σᵢ bestYesAskᵢ < $1`, buying YES in every market pays exactly $1 for less than $1.

The proposed project is a compact engine that (1) records its own full-depth Level-2 tape from Kalshi's `orderbook_delta` WebSocket — necessary, because **no historical order-book data is served by the API at all** **[FACT](https://docs.kalshi.com/getting_started/historical_data)** and no public L2 Kalshi dataset exists **[FINDING](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6583921)**; (2) reconstructs the books deterministically in a replay engine; (3) detects violations of the two inequalities in **O(1) per market-data message** via an incrementally maintained sufficient statistic; (4) prices each opportunity against _actual ladder depth_ and Kalshi's _exact_ published fee formula `fee = ⌈M × 0.07 × C × P × (1−P)⌉` **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**; and (5) re-runs the entire backtest under a ladder of injected execution latencies (0 → 2000 ms) to measure **how much theoretical edge survives contact with the clock**.

The headline research question is deliberately narrow and falsifiable:

> **What fraction of theoretically risk-free intra-event arbitrage edge on Kalshi survives exact fees, finite order-book depth, non-atomic multi-leg execution, and realistic round-trip latency?**

The project is small — roughly 2,000–3,000 lines across seven components — but it exercises the full chain **market structure → pricing identity → mispricing → order book → execution → latency → risk → realisable P&L**, and it produces a defensible negative-or-positive quantitative result either way. A finding of _"98% of the theoretical edge is unreachable"_ is a **better** CV artefact than a bot that claims to print money, because it demonstrates the reasoning a trading desk actually cares about.

---

# Problem Statement

_(This section is deliberately the most detailed, per the brief.)_

## 1. The market inefficiency being exploited

Kalshi organises tradable instruments in a three-level hierarchy **[FACT](https://docs.kalshi.com/api-reference/events/get-event)**:

```
Series   (recurring template, e.g. KXFED "Fed funds rate")
  └── Event    (one occurrence; carries the `mutually_exclusive` flag)
        └── Market   (one binary contract, its own independent order book)
```

Every market settles to exactly $1.00 (YES) or $0.00 (YES), i.e. `notional_value_dollars = 1.00`, with the NO side the exact complement. Within a single event flagged `mutually_exclusive = true`, the exchange **guarantees at settlement** that at most one constituent market resolves YES.

The inefficiency is a **consistency gap between settlement and quoting**:

- **At settlement**, the constraint `Σᵢ Xᵢ ≤ 1` is enforced by the exchange's own resolution rules. It is not a modelling assumption; it is contractual.
- **At quoting**, each market's limit order book is matched in isolation. Kalshi's matching engine has no cross-market awareness. There is no combination order type, no implied-order engine, no synthetic spread book across an event's legs.

Therefore the _aggregate_ implied probability of an event's constituent markets is free to drift away from the value logically implied by the settlement constraint, and it only returns when a human or bot notices and trades all the legs. Every second in which `Σᵢ bestYesBidᵢ > $1` is a second in which the exchange is quoting a set of prices that are jointly impossible.

**This is pure arbitrage, not statistical arbitrage.** The payoff of the basket is non-negative in _every_ terminal state of the world and strictly positive in at least one. It does not depend on any probability model, any forecast, or any assumption about the underlying event. This is why it was selected over the alternatives (Section: _Comparison of Candidate Strategies_).

Two structurally distinct violations exist, and conflating them is the single most common error in retail write-ups of this trade:

|                | Requires mutual exclusivity | Requires **exhaustiveness** | Trade                | Signal             |
| -------------- | --------------------------- | --------------------------- | -------------------- | ------------------ |
| **Overround**  | Yes                         | **No**                      | Buy NO on every leg  | `Σ bestYesBid > 1` |
| **Underround** | Yes                         | **Yes**                     | Buy YES on every leg | `Σ bestYesAsk < 1` |

Kalshi's `mutually_exclusive` flag says nothing about exhaustiveness — the exchange's own help documentation is explicit that "the markets do not need to exhaust every possible outcome" **[FACT](https://news.kalshi.com/p/collateral-return)**. Many Kalshi multi-outcome events (award winners, nominee races, "who will be appointed…") list a _subset_ of candidates. For those, `Σ bestYesAsk < 1` is **not** an arbitrage — an unlisted outcome can win and the whole YES basket expires worthless. Correctly separating these two regimes, and _proving_ exhaustiveness from strike geometry rather than assuming it, is a core deliverable.

## 2. What constitutes an opportunity

An opportunity is a **timestamped, quantity-bounded, fee-inclusive, worst-case-positive basket**. Formally, an opportunity record is emitted when, for event `E` at tape time `t`:

```
∃ q > 0, S ⊆ markets(E) :   worst_case_payoff(S, q) − cost(S, q, book_t) − fees(S, q) > ε
```

where `cost` is computed by _walking the actual resting ladder_ (not top-of-book only), `fees` uses Kalshi's exact per-order formula including its rounding, and `ε` is a safety buffer (see _Risk Management_). Three filters make an opportunity _actionable_ rather than _notional_:

1. **Depth filter.** `q ≥ q_min` at the bottleneck leg. Sub-dust opportunities are discarded. The relevant precedent: an equivalent Polymarket study found 76.9% of combinatorial opportunities capped at an average executable size of ~14.8 shares **[FINDING](https://arxiv.org/html/2605.00864v1)**.
2. **Liveness filter.** Every leg's market `status` must be `active`; events in `closed`, `determined`, `disputed`, or `paused` states are excluded. The same Polymarket study found **81.1%** of raw single-market signals were post-game artefacts where makers had pulled quotes, with median spread blowing out to 7,532 bps **[FINDING](https://arxiv.org/html/2605.00864v1)**. We expect the analogous contamination on Kalshi around `close_time` and during `settlement_timer_seconds`, and we filter it explicitly.
3. **Exhaustiveness filter** (underround only). The union of the legs' outcome regions must provably cover the sample space.

## 3. What information is available

Everything the engine sees comes from three documented surfaces:

- **`orderbook_delta` WebSocket channel** — an `orderbook_snapshot` (full aggregated price levels, `yes_dollars_fp` and `no_dollars_fp` as `[price, size]` string pairs) followed by incremental `orderbook_delta` messages carrying `market_ticker`, `price_dollars`, `delta_fp`, `side`, and a monotonic `seq` for gap detection, plus exchange-side `ts_ms` **[FACT](https://docs.kalshi.com/websockets/orderbook-updates)**.
- **REST market/event metadata** — `mutually_exclusive`, `strike_type` ∈ {greater, greater_or_equal, less, less_or_equal, between, functional, custom, structured}, `floor_strike`, `cap_strike`, `status`, `close_time`, `settlement_timer_seconds`, `price_level_structure`, `price_ranges` **[FACT](https://docs.kalshi.com/api-reference/events/get-event)**.
- **Fee configuration** — the published schedule plus per-series/per-event multiplier overrides (`fee_type_override`, `fee_multiplier_override`) **[FACT](https://docs.kalshi.com/api-reference/events/get-event-fee-changes)**.

**What is _not_ available, and this shapes the whole project:** Kalshi serves **no historical order-book data**. The historical API tier covers markets, candlesticks, trades, orders, and positions only **[FACT](https://docs.kalshi.com/getting_started/historical_data)**. Candlesticks are 1-minute at best. There is therefore no way to backtest a microstructure strategy on Kalshi from public archives — you must record your own tape. This is confirmed independently: "No public Level 2 microstructure dataset exists for Kalshi's prediction markets" **[FINDING](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6583921)**. Building the recorder is therefore not busywork; it _is_ the moat, and it is a legitimate CV line.

## 4. What decisions the engine makes

On every inbound message the engine decides, in order:

1. **Accept or resync.** Is `seq` contiguous? A gap means the local book is corrupt; drop it and wait for a fresh snapshot rather than trading stale state **[FACT](https://docs.kalshi.com/websockets/orderbook-updates)**.
2. **Update.** Apply the delta to the price-indexed ladder, maintain best-bid pointers.
3. **Screen.** Update the event's running sufficient statistic in O(1); compare against threshold.
4. **Size.** If triggered, walk the ladders to find the profit-maximising `(subset, quantity)`.
5. **Gate.** Apply risk checks: position caps, capital caps, staleness, duplicate suppression, kill switch.
6. **Emit.** Produce a leg-by-leg order intent (side, price, count, TIF).

## 5. What constraints exist

| Constraint            | Value / mechanism                                                                                                                                                                                              | Source                                                                     |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| Price grid            | Per-market `price_ranges` array of `{start, end, step}`; ticks range from $0.01 down to $0.0001 depending on `price_level_structure`. **Not always 1¢.**                                                       | **[FACT](https://docs.kalshi.com/getting_started/fixed_point_migration)**  |
| Quantity grid         | Fractional contracts supported, minimum granularity 0.01 contracts                                                                                                                                             | **[FACT](https://docs.kalshi.com/getting_started/fixed_point_migration)**  |
| Taker fee             | `⌈M × 0.07 × C × P × (1−P)⌉`, M defaults to 1; peak $1.75 per 100 contracts at P=$0.50                                                                                                                         | **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**                |
| Maker fee             | `⌈M × 0.0175 × C × P × (1−P)⌉`, **M defaults to 0** → zero maker fee on standard series                                                                                                                        | **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**                |
| Settlement fee        | None                                                                                                                                                                                                           | **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**                |
| Order types           | `fill_or_kill`, `immediate_or_cancel`, `good_till_canceled`; `post_only`, `reduce_only`, `cancel_order_on_pause` flags; self-trade prevention `taker_at_cross` / `maker`                                       | **[FACT](https://docs.kalshi.com/api-reference/orders/create-order-v2)**   |
| Atomicity             | **None across markets.** Batch create exists but bills each order separately and is not a cross-market all-or-none primitive                                                                                   | **[FACT](https://docs.kalshi.com/getting_started/rate_limits)**            |
| Rate limits           | Token buckets, separate Read/Write. Basic tier: 200 read / 100 write tokens per second; default 10 tokens per order ⇒ ~10 orders/s at Basic, with 1–2 s of burst capacity                                      | **[FACT](https://docs.kalshi.com/getting_started/rate_limits)**            |
| Order entry transport | REST (and FIX at higher tiers) — **orders cannot be sent over the market-data WebSocket**                                                                                                                      | **[FACT](https://docs.kalshi.com/getting_started/rate_limits)**            |
| Capital               | Fully cash-collateralised; no leverage on the predictions exchange                                                                                                                                             | **[FACT](https://news.kalshi.com/p/what-are-event-contracts)**             |
| Capital relief        | Optional "collateral return" (`netting_enabled`) returns the guaranteed portion of a hedged basket immediately — but the flag **locks per event at first order placement** and cannot be changed retroactively | **[FACT](https://help.kalshi.com/en/articles/13823816-collateral-return)** |

The rate-limit line deserves emphasis: at the Basic tier, an N-leg basket consumes `10N` write tokens against a 100 tokens/s budget. A 5-leg Fed basket is 50 tokens — half a second of budget. **Rate limits, not network latency, may be the binding constraint on multi-leg execution at low tiers.** That is a genuinely non-obvious systems insight and one of the things the project should measure.

## 6. What constitutes a valid trade

A basket is valid iff **all** of the following hold:

1. Every leg is a resting-liquidity-consuming order at a price on that market's `price_ranges` grid.
2. The worst-case terminal payoff of the assembled basket ≥ total cost + total fees + `ε`.
3. Worst case is evaluated over the **full outcome partition**, including "no listed outcome occurs" when exhaustiveness is not proven.
4. Every leg's market is `active` and the event is not within the pre-close exclusion window.
5. Position, capital, and per-event caps are not breached.
6. No duplicate `client_order_id`; no re-firing on the same book state.

## 7. How execution is modelled

Execution is modelled at three escalating levels of realism, and _the gap between them is the result_:

- **L0 — Frictionless.** Top-of-book, infinite depth, zero fees, zero latency. This is the number that retail screeners report. It is an upper bound and nothing more.
- **L1 — Execution-aware.** Walk the real ladder; size to the bottleneck leg; apply the exact fee formula per leg _with its rounding_; enforce tick and lot grids.
- **L2 — Latency-aware.** Introduce a decision-to-arrival delay `δ`. The order is matched against the book state **as it existed at `t_detect + δ`**, reconstructed from the tape. Legs fill independently; partial fills are permitted; unfilled legs leave a naked residual that must be marked and unwound.

The L2 model is where the intellectual content is. Because the legs are **not atomic**, a partially filled basket is not an arbitrage — it is a directional position acquired at a bad price. The engine must therefore model:

- **Leg-selection adverse selection.** The legs that fill are disproportionately the ones nobody else wanted; the leg that _fails_ is the one that moved. **[HYPOTHESIS]** Conditional on partial fill, realised edge is _worse_ than unconditional edge, i.e. fill and profitability are negatively correlated.
- **Unwind cost.** A residual leg is closed by crossing the spread in the opposite direction, paying a second taker fee. This is modelled explicitly, not waved away.

## 8. How profitability is measured

Per-opportunity, in dollars and in basis points of deployed capital, at each of L0/L1/L2 and at each latency rung. Aggregated into: gross P&L, fees, slippage, net P&L, edge-retention ratio `net_L2 / gross_L0`, and — because these positions are typically **held to settlement** with no exit fee — a **time-weighted return on capital**, since a 1% locked-in gain over three days and over three months are wildly different trades.

## 9. What the final system is expected to demonstrate

Not "a bot that trades." Rather, five demonstrable claims:

1. That we can reconstruct a correct, sequence-gap-safe local order book from an exchange delta feed.
2. That we can state and mechanically verify exact no-arbitrage conditions derived from contract settlement rules.
3. That we can price an opportunity against real depth and an exact published fee function, not a hand-waved "0.1% cost".
4. That we can quantify the decay of that edge as a function of execution latency, and identify the latency at which the strategy's expected value crosses zero.
5. That we understand _which_ frictions actually kill the trade — and can rank them.

---

# Why This Problem Is Interesting

**It is a rare setting where "risk-free" is literally true and checkable.** In equities or FX, "arbitrage" almost always means a statistical relationship with model risk. Here the constraint `Σ Xᵢ ≤ 1` is written into the exchange's settlement rules. You can _prove_ the worst-case payoff of a basket by enumerating a finite outcome space. Interview-wise, that lets you talk about arbitrage with actual rigour instead of hand-waving.

**The interesting part is not the signal; it is everything downstream of it.** The detection condition is one line of arithmetic. The entire difficulty — and therefore the entire value — is in fees, depth, non-atomicity, latency, and capital lockup. That is exactly the correct shape for a quant-trading portfolio project: trivial alpha, hard execution, honest measurement.

**The literature says the answer is "mostly no," which makes it a real experiment.** Comparable work on Polymarket found single-market arbitrage "exceedingly rare" (7 executable episodes across 75M snapshots, median duration 3.6 s) while combinatorial arbitrage was more frequent (290 episodes) but capped at retail size by depth **[FINDING](https://arxiv.org/html/2605.00864v1)**. Separately, cross-platform Kalshi–Polymarket work reports persistent divergence that survives naive arbitrage bounds because of enforceability and capital constraints **[FINDING](https://arxiv.org/html/2601.01706v1)**. So there is a real, contested empirical question, and no comparable published study exists for _Kalshi intra-event_ baskets on L2 data.

**Kalshi's own fee curve creates an exploitable structural asymmetry.** Because `fee ∝ P(1−P)`, arbitrage baskets built from _extreme-priced_ legs are dramatically cheaper to trade than baskets around 50¢: 100 contracts at $0.50 cost $1.75 in fees; at $0.90 or $0.10, $0.63; at $0.99 or $0.01, $0.07 **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**. Since multi-outcome events with many legs necessarily have most legs priced near the extremes, the fee structure is **quietly favourable to exactly this strategy** — a genuinely non-obvious observation that falls straight out of reading the schedule carefully.

**And there are zero-fee series.** The published non-standard fee table lists several series with both maker and taker multiplier `M = 0` — including `KXBTCY` (BTC end-of-year price range) and `KXETHY` (ETH end-of-year price range) **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**. These are _mutually exclusive, exhaustive, contiguous price-bucket partitions with no trading fee_. That is close to a laboratory-grade control group: it lets the study isolate the pure microstructure effect from the fee effect by running the identical detector on a zero-fee and a standard-fee series. **[ENGINEERING]** Multipliers change; the engine must read fee configuration from the API at runtime rather than hardcode the table.

---

# Kalshi Market Structure

## Contracts and settlement

Binary event contracts with `notional_value_dollars = 1.00`. Holding YES pays $1 if the market resolves YES, $0 otherwise; NO is the exact complement. Fully cash-collateralised — you can never owe the exchange **[FACT](https://news.kalshi.com/p/what-are-event-contracts)**. There is **no settlement fee** **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**, which materially changes strategy design: holding to expiry is free, so an arbitrage basket should generally be _held_, not unwound.

Markets progress through `initialized → inactive → active → closed → determined → (disputed | amended) → finalized`, with `settlement_timer_seconds` between determination and settlement **[FACT](https://docs.kalshi.com/api-reference/events/get-event)**. `can_close_early` and `early_close_condition` exist, and the exchange has documented maintenance windows and trading pauses; orders can carry `cancel_order_on_pause` **[FACT](https://docs.kalshi.com/api-reference/orders/create-order-v2)**.

## The single-book representation — the most important structural fact

Kalshi's order book endpoint "returns yes bids and no bids only (no asks are returned)" **[FACT](https://docs.kalshi.com/api-reference/market/get-market-orderbook)**. The documented identities are:

```
YES bid at $X   ≡   NO ask at $(1 − X)
NO  bid at $Y   ≡   YES ask at $(1 − Y)
```

Hence:

```
bestYesAsk = 1 − bestNoBid
bestNoAsk  = 1 − bestYesBid
YES spread = (1 − bestNoBid) − bestYesBid
```

**[FACT](https://docs.kalshi.com/getting_started/orderbook_responses)**

The immediate corollary — and the reason candidate strategy #1 is dead — is developed in _Candidate Mispricing / Arbitrage Opportunities_.

## Order book mechanics

- **Aggregated price levels**, not per-order. `yes_dollars_fp` / `no_dollars_fp` arrays of `[price_string, size_string]`, sorted ascending; **best bid is the last element** **[FACT](https://docs.kalshi.com/getting_started/orderbook_responses)**.
- **Price–time priority.** Kalshi exposes `GET /portfolio/orders/queue_positions`, described as "the number of contracts that need to be matched before an order receives a partial or full match, determined using price-time priority" **[FACT](https://docs.kalshi.com/api-reference/orders/get-queue-positions-for-orders)**. Queue position is therefore _directly observable_, which is unusual and useful for the passive extension.
- **Tick sizes are per-market and can be sub-penny.** `price_level_structure` labels a grid; `price_ranges` is the authoritative `{start, end, step}` array. Documented structures span $0.01 down to $0.0001, often _tapered_ (finer ticks below $0.10 and above $0.90). Multivariate combo markets use `center_deci_edge_centi_cent` **[FACT](https://docs.kalshi.com/getting_started/fixed_point_migration)**. **Any engine that assumes a 1¢ grid is wrong.**
- **Fractional contracts.** Minimum granularity 0.01 contracts **[FACT](https://docs.kalshi.com/getting_started/fixed_point_migration)**.

## Fees — exact

```
Taker: fees = round_up( M × 0.07   × C × P × (1 − P) )
Maker: fees = round_up( M × 0.0175 × C × P × (1 − P) )
P = price in dollars, C = contracts, M = per-series multiplier
Taker M defaults to 1; Maker M defaults to 0.
```

**[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**

Three consequences that shape the whole design:

1. **Maker orders are free on standard series** (M = 0). The published non-standard table assigns maker multiplier 1 to many sports/econ series and 0 to others. This is the single biggest lever on strategy economics.
2. **Rounding is per-order, not per-contract**, so per-contract fee is _decreasing_ in order size. At C = 1, P = $0.50 the rounded fee is ~$0.02 on a $0.50 contract — 4% — which annihilates any small basket. **Fee rounding, not the fee rate, is what kills small arbitrage.** **[HYPOTHESIS]**
3. **The exact rounding granularity is ambiguous in the current schedule** — the July 2026 text says rounding is "such that the fee + positionCost is rounded to a centicent", while the accompanying table shows whole-cent figures for single contracts **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**. **[ENGINEERING]** Do not guess. Calibrate empirically against `average_fee_paid` returned by the order endpoint **[FACT](https://docs.kalshi.com/api-reference/orders/create-order-v2)**, in the demo environment, and encode the observed rule.

## API and execution constraints

| Surface         | Detail                                                                                                                                                                                                                                                                                                                       |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Market data     | `wss://` WebSocket, channel `orderbook_delta`; **connection-level auth required even for public data**; snapshot-then-delta with monotonic `seq`; `update_subscription` supports `add_markets` / `delete_markets` / `get_snapshot`; server Ping ~every 10 s **[FACT](https://docs.kalshi.com/websockets/orderbook-updates)** |
| Order entry     | REST `POST /portfolio/events/orders` (V2), plus batch create/cancel; FIX available. **No order entry over WebSocket** **[FACT](https://docs.kalshi.com/api-reference/orders/create-order-v2)**                                                                                                                               |
| Auth            | RSA-PSS/SHA-256 signature over `timestamp + METHOD + path` (path excludes query string)                                                                                                                                                                                                                                      |
| Rate limits     | Token buckets; Basic 200 read / 100 write per second, default 10 tokens per request, ~2 s burst capacity above Basic; batch does **not** save tokens; 429 with no `Retry-After` **[FACT](https://docs.kalshi.com/getting_started/rate_limits)**                                                                              |
| Cancel/replace  | `Cancel Order`, `Amend Order` (price and/or count), `Decrease Order` all exist **[FACT](https://docs.kalshi.com/api-reference/orders/amend-order-v2)**                                                                                                                                                                       |
| Risk primitives | **Order groups**: a rolling 15-second matched-contracts limit that auto-cancels every order in the group when breached **[FACT](https://docs.kalshi.com/api-reference/order-groups/create-order-group)** — an exchange-side kill switch we should use rather than reimplement                                                |
| Historical data | Markets / candlesticks / trades / orders / positions only, split at a rolling ~3-month cutoff. **No order-book history.** **[FACT](https://docs.kalshi.com/getting_started/historical_data)**                                                                                                                                |

## Multivariate (combo) events

Kalshi supports "multivariate event collections" — combo markets whose settlement depends on a conjunction of legs (`mve_selected_legs`), created on demand via `POST /multivariate_event_collections/{ticker}/markets`, limited to 5,000 creations per week per user **[FACT](https://docs.kalshi.com/api-reference/multivariate/create-market-in-multivariate-event-collection)**. These carry their own fee row (`KXMVE`, maker multiplier 2 / taker 1) and a fine price grid **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**. They are the natural home of Fréchet–Hoeffding bound arbitrage (see _Possible Extensions_) but are **excluded from the baseline** on liquidity and complexity grounds.

---

# Candidate Mispricing / Arbitrage Opportunities

## C1 — Intra-market YES/NO complementarity — **REJECTED, and the rejection is a result**

The classic pitch: "buy YES and NO for less than $1." On Kalshi:

```
cost(YES) + cost(NO) = bestYesAsk + bestNoAsk
                     = (1 − bestNoBid) + (1 − bestYesBid)
                     = 2 − (bestYesBid + bestNoBid)
```

which is `< 1` **iff** `bestYesBid + bestNoBid > 1`. But a YES bid at `X` _is_ a NO offer at `1 − X` in the same single book **[FACT](https://docs.kalshi.com/getting_started/orderbook_responses)**. A YES bid and NO bid summing above $1 is therefore a **crossed book**, which a continuous matching engine resolves on arrival. **[ENGINEERING]** We assert this is structurally unattainable and treat any observation of it as a data-integrity bug, not a signal — precisely the diagnostic role it plays in our validation suite.

The analogous phenomenon on Polymarket is illustrative: its "mirrored order book" produces exactly this identity, and the authors of the NBA study had to build explicit deduplication to avoid double-counting the same dislocation through both the bid and ask paths **[FINDING](https://arxiv.org/html/2605.00864v1)**.

**Verdict:** dead as a strategy; retained as a **book-integrity invariant** in the replay engine. A screener that reports these is reporting bugs.

## C2 — Intra-event overround (mutually exclusive, NO-basket) — **SELECTED (primary)**

Buy NO on every leg of a mutually-exclusive event. At most one leg resolves YES, so at least `N − 1` NO contracts pay. Guaranteed profit when `Σ bestYesBid > 1 + fees`. Requires only the documented `mutually_exclusive` flag. Eligible for collateral return **[FACT](https://help.kalshi.com/en/articles/13823816-collateral-return)**.

## C3 — Intra-event underround (mutually exclusive **and** exhaustive, YES-basket) — **SELECTED (companion)**

Buy YES on every leg of a partition. Exactly one pays $1. Profitable when `Σ bestYesAsk < 1 − fees`. Requires machine-verified exhaustiveness from strike geometry. Applies cleanly to contiguous bucket series (Fed rate ranges, temperature buckets, crypto EOY price ranges) and to two-outcome sports events without draws.

## C4 — Monotone strike-ladder violations (subset relations) — **SELECTED (folded into the general LP)**

When markets in a series are defined by thresholds (`strike_type ∈ {greater, greater_or_equal, less, less_or_equal}`), their outcome sets are _nested_. If `A ⊆ B` then `P(A) ≤ P(B)`, giving a deterministic arbitrage when `bestBid(A) > bestAsk(B)`: buy YES on B, buy NO on A, worst-case payoff ≥ $1. This is the same relation the Polymarket study exploited between spread and moneyline markets (`{Δ > h} ⊂ {Δ ≥ 1}`) **[FINDING](https://arxiv.org/html/2605.00864v1)**, and it generalises C2/C3: **all three are instances of "find a non-negative combination of contracts whose payoff dominates its cost over a finite outcome partition."** They therefore share one LP formulation and one detector.

## C5 — Cross-venue (Kalshi ↔ Polymarket) — **REJECTED for baseline**

Documented as real and persistent: one study reports mean post-fee arbitrage profit of 4.87% with 89.1% of trading days presenting exploitable opportunities on a Clarity-Act contract pair **[FINDING](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6905683)**. But it is rejected here because: (a) capital cannot move between venues intra-trade, so both legs must be pre-funded, doubling capital and eliminating the "risk-free" framing under funding constraints; (b) resolution semantics differ — "semantic non-fungibility" means superficially identical contracts can settle differently, so it is _not_ pure arbitrage **[FINDING](https://arxiv.org/html/2601.01706v1)**; (c) it doubles the engineering surface (two auth schemes, two book models, on-chain settlement) for zero additional microstructure insight. It is a good **extension**, a bad **baseline**.

## C6 — Latency / stale-quote arbitrage — **REJECTED as a strategy, ADOPTED as a measurement**

Genuine stale-quote picking requires being faster than the incumbent market makers, which requires colocation and infrastructure well outside scope. However, _measuring how fast the opportunity dies_ is precisely the project's core experiment. We adopt latency as the **independent variable**, not the edge.

## C7 — Market making / spread capture — **REJECTED for baseline, noted as extension**

Economically attractive on Kalshi because maker fees default to zero, and empirically makers do earn: a 41.6M-trade study finds Kalshi market makers profitable, cross-subsidised by systematic YES-overbetting in single-name markets **[FINDING](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6615739)**. But it is _not arbitrage_ — it carries inventory and adverse-selection risk, needs a fair-value model, and turns the project into a different (larger) project. Excluded.

## C8 — Combo (MVE) Fréchet-bound arbitrage — **REJECTED for baseline, strongest extension**

For a combo contract on `A ∧ B`, deterministic bounds hold: `max(0, p_A + p_B − 1) ≤ p_{A∧B} ≤ min(p_A, p_B)`. Both bounds generate genuine arbitrage baskets (e.g. long combo + NO on A + NO on B has payoff ≥ 1 in every state). Mathematically the most elegant candidate. Rejected for baseline because combo markets must be _created_ before trading, liquidity is uncertain, and the fee table gives them a maker multiplier of 2 **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**.

---

# Comparison of Candidate Strategies

Scores are our own 1–5 assessments, not measurements. **[ENGINEERING]**

| Strategy                      | Math depth | Impl. difficulty (5 = easy) | Data availability | Realistic profitability | Microstructure relevance | Low-latency relevance | CV value | **Total** |
| ----------------------------- | ---------: | --------------------------: | ----------------: | ----------------------: | -----------------------: | --------------------: | -------: | --------: |
| C1 Intra-market YES/NO        |          1 |                           5 |                 5 |                   **1** |                        2 |                     2 |        1 |        17 |
| **C2 Intra-event overround**  |      **4** |                       **4** |             **5** |                   **3** |                    **5** |                 **5** |    **5** |    **31** |
| **C3 Intra-event underround** |      **4** |                       **4** |             **5** |                   **3** |                    **5** |                 **5** |    **5** |    **31** |
| **C4 Strike-ladder subsets**  |      **5** |                       **3** |             **5** |                   **3** |                    **5** |                 **4** |    **5** |    **30** |
| C5 Cross-venue                |          3 |                           2 |                 3 |                       4 |                        3 |                     4 |        4 |        23 |
| C6 Stale-quote                |          3 |                           1 |                 4 |                       2 |                        5 |                     5 |        3 |        23 |
| C7 Market making              |          4 |                           2 |                 5 |                       3 |                        5 |                     5 |        4 |        28 |
| C8 Combo Fréchet              |      **5** |                           2 |                 2 |                       2 |                        4 |                     3 |        5 |        23 |

**Reading of the table.** C2/C3/C4 win not because they score highest on any single axis but because they are the only cluster that is _simultaneously_ provably risk-free, buildable in weeks, fully observable from a free API, and genuinely latency-sensitive. C8 is more beautiful mathematically but is data-starved. C7 scores well but is a different project. Critically, C2, C3 and C4 **share one detector, one execution model, one risk engine, and one backtest** — three research questions for the price of one system. That is what makes the project compact.

---

# Selected Strategy

> **Detect and evaluate violations of intra-event no-arbitrage inequalities on Kalshi's mutually-exclusive market families, using a self-recorded full-depth L2 tape, with exact fee accounting, ladder-depth-limited sizing, non-atomic multi-leg execution, and a controlled latency-decay experiment.**

**Arbitrage category:** **pure arbitrage** at L0/L1 (non-negative payoff in every terminal state, positive in at least one), degrading to **latency/execution-risk arbitrage** at L2 once non-atomic legging is introduced. Naming that degradation precisely — and quantifying it — is the research contribution.

**Universe (proposed, ~4–8 event families):**

| Series                                                | Why                                                                                                                                  | Fee multiplier (as published)                                     |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------- |
| `KXBTCY` / `KXETHY`                                   | Exhaustive contiguous price-bucket partitions; **zero maker and taker fee** — the fee-free control group                             | 0 / 0 **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)** |
| `KXFED` / `KXFEDDECISION` / `KXRATECUTCOUNT`          | Exhaustive rate-range partitions; sharp scheduled information events (FOMC) → the natural setting for latency-sensitive dislocations | 1 / 1                                                             |
| `KXHIGH*` (daily temperature buckets)                 | Exhaustive bucket partitions, resolve daily → short capital lockup, high sample count                                                | check at runtime                                                  |
| One game series (`KXNFLGAME` / `KXNBA` / `KXMLBGAME`) | Two-outcome, mutually exclusive and exhaustive; **highest message rate**, so the best stress test for the latency experiment         | 1 / 1                                                             |
| One award/nominee series                              | Mutually exclusive but **non-exhaustive** — the negative control that proves the engine correctly refuses the underround trade       | 1 / 1                                                             |

That last row matters more than it looks: including a deliberately non-exhaustive family lets the project _demonstrate_ the distinction rather than merely assert it.

---

# Mathematical Formulation

## Setup

Let event `E` contain markets `M₁ … M_N`, each a binary contract with notional $1. Let `Ω` be the terminal sample space and `Xᵢ ∈ {0,1}` the settlement indicator of `Mᵢ`.

- **Mutual exclusivity (documented flag):** `Σᵢ Xᵢ ≤ 1` on every `ω ∈ Ω`.
- **Exhaustiveness (must be _proved_):** `Σᵢ Xᵢ = 1` on every `ω ∈ Ω`.

Books are bid-only ladders:

```
Yᵢ = [(y_{i,1}, q_{i,1}), …]   descending YES bids
Nᵢ = [(n_{i,1}, r_{i,1}), …]   descending NO  bids
```

Derived top-of-book:

```
bᵢ = y_{i,1}          best YES bid
βᵢ = n_{i,1}          best NO  bid
aᵢ = 1 − βᵢ           best YES ask   (implied)
αᵢ = 1 − bᵢ           best NO  ask   (implied)
```

Implied probability of leg _i_ is bracketed by `[bᵢ, aᵢ]`; there is no single "price," and using `last_price` here is a classic error — it is a stale trade print, not a tradable level.

## Condition A — Overround (NO-basket)

Buy 1 NO on each leg in subset `S ⊆ {1…N}`.

- **Cost:** `C_A(S) = Σ_{i∈S} αᵢ = Σ_{i∈S} (1 − bᵢ) = |S| − Σ_{i∈S} bᵢ`
- **Payoff on ω:** `Σ_{i∈S} (1 − Xᵢ) = |S| − Σ_{i∈S} Xᵢ ≥ |S| − 1` (by mutual exclusivity)
- **Worst-case profit:**

```
Π_A(S) = (|S| − 1) − C_A(S) − F_A(S)
       = Σ_{i∈S} bᵢ − 1 − F_A(S)
```

> **Arbitrage condition A: Σ\_{i∈S} bestYesBidᵢ > 1 + fees.**

**Subset optimality.** Because the `−1` is a constant independent of `S`, and each leg contributes `bᵢ − fᵢ` additively, the optimal subset at unit size is simply `S* = { i : bᵢ > fᵢ }`. **No search is needed.** This is a small but genuinely nice result: the combinatorial-looking subset problem collapses to a per-leg threshold test. (Depth makes this size-dependent; see below.)

**Why this only needs mutual exclusivity:** if _zero_ legs resolve YES, the basket pays `|S|`, which is strictly _better_ than the worst case. Non-exhaustiveness is a free option, not a risk. This asymmetry is why Condition A is the primary strategy.

## Condition B — Underround (YES-basket)

Requires exhaustiveness. Buy 1 YES on each of all `N` legs.

- **Cost:** `C_B = Σᵢ aᵢ = Σᵢ (1 − βᵢ) = N − Σᵢ βᵢ`
- **Payoff:** exactly `1` on every `ω`
- **Profit:**

```
Π_B = 1 − C_B − F_B = Σᵢ βᵢ − (N − 1) − F_B
```

> **Arbitrage condition B: Σᵢ bestYesAskᵢ < 1 − fees ⟺ Σᵢ bestNoBidᵢ > (N−1) + fees.**

Note the symmetry: A is a statement about the **sum of YES bids**, B about the **sum of YES asks**. Between them lies the aggregate spread, `Σᵢ (aᵢ − bᵢ)` — the event-level "overround band" a bookmaker would call vig. **[HYPOTHESIS]** The width of this band, not the mid-price, is what determines opportunity frequency; events with many legs have a mechanically wider band (it grows ~linearly in N) and therefore should exhibit _fewer_ violations per unit of individual-leg mispricing. This is directly testable and is one of the more interesting predictions the project can make.

## Condition C — Nested subsets (strike ladders)

If `outcome(A) ⊆ outcome(B)`, then buying YES on B and NO on A yields payoff `X_B + (1 − X_A) ≥ 1` always (since `X_A = 1 ⇒ X_B = 1`).

```
Π_C = 1 − a_B − α_A − F = b_A − a_B − F
```

> **Arbitrage condition C: bestYesBid(A) > bestYesAsk(B) + fees**, for `A ⊆ B`.

Subset relations are derived mechanically from `strike_type`, `floor_strike`, `cap_strike` **[FACT](https://docs.kalshi.com/api-reference/events/get-event)** — no NLP on rules text in the baseline. **[ENGINEERING]** Any market whose `strike_type` is `functional`, `custom`, or `structured` is excluded from automatic relation inference; those require a human-curated mapping and are out of baseline scope.

## The unifying LP

A, B and C are special cases of a single linear program. Let the strikes of all markets in scope induce a finite partition of `Ω` into cells `ω₁ … ω_K` (for a contiguous bucket series, `K = N` or `N+1` with an "outside" cell). Enumerate all tradable **legs** `j = 1 … L`, where a leg is a (market, side, price-level) triple with cost `c_j` (that level's price + its marginal fee) and capacity `u_j` (that level's resting size). Let `A ∈ {0,1}^{K×L}` be the payoff matrix, `A_{kj} = 1` iff leg _j_ pays $1 in cell _k_.

```
maximize        t
over            x ∈ ℝ^L , t ∈ ℝ
subject to      (A x)_k  −  cᵀx   ≥  t        for k = 1 … K
                0 ≤ x_j ≤ u_j                 for j = 1 … L
```

`t*` is the guaranteed worst-case profit of the best basket; `t* > 0` ⟺ arbitrage exists. Depth enters natively as the box constraints `x ≤ u`; walking the ladder is _not_ a separate heuristic. Fee non-linearity (the `P(1−P)` curve and the rounding) is handled by pricing each price-level leg at its own `c_j` and applying the per-order rounding correction post-solve.

**Why keep both the closed form and the LP.** The closed forms (A, B, C) run in O(1) per message and are what the live hot path uses. The LP runs offline over the recorded tape at every detected event, and serves three purposes: (i) it **validates** the closed forms — any disagreement is a bug; (ii) it handles irregular events where the partition is not a clean bucket ladder; (iii) it upgrades the project's mathematical framing from "sum the prices" to "solve for the worst-case-dominant portfolio," which is what a desk would actually recognise. Sizes are tiny (`K ≲ 30`, `L ≲ 300`), so any off-the-shelf simplex solves it in well under a millisecond.

## Fees, exactly

For a taker order of `C` contracts at price `P` with series multiplier `M`:

```
F_taker(C, P, M) = round_up( M × 0.07 × C × P × (1 − P) )
```

**[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**. For a NO purchase, `P` is the NO price, and since `P(1−P)` is symmetric about 0.5 the fee is identical whether expressed on the YES or NO leg — a small convenience worth noting.

**The rounding is the interesting part.** Because `round_up` applies to the _order_, not the contract, the per-contract fee is a decreasing step function of `C`:

```
per_contract_fee(C) = round_up(0.07 · C · P · (1−P)) / C
```

At `P = $0.50`: 1 contract ⇒ ~$0.0175 rounded up (≈3.5% of notional); 100 contracts ⇒ $1.75, i.e. $0.0175/contract. **[HYPOTHESIS]** There exists a _minimum viable basket size_ below which no intra-event arbitrage on standard-fee series is profitable, and this floor — not the raw fee rate — is the dominant fee-side friction. Measuring that floor per series is a concrete, quotable result.

## Capital

Per unit basket:

- **Condition A, no netting:** capital = `Σ_{i∈S} αᵢ ≈ |S| − 1`. Deploying `$X` of edge requires roughly `$X × (|S|−1)/Π_A` of capital — for a 5-leg basket with a 2¢ edge, ~200× the edge. **Capital intensity, not edge size, is the real economic constraint.**
- **Condition A, with collateral return:** Kalshi returns the guaranteed portion of a hedged mutually-exclusive basket immediately — "the maximum amount you can lose at settlement may be less than the amount you put in… we pay you back the difference immediately" **[FACT](https://news.kalshi.com/p/collateral-return)**. For a basket satisfying Condition A the guaranteed floor exceeds cost, so the released capital should approach or exceed the full outlay. **[ENGINEERING]** The exact accounting must be verified empirically in the demo environment; do not assume. Note also the flag `netting_enabled` **locks at first order placement per event and cannot be changed retroactively** **[FACT](https://help.kalshi.com/en/articles/13823816-collateral-return)** — an operational gotcha worth a line in the report.
- **Holding period:** to `settlement_ts`. With no settlement fee, holding is optimal; the correct return metric is therefore **profit per dollar-day of capital**, annualised.

## Worst-case payoff — the discipline

For every emitted basket the engine computes `min_ω payoff(ω)` by **explicit enumeration over the outcome partition**, including an "outside" cell whenever exhaustiveness is not machine-proven. A basket is only labelled _arbitrage_ if that minimum, net of all fees and the unwind cost of any leg that may fail to fill, is strictly positive. Anything else is labelled _statistical_ and reported separately. This one rule is what keeps the project honest.

---

# No-Arbitrage Conditions

Collected in one place, in the exact form the detector implements. All prices in dollars; `f` denotes the fee term for that basket.

| #      | Precondition                          | Condition (violation ⇒ arbitrage)   | Trade                 | Guaranteed profit / basket                       |
| ------ | ------------------------------------- | ----------------------------------- | --------------------- | ------------------------------------------------ |
| **N0** | Single market                         | `bestYesBid + bestNoBid ≤ 1`        | —                     | _Invariant._ Violation = crossed book = data bug |
| **N1** | `mutually_exclusive`                  | `Σ_{i∈S} bestYesBidᵢ ≤ 1 + f`       | Buy NO on all `i ∈ S` | `Σ bestYesBidᵢ − 1 − f`                          |
| **N2** | mutually exclusive **and** exhaustive | `Σᵢ bestYesAskᵢ ≥ 1 − f`            | Buy YES on all legs   | `1 − Σ bestYesAskᵢ − f`                          |
| **N3** | `outcome(A) ⊆ outcome(B)`             | `bestYesBid(A) ≤ bestYesAsk(B) + f` | Buy YES(B), buy NO(A) | `bestYesBid(A) − bestYesAsk(B) − f`              |
| **N4** | Any partition                         | LP optimum `t* ≤ 0`                 | LP solution `x*`      | `t*`                                             |

**Consistency requirements the engine asserts at every tick (validation harness, not trading logic):**

- `N0` holds for every market. Any violation halts the replay with a diagnostic — it means our delta application is wrong.
- `N1` and `N2` cannot both be violated simultaneously in the same direction: `Σ bᵢ > 1` and `Σ aᵢ < 1` are mutually inconsistent because `aᵢ ≥ bᵢ`. If both fire, the book state is corrupt.
- `N4` (LP) must agree with whichever of `N1`/`N2`/`N3` fired, to within floating-point tolerance. Disagreement is a bug in one or the other. This cross-check is cheap and catches an entire class of sign/indexing errors.

**Fee-inclusive thresholds are asymmetric.** Because `f` depends on the _prices_ of the legs and on basket size, the practical detection threshold is not a constant. The engine therefore screens on the raw inequality with a small tolerance and only computes exact fees on candidates — a two-stage filter that keeps the hot path at a handful of arithmetic operations.

---

# Market Microstructure Considerations

The central question of the entire project:

> **How much of the theoretical arbitrage survives once we model the actual order book and execution process?**

## Depth and the bottleneck leg

Basket size is `q = minᵢ (executable depth of leg i at an acceptable price)`. This is a hard minimum, not an average: one thin leg caps the whole basket. As you increase `q`, each leg walks further down its ladder, so realised edge per basket is _decreasing_ in size — the marginal basket is always the worst one. The engine therefore computes the full **edge-versus-size curve** `Π(q)` and reports both `argmax_q q·Π(q)` (profit-maximising) and `Π(q→0)` (the top-of-book number that naive screeners quote). **[HYPOTHESIS]** The ratio between these two will be the single most damning number in the study — the Polymarket analogue showed uncapped theoretical profit of $2,032 collapsing to $560 once a $100-per-episode budget was applied, with 76.9% of opportunities unable to absorb even $100 **[FINDING](https://arxiv.org/html/2605.00864v1)**.

## Spread, and why the _sum_ of spreads is the real hurdle

For an N-leg basket the trade crosses N spreads. The aggregate hurdle `Σᵢ (aᵢ − bᵢ)` grows roughly linearly in N while the mispricing that generates an opportunity does not obviously do so. **[HYPOTHESIS]** Opportunity frequency per event will _decline_ in N beyond some small N (perhaps 2–4), meaning **two-outcome sports events and short Fed ladders should dominate the opportunity count**, not the wide 10-bucket crypto ladders that look most promising on paper. If true, this is a satisfying, counterintuitive, defensible finding.

## Queue position

Only matters for the passive extension, but Kalshi makes it directly observable via the queue-position endpoints **[FACT](https://docs.kalshi.com/api-reference/orders/get-order-queue-position)**, which is unusually generous and worth one paragraph in the report even if the baseline is fully aggressive.

## Stale quotes and the post-close trap

Quote quality is not uniform across a market's life. The Polymarket study's most important methodological finding was that **81.1% of raw signals were post-game artefacts**, where the book looked crossed only because makers had withdrawn and nothing was actually executable; median spread there was 7,532 bps versus 1,031 bps in-game **[FINDING](https://arxiv.org/html/2605.00864v1)**. On Kalshi the analogous windows are: approaching `close_time`, during `settlement_timer_seconds`, during documented maintenance/pause windows, and in `determined`/`disputed` states. **[ENGINEERING]** The engine hard-excludes these and reports the excluded count — a screener that does not do this will over-report opportunities by roughly 5×.

## Adverse selection and the legging problem

Kalshi is not a benign venue. A 41.6M-trade study finds meaningful informed price impact (Kyle's λ, Glosten–Harris decomposition) especially in single-name markets, with one-sided flow (VPIN) predicting maker losses **[FINDING](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6615739)**. For our strategy this manifests as **legging adverse selection**: conditional on one leg failing to fill, the reason it failed is almost always that someone informed just took it. The unfilled leg is precisely the leg that has moved _against_ the basket.

**[HYPOTHESIS]** `E[edge | full fill] > E[edge | partial fill]`, strictly, and the gap widens with latency. If we observe this, it is a textbook winner's-curse result in a novel venue and easily the most interesting sentence in the write-up.

## Price impact

Modest in the baseline: baskets are small, all legs are marketable, and the trade is _corrective_ (it pushes prices back toward consistency). The engine nonetheless charges the full ladder walk, which is the correct first-order impact model for aggressive orders against a visible book.

## Opportunity half-life

Measured directly from the tape as time from violation onset to violation resolution. Because our own recorder defines the observation grid, we must be honest that measured durations are bounded by our capture resolution — the Polymarket authors were explicit that polling cadence makes frequencies a **lower bound** and durations an **upper bound** **[FINDING](https://arxiv.org/html/2605.00864v1)**. Our advantage: an event-driven WebSocket delta feed with exchange `ts_ms` has far finer resolution than their 3.6–5.5 s polling loop, so this study can plausibly resolve sub-second dynamics they could not.

---

# Low-Latency Architecture

The stated objective is **not** nanosecond performance. It is to demonstrate that we understand: _a trading strategy is constrained by the speed and reliability with which information moves through the system_ — and to **instrument** that claim rather than assert it.

```
                          ┌──────── monotonic clock taps at every arrow ────────┐
  Kalshi WSS
      │  t_exch  (ts_ms from exchange)
      ▼
  Feed Handler          ── t_recv   : kernel/user-space receipt
      │
      ▼
  Normalizer            ── t_parse  : fixed-point strings → integer ticks
      │
      ▼
  Order Book State      ── t_book   : delta applied, best-bid pointer updated
      │
      ▼
  Opportunity Detector  ── t_detect : O(1) sufficient-statistic test
      │
      ▼
  Sizing / Arb Engine   ── t_size   : ladder walk + exact fees
      │
      ▼
  Risk Checks           ── t_risk   : caps, staleness, dedupe, kill switch
      │
      ▼
  Execution Engine      ── t_send   : leg ordering + batching decision
      │
      ▼
  Order Gateway (REST)  ── t_ack    : exchange ts_ms on the order response
```

## Design decisions, with justification

| Decision                                          | Rationale                                                                                                                                                                                                                                                                                                                      |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Single-threaded event loop for the hot path**   | The strategy is _stateful across markets within an event_. Sharding by market would require locking the shared event statistic. One thread per event-family, share nothing, avoids lock contention entirely.                                                                                                                   |
| **Integer ticks everywhere**                      | Prices arrive as fixed-point strings; convert once at the boundary to `int` ticks using the market's `price_ranges` step. Never use floats in the book. Eliminates an entire class of comparison bugs and is faster.                                                                                                           |
| **Price-indexed arrays, not hash maps**           | A dense `int32[]` indexed by tick (≤10,000 slots for the finest documented grid) gives O(1) update and cache-friendly best-bid scanning. ~40 KB per market side — trivial at our universe size.                                                                                                                                |
| **Incrementally maintained sufficient statistic** | Keep `Σᵢ bᵢ` (and `Σᵢ βᵢ`) per event. A delta that changes leg _i_'s best bid updates the sum in O(1). **Detection cost is independent of N.** This is the single best algorithmic idea in the project and takes 20 seconds to explain on a whiteboard.                                                                        |
| **Two-stage detection**                           | Stage 1: integer comparison against threshold (a few ns). Stage 2: ladder walk + exact fee arithmetic, only on candidates. Keeps the common path trivially cheap.                                                                                                                                                              |
| **Zero allocation in the hot path**               | Pre-allocated ring buffers for events, pre-sized arrays for ladders, object reuse for opportunity records. Measurable in a GC'd language; state it and show the allocation counter at zero.                                                                                                                                    |
| **Batch order submission for legs**               | `Batch Create Orders (V2)` sends N legs in one HTTP round trip, minimising _inter-leg_ dispersion — the thing that actually causes partial baskets. It does **not** save rate-limit tokens **[FACT](https://docs.kalshi.com/getting_started/rate_limits)**, and it is **not** atomic, but it collapses N round trips into one. |
| **Sequence-gap = hard stop**                      | On a `seq` gap, mark the book _untrusted_, stop emitting, request a fresh snapshot **[FACT](https://docs.kalshi.com/websockets/orderbook-updates)**. Trading a corrupt book is worse than missing the trade.                                                                                                                   |
| **Dual timestamps**                               | Record both exchange `ts_ms` and local monotonic receipt time. Their difference is an estimate of feed latency + clock skew; its _variance_ is the useful signal.                                                                                                                                                              |

## Serialisation

Kalshi's WebSocket is JSON with numeric values as strings. **[ENGINEERING]** JSON parsing will plausibly dominate per-message cost. Mitigations worth benchmarking and reporting: a streaming/zero-copy parser; parsing only the four fields we need rather than full deserialisation; caching the ticker→slot mapping to avoid string hashing per message. Reporting a before/after µs-per-message number here is cheap and demonstrates real profiling discipline.

## What "low latency" honestly means here

We are a retail API client over the public internet. Realistic order round-trips are tens of milliseconds at best. **[ENGINEERING]** The correct framing — and the one to use in an interview — is: _"I cannot control network latency, so I measured what latency the strategy can tolerate, and compared it to what I actually observe."_ That reframes a limitation into the experimental design.

## Language choice

**[ENGINEERING]** Python (asyncio) for the collector, replay, analysis and plots — fastest path to a complete result. Then, _optionally_, port only the order book + detector to Rust or C++ and publish a like-for-like benchmark (messages/second, p50/p99 per-message latency). That single benchmark table is worth more on a CV than a full rewrite, and it costs a weekend. Do not start there.

---

# Baseline System Architecture

Seven components. Each should be independently testable.

```
┌──────────────────────────────────────────────────────────────┐
│ 1. RECORDER            live WSS → append-only tape           │
│    • universe resolver (events → tickers, metadata cache)     │
│    • orderbook_delta subscriber, seq tracking, resync         │
│    • dual timestamping, length-prefixed binary/Parquet sink   │
├──────────────────────────────────────────────────────────────┤
│ 2. REPLAY ENGINE       tape → deterministic book states       │
│    • strict timestamp ordering, no look-ahead                 │
│    • merges N market streams into one event timeline          │
├──────────────────────────────────────────────────────────────┤
│ 3. BOOK                per-market price-indexed ladders       │
│    • apply(delta), best_bid(side), walk(side, qty)            │
│    • N0 invariant assertion                                   │
├──────────────────────────────────────────────────────────────┤
│ 4. DETECTOR            O(1) screen + exact evaluation         │
│    • incremental Σb, Σβ per event                             │
│    • N1/N2/N3 closed forms; LP cross-check offline            │
├──────────────────────────────────────────────────────────────┤
│ 5. EXECUTION SIMULATOR L0 / L1 / L2                           │
│    • latency injection, per-leg fills, partials, unwind       │
├──────────────────────────────────────────────────────────────┤
│ 6. RISK ENGINE         caps, staleness, dedupe, kill switch   │
├──────────────────────────────────────────────────────────────┤
│ 7. ANALYTICS           metrics, latency sweep, plots, report  │
└──────────────────────────────────────────────────────────────┘
```

The **event timeline merge** in (2) is the one piece with a subtle correctness requirement, and it is where the analogous Polymarket work spent its methodological effort: build the union of all legs' update timestamps, and impute each leg's state by **strict forward-fill from its last observed update** to guarantee no look-ahead **[FINDING](https://arxiv.org/html/2605.00864v1)**. We inherit that design directly.

---

# Data Requirements

## What must be recorded

| Stream                               | Fields                                                                                                                                                 | Purpose                                                                 |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------- |
| `orderbook_snapshot`                 | `market_ticker`, full `yes_dollars_fp` / `no_dollars_fp`, `seq`, local `t_recv`                                                                        | Book bootstrap                                                          |
| `orderbook_delta`                    | `market_ticker`, `price_dollars`, `delta_fp`, `side`, `seq`, `ts_ms`, local `t_recv`                                                                   | Incremental state                                                       |
| `trade` (public)                     | price, size, ts                                                                                                                                        | Ground truth for "did anyone actually trade into this?"                 |
| `ticker`                             | last, volume, OI                                                                                                                                       | Context, sanity checks                                                  |
| Market metadata (REST, periodic)     | `mutually_exclusive`, `strike_type`, `floor_strike`, `cap_strike`, `status`, `close_time`, `settlement_timer_seconds`, `price_ranges`, fee multipliers | Structure + eligibility + exact fees                                    |
| Settlement outcomes (REST, post hoc) | `result`, `settlement_ts`                                                                                                                              | Ex-post verification that the "guaranteed" payoff actually materialised |

That last row is a quiet but important integrity check: for every basket the simulator claims was risk-free, resolve it against the _actual_ settlement and confirm the payoff was ≥ the claimed floor. If a basket ever settles below its guaranteed minimum, either the mutual-exclusivity assumption or the outcome partition was wrong — and finding even one such case is a publishable-quality caveat.

## Volume and duration

**[ENGINEERING]** Target: 4–8 event families, ~20–60 markets, recorded continuously for **2–4 weeks**, deliberately spanning at least one scheduled macro release (FOMC, CPI, or payrolls) and a run of sports games. Rough sizing: a busy market during a live game generates single-digit to low-tens of deltas per second **[ENGINEERING — to be measured]**; at ~50 markets averaging 1 delta/s over three weeks that is on the order of 10⁸ messages, and a compact binary row (≈24 bytes: ticker id, tick, delta, side, seq, ts) keeps this in the low single-digit GB range. Entirely tractable on a laptop; do not build a distributed pipeline.

## Storage format

**[ENGINEERING]** Two tiers: (a) a raw append-only log of exact received JSON, gzipped, for forensic replay and reproducibility; (b) a normalised Parquet/columnar table of decoded deltas for analysis. Never analyse the raw log directly, and never discard it.

## Known data limitations to state up front

1. **No pre-existing history.** The study can only cover the window you record **[FACT](https://docs.kalshi.com/getting_started/historical_data)**.
2. **Aggregated levels, not individual orders.** You cannot see order arrivals/cancellations individually, only net level changes — so queue-position modelling for passive strategies is approximate (mitigated by the queue-position endpoint for live orders).
3. **Your feed is not the matching engine's clock.** `ts_ms` is exchange-side; your receipt time includes network and parsing. Report both.
4. **Survivorship in the universe.** Choosing liquid series biases toward efficient markets, which biases the study _against_ finding opportunities. Say so; it makes any positive finding stronger.

---

# Execution Model

## The three levels

|        | Depth                               | Fees                     | Latency      | Fills                    | Interpretation                     |
| ------ | ----------------------------------- | ------------------------ | ------------ | ------------------------ | ---------------------------------- |
| **L0** | Top-of-book, unlimited              | None                     | 0            | Always full              | The number naive screeners publish |
| **L1** | Real ladder walk, bottleneck-capped | Exact formula + rounding | 0            | Always full              | "Perfect execution" upper bound    |
| **L2** | Real ladder at `t + δ`              | Exact                    | `δ` injected | Per-leg, partial allowed | Realistic                          |

**Edge-retention ratio `ρ = net_L2 / gross_L0`** is the project's headline statistic.

## L2 mechanics in detail

1. Opportunity detected at tape time `t` on book state `B(t)`.
2. Orders are conceptually released at `t + δ_decision` and arrive at `t + δ` where `δ = δ_decision + δ_network`.
3. Reconstruct `B(t + δ)` from the tape (this is why strict, gap-free replay matters).
4. Each leg is matched **independently** against `B(t + δ)`, as an `immediate_or_cancel` order at the originally computed limit price, walking that leg's ladder.
5. Record per leg: requested qty, filled qty, average fill price, fee.
6. **Basket reconciliation:** `q_filled = min over legs`. Contracts above `q_filled` on over-filled legs constitute a **naked residual**.
7. **Residual handling** (choose one policy and state it):
   - _Immediate unwind_: cross back at `B(t + δ + δ_unwind)`, paying a second taker fee. Conservative; the baseline.
   - _Hold to settlement_: mark to actual outcome. Realistic for some desks; higher variance.
     The baseline uses immediate unwind and reports both.

## Order-type choice per leg

| Choice                               | Effect                                                                                                                                                                              |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fill_or_kill` on every leg          | Eliminates partial fills _within_ a leg but **not** across legs — an all-or-nothing leg that kills leaves the other legs naked anyway. Reduces one failure mode, worsens fill rate. |
| `immediate_or_cancel` (**baseline**) | Take what's there, size down; residual is the realistic outcome to measure.                                                                                                         |
| `good_till_canceled` + `post_only`   | Passive variant. Zero maker fee on M=0 series **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)** but introduces queueing and adverse selection. Extension only.            |

**[ENGINEERING]** Run the baseline with IOC and additionally report the FOK counterfactual — a one-line change that produces a genuinely interesting comparison (fill rate vs. residual risk), and exactly the kind of trade-off a desk interviews for.

## Leg ordering

**[HYPOTHESIS]** Sending the **thinnest / most-likely-to-vanish leg first** dominates sending the cheapest leg first, because the bottleneck leg determines basket size and its disappearance invalidates everything else. The simulator supports both orderings; comparing them is a cheap, self-contained experiment with a clear economic story.

## Rate-limit modelling

At 10 tokens per order against a 100 tokens/s Basic budget **[FACT](https://docs.kalshi.com/getting_started/rate_limits)**, a 5-leg basket consumes 50% of one second's write budget. The simulator therefore enforces a token bucket and records **opportunities skipped due to rate limiting**. **[HYPOTHESIS]** During an FOMC print, when many events dislocate simultaneously, the rate limit — not latency — becomes the binding constraint. If so, that is a genuinely non-obvious systems finding and a strong talking point.

---

# Risk Management

Deliberately minimal but complete. Each control maps to a specific failure mode.

| Control                        | Setting                                                                                                                                                                                                                   | Failure mode addressed                                             |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| **Max position per market**    | `q_max_market`                                                                                                                                                                                                            | Concentration; settlement-rule surprise                            |
| **Max capital per event**      | `$X_event`                                                                                                                                                                                                                | One bad event family                                               |
| **Max total deployed capital** | `$X_total`                                                                                                                                                                                                                | Portfolio-level blow-up                                            |
| **Max order size**             | `q_max_order`                                                                                                                                                                                                             | Fat finger; ladder-walk bug                                        |
| **Max loss / drawdown kill**   | Daily loss cap → halt                                                                                                                                                                                                     | Systematic model error                                             |
| **Stale-data guard**           | Reject if `now − last_update > τ` (e.g. 500 ms) or `seq` gap unresolved                                                                                                                                                   | Trading a frozen or corrupt book                                   |
| **Duplicate-order protection** | UUID `client_order_id` per intent; suppress re-fire on unchanged book state (hash the relevant top-of-book)                                                                                                               | Same opportunity fired N times as N deltas arrive                  |
| **Kill switch**                | Local flag **plus** exchange-side **order groups** with a rolling 15-second contracts limit that auto-cancels the whole group on breach **[FACT](https://docs.kalshi.com/api-reference/order-groups/create-order-group)** | Runaway loop — the exchange-side version survives a crashed client |
| **Market-state gate**          | Only `active`; exclude pre-close window, pauses, `determined`/`disputed`                                                                                                                                                  | Post-close phantom liquidity                                       |
| **Exhaustiveness gate**        | Condition B disabled unless partition proven                                                                                                                                                                              | The single most common way to lose money on a "risk-free" basket   |
| **Residual limit**             | Halt new baskets while naked residual > `r_max`                                                                                                                                                                           | Compounding legging failures                                       |
| **Fee-model guard**            | Abort if computed fee deviates from `average_fee_paid` by > tolerance                                                                                                                                                     | Silent fee-schedule change                                         |

The last one is worth emphasising: the fee schedule is versioned and changes (the current one is dated 7 July 2026 and the API exposes `GET /exchange/series_fee_changes` and per-event overrides **[FACT](https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes)**). A strategy whose entire edge is 1–3¢ can be turned from profitable to unprofitable by a multiplier change. **Reading fees from the API at runtime is a risk control, not a nicety.**

**Explicitly out of scope:** VaR, portfolio optimisation, margin modelling, hedging. The positions are self-hedging by construction; adding portfolio risk machinery would be scope creep with no analytical payoff.

---

# Backtesting / Replay Methodology

## Core principle

The replay engine answers one question per opportunity:

> _If the strategy had been running at that moment, would the trade actually have been executable and profitable?_

Not "did a mispricing exist," but "could it have been taken."

## Mechanics

1. **Deterministic replay.** Events processed in strict `(ts, seq)` order. Same tape ⇒ byte-identical output. A determinism test (run twice, diff) is part of CI.
2. **No look-ahead, ever.** Leg states are forward-filled from their last _observed_ update only **[FINDING](https://arxiv.org/html/2605.00864v1)**. The detector may never read a message with `ts > t`. The latency simulator is the _only_ component permitted to look forward, and it does so explicitly and by exactly `δ`.
3. **Episode grouping.** Consecutive violating states are grouped into one **episode**. Profit is credited **once per episode** using a _one-shot_ paradigm — take the single best realisable basket within the episode, never the sum across snapshots, which would falsely assume regenerating liquidity **[FINDING](https://arxiv.org/html/2605.00864v1)**. Skipping this inflates results by orders of magnitude and is the most common backtest error in this space.
4. **Duration measurement.** Forward difference `t_{n+1} − t_n`, with a **trust ceiling** capping credited duration so that a feed outage is not recorded as a long-lived opportunity **[FINDING](https://arxiv.org/html/2605.00864v1)**. Report durations with censoring made explicit.
5. **Phase tagging.** Every episode tagged `pre-open / active / pre-close / post-close / paused`, and post-close excluded from headline numbers but reported separately — because that separation _is_ a finding.
6. **Ex-post settlement check.** Join every basket to actual market results and verify the realised payoff met the guaranteed floor.

## Validation suite

- **Book reconstruction:** periodically call `get_snapshot` via `update_subscription` during recording **[FACT](https://docs.kalshi.com/websockets/orderbook-updates)** and diff against the locally maintained book. Report mismatch rate; target zero.
- **Invariant N0:** assert on every tick.
- **Closed form vs. LP:** assert agreement on every detected episode.
- **Zero-latency sanity:** at `δ = 0`, L2 must reproduce L1 exactly.
- **Null test:** run the detector on a **synthetically shuffled tape** (timestamps randomised within markets, destroying genuine cross-market co-movement) and confirm the opportunity rate changes as predicted. A detector that finds the same number of "arbitrages" in scrambled data is finding noise.
- **Determinism:** identical outputs across runs.

---

# Latency Experiment

The centrepiece.

## Design

For each recorded episode, re-run execution under injected delays:

```
δ ∈ { 0, 5, 10, 25, 50, 100, 250, 500, 1000, 2000 } ms
```

Everything else held fixed. Additionally decompose `δ` into `δ_detect` (our processing) and `δ_network` (round trip), since only the first is under our control — and report our _measured_ `δ_detect` distribution (p50/p99) alongside the sweep, so the reader can locate the real system on the curve.

## Measured at each rung

| Metric                      | Definition                                                               |
| --------------------------- | ------------------------------------------------------------------------ |
| **Survival rate**           | Fraction of episodes still satisfying the arbitrage condition at `t + δ` |
| **Fill probability**        | Fraction of legs filled at requested size                                |
| **Basket completion rate**  | Fraction of baskets fully assembled (all legs)                           |
| **Edge retention**          | `realised_edge(δ) / theoretical_edge(0)`                                 |
| **Net P&L**                 | Including residual unwind cost and all fees                              |
| **Break-even latency `δ*`** | The `δ` at which expected net P&L crosses zero                           |

`δ*` is the number the whole project exists to produce. It is one scalar per series, it is directly comparable to the observed real-world round trip, and it answers the question a trading desk actually asks: _is this strategy latency-feasible for us, yes or no?_

## Robustness

- Report per-series `δ*`, not just pooled — **[HYPOTHESIS]** sports game markets will show `δ*` an order of magnitude smaller than year-end crypto range markets, because information arrival is bursty and continuous versus sparse and scheduled.
- Bootstrap confidence intervals over episodes; with a likely small episode count, **report interval estimates and refuse to report a Sharpe ratio if `n` is tiny**. Stating that explicitly is a credibility win.
- Sensitivity to the residual-handling policy (unwind vs. hold).

## Honesty clause

**[ENGINEERING]** If the recorded tape's effective resolution cannot support distinguishing 5 ms from 25 ms, say so and collapse the low rungs. The brief is explicit: "If the data does not support millisecond-level conclusions, use an appropriately conservative experimental design and clearly state the limitation." A study that reports a clean `δ*` it cannot support is worse than one that reports a coarse bound honestly.

---

# Evaluation Metrics

Only metrics obtainable from the data described above are listed. Metrics that are _not_ obtainable are flagged.

## Opportunity statistics

- Episodes total; per event-family; per hour; per market-hour
- % of wall-clock time in a violating state (per event, per phase)
- Edge distribution: mean / median / p90 / max, in cents per basket and bps of capital
- Duration distribution: median, p10/p90, with censoring noted
- Executable size at the bottleneck leg: distribution
- Opportunity count by leg-count `N` (tests the widening-spread hypothesis)
- **Rejection census** — a headline table in its own right: raw signals, minus post-close, minus depth-filtered, minus fee-negative, minus exhaustiveness-failed, equals actionable. The funnel _is_ the result.

## Execution statistics

- Full-fill / partial-fill / miss rate per leg and per basket
- Basket completion rate by `δ`
- Residual size distribution and unwind cost
- Measured internal latency: p50/p99 for parse, book update, detect, size
- % of opportunities surviving each `δ`
- Opportunities skipped due to simulated rate limiting

## P&L

- Gross (L0), fees, slippage-vs-top-of-book, residual cost, net (L1, L2 per `δ`)
- P&L per episode; per basket; per contract
- P&L per dollar of capital, and **per dollar-day** (annualised) — the correct metric given hold-to-settlement
- Max drawdown of the cumulative net curve
- Capital utilisation: deployed vs. available, with and without collateral return

## Strategy quality — with an explicit caveat

- Hit rate (episodes with net > 0)
- Profit factor
- Average holding period (detection → settlement)
- **Sharpe:** report **only if** the episode count is large enough to be meaningful. **[ENGINEERING]** For a hold-to-settlement arbitrage with lumpy, non-i.i.d. returns and a likely small `n`, a Sharpe ratio is close to meaningless. Saying "we deliberately do not report a Sharpe ratio because _n_ is too small and returns are not i.i.d." is a stronger signal of quantitative maturity than reporting one.

## Not obtainable — state plainly

- True per-order queue dynamics (feed is level-aggregated)
- Counterfactual market impact of our own hypothetical fills
- Anything requiring order-book history prior to the recording window
- Realised live P&L, unless a live/demo phase is actually run

---

# Experiments to Run

| #       | Experiment                                                                            | Output                                                        |
| ------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------- | --------- | ----------------------------------- |
| **E1**  | Universe characterisation: spread, depth, update rate, time-of-day profile per series | Descriptive tables; justifies universe choice                 |
| **E2**  | Raw opportunity census at L0 (top-of-book, no fees)                                   | The "naive screener" number — the strawman                    |
| **E3**  | Rejection funnel: apply each filter in sequence                                       | Attribution of _which_ friction removes what fraction         |
| **E4**  | L1: exact fees + ladder depth                                                         | Edge-vs-size curves; minimum viable basket size per series    |
| **E5**  | **Latency sweep** across all `δ` rungs                                                | Survival, fill, retention, net P&L; `δ*` per series           |
| **E6**  | Leg-ordering policy: thinnest-first vs. cheapest-first vs. simultaneous batch         | Completion rate and net P&L delta                             |
| **E7**  | Order type: IOC vs. FOK per leg                                                       | Fill rate vs. residual risk trade-off                         |
| **E8**  | Fee regime: zero-fee series (`KXBTCY`/`KXETHY`) vs. standard-fee series               | Isolates the fee effect from the microstructure effect        |
| **E9**  | Leg-count scaling: opportunity rate and edge vs. `N`                                  | Tests the "aggregate spread grows in N" hypothesis            |
| **E10** | Event-phase analysis: pre-open / active / pre-close / post-close                      | Quantifies the phantom-liquidity contamination                |
| **E11** | Macro-event study: FOMC/CPI windows vs. baseline                                      | Do scheduled information shocks generate dislocations?        |
| **E12** | Legging adverse selection: `E[edge                                                    | full fill]`vs.`E[edge                                         | partial]` | Tests the winner's-curse hypothesis |
| **E13** | Capital study: ROC with and without collateral return; dollar-day returns             | Turns cents-per-basket into an economically meaningful number |
| **E14** | Engineering benchmark: µs per message by stage; optional Python vs. compiled port     | The low-latency evidence                                      |
| **E15** | Validation suite (null test, snapshot diff, determinism, ex-post settlement)          | Credibility                                                   |

E1–E5 are the **required core**. E6–E15 are ordered by marginal value; stop when time runs out and say which you stopped at.

---

# Expected Results / Hypotheses

**All unverified. These are predictions to be tested, not findings.** Stating them in advance — and then reporting where you were wrong — is the single most credible thing this project can do.

| ID      | Hypothesis                                                                                                                                                                                                       | Reasoning                                                                                                                                                                               |
| ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------- | ---------------------- |
| **H1**  | Raw L0 opportunity counts will be **large** (hundreds to thousands) and almost entirely illusory                                                                                                                 | Top-of-book screens ignore fees, depth and market state; the Polymarket analogue rejected 81% of raw signals as post-close artefacts **[FINDING](https://arxiv.org/html/2605.00864v1)** |
| **H2**  | After the full filter funnel, actionable episodes will be **sparse** — plausibly single-digit to low-hundreds over a multi-week window                                                                           | Kalshi has active professional market makers **[FINDING](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6615739)**                                                                 |
| **H3**  | Depth, not edge size, will be the binding constraint; executable size will be small                                                                                                                              | Direct analogue: 76.9% of Polymarket combinatorial opportunities capped at ~14.8 shares **[FINDING](https://arxiv.org/html/2605.00864v1)**                                              |
| **H4**  | Median episode duration will be **seconds, not minutes**, and shorter in high-message-rate sports markets                                                                                                        | Automated participants restore consistency quickly                                                                                                                                      |
| **H5**  | `δ*` will be **materially larger than 0 but smaller than typical retail round trips** for liquid series — i.e. the strategy is latency-feasible for a colocated participant and marginal for a retail API client | The honest expected answer, and the whole point of measuring                                                                                                                            |
| **H6**  | Fee **rounding** at small basket size will dominate the fee rate; a minimum viable basket size will exist and be materially > 1 contract on standard-fee series                                                  | `⌈·⌉` per order is scale-dependent **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**                                                                                          |
| **H7**  | Zero-fee series will show **more** surviving opportunities than fee-bearing series, but not proportionally more — implying microstructure, not fees, is the dominant friction                                    | Tests whether fees or depth/latency dominate                                                                                                                                            |
| **H8**  | Opportunity frequency per event will **decline** with leg count `N` beyond small `N`                                                                                                                             | Aggregate spread hurdle grows ~linearly in `N`                                                                                                                                          |
| **H9**  | `E[edge                                                                                                                                                                                                          | full fill] > E[edge                                                                                                                                                                     | partial fill]`, widening with `δ` | Legging winner's curse |
| **H10** | Condition A (overround) will fire more often than Condition B (underround)                                                                                                                                       | A needs only mutual exclusivity; B needs a proven partition, so B's universe is strictly smaller                                                                                        |
| **H11** | During simultaneous macro dislocations, the **rate limit** will bind before latency does at Basic tier                                                                                                           | 10 tokens/order vs. 100 tokens/s **[FACT](https://docs.kalshi.com/getting_started/rate_limits)**                                                                                        |
| **H12** | Net P&L at realistic `δ` will be **small in absolute dollars but positive in bps**, and unattractive after capital lockup unless collateral return is enabled                                                    | The limits-to-arbitrage conclusion, in a new venue                                                                                                                                      |

**If H2 and H5 hold, the project's conclusion is: "Kalshi intra-event markets are microstructurally efficient at retail latency; the residual edge is real, rare, small, and latency-gated."** That is a strong, defensible, honest result — and a far better interview conversation than a suspicious equity curve.

---

# What Numbers We Want to Obtain

The concrete quantities the experiments must produce. **All placeholders.**

**Dataset scale**

- `___` markets across `___` event families, `___` days of continuous recording
- `___` order-book messages processed, `___` GB of tape
- `___` distinct events observed through to settlement

**Opportunity funnel** (the flagship table)

- Raw L0 signals: `___`
- After market-state filter: `___`
- After depth filter (≥ `q_min`): `___`
- After exact-fee filter: `___`
- After exhaustiveness gate: `___`
- **Actionable episodes: `___`** (i.e. `___`% of raw)

**Opportunity shape**

- Median / p90 edge: `___` ¢ per basket, `___` bps of capital
- Median / p90 duration: `___` s
- Median executable size at bottleneck: `___` contracts
- Minimum viable basket size: `___` contracts (standard fee), `___` (zero fee)

**Latency curve**

- Survival rate at 10 / 50 / 100 / 250 / 500 / 1000 ms: `___`%
- Edge retention at each rung: `___`%
- **Break-even latency `δ*`: `___` ms** (per series)
- Measured internal detect latency: p50 `___` µs, p99 `___` µs
- Measured feed-to-decision latency vs. `δ*`: strategy is / is not feasible

**Economics**

- Net P&L at realistic `δ`: `$___` on `$___` of deployed capital
- Return per dollar-day: `___` bps; annualised `___`%
- Edge-retention ratio `ρ = net_L2 / gross_L0`: `___`%
- Fill rate: `___`%; basket completion: `___`%; residual unwind cost: `$___`

**Engineering**

- Throughput: `___` messages/s sustained; `___` peak
- Book reconstruction mismatch rate vs. exchange snapshots: `___` (target 0)
- Optional compiled port speedup: `___`×

---

# CV-Relevant Outcomes

Templates only. **Fill from measured results; fabricate nothing.**

> **Kalshi Intra-Event Arbitrage Engine** — Built an event-driven prediction-market arbitrage engine in `<lang>`: recorded `___`M full-depth order-book messages across `___` Kalshi markets (no public L2 dataset exists), reconstructed books from an incremental delta feed with sequence-gap recovery, and detected multi-outcome no-arbitrage violations in **O(1) per message** via an incrementally maintained event-level sufficient statistic.

> Quantified arbitrage decay under execution latency: of `___` theoretical opportunities, `___`% survived exact Kalshi fees and order-book depth, and only `___`% remained profitable at `___` ms round-trip — establishing a break-even execution latency of `___` ms.

> Modelled non-atomic multi-leg execution including partial fills, per-leg adverse selection and residual unwind cost; showed that `___`% of theoretical edge is lost to `<depth / latency / fees>`, with `<binding friction>` the dominant constraint.

> Formulated multi-outcome no-arbitrage detection as a linear program over the outcome partition induced by contract strikes, and validated the O(1) closed-form detector against the LP optimum across `___` detected episodes.

**Note on framing.** If the result is that little or no edge survives, the correct bullet is _"quantified the limits of arbitrage in a regulated prediction market, showing X% of theoretical edge is unrealisable at retail latency"_ — which is a stronger claim than a profit number, because it is falsifiable and shows you measured rather than hoped.

---

# Implementation Roadmap

Eight phases. **[ENGINEERING]** Estimated 5–7 weeks part-time. Each phase ends in something demonstrable.

---

# Phase 1 — Data

**Goal:** a correct, continuously running tape.

1. Auth: RSA-PSS request signing; verify against the **demo environment** first.
2. Universe resolver: pull events, filter `mutually_exclusive = true`, expand to market tickers, cache metadata (`strike_type`, `floor_strike`, `cap_strike`, `price_ranges`, `status`, `close_time`, fee multipliers).
3. **Exhaustiveness prover:** from strike geometry, decide per event whether the legs partition `Ω`. Contiguous `between` buckets covering the range ⇒ exhaustive; a candidate list without a residual "field" market ⇒ **not** exhaustive. Emit a per-event certificate with the reasoning. _This module is the intellectual heart of Phase 1 and prevents the most expensive possible mistake._
4. WebSocket recorder: subscribe `orderbook_delta` for the universe (batch tickers per command), track `seq`, resync on gap, dual timestamps, append-only sink, auto-reconnect with exponential backoff.
5. Ops: supervisor/systemd, disk-space guard, daily rotation, heartbeat log.

**Done when:** 48 hours of continuous recording with zero unrecovered sequence gaps and a periodic snapshot diff of zero.

---

# Phase 2 — Market State

**Goal:** deterministic book reconstruction.

1. Tick converter driven by each market's `price_ranges` (**never** a hardcoded 1¢ grid).
2. Price-indexed ladder with `apply(delta)`, `best_bid(side)`, `walk(side, qty) → (avg_price, filled)`.
3. Invariant **N0** asserted every tick.
4. Replay engine: strict `(ts, seq)` ordering; per-event merged timeline with strict forward-fill.
5. Snapshot-diff validation harness.

**Done when:** replay is bit-for-bit deterministic and book state matches exchange snapshots exactly.

---

# Phase 3 — Arbitrage Detector

**Goal:** O(1) detection, plus an LP oracle.

1. Per-event incremental `Σ bᵢ`, `Σ βᵢ`, updated only when a leg's best bid changes.
2. Closed-form tests **N1**, **N2** (gated on the exhaustiveness certificate), **N3** (from the derived subset lattice).
3. Exact fee module, calibrated against `average_fee_paid` from demo-environment orders.
4. Sizing: edge-vs-size curve `Π(q)`; select profit-maximising `q` subject to depth and risk caps.
5. Offline LP oracle; assert agreement with the closed forms on every episode.
6. Episode grouper with one-shot profit crediting and trust-ceiling duration capping.

**Done when:** every detected episode has a hand-checkable worked example, and LP ≡ closed form.

---

# Phase 4 — Execution Simulator

**Goal:** L0 / L1 / L2.

1. L0 and L1 as above.
2. L2: latency injection, per-leg independent matching against `B(t+δ)`, partial fills, residual computation, unwind pricing.
3. Order-type and leg-ordering policies as switchable parameters.
4. Rate-limit token bucket with skipped-opportunity accounting.
5. Assert `L2(δ=0) ≡ L1`.

**Done when:** a single opportunity can be traced end-to-end and reconciled by hand.

---

# Phase 5 — Risk Engine

**Goal:** every control from the risk table, exercised by tests.

1. Implement caps, stale-data guard, dedupe (book-state hashing), residual limit, market-state gate, fee-model guard.
2. Local kill switch **plus** exchange-side order-group configuration.
3. Unit tests that _deliberately_ trigger each control.

**Done when:** each control has a test that fires it and a test that confirms it does not fire spuriously.

---

# Phase 6 — Backtesting

**Goal:** the full pipeline over the whole tape.

1. Batch runner across universe × configuration × latency grid.
2. Per-episode record with every field listed in _Backtesting Methodology_.
3. Ex-post settlement join and guaranteed-floor verification.
4. Validation suite: null test on shuffled tape, determinism, N0/LP invariants.

**Done when:** a single command reproduces every number in the report from the raw tape.

---

# Phase 7 — Latency Analysis

**Goal:** the centrepiece result.

1. Full sweep over `δ`.
2. Survival / fill / retention / net P&L curves; `δ*` per series with bootstrap CIs.
3. Measured internal latency distributions plotted on the same axis as `δ*`.
4. Sensitivity: residual policy, leg ordering, order type.

**Done when:** the `δ*` figure is producible for every series with a stated confidence interval, or the limitation is stated explicitly.

---

# Phase 8 — Results

**Goal:** the artefacts a reader actually consumes.

1. Figures: rejection funnel; edge distribution; duration survival curve; edge-vs-size; **edge retention vs. latency** (the money plot); cumulative net P&L; per-stage latency histogram.
2. Written report (6–10 pages): question, structure, math, method, results, limitations, conclusion.
3. README with a reproduction command, and a 10-slide / whiteboard summary.
4. **Optional Phase 8b:** paper-trade in the demo environment for 1–2 weeks and compare _predicted_ fill rates against _observed_ ones. This closes the loop between simulation and reality and is the highest-value optional addition in the entire plan.

**Done when:** someone can read the report and reproduce the headline number.

---

# Scope Boundaries

**Explicitly excluded, with reasons:**

| Excluded                              | Why                                                                                                                                                             |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Machine learning / deep learning / RL | The signal is a deterministic inequality. Adding a model would _reduce_ rigour by replacing a proof with a prediction.                                          |
| Fair-value / forecasting models       | Not needed for pure arbitrage. Needed for market making — a different project.                                                                                  |
| Cross-venue (Polymarket) arbitrage    | Doubles engineering and capital; introduces resolution-semantics risk that breaks the "pure arbitrage" claim **[FINDING](https://arxiv.org/html/2601.01706v1)** |
| Combo / MVE Fréchet-bound arbitrage   | Elegant but data-starved; markets must be created before trading                                                                                                |
| Distributed infra, Kubernetes, cloud  | One laptop and one VPS. Distributed systems add no analytical content.                                                                                          |
| FPGA / kernel bypass                  | Meaningless at internet round-trip latencies                                                                                                                    |
| Live trading at size                  | Optional demo-environment paper run only; the research question does not require real money                                                                     |
| Multiple unrelated strategies         | The value is depth on one mechanism, not breadth                                                                                                                |
| Perpetual futures / margin exchange   | Different fee schedule, different risk model, different project                                                                                                 |

**The 10–15 minute whiteboard test.** The entire project reduces to: _"Kalshi tells you at most one market in this event can pay. So the YES bids must sum to under a dollar. Sometimes they don't. Here's the trade, here's the exact fee, here's how deep the book actually is, and here's how fast the opportunity dies."_ Four boxes, two inequalities, one plot. If any addition cannot be defended inside that frame, it is out of scope.

---

# Limitations

Stated plainly; each should appear in the final report.

1. **Self-recorded data only.** No pre-existing L2 history exists **[FACT](https://docs.kalshi.com/getting_started/historical_data)**, so the study covers only the recorded window and cannot make claims about other periods, regimes, or seasons.
2. **Small sample risk.** If actionable episodes are few (H2), most statistics will be descriptive rather than inferential. Report intervals; avoid Sharpe.
3. **Simulated, not realised, execution.** Fills are modelled against the recorded book. Real fills are subject to queue dynamics, competing takers, and exchange-side behaviour we cannot observe. Phase 8b partially addresses this.
4. **No own-impact modelling.** Our hypothetical orders do not perturb the recorded tape. Acceptable at small size; false at scale.
5. **Level-aggregated feed.** No per-order visibility, so cancellation dynamics and true queue position are approximated.
6. **Latency is injected, not experienced.** The sweep is a controlled counterfactual, not a live measurement of our own stack end-to-end.
7. **Fee-schedule drift.** The schedule is versioned and multipliers vary by series and event **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**; results are conditional on the schedule in force during the recording window.
8. **Exhaustiveness is inferred, not certified.** The prover reads strike metadata; markets with `functional`/`custom`/`structured` strikes are excluded rather than guessed at.
9. **Universe selection bias.** Liquid series were chosen for data quality, which biases _against_ finding opportunities.
10. **Regulatory and operational risk excluded.** Market amendment, dispute, void, and early-close scenarios are handled as filters, not modelled economically.
11. **Collateral-return accounting is assumed, not verified**, unless a demo-environment test is run. Return-on-capital figures depend materially on it **[FACT](https://help.kalshi.com/en/articles/13823816-collateral-return)**.

---

# Possible Extensions

Ordered by marginal value per unit of effort.

1. **Passive (maker) variant.** Maker fees default to zero on standard series **[FACT](https://kalshi.com/docs/kalshi-fee-schedule.pdf)**. Instead of crossing all legs, quote the widest leg and cross the rest. Trades certainty for cost. Uses the queue-position endpoint. High value, moderate effort.
2. **Demo-environment live validation (Phase 8b).** Compare predicted vs. observed fill rates. Highest credibility-per-hour of anything on this list.
3. **Compiled hot path.** Port book + detector to Rust/C++; publish a like-for-like throughput and p99 benchmark.
4. **Combo (MVE) Fréchet-bound arbitrage.** `max(0, p_A + p_B − 1) ≤ p_{A∧B} ≤ min(p_A, p_B)`; both bounds yield deterministic baskets. The most mathematically attractive extension.
5. **Cross-venue with explicit semantic matching.** Only with a rules-text equivalence check and honest labelling as _statistical_, not pure, arbitrage **[FINDING](https://arxiv.org/html/2601.01706v1)**.
6. **Event-driven convergence study.** Measure information-incorporation speed around scheduled releases (FOMC/CPI/payrolls) using the same tape — a second paper from the same data at near-zero marginal cost.
7. **Toxicity-aware gating.** Adapt VPIN-style one-sided-flow metrics **[FINDING](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6615739)** to suppress basket firing when flow is toxic, and test whether it improves conditional fill quality.
8. **Public dataset release.** Publishing the recorded L2 tape would be a genuine contribution given that none exists **[FINDING](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6583921)** — subject to Kalshi's terms of use, which must be checked first.

---

# Final Definition of Done

The project is complete when **all** of the following are true:

**Data**

- [ ] ≥ 2 weeks of continuous L2 recording across ≥ 4 mutually-exclusive event families, including ≥ 1 zero-fee series and ≥ 1 deliberately non-exhaustive series
- [ ] Zero unrecovered sequence gaps; snapshot-diff mismatch rate of zero
- [ ] Per-event exhaustiveness certificates emitted and spot-checked by hand

**Correctness**

- [ ] Invariant N0 holds across the entire tape
- [ ] Closed-form detector agrees with the LP oracle on every episode
- [ ] `L2(δ=0) ≡ L1`
- [ ] Replay is deterministic across runs
- [ ] Null test on shuffled data behaves as predicted
- [ ] Every "risk-free" basket verified against actual settlement outcomes

**Analysis**

- [ ] E1–E5 complete
- [ ] Rejection funnel table populated with real counts
- [ ] Latency sweep complete with `δ*` per series and confidence intervals (or an explicit statement of why `δ*` cannot be resolved)
- [ ] Edge-retention ratio `ρ` reported
- [ ] Per-stage internal latency measured and plotted against `δ*`

**Artefacts**

- [ ] One command reproduces every headline number from the raw tape
- [ ] 6–10 page report with limitations section
- [ ] Six figures including the edge-retention-vs-latency plot
- [ ] README + 10-slide summary
- [ ] CV bullets populated **only** with measured numbers

**Discipline**

- [ ] No fabricated numbers anywhere
- [ ] Every claim tagged FACT / FINDING / ENGINEERING / HYPOTHESIS
- [ ] Hypotheses H1–H12 each explicitly marked supported, refuted, or untested

---

# Research Sources

## Primary — Kalshi official documentation

1. Kalshi Fee Schedule (eff. 7 July 2026) — taker/maker formulas, multiplier table, zero-fee series — https://kalshi.com/docs/kalshi-fee-schedule.pdf
2. Orderbook Responses — bid-only book, YES/NO reciprocity, spread derivation — https://docs.kalshi.com/getting_started/orderbook_responses
3. WebSockets: Orderbook Updates — snapshot/delta schema, `seq`, `ts_ms`, `get_snapshot` — https://docs.kalshi.com/websockets/orderbook-updates
4. Fixed-Point Representation — `price_ranges`, tick structures, fractional contracts — https://docs.kalshi.com/getting_started/fixed_point_migration
5. Rate Limits and Tiers — token buckets, tier budgets, batch costs — https://docs.kalshi.com/getting_started/rate_limits
6. Historical Data — what is and is not archived (no order-book history) — https://docs.kalshi.com/getting_started/historical_data
7. Get Event — `mutually_exclusive`, strike fields, market schema — https://docs.kalshi.com/api-reference/events/get-event
8. Create Order (V2) — TIF, `post_only`, `reduce_only`, STP, `average_fee_paid` — https://docs.kalshi.com/api-reference/orders/create-order-v2
9. Order Groups — rolling 15-second auto-cancel limits — https://docs.kalshi.com/api-reference/order-groups/create-order-group
10. Order Queue Position — price-time priority exposure — https://docs.kalshi.com/api-reference/orders/get-order-queue-position
11. Multivariate Event Collections — combo market creation — https://docs.kalshi.com/api-reference/multivariate/create-market-in-multivariate-event-collection
12. Collateral Return — netting mechanics, `netting_enabled` locking — https://help.kalshi.com/en/articles/13823816-collateral-return and https://news.kalshi.com/p/collateral-return
13. API Changelog — recent feed and rate-limit changes — https://docs.kalshi.com/changelog

## Academic and empirical research

14. Cheng, Yang & Zou (2026), _Arbitrage Analysis in Polymarket NBA Markets_, arXiv:2605.00864 — 75M LOB snapshots; single-market vs. combinatorial arbitrage; depth as the binding constraint; episode/duration methodology. **The closest methodological analogue to this project.** https://arxiv.org/html/2605.00864v1
15. Bartlett & O'Hara (2026), _Adverse Selection in Prediction Markets: Evidence from Kalshi_, SSRN 6615739 — 41.6M trades; Kyle's λ, Glosten–Harris, VPIN; maker profitability. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6615739
16. Marriott (2026), _Reconstructing Full Limit Order Books for Kalshi from WebSocket Streams_, SSRN 6583921 — confirms no public Kalshi L2 dataset; snapshot-anchored delta replay. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6583921
17. Saguillo, Ghafouri, Kiffer & Suárez-Tangil (2025), _Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets_, arXiv:2508.03474 — large-scale measurement of Polymarket arbitrage extraction.
18. Krause (2026), _From Forecasting Tool to Financial Asset: Evidence of Persistent Arbitrage in Prediction Markets_, SSRN 6905683 — Kalshi vs. Polymarket persistence. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6905683
19. _Semantic Non-Fungibility and Violations of the Law of One Price in Prediction Markets_ (2026), arXiv:2601.01706 — why cross-venue "arbitrage" is not pure arbitrage. https://arxiv.org/html/2601.01706v1
20. Shleifer & Vishny (1997), _The Limits of Arbitrage_, Journal of Finance 52(1) — the theoretical frame for every result this project is likely to produce.
21. Kyle (1985), _Continuous Auctions and Insider Trading_; Glosten & Milgrom (1985), _Bid, Ask and Transaction Prices_ — foundational adverse-selection microstructure.
22. Diercks, Katz & Wright (2026), _Kalshi and the Rise of Macro Markets_, NBER WP 34702 — macro-event context for the FOMC/CPI experiments.

## Practitioner / secondary (used only where primary sources are unavailable)

23. Kalshi Help Centre and news.kalshi.com explainers on event-contract mechanics and collateral return.
24. Community API guides on WebSocket keepalive, subscription limits and reconnect behaviour — useful operational detail, **verified against official docs before use**.

---

_End of specification. This document is a research plan and an implementation blueprint. It contains no measured results; producing them is the project._
