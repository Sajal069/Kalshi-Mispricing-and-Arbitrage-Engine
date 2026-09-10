# KIMA — Kalshi Intra-Event Mispricing & Arbitrage Engine

A latency-aware study of multi-outcome no-arbitrage violations in a regulated
binary event-contract exchange.

> **What fraction of theoretically risk-free intra-event arbitrage edge on Kalshi
> survives exact fees, finite order-book depth, non-atomic multi-leg execution,
> and realistic round-trip latency?**

---

## The idea in four sentences

Kalshi groups markets into _events_, and the API exposes a first-class boolean
`mutually_exclusive`: at most one market in the event can resolve YES. But those
markets trade on completely independent order books with no cross-market
matching — so a constraint that is guaranteed at settlement is enforced by nobody
at the quote level. When `Σ bestYesBid > $1`, buying NO on every leg locks in
`Σ bestYesBid − 1` in _every_ terminal state. The interesting part is not the
signal, which is one line of arithmetic; it is everything downstream — fees,
depth, legging risk and latency.

**The classic "buy YES and NO for under $1" trade is dead on arrival here**, and
saying so precisely is part of the result. Kalshi publishes a single bid-only
book where a YES bid at _X_ _is_ a NO offer at _1−X_, so that discrepancy is a
crossed book the matching engine would already have resolved. This engine treats
any sighting of it as a data-integrity bug (invariant **N0**), not a signal.

---

## Quick start

No credentials required — the synthetic path exercises the entire pipeline:

```bash
pip install -r requirements.txt
python -m kima all --hours 6            # simulate → backtest → validate → report
```

That writes `data/run/report.md`, eight figures, `metrics.json` and
`validation.json`. **One command reproduces every number in the report from the
raw tape.**

To record real data (connection-level auth is required even for public market
data, so a key is needed even though the recorder never places an order):

```bash
export KALSHI_KEY_ID=...                       # from Kalshi account settings
export KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem

python -m kima auth                                                # preflight the key
python -m kima --tape data/live/tape.jsonl.gz record --minutes 5   # smoke test
python -m kima --tape data/live/tape.jsonl.gz universe             # check the certificates
python -m kima --tape data/live/tape.jsonl.gz record --minutes 20160   # 2 weeks
python -m kima --tape data/live/tape.jsonl.gz settle               # once events resolve
python -m kima --tape data/live/tape.jsonl.gz report
```

`record` defaults to **production** because it is read-only and the demo
environment carries synthetic liquidity — a demo tape cannot answer a
microstructure question. Use `--env demo` only to rehearse the auth path.

The tape is append-only, so stopping and restarting `record` is safe and never
truncates. Run `settle` after the recorded events resolve: it fills in the actual
outcomes that the ex-post floor check joins against, and that check is the single
most valuable thing in the validation suite.

| Command    | What it does                                                                |
| ---------- | --------------------------------------------------------------------------- |
| `auth`     | Preflights credentials: key loads, signature verifies, exchange accepts     |
| `discover` | Resolves a real universe from REST and prints certificates (no auth needed) |
| `simulate` | Writes a synthetic tape (clearly stamped as such)                           |
| `record`   | Captures a live `orderbook_delta` tape with sequence-gap recovery           |
| `universe` | Resolves events and prints exhaustiveness certificates                      |
| `settle`   | Refreshes settlement outcomes once recorded events resolve                  |
| `backtest` | Replays, detects, executes and sweeps the latency ladder                    |
| `validate` | N0, snapshot diff, LP agreement, settlement floors, determinism, null test  |
| `report`   | Metrics, figures and the written report                                     |
| `export`   | Normalises a raw tape to Parquet for analysis                               |

---

## The conditions it detects

All prices in dollars; `f` is the fee term for the basket.

| #      | Precondition                          | Violation ⇒ arbitrage               | Trade                 | Guaranteed profit                    |
| ------ | ------------------------------------- | ----------------------------------- | --------------------- | ------------------------------------ |
| **N0** | single market                         | `bestYesBid + bestNoBid > 1`        | —                     | _Invariant._ A crossed book is a bug |
| **N1** | `mutually_exclusive`                  | `Σ bestYesBidᵢ > 1 + f`             | buy NO on all `i ∈ S` | `Σ bestYesBidᵢ − 1 − f`              |
| **N2** | mutually exclusive **and exhaustive** | `Σ bestYesAskᵢ < 1 − f`             | buy YES on every leg  | `1 − Σ bestYesAskᵢ − f`              |
| **N3** | `outcome(A) ⊆ outcome(B)`             | `bestYesBid(A) > bestYesAsk(B) + f` | buy YES(B), NO(A)     | `bestYesBid(A) − bestYesAsk(B) − f`  |
| **N4** | any partition                         | LP optimum `t* > 0`                 | LP solution `x*`      | `t*`                                 |

**N1 needs only mutual exclusivity.** If _zero_ legs resolve YES the basket pays
more than its worst case, so non-exhaustiveness is a free option, not a risk.

**N2 needs a proven partition**, and this is the single most expensive mistake
available in this strategy. Kalshi's own documentation is explicit that "the
markets do not need to exhaust every possible outcome". `kima/exhaustive.py`
therefore _refuses by default_ and emits a human-readable certificate for every
event explaining its reasoning:

```
[NOT EXHAUSTIVE] KXAWARD-000  (6 legs, 0 opaque)
  method: categorical
  reason: categorical outcome list with no residual field market; an unlisted
          outcome can win, so the YES basket can expire worthless
```

The synthetic universe deliberately includes such a family. Without one, "we
handle non-exhaustiveness correctly" would be an assertion rather than a
demonstration.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ 1. RECORDER      live WSS → append-only tape                 │  kima/recorder/
│    seq tracking, resync, dual timestamps, RSA-PSS signing    │
├──────────────────────────────────────────────────────────────┤
│ 2. REPLAY        tape → deterministic book states            │  kima/replay.py
│    strict (ts, seq) order, no look-ahead, gap → untrusted    │
├──────────────────────────────────────────────────────────────┤
│ 3. BOOK          price-indexed integer ladders, N0 invariant │  kima/book.py
├──────────────────────────────────────────────────────────────┤
│ 4. DETECTOR      O(1) screen + exact evaluation, LP oracle   │  kima/detector.py
├──────────────────────────────────────────────────────────────┤
│ 5. EXECUTION     L0 / L1 / L2, partial fills, residuals      │  kima/execution.py
├──────────────────────────────────────────────────────────────┤
│ 6. RISK          caps, staleness, dedupe, order groups       │  kima/risk.py
├──────────────────────────────────────────────────────────────┤
│ 7. ANALYTICS     metrics, latency sweep, figures, report     │  kima/analytics/
└──────────────────────────────────────────────────────────────┘
```

### Three design decisions worth defending

**Integer everything.** Prices are integer _centicents_ (1 dollar = 10 000), so
the finest documented Kalshi grid ($0.0001) is exactly 1 unit; quantities are
_centi-contracts_, matching the documented 0.01-contract granularity; money is
_microdollars_, chosen so `cost = price × qty` is exact with no division. No
float touches a book, a fee or a P&L. Tick sizes are per-market and often
tapered — finer in the tails — so `PriceGrid` is driven by the market's own
`price_ranges` and never a hardcoded penny.

**An O(1) sufficient statistic.** Each event maintains `Σ bestYesBidᵢ` and
`Σ bestNoBidᵢ` incrementally, so a delta that moves one leg updates the screen
with a single integer add — **detection cost does not grow with the number of
legs.** The screen is also _sound_: any profitable subset `S*` satisfies
`Σ_{S*} bᵢ ≤ Σ_all bᵢ`, so a profitable subset always forces the full sum above
$1. No false negatives. Exact sizing runs only on survivors.

**The LP is an oracle, not the hot path.** N1/N2/N3 are special cases of one
linear program over the outcome partition induced by contract strikes. The
closed forms run per message; the LP re-derives the answer offline and any
disagreement is a bug.

It has earned its place twice. It caught a partition bug that made unconstrained
NO legs look risk-free (an event whose geometry could not be inferred collapsed
to a single empty outcome cell, in which every NO leg appears to pay $1
unconditionally). And it caught a sizing bug worth ~16% of the optimum: the
first N1 optimiser used **one size for every leg**, so a leg too thin to fill the
basket was discarded rather than held at its own depth. The correct statement is
that fixing the largest leg at `M` makes the problem separable —

```
profit(M) = Σᵢ max_{q ≤ M} [ q − costᵢ(q) − feeᵢ(q) ]  −  M
```

— so each leg independently takes the best size it can reach. Equal sizing is
optimal only for the YES basket and the nested pair, whose payoff is `min(q)`.

---

## What the execution model actually models

|        | Depth                               | Fees                                | Latency      | Fills                    |
| ------ | ----------------------------------- | ----------------------------------- | ------------ | ------------------------ |
| **L0** | top-of-book, unlimited              | none                                | 0            | always full              |
| **L1** | real ladder walk, bottleneck-capped | exact formula **with its rounding** | 0            | always full              |
| **L2** | real ladder at `t + δ`              | exact                               | `δ` injected | per-leg, partial allowed |

The gap between them is the result. Because Kalshi has **no cross-market
atomicity**, a partially filled basket is not an arbitrage — it is a directional
position acquired at a bad price. So L2 models legs as independent orders,
computes the naked residual, and charges a second taker fee to unwind it.

`L2(δ=0) ≡ L1` is asserted in the test suite. It is the control for every other
row in the sweep, and it caught a real bug: zero-latency orders were waiting for
the next inbound message and silently becoming a positive-latency run.

### Fees, exactly

```
taker  fee = ⌈M × 0.07   × C × P × (1−P)⌉      M defaults to 1
maker  fee = ⌈M × 0.0175 × C × P × (1−P)⌉      M defaults to 0
```

Implemented in exact rational arithmetic and pinned by tests against every
figure in the published schedule ($1.75 / $0.63 / $0.07 per 100 contracts at
$0.50 / $0.90 / $0.99). The rounding granularity is genuinely ambiguous in the
current schedule, so it is a _configurable field_ with a calibration routine that
recovers the true rule from observed `average_fee_paid` — not a guess.

**The rounding, not the rate, is what kills small baskets.** The ceiling applies
per _order_, so a single contract at $0.50 pays ~$0.02 — 4% of notional — while
100 contracts pay $0.0175 each. A minimum viable basket size therefore exists,
and the engine measures it per series.

---

## Validation

`python -m kima validate` runs the checks that decide whether anything else is
worth reading:

| Check                | What it would catch                                                                                                                                                                                                             |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **N0 invariant**     | A crossed book means our delta application is wrong                                                                                                                                                                             |
| **Snapshot diff**    | Local book vs exchange snapshots; mismatches after a known gap are counted separately, because those prove the gap detector works                                                                                               |
| **LP agreement**     | Closed forms vs the LP optimum on every sampled basket                                                                                                                                                                          |
| **Settlement floor** | Every "risk-free" basket joined to actual outcomes; a single basket settling below its floor would invalidate the premise                                                                                                       |
| **`L2(δ=0) ≡ L1`**   | The latency control                                                                                                                                                                                                             |
| **Determinism**      | Same tape, byte-identical output                                                                                                                                                                                                |
| **Null test**        | The detector run on a tape whose per-market timelines are randomly offset — each market keeps its own dynamics, only cross-market coherence is destroyed. A detector finding the same rate on scrambled data is measuring noise |

The null test is worth reading closely. Scrambling produces **~21× more** raw
signals, not fewer: once an event's legs no longer share a coherent view of the
world, their prices stop summing to a dollar and the inequality is violated
constantly. That the detector fires far _less_ on real, coherent data is
evidence it is responding to genuine cross-market structure rather than to noise.

```bash
python -m pytest -q          # 150 tests
```

---

## Provenance, and a deliberate refusal

Kalshi serves **no historical order-book data** and no public Level 2 dataset
exists, so there is nothing to backtest against until you record your own tape.
That makes `kima/recorder/` the moat rather than the boilerplate.

The pipeline also runs on a **synthetic** tape when no live recording is present.
Those tapes are stamped `source="synthetic"` in their header, every report built
from one carries a provenance banner, every hypothesis is marked `UNTESTED (live)`
regardless of what the simulation showed, and the CV bullets are withheld until a
live tape is available. A simulation measures the engine, not the exchange.

**A self-recorded live tape now exists** (`data/live/`, 72-hour wall-clock span,
178 markets across KXBTCY / KXETHY / KXGDPYEAR / KXHIGHNY). Every number in the
CV bullets below is produced by a single deterministic replay of that tape;
none is hand-entered and all validation checks pass (`all_passed: true`).

The simulator is not rigged in the strategy's favour. Fair values within an
event are always coherent; dislocations arise _only_ from heterogeneous
market-maker reaction lag after an information shock — the same mechanism as
reality. So the structural predictions (the hurdle grows with leg count,
opportunities die as the slowest maker requotes) are emergent, not assumed.

---

---

## Scope boundaries

Excluded, with reasons: machine learning (the signal is a deterministic
inequality — a model would replace a proof with a prediction), fair-value
modelling (not needed for pure arbitrage), cross-venue arbitrage (resolution
semantics differ, so it is not pure arbitrage), combo/MVE Fréchet bounds
(elegant but data-starved), distributed infrastructure, and live trading at size.

**The whiteboard test.** The project reduces to: _"Kalshi tells you at most one
market in this event can pay, so the YES bids must sum to under a dollar.
Sometimes they don't. Here's the trade, here's the exact fee, here's how deep the
book actually is, and here's how fast the opportunity dies."_ Four boxes, two
inequalities, one plot. Anything that cannot be defended inside that frame is out
of scope.

See [`Research.md`](Research.md) for the full specification and citations.
