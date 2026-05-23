# VortExec — A Layer-by-Layer System Reference

A comprehensive walk-through of the VortExec project: every module, every
non-obvious decision, every piece of math and finance that the code touches.
Written so that a person who has never seen this codebase before — but who is
willing to read carefully — can come away with an actual mental model of how
each piece works and why it works that way.

This is not a quick tour. The intent is depth. If you want a one-page
summary, read `README.md` or the `vortexec.md` product description. If you
want to *understand the system*, read this.

---

## Table of contents

The document builds in layers, from "what is the world" to "how does the
specific Python module work."

**Part I — The ground**
1. The problem the system exists to solve
2. The financial concepts that show up everywhere
3. The system at a glance — architecture map and data flow

**Part II — The core (`src/vortexec/core/`)**
4. `core/types.py` — the canonical data shapes
5. `core/book.py` — the in-memory order book
6. `core/simulator.py` — the deterministic walk
7. `core/features.py` — what the ML model will see

**Part III — Talking to exchanges (`src/vortexec/venues/`)**
8. `venues/base.py` — what every exchange connector must do
9. `venues/binance.py` — connecting to Binance, the bootstrap protocol, the `_Aligner`

**Part IV — Keeping a book alive (`src/vortexec/maintainer/`)**
10. `maintainer/book_maintainer.py` — the live-book lifecycle, pub/sub, resync, health

**Part V — Persistence (`src/vortexec/recorder/`)**
11. `recorder/parquet_recorder.py` — diffs to disk, periodic snapshots, file layout

**Part VI — The API (`src/vortexec/api/`)**
12. `api/models.py`, `api/deps.py`, `api/server.py` — Pydantic, dependency injection, app factory
13. `api/routes/health.py` and `api/routes/pretrade.py` — the two HTTP endpoints

**Part VII — Orchestration (`src/vortexec/service.py`)**
14. `service.py` and `__main__.py` — wiring it all together, signal handling, multi-symbol

**Part VIII — Operations (`deploy/`)**
15. The systemd unit, the run wrapper, the bootstrap script
16. Backup, daily diff-count check, external monitoring

**Part IX — What isn't built yet, and why**
17. The ML layer, auth, posttrade, multi-venue, and the deferral logic

A reader pressed for time can skip Part I and start at Part II, but the
financial concepts in Section 2 are referenced everywhere afterwards; if any
term feels unfamiliar later on, return there.

> **Document status.** Sections 1-5 are written below in this turn. The
> remainder are produced in subsequent turns and appended to this same file.

---

# Part I — The ground

## 1. The problem the system exists to solve

Imagine you have a Python script that decides, every now and then, that it
wants to buy 5 BTC on Binance. Before the script actually sends the order, it
needs to answer a question:

> **How much will this trade cost me, in basis points of the mid price, if I
> send it right now?**

This is the *pre-trade slippage estimation* problem. It sounds simple. The
naive answer is "look at the best ask price and assume you fill there", but
this is only correct when the size you want to trade is smaller than the
quantity sitting at the best ask. Once your trade size exceeds the top
level's quantity, your order eats through the top level and walks down (or
up, depending on side) into deeper, worse-priced levels. The actual average
fill price is a function of the *shape* of the order book at that exact
moment.

The shape of the order book changes constantly. On Binance, the depth feed
publishes incremental updates every 100 ms. Within any 15-minute window the
top-of-book quantity for BTCUSDT can change by an order of magnitude, the
spread can widen or tighten, and the imbalance between bid and ask depths
can flip sign. As a direct consequence, the cost of executing the same
hypothetical trade — same side, same size — varies meaningfully *as a
function of when you ask the question*.

Concretely: in a recording we made on 2026-05-12, between 21:00 and 22:00
UTC, a market buy of 5 BTC on Binance produced slippages from ~0 bps (when
the top ask was deep) to 2.45 bps (when the top ask had thinned to under
0.5 BTC). Same trade, same exchange, fifteen minutes apart, $99 vs $0
in absolute cost on a ~$405,000 trade. The cost was not a property of the
trade itself; it was a property of *the moment in which the trade was
executed*.

This is the gap that VortExec exists to address. Most retail crypto trading
tools either (a) don't model depth at all and just quote a single number
based on the best bid/ask, or (b) quote some recent average that is wrong by
a factor of ten when conditions deviate from average. Algorithmic traders
who care about execution quality need a number computed against the *current*
order book — not an average, not a top-of-book proxy. They also need a sense
of the *tail* of the cost distribution, because a strategy that prices in
average slippage and gets blindsided by a 95th-percentile slippage moment
loses money on its worst executions.

VortExec's design responds to those two needs:

- A **deterministic walk** of the live, in-memory order book gives an exact
  slippage number for any hypothetical `(side, size)` against the current
  state of the book. The accuracy of this number is bounded only by the
  accuracy of the maintained book — i.e. by how faithfully the system
  reflects the exchange's actual book.
- A **quantile-regression model** trained on book features (spread, depth,
  imbalance, etc.) and historical outcomes provides calibrated tail
  estimates — P50, P90, P95 — to capture the conditional distribution of
  slippage given the current state. (The model itself does not yet exist
  in the codebase; the *features* and the *recording infrastructure* it
  needs do exist. The model is added once enough data has accumulated.
  See Section 17.)

The first half of that — the deterministic walk on a maintained live book —
is the engineering substrate of the project. It is what every other feature
will eventually sit on top of. The reason this document exists is because
that substrate has many subtle layers, and shipping a correct deterministic
walk requires shipping all of them correctly. A weak link anywhere — a
miscounted sequence number, a botched bootstrap protocol, a stale book —
makes the slippage number wrong, and a wrong slippage number is worse than
no number, because traders take it at face value.

The rest of this document explains every layer.

---

## 2. The financial concepts that show up everywhere

The codebase touches a small number of finance concepts repeatedly. They are
not deep but they have to be precisely understood, because using them
sloppily creates bugs that are very hard to find later. This section
defines each.

### 2.1 Limit orders, market orders, and the order book

A **limit order** is an instruction to a venue: "buy up to X units of this
asset at no more than price P" (or "sell at no less than price P"). The
venue does not execute it immediately; instead, it sits in a queue waiting
for a counterparty willing to trade at that price or better.

A **market order** is an instruction: "buy (or sell) X units immediately, at
whatever the best available price is right now." It is matched against
existing limit orders on the opposite side, walking through them from best
price outward until X units are filled.

The **order book** is the data structure the venue maintains. It has two
sides:

- **Bids**: the limit-buy orders, indexed by price. The buyer at the highest
  price is at the top.
- **Asks**: the limit-sell orders, indexed by price. The seller at the
  lowest price is at the top.

At each price, the venue holds a single aggregated quantity (the sum of all
orders sitting at that price). A "level" of the book is one such (price,
quantity) pair. So the book is two sorted lists of levels, one for each side.

Throughout the codebase a "level" is exactly that: a frozen `Level` object
with `price` and `quantity` fields (see Section 4).

### 2.2 Best bid, best ask, mid, spread

- **Best bid**: the highest price someone is currently willing to buy at.
- **Best ask**: the lowest price someone is currently willing to sell at.
- **Mid price**: the arithmetic mean of best bid and best ask. `mid = (bid + ask) / 2`. This is the conventional "fair price right now" reference. It does not correspond to a price anyone is actually trading at; it is a midpoint.
- **Spread**: `best_ask − best_bid`. Always non-negative on a well-functioning market. A wide spread implies either low liquidity or high volatility (market makers demanding more compensation for the risk of being on either side).

When `best_bid` or `best_ask` is undefined — because the book has no levels
on that side — `mid` and `spread` are undefined too. The codebase
consistently uses `None` to mean "undefined" rather than 0 or NaN. This
matters a lot in the simulator and feature extraction, because asking
"what's the slippage on a buy order against an empty ask side?" is a
genuinely undefined question and the right answer is "I cannot tell you",
not "0 bps".

### 2.3 Slippage and basis points

When a market order eats through several levels, the average price you paid
is not the best ask. It is the size-weighted average of the prices at each
level your order touched. The difference between that average and the
*mid* at the moment of execution is the **slippage**.

Formally, for a market buy of size $S$ that consumes $q_i$ units at price
$p_i$ across levels $i = 1, 2, \ldots, k$ (with $\sum q_i = S$):

$$
\text{avg\_price} = \frac{\sum_i p_i q_i}{\sum_i q_i} = \frac{\sum_i p_i q_i}{S}
$$

$$
\text{slippage}_{\text{buy}} = \frac{\text{avg\_price} - \text{mid}}{\text{mid}}
$$

For a market sell, the sign convention flips so that slippage is still
non-negative under "worse than mid":

$$
\text{slippage}_{\text{sell}} = \frac{\text{mid} - \text{avg\_price}}{\text{mid}}.
$$

Both quantities are unitless ratios. To convert to **basis points (bps)**, multiply
by $10{,}000$:

$$
\text{slippage in bps} = \text{slippage} \times 10{,}000.
$$

A bps is one one-hundredth of a percent, i.e. $1/10{,}000$. So 1 bps
slippage on a 5 BTC trade at $80{,}000 \times 5 = \$400{,}000$ is
$\$400{,}000 / 10{,}000 = \$40$. Bps is the standard unit of cost in
trading because it lets you compare costs across vastly different trade
sizes and asset prices.

The simulator (Section 6) implements exactly this calculation.

### 2.4 Why bps not absolute dollars

Two reasons.

First, comparability. A 1 bps cost on a $1M trade is $100; the same 1 bps on
a $10K trade is $1. The dollar amounts are wildly different, but the
*relative* cost is identical, and for an algorithmic trader sizing many
trades it is the relative cost that determines whether the strategy is
profitable. Quoting in bps lets you compare execution quality across trade
sizes without doing arithmetic in your head.

Second, distributional shape. Slippage in bps is roughly the same order of
magnitude across asset prices and trade sizes (typically 0–100 bps for
everything except outliers), which means a single quantile model can be
trained across many regimes. Slippage in dollars varies by 4-5 orders of
magnitude depending on trade size, which is much harder for a model to
generalise across.

The internal API uses bps everywhere. Dollar amounts only show up in the
HTTP API response and in chart titles, both as derived quantities for
end-user readability.

### 2.5 The depth feed: snapshots and diffs

A venue cannot keep transmitting the full order book every 100 ms — the
book has thousands of levels, and the message would be huge. Instead, every
real exchange uses an **incremental** publication model:

1. The client makes a one-off **REST snapshot** request to get the current
   state of the book at a particular moment. The response includes a
   *sequence number* (Binance calls it `lastUpdateId`).
2. The client subscribes to a **WebSocket diff stream**. Each WS message
   describes a small set of changes — a few levels whose quantities have
   changed (or whose quantity is now zero, meaning the level was removed).
   Each message carries its own sequence range (Binance: `U` is the first
   update id in the message, `u` is the last).
3. The client maintains the book in memory by applying the snapshot, then
   applying every diff in order, validating that no diff in the sequence
   was missed.

The interesting part is gluing the snapshot to the diffs. If you do it
naively — fetch the snapshot, then start the WS — you'll miss the diffs
that arrived during the fetch, and your maintained book diverges. The
*correct* protocol (which Binance documents in one specific paragraph and
which most retail libraries get wrong in a way the trader will never
notice) is to start the WS *first*, buffer everything that arrives,
*then* fetch the REST snapshot, then drop buffered diffs already covered
by the snapshot and start applying from the first one that bridges
`lastUpdateId + 1`. Section 9 explains this in full.

When you do it correctly, your in-memory book stays in lockstep with the
exchange's. When you do it wrong, your "best ask" drifts from reality by
seconds to minutes, and your slippage estimates become detached from
the actual market. This is the foundational correctness problem of the
project.

### 2.6 Sequence numbers and gaps

Each diff message has a sequence range. The contract is that successive
diffs are contiguous: `current.U == previous.u + 1`. If a message arrives
where `current.U > previous.u + 1`, you have a **sequence gap** —
something was lost in transit, your book is now missing one or more
updates, and the cleanest recovery is to discard everything and
re-bootstrap (fetch a fresh snapshot, restart the WS alignment).

Gaps happen, rarely, on long-running connections. The system has to
detect them and recover automatically. The `_Aligner` class (Section 9)
does the detection; the `BookMaintainer` (Section 10) does the recovery.

### 2.7 Imbalance

Within a small window of the top of book, there is often more aggregate
quantity on one side than the other. **Imbalance** is the conventional
summary of this:

$$
\text{imbalance} = \frac{\text{depth}_{\text{bid}} - \text{depth}_{\text{ask}}}{\text{depth}_{\text{bid}} + \text{depth}_{\text{ask}}}.
$$

It ranges from $-1$ (all volume on the ask side, no bids) to $+1$ (all
volume on the bid side, no asks). $0$ means perfectly balanced. The
choice of "depth" — top 5? top 10? all levels? — is a parameter; the
codebase uses top-10 by default, on the reasoning that it captures
slightly deeper context than top-5 without being so deep that it includes
faraway levels that are unlikely to matter to the next few seconds of
trading.

Imbalance is one of the strongest single-feature predictors of short-term
price direction in the academic microstructure literature. A book with
imbalance $+0.8$ (heavily bid-skewed) is much more likely to tick up than
down in the next few seconds; a book with imbalance $-0.8$ is more likely
to tick down. The intuition is that aggressive buyers have already eaten
through one side and are queueing on the other; the side with more queue
depth is the side under pressure to clear.

We extract this as a feature in `extract_features` (Section 7). It will
be one of the inputs to the future quantile model.

### 2.8 Latency, staleness, and "now"

A pre-trade slippage estimate is implicitly answering "what would happen
if I sent this trade right now?" — but "right now" is fuzzy. By the time
the API response reaches the trader, by the time the trader's algo
decides to send the order, by the time the order reaches the exchange,
the book has moved by a few milliseconds. The estimate has a built-in
staleness bounded by the round-trip latency between the trader and the
maintained book, plus the trader's own decision and order-placement
latency.

For most trades and most market conditions, this staleness is negligible
(a few hundred bps of a basis point). For moments of rapid book change —
which are exactly the moments that matter — the staleness can be large
relative to the slippage itself. This is why the *quantile model* exists:
the deterministic estimate is a snapshot; the quantile model captures the
distribution of outcomes conditional on the current snapshot, including
how the book is likely to evolve over the next few hundred ms.

The maintainer's `is_healthy()` flag (Section 10) is the system's
self-report of how stale the book is. If `is_healthy()` returns `False`,
no one should be trusting the slippage estimate, because the book is
known to be lagging real-world events.

---

## 3. The system at a glance

This section is a short bridge between the abstract concepts in Section 2
and the per-module deep dives that follow. It establishes the architecture
map so that you can place every later section in context.

### 3.1 The data flow, end to end

Schematically:

```
┌─────────────┐    REST snapshot                    ┌──────────────┐
│             │◄─────────────────────────┐          │              │
│   Binance   │                          │          │   HTTP API   │
│             │    WS @depth@100ms       │          │  (FastAPI)   │
│             │─────────────────────────┐│          │              │
└─────────────┘                         ││          └──────┬───────┘
                                        ▼▼                  │
                              ┌──────────────────┐          │
                              │ BinanceConnector │          │
                              │  (venues/...)    │          │ /v1/estimate
                              └────┬─────────┬───┘          │
                                   │ snapshot│ aligned       │ /health
                                   │         │ Diff stream   │
                                   ▼         ▼               │
                              ┌──────────────────┐           │
                              │   BookMaintainer │◄──────────┘
                              │  (maintainer/..) │  get_book()
                              │                  │  is_healthy()
                              │  - apply diffs   │
                              │  - resync on gap │
                              │  - publish       │
                              └────┬─────────┬───┘
                                   │         │
                          stream_  │         │ get_book →
                          updates  │         │   simulate_market_order
                                   ▼         │   extract_features
                         ┌──────────────────┐│
                         │ ParquetRecorder  ││
                         │ (recorder/...)   ││
                         │  - buffer diffs  ││
                         │  - hourly files  ││
                         │  - 10-min snaps  ││
                         └────────┬─────────┘│
                                  │          │
                                  ▼          ▼
                        ┌────────────┐ ┌──────────────┐
                        │  Parquet   │ │ HTTP response│
                        │  on disk   │ │              │
                        └────────────┘ └──────────────┘
```

Every component lives in one Python process, sharing one asyncio event
loop. There are no databases, no message brokers, no microservices. The
maintainer is the central authority on "the current book"; everything
else is a producer (the connector) or a consumer (the recorder, the
HTTP API).

### 3.2 The module map

The Python package layout under `src/vortexec/`:

| Module | Lines (approx) | What it owns |
|---|---|---|
| `core/types.py` | ~30 | Frozen dataclasses. The vocabulary every other module speaks. |
| `core/book.py` | ~70 | `OrderBook` — the in-memory book, with `apply_snapshot`, `apply_diff`, `best_bid/ask/mid/spread`. |
| `core/simulator.py` | ~70 | `simulate_market_order` — the deterministic walk + slippage math. |
| `core/features.py` | ~60 | `extract_features` — compute the inputs the future ML model needs. |
| `venues/base.py` | ~30 | `VenueConnector` ABC + `SequenceGapError`. |
| `venues/binance.py` | ~150 | Binance connector, the bootstrap protocol, the `_Aligner`. |
| `maintainer/book_maintainer.py` | ~150 | `BookMaintainer` — owns the live-book lifecycle and pub/sub. |
| `recorder/parquet_recorder.py` | ~200 | `ParquetRecorder` — writes diffs and snapshots to Parquet. |
| `api/models.py` | ~50 | Pydantic request/response shapes. |
| `api/deps.py` | ~20 | FastAPI dependency-injection wiring. |
| `api/server.py` | ~25 | FastAPI app factory. |
| `api/routes/health.py` | ~25 | `GET /health`. |
| `api/routes/pretrade.py` | ~55 | `POST /v1/estimate`. |
| `service.py` | ~150 | Top-level orchestration: wires N trios, runs HTTP, signals. |
| `__main__.py` | ~3 | Entry point so `python -m vortexec` works. |

The dependency direction is strict: `core/` depends on nothing within the
project (only stdlib + `sortedcontainers`). `venues/` depends on `core/`.
`maintainer/` depends on `core/` and `venues/`. `recorder/` depends on
`core/` and `maintainer/`. `api/` depends on everything except `service.py`.
`service.py` depends on everything. There are no circular imports and there
cannot be — the module structure enforces a clean layered architecture.

### 3.3 The control flow at startup

When you run `python -m vortexec --symbols BTCUSDT ETHUSDT --record-to ~/data`:

1. `__main__.py` calls `service.main()`.
2. `service.main()` parses CLI args and calls `asyncio.run(service.run(...))`.
3. `service.run()` constructs one trio per symbol: `(BinanceConnector,
   BookMaintainer, ParquetRecorder)`. The connector and recorder are wired
   to the maintainer.
4. The recorder subscribes to the maintainer's `stream_updates` channel
   *before* the maintainer starts producing.
5. Each maintainer's `start()` is called, kicking off a background task per
   symbol that does: `connector.connect()` → `connector.fetch_snapshot()` →
   `book.apply_snapshot()` → `async for diff in connector.stream_diffs(): book.apply_diff(diff)`.
6. The FastAPI app is built (`api.server.create_app(maintainers)`) and
   uvicorn is started in the same event loop.
7. SIGINT/SIGTERM handlers are installed; a stats-logging task and an
   optional Healthchecks-ping task are spawned.
8. The main coroutine awaits the stop event, which fires when a signal
   arrives.
9. On stop: cancel all background tasks, send `None` sentinels to recorder
   subscribers, await `recorder.stop()` (which flushes buffered Parquet
   row groups and closes writers), await `maintainer.stop()` (which closes
   the WS task and the aiohttp session), exit cleanly.

That entire arc is what subsequent sections explain in detail. It is not
complicated, but every line in the chain depends on every other line being
correct, and most of the subtlety lives in the connector → maintainer
boundary, where the bootstrap protocol decides whether the maintained book
is actually correct.

---

# Part II — The core (`src/vortexec/core/`)

The `core/` package is the only part of the codebase that is purely
synchronous and I/O-free. Everything in here is a pure function or an
in-memory data structure. The reasoning is mechanical: pure code is
trivially testable and trivially composable. The asynchronous,
network-talking, file-writing parts of the system live in higher layers
that build *on top of* `core/`. If you want to understand the math of what
the system computes — separated from the operational complexity of
keeping a live book in sync with an exchange — you can read all of
`core/` in an afternoon and understand it completely, with no concurrency
to reason about.

## 4. `core/types.py` — the canonical data shapes

This file defines five types: one enum (`Side`) and four frozen dataclasses
(`Level`, `Snapshot`, `Diff`, `BookUpdate`). Together they form the *only*
vocabulary that crosses module boundaries inside the project. If a venue
connector wants to hand a snapshot to the maintainer, it does so as a
`Snapshot`. If the maintainer wants to publish an update event to the
recorder, it does so as a `BookUpdate`. There is no other shape allowed.

This is a deliberate constraint. By insisting that the types crossing
module boundaries are these and only these, the system gets a single point
of truth for what its data looks like. New exchange connectors don't get to
invent their own message shapes; they translate exchange-specific JSON into
the canonical types and the rest of the system never sees the difference.
This is what the architecture document calls "normalised types".

### 4.1 The full file

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Level:
    price: float
    quantity: float


@dataclass(frozen=True)
class Snapshot:
    bids: list[Level]
    asks: list[Level]
    timestamp: datetime


@dataclass(frozen=True)
class Diff:
    side: Side
    price: float
    quantity: float
    timestamp: datetime


@dataclass(frozen=True)
class BookUpdate:
    venue: str
    symbol: str
    diff: Diff
```

Let me walk through each piece.

### 4.2 `Side`

```python
class Side(Enum):
    BUY = "buy"
    SELL = "sell"
```

An enum with two members. The string values (`"buy"`, `"sell"`) are
significant because they are what we serialise to the API and to disk —
the JSON representation of a `Side.BUY` is the string `"buy"`. If we
ever needed to add `SHORT` or some other side, this enum is the place.

The choice of an enum vs a string vs a boolean is worth dwelling on. A
boolean (`is_buy: bool`) is the most compact but error-prone — `True`
means "buy" only by convention, and reading `if side: ...` later is
ambiguous. A string (`"buy"` / `"sell"`) is human-readable but offers no
type-checker protection against typos like `"by"`. An enum gives both
human readability *and* the type checker checks: `Side.BUY` is a single
import and a typo becomes an `AttributeError` at import time, not a
silent logic bug at run time. This is the kind of small choice that pays
back many times over the life of a codebase.

The enum is used absolutely everywhere — every `Diff` has a side, every
call to `simulate_market_order` takes a side, every level update from
Binance is parsed into a side. Having a single canonical type for it
means none of those callers can disagree about what "buy" means.

### 4.3 `Level`

```python
@dataclass(frozen=True)
class Level:
    price: float
    quantity: float
```

A single price level on one side of the book. Two fields: a price and a
quantity. Both `float`. This is the atomic unit of book content.

There are several non-obvious decisions here.

**Why `frozen=True`?** Frozen dataclasses raise `FrozenInstanceError` on
any attempt to mutate a field after construction. In production code this
is mostly defensive — a `Level` you receive from a producer should not
be mutated by the consumer. But the bigger reason is that frozen
instances are hashable, which means they can be used as dictionary keys
or set members. We don't currently *need* that, but the cost of `frozen`
is zero and the benefit of optionality is real.

**Why `float` and not `Decimal`?** Real exchanges report prices and
quantities as decimal strings, and a strict reading of "correct" would
say we should hold them as `Decimal` to avoid floating-point error in
arithmetic. We chose `float` for two reasons. First, the Python `float`
is a 64-bit IEEE 754 number with 15-17 significant digits, which is
ample for prices in the range we encounter (BTC at $100,000 with 0.01
tick = 8 significant digits) and quantities (BTC sizes with 8 decimals
= 8-10 significant digits). The relative precision loss from
floating-point arithmetic at these magnitudes is on the order of
$10^{-15}$, far below the granularity that matters for slippage in bps
(which has 4-decimal precision). Second, `Decimal` is much slower than
`float` and has poor library support — `numpy`, `pyarrow`, `pandas` all
prefer `float64`, and converting at I/O boundaries gets clumsy. `float`
is the pragmatic choice; the precision concern is largely theoretical.

**Why no `side` field on `Level`?** A `Level` describes *just* a (price,
quantity) pair. Whether it's a bid or an ask is determined by which list
it appears in (the `bids` list or the `asks` list of a `Snapshot`).
Embedding the side on every `Level` would be redundant for snapshots
(you'd be storing the same string thousands of times) and confusing
for diffs (a `Diff` has its own `side` field). Levels, on their own,
are just (price, quantity). Their bid/ask role is a property of where
they sit in a containing structure.

### 4.4 `Snapshot`

```python
@dataclass(frozen=True)
class Snapshot:
    bids: list[Level]
    asks: list[Level]
    timestamp: datetime
```

A full state of the order book at a point in time. `bids` is a list of
buy-side levels; `asks` is a list of sell-side levels; `timestamp` is
when this snapshot was generated.

A few things worth noticing.

**The lists are not ordered by promise of the type.** The `Snapshot` type
itself does not guarantee that `bids[0]` is the best bid or that the
list is sorted in any particular way. The convention used by the
`OrderBook` consumer (Section 5) is "any order is fine; we'll sort on
ingestion." This matters because different venues produce snapshots in
different orders — Binance returns asks ascending and bids descending in
their REST response — and we don't want to bake that into the type
contract. The `Snapshot` is just "here are the levels"; the `OrderBook`
imposes the ordering it needs.

**`bids: list[Level]` is mutable, in spite of `frozen=True`.** Frozen
on a dataclass means the *fields can't be reassigned* after construction
— you can't do `snapshot.bids = [...]`. It does *not* mean the contents
of the list are immutable. `snapshot.bids.append(Level(...))` would
work and would mutate the snapshot's contents. We accept this leakage in
exchange for using the standard library list (which is what producers
naturally give us); a stricter alternative would be `tuple[Level, ...]`,
which is recursively immutable, and we could switch if it ever became a
real problem. So far it hasn't.

**`timestamp: datetime`, not `int` (epoch ms).** Real exchanges report
timestamps as integer milliseconds since the Unix epoch. We could store
them that way and convert at the API layer. We chose `datetime` because
it forces every consumer to think about timezones (a naive `datetime` is
disallowed by the type itself; we always construct it with `tzinfo=UTC`)
and because comparison and arithmetic on `datetime` is more
self-documenting than on `int`. The conversion happens once, in the
connector (Section 9), where the millisecond integer is parsed into
`datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)`.

### 4.5 `Diff`

```python
@dataclass(frozen=True)
class Diff:
    side: Side
    price: float
    quantity: float
    timestamp: datetime
```

A single incremental update to the book. "Set the quantity at price $P$
on side $S$ to $Q$, as of time $T$."

There is a subtle but important contract here: `quantity` of zero means
"remove this level from the book". This is the convention every real
exchange uses for book diffs — they don't send a separate "delete"
message; they send a quantity-zero update. The `OrderBook.apply_diff`
method (Section 5) interprets this convention by deleting the level
from its internal storage rather than storing a 0-quantity entry. This
keeps the book sparse — only levels with actual liquidity are present.

The `timestamp` on a `Diff` is the *exchange's* timestamp for the
update, not the time we received it. This matters because we sometimes
compare timestamps across exchanges (latency-of-information analyses)
and because the recorder writes the exchange's timestamp into the
Parquet files for replay accuracy.

### 4.6 `BookUpdate`

```python
@dataclass(frozen=True)
class BookUpdate:
    venue: str
    symbol: str
    diff: Diff
```

The maintainer publishes one `BookUpdate` per applied diff, broadcasting
to all subscribers (the recorder, eventually the API streaming
subscribers, etc.). The wrapper exists because the consumer needs to
know which `(venue, symbol)` book the diff applied to — the maintainer
itself knows because it owns one specific maintainer per `(venue,
symbol)` pair, but a downstream consumer subscribing to multiple
maintainers needs the metadata explicitly.

We chose to broadcast one `BookUpdate` per `Diff` rather than one per
WS message (which might bundle several diffs). The reasoning is that
each `Diff` is the smallest meaningful change to the book, and
downstream consumers (especially the recorder) don't care about the
WS message boundaries. If at some point we want to recover the message
boundaries (e.g. to compress storage by grouping), we can add a
`message_id: int` to `BookUpdate` later without changing the wire
format.

### 4.7 Why these and only these

A reasonable question: why not, say, a `Trade` type? A `Quote` type? An
`OrderEvent` type?

The answer is that this codebase, today, is concerned with *order book
maintenance* and *pre-trade analytics*. It does not consume the trade
print stream from the exchange. It does not place orders. It does not
know about user-level events. Adding types for those concepts now would
be speculative — building scaffolding for features we haven't designed
yet. The architecture document is explicit about this: the canonical
types are `Side`, `Level`, `Snapshot`, `Diff`, `BookUpdate`. When we
add a feature that needs new types (e.g. `Fill` for the post-trade
analyser), we add the type then.

The discipline this enforces is good: the codebase only carries the
abstractions it currently needs, and the type list itself is a kind of
honest disclosure of the project's current scope.

### 4.8 Tests

`tests/unit/test_types.py` covers nine cases:

- `Side` has `BUY` and `SELL` with the correct string values.
- `Level` holds price and quantity, is frozen, supports equality.
- `Snapshot` holds bids/asks/timestamp, is frozen.
- `Diff` holds all four fields, is frozen.
- `BookUpdate` holds the three fields, is frozen.

These look trivial, but they are load-bearing: every other test in the
project imports these types, and a regression here would cascade. The
frozenness checks specifically prevent someone from later "helpfully"
removing `frozen=True` to allow mutation in some specific case — the
test would catch it. The string values of `Side.BUY.value == "buy"`
are pinned because they are what we serialise to JSON and Parquet; a
typo there would break the wire contract silently.

---

## 5. `core/book.py` — the in-memory order book

This is the central data structure of the project. Every other module
either feeds an `OrderBook` (the connector via the maintainer), reads
from one (the simulator, the feature extractor, the API), or copies its
output to disk (the recorder, indirectly via the maintainer's pub/sub).

The class has four concerns:

1. **Storage.** Hold the bids and asks in a way that supports fast
   updates and fast lookups of the best price.
2. **Snapshot loading.** Replace the entire book contents with a fresh
   snapshot.
3. **Diff application.** Apply a single incremental update.
4. **Read methods.** Best bid, best ask, mid, spread.

Let me walk through each.

### 5.1 The full file

```python
from __future__ import annotations

from typing import cast

from sortedcontainers import SortedDict

from vortexec.core.types import Diff, Side, Snapshot


class OrderBook:
    def __init__(self) -> None:
        self._bids: SortedDict = SortedDict()
        self._asks: SortedDict = SortedDict()

    def apply_snapshot(self, snapshot: Snapshot) -> None:
        self._bids.clear()
        self._asks.clear()
        for level in snapshot.bids:
            if level.quantity != 0:
                self._bids[level.price] = level.quantity
        for level in snapshot.asks:
            if level.quantity != 0:
                self._asks[level.price] = level.quantity

    def apply_diff(self, diff: Diff) -> None:
        side_dict = self._bids if diff.side is Side.BUY else self._asks
        if diff.quantity == 0:
            side_dict.pop(diff.price, None)
        else:
            side_dict[diff.price] = diff.quantity

    def best_bid(self) -> float | None:
        if not self._bids:
            return None
        return cast(float, self._bids.peekitem(-1)[0])

    def best_ask(self) -> float | None:
        if not self._asks:
            return None
        return cast(float, self._asks.peekitem(0)[0])

    def mid(self) -> float | None:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    def spread(self) -> float | None:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return None
        return ask - bid
```

### 5.2 Why `SortedDict`

The book needs a data structure that supports:

- Fast insert/update at any price: $O(\log n)$ ideally.
- Fast delete of any price: $O(\log n)$.
- Fast lookup of the best price (max for bids, min for asks): $O(\log n)$ or better.
- Iteration in price order, both ascending and descending: $O(k)$ for
  $k$ levels touched.

The standard library `dict` gives O(1) insert/delete/lookup but does not
maintain price order, so finding the best price is O(n). A standard
library `list` keeps order but has O(n) insert. A sorted list could be
O(log n) lookup with O(log n) insert if implemented with a binary
indexed tree, but Python's stdlib doesn't have one.

`sortedcontainers.SortedDict` solves this. It is implemented as a list
of sorted lists with periodic rebalancing — a structure called a
"sorted list of buckets" — and gives O(log n) for all the operations we
need plus efficient sorted iteration. The author, Grant Jenks, has put
serious effort into making it competitive with C implementations even
though it's pure Python.

We use one `SortedDict` per side (`_bids` and `_asks`), with prices as
keys and quantities as values. For BTCUSDT on Binance this typically
holds around 5,000 keys per side; insert/delete/lookup remain submillisecond.

### 5.3 `__init__`

```python
def __init__(self) -> None:
    self._bids: SortedDict = SortedDict()
    self._asks: SortedDict = SortedDict()
```

Two empty `SortedDict`s. The leading underscore is a Python convention
for "internal" — external code should not poke at `_bids` directly.
That said, the simulator and feature extractor (Sections 6 and 7) do
read `_bids` and `_asks` directly, because they need to iterate over
all levels and the public API doesn't expose an iterator. This is a
deliberate scope-management choice — adding a public iterator method
would be more surface than we currently need, and the simulator and
feature extractor are tightly coupled to the book by design (they're
all in the same `core/` package). If we add another consumer that
needs iteration, we'd add a public method then.

### 5.4 `apply_snapshot`

```python
def apply_snapshot(self, snapshot: Snapshot) -> None:
    self._bids.clear()
    self._asks.clear()
    for level in snapshot.bids:
        if level.quantity != 0:
            self._bids[level.price] = level.quantity
    for level in snapshot.asks:
        if level.quantity != 0:
            self._asks[level.price] = level.quantity
```

Replace the entire book with the snapshot's contents. Clear both sides,
then insert every level from the snapshot.

The `if level.quantity != 0` filter handles a corner case: some
exchanges sometimes include zero-quantity levels in their snapshots
(usually as a side effect of the exchange's own internal storage). We
filter them out at ingestion so the book stays sparse — a level should
only exist if it has actual liquidity.

The two passes (one for bids, one for asks) are written separately
rather than as a single loop because they go into different data
structures. Could factor into a helper but at four lines each it's
not worth the abstraction cost.

`apply_snapshot` is called once at startup (when the maintainer first
fetches a snapshot from the exchange) and again on every resync (when
the maintainer detects a sequence gap and re-bootstraps). Each call
fully replaces the book — there is no partial update of the snapshot.

### 5.5 `apply_diff`

```python
def apply_diff(self, diff: Diff) -> None:
    side_dict = self._bids if diff.side is Side.BUY else self._asks
    if diff.quantity == 0:
        side_dict.pop(diff.price, None)
    else:
        side_dict[diff.price] = diff.quantity
```

Apply one incremental update. Three lines of logic; let me unpack each
piece.

`side_dict = self._bids if diff.side is Side.BUY else self._asks` —
pick which side this diff applies to. We use `is` comparison (identity)
rather than `==` (equality) because `Side.BUY` is a singleton enum
value; identity comparison is a tiny bit faster and clearer in intent
("this *is* the BUY enum member").

`if diff.quantity == 0: side_dict.pop(diff.price, None)` — quantity-zero
means "remove this level". `dict.pop(key, default)` removes the key if
present and returns its value, or returns the default if the key isn't
present. We pass `None` as the default so the call is a no-op when the
level doesn't exist. This handles the case where the exchange sends a
delete for a level we don't have (because we already deleted it, or we
never had it). Idempotent.

`else: side_dict[diff.price] = diff.quantity` — non-zero quantity means
"set the level to this quantity". This both inserts new levels and
updates existing ones, because dict assignment does both in one
operation.

The whole method is three lines plus a one-line dispatch and runs in
$O(\log n)$. It is called once per diff, which on Binance happens
~50-300 times per second for an active symbol. Performance has never
been close to a bottleneck.

### 5.6 `best_bid`

```python
def best_bid(self) -> float | None:
    if not self._bids:
        return None
    return cast(float, self._bids.peekitem(-1)[0])
```

The best bid is the highest-price bid. `SortedDict` stores keys in
ascending order, so the highest is at the end of the sorted sequence.
`peekitem(-1)` returns the last `(key, value)` tuple in O(1). We take
the key (`[0]`) which is the price.

The empty-book case returns `None` rather than 0 or raising, consistent
with the "undefined is `None`" convention discussed in Section 2.

`cast(float, ...)` is a type-system hint with no runtime effect. It's
needed because `SortedDict` from `sortedcontainers` doesn't ship with
type stubs (the project has `ignore_missing_imports = true` for
`sortedcontainers.*` in its mypy config), so mypy infers the result
of `peekitem(-1)[0]` as `Any`. The `cast` tells mypy "treat this as
`float`" so that the function signature `-> float | None` is honest at
the type level. Without it, mypy under `--strict` would flag returning
`Any` from a function declared to return `float | None`.

### 5.7 `best_ask`

```python
def best_ask(self) -> float | None:
    if not self._asks:
        return None
    return cast(float, self._asks.peekitem(0)[0])
```

The best ask is the lowest-price ask. `SortedDict` stores ascending,
so the lowest is at index 0. `peekitem(0)`.

Symmetric to `best_bid` except for the index.

### 5.8 `mid`

```python
def mid(self) -> float | None:
    bid = self.best_bid()
    ask = self.best_ask()
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2
```

The mid price. Reads best bid and best ask, returns their arithmetic
mean. If either side is empty, mid is undefined and we return `None`.

The `or` in the None check is doing real work: if even one side is
empty, mid is undefined. We don't try to invent a fallback (e.g.
"return the bid if there's no ask") because that would silently mask
a degenerate market state from downstream code.

### 5.9 `spread`

```python
def spread(self) -> float | None:
    bid = self.best_bid()
    ask = self.best_ask()
    if bid is None or ask is None:
        return None
    return ask - bid
```

The spread. `ask - bid` (always non-negative on a healthy market). Same
None handling as `mid`.

There is some intentional code duplication between `mid` and `spread`
(both fetch `best_bid()` and `best_ask()` and check for None). We could
factor this into a `_top_of_book(self) -> tuple[float, float] | None`
helper. The reason we don't is that the two methods are independently
called, sometimes only one is needed, and the duplication is six
short lines. Factoring would save four lines and add an indirection
level for a reader. Not worth it.

### 5.10 Iteration order: a subtle convention

A consumer of `OrderBook` who wants to walk the bids from best to worst
(highest to lowest price) needs to iterate `_bids` *in reverse*.
`SortedDict` supports this via `reversed(self._bids)`. The simulator
and feature extractor both use this pattern.

For asks, walking from best to worst is *forward* iteration (lowest to
highest). `for price in self._asks:` works directly.

This asymmetry is a consequence of storing both sides in ascending
order. We considered the alternative of storing bids in descending
order (so iteration is always forward) but the storage we use,
`SortedDict`, only supports ascending storage; reversing would require
either a custom sort key (negate the price) or a different container.
Both are more code for no real benefit; it's much simpler to just
remember "reverse for bids, forward for asks" at the small number of
callsites.

### 5.11 Concurrency and the read-during-write question

A reasonable question: what happens if the maintainer is mid-`apply_diff`
when a reader calls `best_bid()`?

The answer is: that scenario can't happen, because `apply_diff` is
synchronous Python code with no `await` points, and asyncio is
single-threaded. Once `apply_diff` starts running, no other coroutine —
including the API handler that's about to call `best_bid()` — can run
until `apply_diff` returns. The book is always in a consistent state
when read.

This is a load-bearing property of the design. If we ever moved
`apply_diff` to a worker thread, we'd need a lock. We haven't and
won't, because the work is so fast that thread overhead would dwarf
the benefit.

The architecture document calls this out as an explicit invariant:
"Books are never read mid-update." It's true *by construction* given
the synchronous mutators and the single-threaded asyncio model.

### 5.12 Tests

`tests/unit/test_book.py` has nineteen tests, exercising:

- The empty book starts with no bids or asks.
- `apply_snapshot` loads bids and asks correctly.
- `apply_snapshot` ignores zero-quantity levels.
- `apply_snapshot` replaces previous data (i.e. the second snapshot
  fully supersedes the first).
- `apply_snapshot` with empty bids/asks leaves the book empty.
- `best_bid`/`best_ask` return `None` on empty sides.
- `mid`/`spread` return `None` on empty sides.
- `best_bid` returns the highest price.
- `best_ask` returns the lowest.
- `mid` is the arithmetic mean.
- `spread` is `ask - bid` (with `pytest.approx` for floating-point
  tolerance).
- One-sided books: `mid`/`spread` `None` even when one side is full.
- `apply_diff` adds new bid/ask levels.
- `apply_diff` replaces quantity at an existing level.
- `apply_diff` with quantity zero deletes the level.
- `apply_diff` with quantity zero on a missing level is a no-op
  (doesn't raise).
- `apply_diff` for a buy doesn't affect the asks.

The tests reach into `_bids` and `_asks` directly to verify the
internal state. This is acceptable for a same-package test (it tests
the implementation, not just the public contract) and lets us assert
on level counts and ordering without exposing internal iteration.
The tests use `dict(book._bids)` to snapshot the state for assertion,
which is read-only.

---

## 6. `core/simulator.py` — the deterministic walk

The simulator answers the question: "If I sent a market order of size $S$
on side $\text{side}$ against the current book, what would I actually pay?"

It is a single function, `simulate_market_order`, with a small companion
dataclass, `SimResult`. The whole module is ~70 lines. The compactness is
not because the logic is trivial — there are several edge cases — but
because the logic is *isolated*. The function takes an `OrderBook` and
returns a `SimResult`. No I/O, no async, no state, no dependencies beyond
the book itself. Every behaviour can be tested by handing it a constructed
book and asserting on the returned `SimResult`.

This is the function the HTTP `/v1/estimate` endpoint ultimately calls (via
the maintainer's `get_book()`). It is also the function the future ML
training pipeline will use as its label generator: walk historical books
with a hypothetical trade, record the slippage, train a model that learns
to predict that distribution from features. So even though the simulator
sits low in the dependency graph, it is the single most user-visible piece
of computation in the system.

### 6.1 The full file

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from vortexec.core.book import OrderBook
from vortexec.core.types import Side


@dataclass(frozen=True)
class SimResult:
    avg_price: float | None
    slippage_bps: float | None
    unfilled_qty: float
    levels_consumed: int


def simulate_market_order(book: OrderBook, side: Side, size: float) -> SimResult:
    if side is Side.BUY:
        side_dict = book._asks
        prices = list(side_dict)
    else:
        side_dict = book._bids
        prices = list(reversed(side_dict))

    remaining = size
    total_cost = 0.0
    levels_consumed = 0

    for raw_price in prices:
        if remaining <= 0:
            break
        price = cast(float, raw_price)
        qty = cast(float, side_dict[raw_price])
        take = min(remaining, qty)
        total_cost += take * price
        remaining -= take
        levels_consumed += 1

    filled = size - remaining
    if filled <= 0:
        return SimResult(
            avg_price=None,
            slippage_bps=None,
            unfilled_qty=size,
            levels_consumed=0,
        )

    avg_price = total_cost / filled
    mid = book.mid()
    slippage_bps: float | None = None
    if mid is not None:
        if side is Side.BUY:
            slippage_bps = (avg_price - mid) / mid * 10_000
        else:
            slippage_bps = (mid - avg_price) / mid * 10_000

    return SimResult(
        avg_price=avg_price,
        slippage_bps=slippage_bps,
        unfilled_qty=remaining,
        levels_consumed=levels_consumed,
    )
```

Let me derive the math first, then walk through the code.

### 6.2 The walk, formally

A market order of size $S$ on side $\text{side}$ consumes liquidity from
the *opposite* side of the book. A buy eats asks (you take liquidity that
sellers have offered). A sell eats bids (you take liquidity that buyers
have offered).

Let $L = \{(p_1, q_1), (p_2, q_2), \ldots, (p_n, q_n)\}$ be the levels on
the consumed side, ordered such that $p_1$ is the *best* (most aggressive)
price for someone walking the book — for a BUY, that's the lowest ask;
for a SELL, that's the highest bid. So:

- BUY: $p_1 < p_2 < \ldots < p_n$ (asks ascending; the lowest ask is best).
- SELL: $p_1 > p_2 > \ldots > p_n$ (bids descending; the highest bid is best).

The walk algorithm is:

```
remaining ← S
total_cost ← 0
levels_consumed ← 0

for each (p, q) in L (in best-first order):
    if remaining ≤ 0: stop
    take ← min(remaining, q)
    total_cost ← total_cost + take · p
    remaining ← remaining − take
    levels_consumed ← levels_consumed + 1
```

After the loop, define $\text{filled} = S - \text{remaining}$.

If $\text{filled} = 0$ (the side was empty), the average price is undefined and so is the slippage. Return `None` for both, and `unfilled_qty = S`.

If $\text{filled} > 0$, the average fill price is:

$$
\text{avg\_price} = \frac{\text{total\_cost}}{\text{filled}}.
$$

The slippage in bps, with the "positive = worse" convention:

$$
\text{slippage}_{\text{bps}} = \begin{cases}
\dfrac{\text{avg\_price} - \text{mid}}{\text{mid}} \cdot 10^4 & \text{(BUY)} \\[6pt]
\dfrac{\text{mid} - \text{avg\_price}}{\text{mid}} \cdot 10^4 & \text{(SELL)}
\end{cases}
$$

If `mid` is undefined (the *opposite* side of the book — the one you're
not eating — is empty, so there's no mid to reference), then
`slippage_bps` is also undefined and we return `None` for it. Note that
this means a BUY can fill against asks but still have undefined slippage
if there are no bids — because mid needs both sides.

### 6.3 Why the sign convention

The convention is "positive slippage = execution worse than mid".

For a BUY, "worse than mid" means paying more than mid — `avg_price > mid`. The numerator of the BUY formula is `avg_price - mid`, which is positive precisely when execution is worse. ✓

For a SELL, "worse than mid" means receiving less than mid — `avg_price < mid`. The numerator of the SELL formula is `mid - avg_price`, which is positive precisely when execution is worse. ✓

This convention is universal in execution analytics — slippage is reported
as a non-negative cost regardless of side, and the magnitude is the cost
to the trader. If we used the same formula for both sides, we'd get
positive slippage for BUYs and negative for SELLs, and end-users would
have to remember which sign meant what. Far less confusing to flip the
sign once on the SELL side and report a single consistent quantity.

### 6.4 `SimResult` — the return type

```python
@dataclass(frozen=True)
class SimResult:
    avg_price: float | None
    slippage_bps: float | None
    unfilled_qty: float
    levels_consumed: int
```

Four fields:

- **`avg_price`** — the size-weighted average fill price across the
  consumed levels. `None` when nothing filled (side empty).
- **`slippage_bps`** — slippage in basis points, with the
  positive-is-worse convention. `None` when nothing filled OR when mid
  is undefined.
- **`unfilled_qty`** — how much of the requested size couldn't be filled
  because the book ran out. Always defined; equals `size` if nothing
  filled, `0` if everything filled, somewhere in between otherwise.
- **`levels_consumed`** — how many distinct price levels the walk
  touched. Always defined; `0` if nothing filled.

The `None` choice for `avg_price` and `slippage_bps` follows the same
"undefined is `None`" convention used throughout the project. We could
have returned `0.0` and added a `success: bool` field, but that would
require every consumer to check the boolean; the `None` shape forces
callers to handle the undefined case explicitly via type narrowing.

### 6.5 Picking which side to walk

```python
if side is Side.BUY:
    side_dict = book._asks
    prices = list(side_dict)
else:
    side_dict = book._bids
    prices = list(reversed(side_dict))
```

A BUY walks asks; a SELL walks bids. The chosen side dict is read from
the book directly (`_asks` or `_bids`).

`prices = list(side_dict)` for asks gives ascending prices because
`SortedDict` iterates keys in sort order. For asks, ascending price *is*
best-first (lowest ask first), which is what we want.

`prices = list(reversed(side_dict))` for bids gives descending prices.
For bids, descending price is best-first (highest bid first). The
`reversed(SortedDict)` call uses the SortedDict's reverse-iteration
support; we materialise it into a list so the inner for-loop can iterate
without consuming a generator.

Materialising into a list rather than iterating the SortedDict directly
costs a small allocation (a list of N price floats), but means that any
mutation of the underlying SortedDict during the walk doesn't corrupt
the iteration. In practice mutation can't happen during the walk (the
walk is synchronous, no awaits), but the list copy is also a defensive
choice — it isolates the simulator's iteration from any future change
in how the book is mutated.

### 6.6 The walk

```python
remaining = size
total_cost = 0.0
levels_consumed = 0

for raw_price in prices:
    if remaining <= 0:
        break
    price = cast(float, raw_price)
    qty = cast(float, side_dict[raw_price])
    take = min(remaining, qty)
    total_cost += take * price
    remaining -= take
    levels_consumed += 1
```

`remaining` starts at the full requested `size` and shrinks toward zero
as each level is consumed. `total_cost` accumulates the running sum
$\sum p_i \cdot \text{take}_i$. `levels_consumed` counts how many levels
the walk touched.

The early-exit `if remaining <= 0: break` short-circuits as soon as we've
filled the requested size. Without it, we'd keep iterating through the
remaining (untouched) levels with `take = 0` each time. The break is
required for correctness (we shouldn't increment `levels_consumed` for
levels we didn't actually use) and for efficiency.

`take = min(remaining, qty)` is the heart of the walk: take as much as
we still need, but no more than the level offers. If `remaining < qty`,
we take only what we need and the level still has liquidity afterwards
(we don't track that in the simulator — we're just simulating, not
modifying the book). If `remaining >= qty`, we take the whole level and
move on.

`total_cost += take * price` accumulates cost. `remaining -= take`
reduces remaining. `levels_consumed += 1` counts.

The two `cast(float, ...)` calls are the same type-narrowing trick as
in `OrderBook.best_bid` (Section 5.6): `SortedDict` returns `Any`, and
mypy `--strict` needs us to assert the actual type. The casts have no
runtime effect.

### 6.7 The "did anything fill?" branch

```python
filled = size - remaining
if filled <= 0:
    return SimResult(
        avg_price=None,
        slippage_bps=None,
        unfilled_qty=size,
        levels_consumed=0,
    )
```

If we got through the walk without consuming anything (because the side
was empty, or because `size` was 0), report nothing filled with
`avg_price` and `slippage_bps` undefined. `unfilled_qty` equals the
original `size` (we didn't take anything from it). `levels_consumed` is
still `0` because we hit the `break` immediately or the loop body never
ran.

The `filled <= 0` check rather than `filled == 0` is paranoia about
floating-point — in principle `filled` could be a tiny negative number
due to FP error in `size - remaining` even when conceptually nothing
filled. In practice this would require very specific inputs; the
`<=` is a $0$-cost defensive choice.

### 6.8 Computing avg_price and slippage

```python
avg_price = total_cost / filled
mid = book.mid()
slippage_bps: float | None = None
if mid is not None:
    if side is Side.BUY:
        slippage_bps = (avg_price - mid) / mid * 10_000
    else:
        slippage_bps = (mid - avg_price) / mid * 10_000

return SimResult(
    avg_price=avg_price,
    slippage_bps=slippage_bps,
    unfilled_qty=remaining,
    levels_consumed=levels_consumed,
)
```

Once we know something filled, divide `total_cost` by `filled` to get
the size-weighted average price. This is the formula from Section 6.2.

Then read `mid` from the book. If it's `None` (i.e. the *other* side of
the book is empty), we can't compute slippage — slippage is referenced
to mid, and there's no mid. Return `slippage_bps = None` but still
return a meaningful `avg_price` (you did fill at *some* price, even if
we can't tell you how that compares to a mid).

If mid is defined, apply the appropriate side-aware formula and convert
to bps in one step (`* 10_000`).

The explicit `slippage_bps: float | None = None` declaration on its own
line is important for mypy: it tells the type checker that
`slippage_bps` has type `float | None` from the start. Without that
declaration, mypy would infer the type from the first assignment in the
if/else branches, and the case where neither branch runs (mid is None)
would leave `slippage_bps` undefined for the `SimResult(...)` call. The
upfront declaration with default `None` covers all paths.

`unfilled_qty=remaining` correctly reports how much wasn't filled. If
the walk completed all of `size`, `remaining` is 0 (or near-0 due to FP)
and `unfilled_qty` is 0. If the walk ran out of book partway, `remaining`
holds whatever was left.

### 6.9 Edge cases

The function handles four meaningful edge cases.

**Empty consumed side.** No levels to walk. `prices` is empty, the for
loop body never executes, `remaining` stays at `size`, `filled` is 0,
return all-`None` result with `unfilled_qty = size`.

**Partial fill (size > total available depth).** Walk consumes all
levels, `remaining` ends positive, `filled = size - remaining` is
positive but less than `size`. `avg_price` and `slippage_bps` are
computed against the fraction that did fill; `unfilled_qty` reports the
shortfall.

**One-sided book.** Consumed side has liquidity, opposite side is empty.
The walk fills successfully and reports `avg_price`, but `mid` is `None`
so `slippage_bps` is `None`. This is a real degenerate state on
exchanges in low-volume conditions.

**Zero-size order.** `size = 0`. The walk exits on the first `if
remaining <= 0` check without touching anything. `filled = 0`, return
all-`None`. Caller should validate non-zero size at the API layer (the
Pydantic `EstimateRequest` does so via `Field(gt=0)`); this is a defensive
no-op rather than an expected path.

### 6.10 Performance

The walk is $O(k)$ in the number of levels touched. A 5 BTC market buy
on a typical Binance book consumes 1-3 levels. A 100 BTC order might
consume 10-50 levels. Even pathological 1000 BTC orders consume only a
few hundred levels.

End-to-end latency, measured via the API: median 0.5 ms, p95 0.75 ms
(see Section 13). The simulator itself is well under 0.1 ms; the rest
is HTTP framework overhead.

There is no caching, no precomputation. The walk runs from scratch on
every call. This is fine because the cost is so small, and any caching
strategy would have to invalidate on every diff applied (i.e. ~50-300
times per second), making the cache machinery more expensive than the
walk it would save.

### 6.11 Tests

`tests/unit/test_simulator.py` has eleven tests:

- `SimResult` is frozen.
- BUY fully fills the first ask level (top depth ≥ size).
- BUY walks multiple ask levels (top depth < size, deeper levels exist).
- BUY partially fills when book runs out.
- BUY against empty asks returns no-fill.
- SELL walks bids highest-first.
- SELL against empty bids returns no-fill.
- BUY slippage is positive when avg > mid.
- SELL slippage is positive when avg < mid.
- Slippage is `None` when the *other* side is empty (mid undefined).
- Size = 0 returns no-fill.

The numerics in the tests are calculated by hand and pinned with
`pytest.approx` for floating-point tolerance. For example, the multi-level
BUY test sets up asks `[(101, 1), (102, 2), (103, 5)]`, requests size 4,
and asserts:

- `avg_price = (1·101 + 2·102 + 1·103) / 4 = 408 / 4 = 102.0`
- `unfilled_qty = 0`
- `levels_consumed = 3`

These hand-calculated cases are the load-bearing assurance that the
math is right. If the simulator ever silently changed the formula, the
test would catch it instantly.

---

## 7. `core/features.py` — what the ML model will see

This module computes the seven numbers that summarise the current state of
the book. It exists for one reason: the future quantile-regression model
needs inputs, and these are the inputs. The deterministic walk
(`simulate_market_order`) gives the *outcome* under current conditions; the
features describe *the conditions themselves*, in a form a model can
ingest.

The features are deliberately compact (seven numbers, all derivable from
the current `OrderBook` state) and deliberately model-agnostic — they
don't depend on which model architecture we eventually train.

### 7.1 The full file

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from vortexec.core.book import OrderBook


@dataclass(frozen=True)
class Features:
    spread_bps: float | None
    mid_price: float | None
    depth_top_5_bids: float
    depth_top_5_asks: float
    depth_top_10_bids: float
    depth_top_10_asks: float
    imbalance: float | None


def extract_features(book: OrderBook) -> Features:
    bid_prices = list(reversed(book._bids))
    ask_prices = list(book._asks)

    depth_top_5_bids = sum(
        (cast(float, book._bids[p]) for p in bid_prices[:5]), 0.0
    )
    depth_top_5_asks = sum(
        (cast(float, book._asks[p]) for p in ask_prices[:5]), 0.0
    )
    depth_top_10_bids = sum(
        (cast(float, book._bids[p]) for p in bid_prices[:10]), 0.0
    )
    depth_top_10_asks = sum(
        (cast(float, book._asks[p]) for p in ask_prices[:10]), 0.0
    )

    mid = book.mid()
    spread = book.spread()
    spread_bps: float | None = None
    if mid is not None and spread is not None:
        spread_bps = spread / mid * 10_000

    total_top_10 = depth_top_10_bids + depth_top_10_asks
    imbalance: float | None = None
    if total_top_10 > 0:
        imbalance = (depth_top_10_bids - depth_top_10_asks) / total_top_10

    return Features(
        spread_bps=spread_bps,
        mid_price=mid,
        depth_top_5_bids=depth_top_5_bids,
        depth_top_5_asks=depth_top_5_asks,
        depth_top_10_bids=depth_top_10_bids,
        depth_top_10_asks=depth_top_10_asks,
        imbalance=imbalance,
    )
```

### 7.2 The seven features, one by one

**`spread_bps`** — the bid-ask spread normalised to basis points of mid.

$$
\text{spread\_bps} = \frac{\text{best\_ask} - \text{best\_bid}}{\text{mid}} \cdot 10{,}000
$$

A small spread means market makers are confident in the price — they're
willing to quote both sides tightly. A wide spread means uncertainty:
volatility is high, or liquidity is thin, or both. For BTCUSDT in calm
conditions this is typically 0.1-0.5 bps; during news events or
liquidity crunches it can blow out to several bps.

`None` when either side of the book is empty.

**`mid_price`** — the mid price itself, in absolute units.

A mid of $80,000 vs a mid of $100,000 are different *regimes* even if
spreads and depths look similar — at higher prices, the same dollar
size is a smaller fraction of the book. Including mid as an explicit
feature lets the model condition its predictions on the price level
without us having to manually normalise other features against it.

`None` when either side of the book is empty.

**`depth_top_5_bids` / `depth_top_5_asks`** — total quantity in the top
five price levels of each side. "Top" means most aggressive: highest
five bids, lowest five asks.

These are the levels a small-to-medium market order would actually walk
through. Deep top-5 means a small trade clears at top-of-book; thin
top-5 means even a small trade walks. Both quantities are reported
separately so the model can see asymmetry — bid depth and ask depth
behave differently in different regimes.

Always defined (returns `0.0` when the side is empty, since `sum(...)`
with start `0.0` produces `0.0` on empty input). Returning `0.0` rather
than `None` here is intentional — for a depth measure, "no levels" *is*
zero depth, not undefined. (Compare `mid_price`, where "no top of book"
is genuinely undefined, not zero.)

**`depth_top_10_bids` / `depth_top_10_asks`** — same as the top-5
versions, but for the top ten levels. These give a slightly deeper view
of resting liquidity, capturing levels that a larger market order would
touch.

The choice of "top 5" and "top 10" specifically (rather than, say, top
3, top 7, top 20) is somewhat arbitrary — these are the conventional
depth windows in the academic microstructure literature, and they give
the model both a "what would a small trade pay" and a "what would a
medium trade pay" view without combinatorial explosion.

If we ever discover the model would benefit from depth-at-100 or
depth-at-1000, we add those features. For now seven features is the
minimum that captures the relevant regime descriptors.

**`imbalance`** — the relative skew of top-10 depth between bid and ask
sides.

$$
\text{imbalance} = \frac{\text{depth\_top\_10\_bids} - \text{depth\_top\_10\_asks}}{\text{depth\_top\_10\_bids} + \text{depth\_top\_10\_asks}}
$$

Range: $[-1, +1]$. Sign and magnitude both matter: a positive value
means bid-heavy (more buyers stacked, asks have been thinned), a
negative value means ask-heavy. The magnitude reflects how skewed.

`None` only when *both* sides are empty (the denominator is 0).

The choice to compute imbalance over the top-10 window rather than top-5
or all levels is a judgement call. Top-10 is more stable than top-5
(less sensitive to a single market maker pulling one level) but more
local than all-levels (captures the depth that actually matters for
near-term trades). The academic literature uses various windows; top-10
is a common default. The codebase pins this choice via a discriminating
test (`test_imbalance_uses_top_10_window`) so a future change would have
to be deliberate.

### 7.3 Why these features and not others

Features we deliberately don't include:

- **Price velocity / momentum.** Would require maintaining a time series
  of mids, not just the current state. We could add it; we haven't,
  because the current architecture extracts features from a single
  in-memory book. Adding velocity would require either (a) the maintainer
  recording recent mid prices, or (b) the feature extractor maintaining
  its own buffer. Both are fine engineering, but they're features beyond
  the current scope.
- **Recent diff rate.** Same story. "How active is the book right now"
  is a useful signal but requires book history.
- **Realised volatility.** Same story, plus needs a longer window.
- **Cross-venue features.** Requires a second venue connector (Phase 6).
- **Time-of-day, day-of-week.** Trivially derivable from the system clock
  but currently not extracted. Would be a one-line addition when the
  model needs them.
- **Trade-flow features (aggressor side, recent trade volume).** Requires
  consuming the trade print stream from the exchange — an entirely
  separate WS feed that we don't currently subscribe to.

The current set is the *minimum* that captures regime distinctions
("calm vs. busy", "balanced vs. skewed", "tight vs. wide spread") from
the depth feed alone. When we have a model that's hitting the limits of
these features, that's the time to add more — not now, when we don't
even have a model yet.

### 7.4 The walkthrough

```python
bid_prices = list(reversed(book._bids))
ask_prices = list(book._asks)
```

`bid_prices` holds the prices in best-first order (highest first) by
reversing the SortedDict's natural ascending iteration. `ask_prices`
holds them in best-first order naturally (lowest first). Same pattern
as the simulator (Section 6.5).

```python
depth_top_5_bids = sum(
    (cast(float, book._bids[p]) for p in bid_prices[:5]), 0.0
)
```

Sum the quantities for the top 5 prices. The slice `[:5]` returns at
most 5 prices (fewer if the book has fewer than 5 levels — `[:5]` on a
3-element list returns 3 elements, no error). `book._bids[p]` looks up
the quantity at price `p`. The generator expression is wrapped in
`sum(..., 0.0)` rather than `sum(...)` because `sum` defaults to
returning `0` (int) on an empty iterable, but we want `0.0` (float) for
the field type. Passing `0.0` as the start argument forces float arithmetic.

The four depth lines are nearly identical with different slice indices
and different side-dicts. They could be factored into a helper function.
We don't, because the helper would have to take the side dict, the
prices list, and the slice limit — three arguments — and the resulting
call site would be barely shorter than the inlined version. Sometimes
four similar lines is more readable than three lines plus a function.

```python
mid = book.mid()
spread = book.spread()
spread_bps: float | None = None
if mid is not None and spread is not None:
    spread_bps = spread / mid * 10_000
```

Spread in bps is `(spread / mid) * 10_000`. Both `mid` and `spread` are
`None` when the book has an empty side, so we compute `spread_bps`
only when both are defined.

```python
total_top_10 = depth_top_10_bids + depth_top_10_asks
imbalance: float | None = None
if total_top_10 > 0:
    imbalance = (depth_top_10_bids - depth_top_10_asks) / total_top_10
```

Imbalance over the top-10 window. The numerator is signed (bid heavy =
positive); the denominator is the total top-10 depth, always
non-negative. We guard against `total_top_10 == 0` (both sides empty)
to avoid division by zero, returning `None` in that case.

This formula has nice properties: when only one side has depth, the
imbalance is exactly $\pm 1$ (sign depends on which side). When the
two sides are equal, it's 0. When they're proportional with ratio $r$
(bid_depth = $r$ · ask_depth), the imbalance is $(r-1)/(r+1)$, which
maps the asymmetry into $[-1, +1]$ smoothly.

### 7.5 Tests

`tests/unit/test_features.py` covers nine cases:

- `Features` is frozen.
- Empty book: `spread_bps`, `mid_price`, `imbalance` all `None`; depths all `0.0`.
- Bid-only book: imbalance is `+1.0`; ask-side depths are `0.0`.
- Ask-only book: imbalance is `-1.0`.
- Two-sided book: `mid_price` and `spread_bps` are correctly computed.
- Top-5 and top-10 depths sum correctly across an asymmetric book.
- Fewer than N levels: depth equals what's there (no error).
- Skewed imbalance: the numbers come out where you'd expect.
- The discriminating test for top-10 vs top-5: build a book where top-5
  imbalance is 0 (symmetric in the top 5) but top-10 imbalance is large
  (deep levels are skewed). Verify the imbalance reported is the
  top-10 one. This pins the design choice.

The discriminating test in particular is load-bearing — without it, a
future "let me change this to top-5" wouldn't break any other test.

---

# Part III — Talking to exchanges (`src/vortexec/venues/`)

The `venues/` package handles the protocol-specific work of connecting to
each exchange and translating its specific message formats into the
canonical `Snapshot` / `Diff` types. There is one abstract base class
(`VenueConnector`) defining the interface that every connector must
satisfy, and one concrete implementation (`BinanceConnector`). Eventually
there will be `OKXConnector` and `BybitConnector`; they will subclass the
same abstract base, translate their respective message formats into the
same canonical types, and the rest of the system will not know the
difference.

Inside the `BinanceConnector` is the most subtle piece of engineering in
the project: the *bootstrap protocol*. This is the procedure for gluing a
REST snapshot to a WebSocket diff stream such that the in-memory book is
guaranteed correct. Doing it naively gives you a book that drifts; doing
it correctly is a specific dance involving an asyncio task, an event, and
a careful ordering of `connect → start WS task → wait for first message →
fetch REST snapshot → align`. The whole of Section 9.4 onward is about
this.

## 8. `venues/base.py` — what every exchange connector must do

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from vortexec.core.types import Diff, Snapshot


class SequenceGapError(Exception):
    """Raised when a diff stream's sequence numbers indicate a gap."""


class VenueConnector(ABC):
    @abstractmethod
    async def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def fetch_snapshot(self, symbol: str) -> Snapshot:
        raise NotImplementedError

    @abstractmethod
    def stream_diffs(self, symbol: str) -> AsyncIterator[Diff]:
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        raise NotImplementedError
```

Two things: the `SequenceGapError` exception class, and the
`VenueConnector` abstract base class.

### 8.1 `SequenceGapError`

```python
class SequenceGapError(Exception):
    """Raised when a diff stream's sequence numbers indicate a gap."""
```

A custom exception type representing one specific failure mode: the
diff stream has a sequence-number discontinuity (a message arrived
where `current.U > previous.u + 1`), meaning we've lost one or more
diffs in transit. The book is now potentially out of sync with the
exchange's true state, and the only correct recovery is to discard
everything and re-bootstrap.

We use a custom exception type rather than a generic `Exception` for
two reasons:

1. **Catchability.** The maintainer's resync logic (Section 10.4)
   specifically catches `SequenceGapError` and treats it as a "trigger
   re-bootstrap" signal. Any other exception is treated as a generic
   transient error (logged, retried). The two paths are semantically
   different — a gap is a known protocol-level event that has a
   well-defined recovery; a generic exception might be anything.
   Distinguishing them in the type system gives the maintainer a
   structured way to handle each.

2. **Surface area for future extensions.** The architecture document
   mentions other custom exceptions of the same shape: `BookStaleError`,
   `VenueDisconnectError`. These would live alongside `SequenceGapError`
   in this file. By defining the first one in the right place, we
   establish the pattern.

The exception lives in `venues/base.py` rather than in `binance.py`
because it is *protocol-level*, not Binance-specific. OKX and Bybit
also have sequence numbers and gap-detection requirements. When their
connectors are added, they'll raise the same `SequenceGapError`.

### 8.2 `VenueConnector` — the four-method contract

```python
class VenueConnector(ABC):
    @abstractmethod
    async def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def fetch_snapshot(self, symbol: str) -> Snapshot:
        raise NotImplementedError

    @abstractmethod
    def stream_diffs(self, symbol: str) -> AsyncIterator[Diff]:
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        raise NotImplementedError
```

Four methods that every connector must implement.

**`connect()`** — establish whatever long-lived connections the
connector needs. For Binance this creates an `aiohttp.ClientSession`;
for other exchanges it might be different. Called once at startup.
Async because connection establishment is I/O.

**`fetch_snapshot(symbol)`** — return a `Snapshot` for the given
symbol. For Binance this hits `/api/v3/depth` over REST; for other
exchanges, whatever the equivalent is. Async because it's I/O. Can be
called multiple times (the maintainer calls it once at startup and
again on every resync).

**`stream_diffs(symbol)`** — return an `AsyncIterator[Diff]` that
yields each individual `Diff` object as the exchange's diff stream
delivers them. Note the signature: it's `def`, not `async def`. The
return type is an iterator, and the *caller* iterates it asynchronously
with `async for`. The reason for this specific shape is discussed at
length in Section 9.10.

**`disconnect()`** — clean up whatever was created in `connect()`.
Idempotent — calling it on a non-connected instance is a no-op.

### 8.3 Why an ABC and not a Protocol

Python has two main ways to express "any type that satisfies this
interface":

- `abc.ABC` with `@abstractmethod` — explicit subclassing required;
  classes opt in by inheriting and implementing.
- `typing.Protocol` — structural subtyping; any class with the right
  shape satisfies the protocol, no inheritance needed.

We chose ABC because the connector contract is an *opinionated*
contract — we want every implementation to think about what it means
to satisfy each method, not to accidentally satisfy the contract by
having the right shape. Inheritance forces the connector author to
explicitly engage with each method (write `class
MyConnector(VenueConnector): ...` and provide an implementation; the
abstract decorator means instantiation fails if any method is missing).

ABCs also play better with mypy under `--strict` for runtime checks
(`isinstance(x, VenueConnector)` works without needing a `Protocol`
runtime-checkable annotation).

### 8.4 Why `stream_diffs` is `def`, not `async def`

The natural intuition is that since `stream_diffs` "is async" it should
be `async def`. But the return type tells a different story.

The function returns an `AsyncIterator[Diff]`. The caller iterates with:

```python
async for diff in connector.stream_diffs(symbol):
    ...
```

An async generator function — one defined with `async def` and
containing `yield` — when *called*, returns an `AsyncIterator`
directly, with no `await` needed. So calling code looks like:

```python
iterator = connector.stream_diffs(symbol)  # no await
async for diff in iterator:                # async iteration
    ...
```

If we declared `stream_diffs` as `async def -> AsyncIterator[Diff]`
*and* implemented it as a coroutine that returns an iterator, callers
would need `iterator = await connector.stream_diffs(symbol)` — an extra
`await`. That's clumsy and inconsistent with the standard async
generator pattern.

If we declared `stream_diffs` as `async def -> AsyncIterator[Diff]` *and*
implemented it as an async generator (with `yield`), mypy `--strict`
would flag the override mismatch — the abstract is a coroutine
returning `AsyncIterator`, the implementation is an async generator
function that *itself* is an `AsyncIterator`. Different things, even
if they look the same.

Declaring it `def -> AsyncIterator[Diff]` and implementing as an async
generator is the canonical pattern that mypy accepts and that gives
callers the natural `async for` syntax with no extra `await`. We
discovered this the hard way during Stage 2 of the Binance build —
mypy was very explicit about the right shape.

### 8.5 Tests

`tests/unit/test_venue_base.py` has two tests:

- `VenueConnector` cannot be instantiated directly (the ABC machinery
  blocks it).
- A subclass that implements only some of the required methods also
  cannot be instantiated.

Both are smoke tests — they check that the abstract decorator is
correctly wired. Concrete behaviour is tested via `BinanceConnector`'s
own test file.

---

## 9. `venues/binance.py` — the Binance connector

This is the most subtle module in the project. It implements the full
Binance order-book protocol: REST snapshot fetch, WebSocket diff
subscription, sequence-number alignment, and the bootstrap procedure that
glues snapshot to stream correctly.

The module has roughly four parts:

1. Module-level constants and the `_parse_*` pure parsing functions.
2. The `_Aligner` class — the sequence-validation state machine.
3. The `BinanceConnector` class — the implementation of `VenueConnector`.
4. The bootstrap protocol, which lives inside `BinanceConnector`'s
   `fetch_snapshot` and the background `_buffer_ws` task.

The bootstrap protocol is the hard part. It is what we got wrong in the
first pass and corrected after live validation against real Binance
exposed the failure (every five seconds, the maintainer would catch a
"sequence gap" that wasn't really a gap — it was a side-effect of doing
the snapshot fetch *before* starting the WS subscription, missing the
diffs in between, and the `_Aligner` correctly flagging the missed
diffs as a gap). Fixing it required a structural change to the connector;
that change is what's in the code today.

### 9.1 Binance's depth feed at a glance

Binance publishes its order-book depth data via two endpoints:

- **REST**: `GET https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=5000` — returns a snapshot:
  ```json
  {
    "lastUpdateId": 123456789,
    "bids": [["79900.00", "0.50"], ["79899.99", "1.20"], ...],
    "asks": [["79900.01", "0.30"], ["79900.02", "2.10"], ...]
  }
  ```
  `lastUpdateId` is the snapshot's sequence number. `limit=5000` requests
  the deepest available snapshot (the maximum). Bids and asks are arrays
  of `[price_string, quantity_string]`.

- **WebSocket**: `wss://stream.binance.com:9443/ws/btcusdt@depth@100ms` —
  pushes diff messages every 100 ms:
  ```json
  {
    "e": "depthUpdate",
    "E": 1700000000000,
    "s": "BTCUSDT",
    "U": 123456790,
    "u": 123456795,
    "b": [["79899.50", "0.0"], ["79898.00", "0.75"]],
    "a": [["79900.50", "1.5"]]
  }
  ```
  Fields: `e` is the event type (always `"depthUpdate"`), `E` is the
  event timestamp in ms epoch, `s` is the symbol, `U` is the first
  update id in the message, `u` is the last update id in the message,
  `b` and `a` are arrays of `[price, quantity]` updates for bids and
  asks. A quantity of `"0.0"` means "delete this level".

The bootstrap sequence per Binance's own documentation:

1. Open the WebSocket connection. Buffer all messages.
2. Fetch the REST snapshot. Note its `lastUpdateId`.
3. Discard any buffered WS messages where `u <= lastUpdateId` — they're
   already covered by the snapshot.
4. The first kept message must satisfy `U <= lastUpdateId + 1 <= u` — it
   must bridge `lastUpdateId + 1`. If it doesn't, you missed something
   between the snapshot and the WS, and you should restart from step 1.
5. Apply that message and every subsequent one. Each subsequent message
   must satisfy `U == previous_u + 1` (contiguous). If not, sequence gap
   — restart from step 1.

The order of steps 1 and 2 is the critical part. **WS-first, snapshot-second.**
This is what the corrected `BinanceConnector` implements.

### 9.2 The full file

```python
from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import aiohttp
import websockets

from vortexec.core.types import Diff, Level, Side, Snapshot
from vortexec.venues.base import SequenceGapError, VenueConnector

DEFAULT_REST_BASE_URL = "https://api.binance.com"
DEFAULT_WS_BASE_URL = "wss://stream.binance.com:9443"
WS_READY_TIMEOUT = 10.0


def _parse_depth_response(data: dict[str, Any], timestamp: datetime) -> Snapshot:
    bids = [Level(price=float(p), quantity=float(q)) for p, q in data["bids"]]
    asks = [Level(price=float(p), quantity=float(q)) for p, q in data["asks"]]
    return Snapshot(bids=bids, asks=asks, timestamp=timestamp)


class _Aligner:
    """Sequence-number validator for Binance depth diffs post-snapshot.

    Drops messages already covered by the snapshot (u <= last_update_id),
    requires the first kept message to bridge last_update_id+1
    (U <= last_update_id+1), and requires subsequent messages to be
    contiguous (U == prev_u + 1). Raises SequenceGapError on any violation.
    """

    def __init__(self, last_update_id: int) -> None:
        self._snap_id = last_update_id
        self._prev_u: int | None = None

    def should_emit(self, msg: dict[str, Any]) -> bool:
        first_id = int(msg["U"])
        final_id = int(msg["u"])

        if final_id <= self._snap_id:
            return False

        if self._prev_u is None:
            if first_id > self._snap_id + 1:
                raise SequenceGapError(
                    f"snapshot last_update_id={self._snap_id}, "
                    f"first ws message U={first_id} "
                    f"(expected U <= {self._snap_id + 1})"
                )
            self._prev_u = final_id
            return True

        if first_id != self._prev_u + 1:
            raise SequenceGapError(
                f"sequence gap: expected U={self._prev_u + 1}, got U={first_id}"
            )
        self._prev_u = final_id
        return True


def _parse_diff_message(data: dict[str, Any]) -> list[Diff]:
    event_ms: int = data["E"]
    timestamp = datetime.fromtimestamp(event_ms / 1000.0, tz=timezone.utc)
    diffs: list[Diff] = []
    for price_str, qty_str in data.get("b", []):
        diffs.append(
            Diff(
                side=Side.BUY,
                price=float(price_str),
                quantity=float(qty_str),
                timestamp=timestamp,
            )
        )
    for price_str, qty_str in data.get("a", []):
        diffs.append(
            Diff(
                side=Side.SELL,
                price=float(price_str),
                quantity=float(qty_str),
                timestamp=timestamp,
            )
        )
    return diffs


class BinanceConnector(VenueConnector):
    def __init__(
        self,
        rest_base_url: str = DEFAULT_REST_BASE_URL,
        ws_base_url: str = DEFAULT_WS_BASE_URL,
        verify_ssl: bool = True,
    ) -> None:
        self._rest_base_url = rest_base_url
        self._ws_base_url = ws_base_url
        self._verify_ssl = verify_ssl
        self._session: aiohttp.ClientSession | None = None
        self._last_update_id: int | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._ws_queue: asyncio.Queue[dict[str, Any] | None] | None = None
        self._ws_ready: asyncio.Event | None = None

    async def connect(self) -> None:
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=self._verify_ssl)
            self._session = aiohttp.ClientSession(connector=connector)

    async def disconnect(self) -> None:
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_task = None
            self._ws_queue = None
            self._ws_ready = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch_snapshot(self, symbol: str) -> Snapshot:
        assert self._session is not None, "call connect() before fetch_snapshot()"

        if self._ws_task is None or self._ws_task.done():
            self._ws_queue = asyncio.Queue()
            self._ws_ready = asyncio.Event()
            self._ws_task = asyncio.create_task(self._buffer_ws(symbol))
            await asyncio.wait_for(self._ws_ready.wait(), timeout=WS_READY_TIMEOUT)

        url = f"{self._rest_base_url}/api/v3/depth"
        params: dict[str, Any] = {"symbol": symbol.upper(), "limit": 5000}
        async with self._session.get(url, params=params) as response:
            response.raise_for_status()
            data: dict[str, Any] = await response.json()
        self._last_update_id = int(data["lastUpdateId"])
        return _parse_depth_response(data, datetime.now(timezone.utc))

    async def _buffer_ws(self, symbol: str) -> None:
        assert self._ws_queue is not None
        assert self._ws_ready is not None
        uri = f"{self._ws_base_url}/ws/{symbol.lower()}@depth@100ms"
        ssl_ctx = ssl.create_default_context()
        if not self._verify_ssl:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        try:
            async with websockets.connect(uri, ssl=ssl_ctx) as ws:
                self._ws_ready.set()
                async for raw in ws:
                    msg: dict[str, Any] = json.loads(raw)
                    await self._ws_queue.put(msg)
        finally:
            await self._ws_queue.put(None)

    async def stream_diffs(self, symbol: str) -> AsyncIterator[Diff]:
        assert self._last_update_id is not None, (
            "fetch_snapshot() must be called before stream_diffs() "
            "to establish the bootstrap sequence baseline"
        )
        assert self._ws_queue is not None, (
            "fetch_snapshot() must be called before stream_diffs() "
            "to start the WS buffer"
        )
        aligner = _Aligner(self._last_update_id)
        while True:
            msg = await self._ws_queue.get()
            if msg is None:
                return
            if aligner.should_emit(msg):
                for diff in _parse_diff_message(msg):
                    yield diff
```

That's all of it. About 150 lines. We'll walk through each piece, but
the structural overview is:

- `_parse_depth_response` and `_parse_diff_message` are pure functions
  that translate Binance JSON into our canonical types.
- `_Aligner` is a small state machine that validates sequence numbers
  and decides which WS messages to emit.
- `BinanceConnector` is the class. Its public methods are `connect`,
  `disconnect`, `fetch_snapshot`, `stream_diffs`. Its private state is a
  background WS task, a queue between that task and `stream_diffs`, an
  event signalling WS readiness, and a stored `last_update_id`.

### 9.3 `_parse_depth_response` — REST snapshot to canonical Snapshot

```python
def _parse_depth_response(data: dict[str, Any], timestamp: datetime) -> Snapshot:
    bids = [Level(price=float(p), quantity=float(q)) for p, q in data["bids"]]
    asks = [Level(price=float(p), quantity=float(q)) for p, q in data["asks"]]
    return Snapshot(bids=bids, asks=asks, timestamp=timestamp)
```

Two lines of work plus a return. The `data` argument is the raw JSON
dict from Binance. Each entry in `data["bids"]` is a 2-element list of
strings like `["79900.00", "0.50"]`; the list comprehension destructures
into `(p, q)` and constructs a `Level`. Same for asks.

The `timestamp` is passed in by the caller — `_parse_depth_response` is
pure and doesn't read the system clock. The caller (`fetch_snapshot`)
captures `datetime.now(timezone.utc)` at the moment of the REST
response and passes it in. This separation keeps the parser
deterministic and trivially testable.

We use `float(p)` and `float(q)` to convert from string to float. This
is where the "float vs Decimal" decision discussed in Section 4.3 actually
hits the wire — Binance gives us strings, we parse to floats, the
floats live on through the rest of the system. The precision concern
is theoretical at the magnitudes involved (BTC prices have at most 8
significant digits; floats have 15-17).

### 9.4 `_Aligner` — the sequence validator

```python
class _Aligner:
    def __init__(self, last_update_id: int) -> None:
        self._snap_id = last_update_id
        self._prev_u: int | None = None
```

Constructor takes the snapshot's `lastUpdateId`. Stores it as `_snap_id`.
Initialises `_prev_u` to `None` — we haven't seen any messages yet.

```python
    def should_emit(self, msg: dict[str, Any]) -> bool:
        first_id = int(msg["U"])
        final_id = int(msg["u"])

        if final_id <= self._snap_id:
            return False

        if self._prev_u is None:
            if first_id > self._snap_id + 1:
                raise SequenceGapError(...)
            self._prev_u = final_id
            return True

        if first_id != self._prev_u + 1:
            raise SequenceGapError(...)
        self._prev_u = final_id
        return True
```

The state machine has three branches.

**Branch 1: pre-snapshot drop.** `if final_id <= self._snap_id: return False`

If the message's last update id is less than or equal to the snapshot's
update id, every change in this message is already reflected in the
snapshot. Drop it. Don't update any state.

This is the common case for the first few buffered messages: while the
WS task was running and buffering, the REST snapshot was being fetched.
The buffered messages might have update IDs lower than the snapshot's
`lastUpdateId`, meaning they happened *before* the snapshot was taken.
Drop them.

**Branch 2: bridge check (first kept message).** `if self._prev_u is None: if first_id > self._snap_id + 1: raise SequenceGapError`

If we haven't seen any kept messages yet (`_prev_u is None`) and we've
made it past Branch 1 (meaning this message's `final_id` exceeds
`_snap_id`), this is our first post-snapshot message. The contract is
that the first kept message must bridge `_snap_id + 1` — that is, its
range must include `_snap_id + 1` somewhere within it.

`first_id <= _snap_id + 1 <= final_id`

We've already established `final_id > _snap_id` from Branch 1, which
means `final_id >= _snap_id + 1`. So the right half of the bridge
condition (`_snap_id + 1 <= final_id`) holds automatically. We only
need to check the left half: `first_id <= _snap_id + 1`. Equivalently,
`first_id > _snap_id + 1` means we've missed the bridge — there's a
gap between the snapshot and the start of this message.

If we missed the bridge, raise `SequenceGapError`. Otherwise, store
this message's `final_id` as `_prev_u` and emit.

**Branch 3: contiguity check (subsequent messages).** `if first_id != self._prev_u + 1: raise SequenceGapError`

For every kept message after the first, the contract is strict
contiguity: this message's `first_id` must equal the previous message's
`final_id + 1`. If not, we've lost messages in transit. Gap, raise.

If contiguous, update `_prev_u` and emit.

The state machine has just three pieces of state: `_snap_id` (constant
after construction), `_prev_u` (updated on each kept message). Three
branches of logic. The whole class is about 15 lines.

It is also pure — no I/O, no async, no clock. Trivially unit-testable
by feeding constructed messages and asserting on return values and
exceptions. The test file `tests/unit/test_binance_parsing.py` has six
tests covering each branch and each error path.

### 9.5 `_parse_diff_message` — WS message to canonical Diffs

```python
def _parse_diff_message(data: dict[str, Any]) -> list[Diff]:
    event_ms: int = data["E"]
    timestamp = datetime.fromtimestamp(event_ms / 1000.0, tz=timezone.utc)
    diffs: list[Diff] = []
    for price_str, qty_str in data.get("b", []):
        diffs.append(Diff(side=Side.BUY, price=float(price_str),
                          quantity=float(qty_str), timestamp=timestamp))
    for price_str, qty_str in data.get("a", []):
        diffs.append(Diff(side=Side.SELL, price=float(price_str),
                          quantity=float(qty_str), timestamp=timestamp))
    return diffs
```

Translate one Binance WS message into a list of canonical `Diff`
objects. One message can contain multiple level updates (multiple bids
and asks); we expand each into its own `Diff`.

The `data["E"]` field is the event time in ms epoch. Convert via
`datetime.fromtimestamp(event_ms / 1000.0, tz=timezone.utc)` — divide
by 1000 to get seconds (with fractional part for the ms), pass UTC
timezone explicitly so the resulting `datetime` is tz-aware (the
`Snapshot` and `Diff` types implicitly assume UTC).

Bids first, then asks. The order matters only for the recorder (which
writes diffs in the order they arrive) and for tests that assert on
order; downstream consumers (the book) are order-agnostic.

Zero-quantity diffs are *preserved* by the parser — they're translated
into `Diff(quantity=0.0, ...)` and emitted normally. The book's
`apply_diff` (Section 5.5) is the one that interprets quantity zero as
"delete this level". The parser is intentionally pass-through; it
doesn't filter or interpret.

### 9.6 `BinanceConnector.__init__`

```python
def __init__(
    self,
    rest_base_url: str = DEFAULT_REST_BASE_URL,
    ws_base_url: str = DEFAULT_WS_BASE_URL,
    verify_ssl: bool = True,
) -> None:
    self._rest_base_url = rest_base_url
    self._ws_base_url = ws_base_url
    self._verify_ssl = verify_ssl
    self._session: aiohttp.ClientSession | None = None
    self._last_update_id: int | None = None
    self._ws_task: asyncio.Task[None] | None = None
    self._ws_queue: asyncio.Queue[dict[str, Any] | None] | None = None
    self._ws_ready: asyncio.Event | None = None
```

Three constructor arguments: REST base URL, WS base URL, and an SSL
verify flag. The base URLs are injectable so we can point them at test
servers in integration tests; in production they default to Binance's
real endpoints.

`verify_ssl` defaults to `True` (the safe production default). It can
be set to `False` for local-machine deployment where the system's
Python doesn't see the corporate-keychain root CAs and falls back to
certifi which doesn't have whatever cert chain Binance's edge is
serving. We discovered this during initial testing on macOS where
`curl` worked fine but Python's SSL stack didn't. The flag exists so
local development isn't blocked; on a clean Linux VPS, the default
`True` works.

State fields:

- `_session: aiohttp.ClientSession | None` — the HTTP session for REST
  calls. Lazily created in `connect()`.
- `_last_update_id: int | None` — the snapshot's `lastUpdateId`,
  populated by `fetch_snapshot()`, consumed by `stream_diffs()` to
  initialise the `_Aligner`.
- `_ws_task: asyncio.Task[None] | None` — the background asyncio task
  that holds the WebSocket connection and pushes received messages
  into `_ws_queue`. Created by `fetch_snapshot()`.
- `_ws_queue: asyncio.Queue[dict[str, Any] | None]` — the queue between
  the WS task (producer) and `stream_diffs()` (consumer). The element
  type allows `None` because we use `None` as an end-of-stream sentinel.
- `_ws_ready: asyncio.Event` — set by the WS task when its WS handshake
  completes. `fetch_snapshot()` waits on this before doing the REST
  fetch, so we know the WS is buffering messages by the time the
  snapshot is taken.

All the WS-related state is `None` initially and gets created in
`fetch_snapshot`. This is the structural change that fixed the bootstrap
race — see Section 9.10.

### 9.7 `connect()` and `disconnect()`

```python
async def connect(self) -> None:
    if self._session is None:
        connector = aiohttp.TCPConnector(ssl=self._verify_ssl)
        self._session = aiohttp.ClientSession(connector=connector)

async def disconnect(self) -> None:
    if self._ws_task is not None:
        self._ws_task.cancel()
        try:
            await self._ws_task
        except (asyncio.CancelledError, Exception):
            pass
        self._ws_task = None
        self._ws_queue = None
        self._ws_ready = None
    if self._session is not None:
        await self._session.close()
        self._session = None
```

`connect()` creates the aiohttp session, idempotent (a second call when
already connected is a no-op). The `aiohttp.TCPConnector(ssl=self._verify_ssl)`
respects the SSL verification flag.

`disconnect()` cleans up everything. First, cancel and await the WS
task. The `try/except (CancelledError, Exception)` is broad
intentionally — during cancellation, the WS task can raise either
`CancelledError` (the normal cancellation path) or some other exception
(if the WS connection was already broken when we tried to cancel). We
swallow both because we're in a teardown path and there's nothing
actionable to do.

After cancelling the WS task, clear all three pieces of WS state. Then
close the aiohttp session and clear it.

`disconnect()` is also idempotent — if nothing was connected, all the
checks fail and the method returns without doing anything.

### 9.8 `fetch_snapshot` — the bootstrap entry point

```python
async def fetch_snapshot(self, symbol: str) -> Snapshot:
    assert self._session is not None, "call connect() before fetch_snapshot()"

    if self._ws_task is None or self._ws_task.done():
        self._ws_queue = asyncio.Queue()
        self._ws_ready = asyncio.Event()
        self._ws_task = asyncio.create_task(self._buffer_ws(symbol))
        await asyncio.wait_for(self._ws_ready.wait(), timeout=WS_READY_TIMEOUT)

    url = f"{self._rest_base_url}/api/v3/depth"
    params: dict[str, Any] = {"symbol": symbol.upper(), "limit": 5000}
    async with self._session.get(url, params=params) as response:
        response.raise_for_status()
        data: dict[str, Any] = await response.json()
    self._last_update_id = int(data["lastUpdateId"])
    return _parse_depth_response(data, datetime.now(timezone.utc))
```

This is where the bootstrap protocol lives. Read it carefully — it's the
fix for the race condition we discovered during live validation.

Three phases:

**Phase 1: precondition.** `assert self._session is not None`. The
caller must have called `connect()` first. We use `assert` rather than
raising a custom exception because this is a programming error (the
maintainer always calls `connect()` first; a violation means the
maintainer is buggy, not that the connector should handle it
gracefully).

**Phase 2: start the WS task if needed.**

```python
if self._ws_task is None or self._ws_task.done():
    self._ws_queue = asyncio.Queue()
    self._ws_ready = asyncio.Event()
    self._ws_task = asyncio.create_task(self._buffer_ws(symbol))
    await asyncio.wait_for(self._ws_ready.wait(), timeout=WS_READY_TIMEOUT)
```

If there isn't already a live WS task (either because this is the first
call, or because the previous task ended — e.g. on resync), spin one up.
Create fresh queue and ready-event objects, launch the task, and *wait
for the ready event* with a 10-second timeout.

The ready event is set by the WS task itself once its WebSocket handshake
completes. Waiting for it here means: by the time we proceed to the REST
fetch, we know the WS connection is open and the task is sitting in
`async for raw in ws:` ready to receive. Any messages Binance sends from
this moment forward will be captured in the queue.

This is the critical bit. Because the WS is *already buffering* before
we do the REST fetch, the snapshot we receive will be glued to a queue
that already contains the messages around the snapshot's `lastUpdateId`.
The aligner will drop pre-snapshot messages, find the bridging one, and
emit from there.

If this `await asyncio.wait_for(...)` ever times out — meaning the WS
task didn't open its connection within 10 seconds — `TimeoutError`
propagates to the maintainer, which catches it as a generic exception
and retries. (Section 10.5.)

**Phase 3: REST fetch.**

```python
url = f"{self._rest_base_url}/api/v3/depth"
params: dict[str, Any] = {"symbol": symbol.upper(), "limit": 5000}
async with self._session.get(url, params=params) as response:
    response.raise_for_status()
    data: dict[str, Any] = await response.json()
self._last_update_id = int(data["lastUpdateId"])
return _parse_depth_response(data, datetime.now(timezone.utc))
```

Standard aiohttp GET with the symbol uppercased (Binance is case-sensitive
about this) and `limit=5000` for maximum depth. `raise_for_status()`
turns any HTTP error into an exception. `await response.json()` parses
the JSON.

Store the snapshot's `lastUpdateId` as `_last_update_id` — `stream_diffs`
will read this when it constructs the aligner. Convert and return the
`Snapshot` via `_parse_depth_response`, capturing the wall-clock time at
this moment as the snapshot's timestamp.

### 9.9 `_buffer_ws` — the background WS consumer

```python
async def _buffer_ws(self, symbol: str) -> None:
    assert self._ws_queue is not None
    assert self._ws_ready is not None
    uri = f"{self._ws_base_url}/ws/{symbol.lower()}@depth@100ms"
    ssl_ctx = ssl.create_default_context()
    if not self._verify_ssl:
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        async with websockets.connect(uri, ssl=ssl_ctx) as ws:
            self._ws_ready.set()
            async for raw in ws:
                msg: dict[str, Any] = json.loads(raw)
                await self._ws_queue.put(msg)
    finally:
        await self._ws_queue.put(None)
```

The body that runs as the background task. Three things happen.

**Establish the WebSocket.** Build the URI from the base + symbol +
stream name. Note `symbol.lower()` — Binance's WS endpoints use
lowercase symbols in the URL path (vs uppercase in REST query strings).
The stream name `@depth@100ms` requests the depth feed throttled to
one message every 100 ms (Binance has both 100 ms and 1000 ms variants;
we use 100 ms for low latency).

Construct an SSL context — verifying by default, weakened if `verify_ssl`
is False. The `check_hostname = False` and `verify_mode = ssl.CERT_NONE`
flags together disable verification entirely. This mirrors the aiohttp
SSL handling in `connect()`.

`async with websockets.connect(uri, ssl=ssl_ctx) as ws:` — open the
WebSocket. The `async with` ensures clean shutdown on exit.

**Signal readiness.** `self._ws_ready.set()`. This unblocks
`fetch_snapshot()`'s `await asyncio.wait_for(self._ws_ready.wait(), ...)`.
We set it *immediately after the handshake completes*, before reading
any messages, so that fetch_snapshot can proceed in parallel with the
first-message read.

**Drain messages into the queue.** `async for raw in ws:` iterates
incoming WS frames. Each frame is a JSON-encoded `depthUpdate` message;
`json.loads(raw)` parses it. `await self._ws_queue.put(msg)` enqueues
for `stream_diffs` to read. The await is on the queue's put, which
provides backpressure if the queue ever filled (it has unbounded
capacity by default, so `put` is effectively non-blocking).

**The finally clause.** `await self._ws_queue.put(None)`. When the WS
ends — whether by clean disconnect, exception, or task cancellation —
we put a `None` sentinel onto the queue. `stream_diffs` reads `None`
and treats it as end-of-stream, returning normally. This is how the
WS task's death propagates to the consumer cleanly.

Critical detail: we use `None` as the sentinel rather than something
like a `EndOfStream` exception because async generators can't easily
propagate exceptions through the queue without complicating the
consumer. A sentinel value is the standard async producer/consumer
pattern.

### 9.10 `stream_diffs` — the consumer

```python
async def stream_diffs(self, symbol: str) -> AsyncIterator[Diff]:
    assert self._last_update_id is not None, (
        "fetch_snapshot() must be called before stream_diffs() "
        "to establish the bootstrap sequence baseline"
    )
    assert self._ws_queue is not None, (
        "fetch_snapshot() must be called before stream_diffs() "
        "to start the WS buffer"
    )
    aligner = _Aligner(self._last_update_id)
    while True:
        msg = await self._ws_queue.get()
        if msg is None:
            return
        if aligner.should_emit(msg):
            for diff in _parse_diff_message(msg):
                yield diff
```

An async generator. The signature is `async def -> AsyncIterator[Diff]`
*with `yield` in the body*, which makes it an async generator function
— calling it returns an `AsyncGenerator[Diff, None]` (which is an
`AsyncIterator[Diff]`).

(See Section 8.4 on why this works as an override of the abstract
`def stream_diffs` despite the `async` keyword.)

**Preconditions.** Both asserts: `_last_update_id` and `_ws_queue` must
be populated, which means `fetch_snapshot` must have been called first.

**Construct the aligner.** `_Aligner(self._last_update_id)`. This is
the per-call aligner state — fresh on every call. If the maintainer
re-bootstraps (calls `fetch_snapshot` then `stream_diffs` again), it
gets a fresh aligner.

**The main loop.** `while True: msg = await self._ws_queue.get()`. Pull
a message from the queue. The await yields control to the event loop
if the queue is empty.

`if msg is None: return` — sentinel detected. The async generator
returns, which causes the consuming `async for` loop to exit cleanly.

`if aligner.should_emit(msg):` — pass the message through the aligner.
`should_emit` returns False for pre-snapshot messages (drop), True for
keepers, or raises `SequenceGapError` on a gap.

`for diff in _parse_diff_message(msg): yield diff` — for each level
update in the message, parse it into a `Diff` and yield.

The generator yields one `Diff` per level update, not one per WS
message. Downstream consumers (the maintainer's apply loop, the
recorder) operate on individual `Diff`s. The unbundling happens here.

If `should_emit` raises `SequenceGapError`, the exception propagates
out of `stream_diffs`, through the maintainer's `async for diff in
stream_diffs(...)` loop, and is caught by the maintainer's resync
handler. (Section 10.5.)

### 9.11 The bootstrap protocol, end to end

Putting Sections 9.6 through 9.10 together in time:

```
Time t=0:   maintainer.start() schedules maintainer._run() as a task
Time t=1:   maintainer._run() calls connector.connect()
              → creates aiohttp session
              → returns

Time t=2:   maintainer._run() calls connector.fetch_snapshot(symbol)
            ┌───────────── inside fetch_snapshot ─────────────┐
            │                                                  │
Time t=2:   │  _ws_queue, _ws_ready created; _buffer_ws task   │
            │  scheduled                                       │
Time t=3:   │  _buffer_ws starts: opens WebSocket connection   │
Time t=4:   │  WS handshake completes; _buffer_ws sets         │
            │  _ws_ready and starts async for ws loop          │
Time t=5:   │  fetch_snapshot's await asyncio.wait_for(...)    │
            │  unblocks                                        │
Time t=5+:  │  _buffer_ws receives WS message, puts in queue   │
Time t=5+:  │  fetch_snapshot makes REST GET                   │
Time t=6:   │  REST returns; data parsed; _last_update_id set; │
            │  Snapshot returned                               │
            │                                                  │
            └──────────────────────────────────────────────────┘

Time t=7:   maintainer._run() calls book.apply_snapshot(snapshot)
Time t=8:   maintainer._run() iterates connector.stream_diffs(symbol)
            ┌──────── inside stream_diffs ──────┐
            │                                    │
            │  Aligner constructed with _snap_id│
            │  Loop:                             │
            │    msg = await _ws_queue.get()    │
            │    aligner.should_emit(msg)        │
            │    for diff in _parse_diff: yield │
            │                                    │
            └────────────────────────────────────┘

Time t=8+:  Each yielded diff is applied to the book
Time t=8+:  Each applied diff publishes a BookUpdate
            ... continues indefinitely ...
```

The key invariant is that between t=4 and t=6, the WS task is buffering
messages while the REST fetch is in progress. Whatever `lastUpdateId`
the REST returns, there are buffered WS messages around that point in
time waiting in the queue. When `stream_diffs` starts draining the
queue, the aligner finds the bridging message naturally.

### 9.12 What the bug was, and what the fix does

The original (broken) version of `BinanceConnector` had this structure:

```python
# WRONG — original version
async def fetch_snapshot(self, symbol: str) -> Snapshot:
    # ... REST fetch only, no WS task ...
    self._last_update_id = int(data["lastUpdateId"])
    return _parse_depth_response(data, datetime.now(timezone.utc))

async def stream_diffs(self, symbol: str) -> AsyncIterator[Diff]:
    aligner = _Aligner(self._last_update_id)
    uri = f"{self._ws_base_url}/ws/{symbol.lower()}@depth@100ms"
    async with websockets.connect(uri, ssl=ssl_ctx) as ws:
        async for raw in ws:
            msg = json.loads(raw)
            if aligner.should_emit(msg):
                for diff in _parse_diff_message(msg):
                    yield diff
```

The structural difference: WS connection happened *inside* `stream_diffs`,
*after* `fetch_snapshot` had already completed. So the timeline was:

```
1. fetch_snapshot → REST fetch → _last_update_id = N
2. stream_diffs called → opens WS → first WS message has U=N+15 (we missed
   N+1 through N+14 during steps 1 and 2)
3. aligner sees first_id=N+15 > snap_id+1=N+1 → raises SequenceGapError
4. maintainer catches → resync → fetch_snapshot again → same problem
5. infinite loop
```

In live testing, this manifested as ~31 resyncs in 5 minutes. The
maintainer was constantly catching gap errors and re-bootstrapping,
never making forward progress.

The fix moved the WS connection establishment into a background task
that starts *during* `fetch_snapshot`, *before* the REST fetch. The
WS is buffering messages while the REST is in flight, so by the time
the REST returns and `stream_diffs` starts consuming, the queue
contains messages that span `lastUpdateId + 1`. The aligner finds the
bridge and proceeds normally. After the fix, live testing showed
zero resyncs over 30 minutes.

This is the kind of bug you can only find with live network testing.
Synthetic fixtures pass because they don't have the latency between
"REST starts" and "REST returns" — in a fixture, both happen
instantaneously and the WS messages happen to land in the right
order. In real life, the REST fetch takes 100-300 ms, during which
Binance publishes 1-3 WS messages, and missing those is the entire
problem.

### 9.13 Tests

The tests are split:

`tests/unit/test_binance_parsing.py` — pure tests for the parsers and
the aligner. Ten tests:

- `_parse_diff_message` emits bids before asks.
- Empty bid/ask sides handled.
- Zero quantity preserved (parser doesn't filter).
- Timestamp uses ms event time correctly.
- Aligner skips messages fully before the snapshot.
- Aligner accepts the first bridging message (multiple variants:
  bridges across, exact +1, etc.).
- Aligner raises on first-message gap.
- Aligner accepts contiguous sequences.
- Aligner raises on mid-stream gap.

`tests/integration/test_binance_connector.py` — integration tests with
a `_FakeSession` (mocking aiohttp) and a `_FakeWebSocket` (mocking
`websockets.connect`). Nine tests:

- `fetch_snapshot` parses the depth fixture correctly.
- `fetch_snapshot` calls the right URL with the right params.
- `fetch_snapshot` without `connect()` first raises `AssertionError`.
- `stream_diffs` yields aligned diffs from buffered WS messages.
- `fetch_snapshot` stores `lastUpdateId`.
- `stream_diffs` without `fetch_snapshot()` first raises `AssertionError`.
- `stream_diffs` drops messages already covered by snapshot.
- `stream_diffs` raises `SequenceGapError` on first-message gap.
- `stream_diffs` raises `SequenceGapError` on mid-stream gap.

The fakes mock the *mechanics* of aiohttp and websockets while letting
the real connector code run. This means we test the actual control
flow of `fetch_snapshot` and `stream_diffs` end-to-end, including the
async task setup, the queue drainage, and the aligner integration.
The only thing we don't test in the unit suite is real-network
behaviour — which is what the live validation script
(`scripts/live_validate.py`) covers.

---

# Part IV — Keeping a book alive (`src/vortexec/maintainer/`)

The maintainer is the **keystone module** — the architecture document's
own word for it. Every other layer either feeds into it (the connector
produces snapshots and diffs that the maintainer applies) or reads from
it (the simulator and feature extractor walk the book that the maintainer
keeps current; the recorder subscribes to its pub/sub channel; the HTTP
API queries its health and book state). If the maintainer is wrong,
everything above it is wrong.

The class has one job at the highest level: **own the live order book for
one (venue, symbol) pair, indefinitely, surviving everything that can go
wrong**. Concretely, "indefinitely" and "surviving everything" decompose
into:

1. Fetch the initial snapshot and apply it.
2. Consume the diff stream and apply each diff in order.
3. Publish each applied diff to subscribers (via a pub/sub channel).
4. When a sequence gap is detected, re-bootstrap (fetch a fresh snapshot,
   replace the book, resume).
5. When any other transient error happens (network blip, parser error),
   log and retry with backoff.
6. When external observers ask "is your book healthy?", answer based on
   how recently we applied an update.
7. When asked to stop, shut down cleanly — cancel background tasks, send
   end-of-stream sentinels to subscribers, disconnect the connector.

The lifecycle complexity is real. The `BookMaintainer` class is roughly
150 lines, and almost every line is doing something subtle.

## 10. `maintainer/book_maintainer.py`

### 10.1 The full file

```python
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from vortexec.core.book import OrderBook
from vortexec.core.types import BookUpdate
from vortexec.venues.base import SequenceGapError, VenueConnector

log = logging.getLogger("vortexec.maintainer")

_DEFAULT_SUBSCRIBER_QUEUE_SIZE = 1024
_DEFAULT_STALENESS_THRESHOLD_SECONDS = 5.0
_RECONNECT_BACKOFF_SECONDS = 1.0


class BookMaintainer:
    def __init__(
        self,
        connector: VenueConnector,
        venue: str,
        symbol: str,
        subscriber_queue_size: int = _DEFAULT_SUBSCRIBER_QUEUE_SIZE,
        staleness_threshold_seconds: float = _DEFAULT_STALENESS_THRESHOLD_SECONDS,
    ) -> None:
        self._connector = connector
        self._venue = venue
        self._symbol = symbol
        self._book = OrderBook()
        self._task: asyncio.Task[None] | None = None
        self._subscriber_queues: list[asyncio.Queue[BookUpdate | None]] = []
        self._subscriber_queue_size = subscriber_queue_size
        self._staleness_threshold = staleness_threshold_seconds
        self._last_update_at: float | None = None
        self._drop_count = 0
        self._resync_count = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        for q in list(self._subscriber_queues):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

        await self._connector.disconnect()

    def get_book(self) -> OrderBook:
        return self._book

    @property
    def venue(self) -> str:
        return self._venue

    @property
    def symbol(self) -> str:
        return self._symbol

    def is_healthy(self) -> bool:
        if self._last_update_at is None:
            return False
        return (time.monotonic() - self._last_update_at) < self._staleness_threshold

    @property
    def drop_count(self) -> int:
        return self._drop_count

    @property
    def resync_count(self) -> int:
        return self._resync_count

    def stream_updates(self) -> AsyncIterator[BookUpdate]:
        q: asyncio.Queue[BookUpdate | None] = asyncio.Queue(
            maxsize=self._subscriber_queue_size
        )
        self._subscriber_queues.append(q)
        return self._iterate_queue(q)

    async def _iterate_queue(
        self, q: asyncio.Queue[BookUpdate | None]
    ) -> AsyncIterator[BookUpdate]:
        try:
            while True:
                item = await q.get()
                if item is None:
                    return
                yield item
        finally:
            if q in self._subscriber_queues:
                self._subscriber_queues.remove(q)

    def _publish(self, update: BookUpdate) -> None:
        for q in list(self._subscriber_queues):
            try:
                q.put_nowait(update)
            except asyncio.QueueFull:
                self._drop_count += 1

    async def _run(self) -> None:
        await self._connector.connect()
        while True:
            try:
                snapshot = await self._connector.fetch_snapshot(self._symbol)
                self._book.apply_snapshot(snapshot)
                self._last_update_at = time.monotonic()
                async for diff in self._connector.stream_diffs(self._symbol):
                    self._book.apply_diff(diff)
                    self._publish(
                        BookUpdate(
                            venue=self._venue, symbol=self._symbol, diff=diff
                        )
                    )
                    self._last_update_at = time.monotonic()
                # Stream ended with no exception (typically a WS disconnect).
                # In production the stream should never end of its own accord,
                # so treat this as a resync trigger and rebuild the book.
                self._resync_count += 1
                log.info(
                    "%s/%s: stream ended cleanly, rebootstrapping in %.1fs",
                    self._venue, self._symbol, _RECONNECT_BACKOFF_SECONDS,
                )
                await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
            except asyncio.CancelledError:
                raise
            except SequenceGapError as e:
                self._resync_count += 1
                log.info(
                    "%s/%s: sequence gap (%s), rebootstrapping in %.1fs",
                    self._venue, self._symbol, e, _RECONNECT_BACKOFF_SECONDS,
                )
                await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
            except Exception as e:
                self._resync_count += 1
                log.warning(
                    "%s/%s: unexpected error %r, rebootstrapping in %.1fs",
                    self._venue, self._symbol, e, _RECONNECT_BACKOFF_SECONDS,
                )
                await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
```

The structure has five conceptual chunks:

- **State and constructor** (10.2)
- **Lifecycle: start, stop** (10.3, 10.4)
- **Read methods: `get_book`, `venue`, `symbol`, `is_healthy`, counters** (10.5)
- **Pub/sub: `stream_updates`, `_iterate_queue`, `_publish`** (10.6)
- **The run loop: `_run`** (10.7)

Each is small in line count but doing real work. Let me walk through.

### 10.2 Constructor and state

```python
def __init__(
    self,
    connector: VenueConnector,
    venue: str,
    symbol: str,
    subscriber_queue_size: int = _DEFAULT_SUBSCRIBER_QUEUE_SIZE,
    staleness_threshold_seconds: float = _DEFAULT_STALENESS_THRESHOLD_SECONDS,
) -> None:
    self._connector = connector
    self._venue = venue
    self._symbol = symbol
    self._book = OrderBook()
    self._task: asyncio.Task[None] | None = None
    self._subscriber_queues: list[asyncio.Queue[BookUpdate | None]] = []
    self._subscriber_queue_size = subscriber_queue_size
    self._staleness_threshold = staleness_threshold_seconds
    self._last_update_at: float | None = None
    self._drop_count = 0
    self._resync_count = 0
```

Five constructor arguments: the connector (injected — could be a real
`BinanceConnector` or a fake for tests), the venue and symbol (used as
metadata when publishing `BookUpdate`s), and two configuration knobs
(subscriber queue size, staleness threshold).

The state fields:

- **`_connector`** — the venue connector. Owned by external code (the
  service composes connector + maintainer), but the maintainer is
  responsible for calling `connect()` / `disconnect()` on its
  lifecycle boundaries.
- **`_venue`, `_symbol`** — identification metadata. Surfaced via the
  `venue` and `symbol` properties so external code can read them.
- **`_book`** — the live `OrderBook` instance. This is the central state
  the whole module exists to maintain.
- **`_task`** — the asyncio Task that runs `_run()`. `None` until
  `start()` is called.
- **`_subscriber_queues`** — list of asyncio Queues, one per active
  subscriber. The pub/sub fanout mechanism.
- **`_subscriber_queue_size`** — bounded capacity for each subscriber
  queue. Default 1024. Bounded so a slow subscriber can't cause
  unbounded memory growth in the maintainer.
- **`_staleness_threshold`** — how many seconds without an update
  before `is_healthy()` returns False. Default 5 seconds.
- **`_last_update_at`** — `time.monotonic()` timestamp of the most
  recent applied update (snapshot or diff). `None` until the first
  update is applied. Used by `is_healthy()`.
- **`_drop_count`** — count of `BookUpdate`s dropped because a
  subscriber's queue was full. Exposed as a property for monitoring.
- **`_resync_count`** — count of bootstrap re-attempts. Exposed for
  monitoring. Useful for "is something subtly wrong" diagnosis — a
  healthy maintainer in steady-state should have `resync_count = 0`
  for hours at a time.

None of these are public attributes (all underscore-prefixed). The
public surface is the methods and the small set of `@property`
accessors.

### 10.3 `start`

```python
async def start(self) -> None:
    if self._task is not None:
        return
    self._task = asyncio.create_task(self._run())
```

Idempotent: if `_task` is already set, return without doing anything.
Otherwise, create a background task running `_run()`.

`asyncio.create_task` schedules the coroutine for execution on the
event loop *immediately* (or as soon as the current coroutine yields).
The maintainer starts producing on next event-loop tick.

Note that `start` itself doesn't `await` the task — it just kicks it
off. The task runs in the background; the caller proceeds. This is
deliberate. The maintainer is a long-running concurrent activity, not
a request/response operation. If `start` blocked until the maintainer
was "ready", the caller would have no easy way to manage multiple
maintainers in parallel.

External code that wants to know whether the maintainer is up-and-running
should poll `is_healthy()` or check `get_book().best_bid() is not None`.

### 10.4 `stop`

```python
async def stop(self) -> None:
    if self._task is not None:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    for q in list(self._subscriber_queues):
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(None)
        except asyncio.QueueFull:
            pass

    await self._connector.disconnect()
```

Three-phase shutdown:

**Phase 1: cancel the run task.** `self._task.cancel()` sends a
cancellation to the running task. `await self._task` waits for the task
to finish processing the cancellation. `except CancelledError: pass`
absorbs the cancellation exception that the task raises during teardown
(this is the normal path). After the task is fully done, `_task = None`.

The `if self._task is not None` guard makes `stop` safe to call even
when `start` was never called — e.g. when the service shuts down before
fully initialising.

**Phase 2: signal subscribers.** For each subscriber's queue, send a
`None` sentinel so the subscriber's `_iterate_queue` loop sees it and
exits cleanly. The `if q.full()` branch makes room first — if the
subscriber has been ignoring its queue and the queue is full, we drop
one item to make space for the sentinel. Without this, `put_nowait`
would raise `QueueFull` and the subscriber would hang forever waiting
for messages that never come.

The `for q in list(self._subscriber_queues)` iterates a snapshot of the
list rather than the list itself — because as subscribers exit, their
`_iterate_queue` finally clauses remove their queues from the list, and
modifying a list during iteration is unsafe.

**Phase 3: disconnect the connector.** `await self._connector.disconnect()`.
This is what closes the WS task in `BinanceConnector` and the aiohttp
session.

The order matters. Cancel the task first (so it stops trying to apply
diffs or publish). Then signal subscribers (so they exit). Then close
the connection (so we free the network resources).

### 10.5 Read methods

```python
def get_book(self) -> OrderBook:
    return self._book

@property
def venue(self) -> str:
    return self._venue

@property
def symbol(self) -> str:
    return self._symbol

def is_healthy(self) -> bool:
    if self._last_update_at is None:
        return False
    return (time.monotonic() - self._last_update_at) < self._staleness_threshold

@property
def drop_count(self) -> int:
    return self._drop_count

@property
def resync_count(self) -> int:
    return self._resync_count
```

Simple accessors, but worth dwelling on a few.

**`get_book()` is sync.** It returns the `OrderBook` reference directly.
The caller can read `best_bid()`, `best_ask()`, walk levels, etc. — all
synchronously, no `await` needed.

This is the question that came up in the architecture review: what if
the maintainer is mid-`apply_diff` when a caller reads the book? The
answer (covered in Section 5.11) is that it *can't* happen, because
`apply_diff` is synchronous Python code with no `await` points, and
asyncio is single-threaded. Once `apply_diff` starts, no other coroutine
runs until it finishes. `get_book()` followed by reads always sees a
consistent book.

If you held the book across an `await` and then read it, you could see
mutations that happened during the await. But callers don't do that —
the simulator and feature extractor both do their work synchronously
within a single sync block. The HTTP API handler holds `get_book()`
across `simulate_market_order` and `extract_features` calls but those
are also sync.

**`is_healthy()` uses `time.monotonic`.** Not `time.time` or
`datetime.now`. `time.monotonic` returns a monotonically increasing
float that's immune to wall-clock adjustments (NTP corrections, manual
clock changes, leap seconds). For measuring elapsed time we always
want monotonic; for recording timestamps we want wall-clock UTC.

`is_healthy()` returns `False` in two situations: when we've never
applied any update (`_last_update_at is None`, e.g. before the first
snapshot lands), and when we have applied updates but the most recent
one is more than `_staleness_threshold` seconds in the past. The
threshold defaults to 5 seconds, configurable. The choice of 5 seconds
is conservative — on a busy symbol like BTCUSDT, diffs arrive every
100 ms, so 5 seconds of silence means 50 missed updates, which is
plenty of evidence that something is wrong. On a quieter symbol, you
might want a larger threshold.

The threshold check is `< _staleness_threshold`, not `<=`. At exactly
the threshold, we consider stale. This is conventional and matches
how most monitoring tools define liveness.

**`drop_count` and `resync_count` are read-only.** Exposed as
`@property` to make this explicit. External code can read but not write.

### 10.6 Pub/sub

The pub/sub mechanism is how the recorder receives diffs without being
tightly coupled to the maintainer's internals. The maintainer publishes
each applied diff as a `BookUpdate`; any number of subscribers can call
`stream_updates()` to receive an async-iterable of those updates.

```python
def stream_updates(self) -> AsyncIterator[BookUpdate]:
    q: asyncio.Queue[BookUpdate | None] = asyncio.Queue(
        maxsize=self._subscriber_queue_size
    )
    self._subscriber_queues.append(q)
    return self._iterate_queue(q)

async def _iterate_queue(
    self, q: asyncio.Queue[BookUpdate | None]
) -> AsyncIterator[BookUpdate]:
    try:
        while True:
            item = await q.get()
            if item is None:
                return
            yield item
    finally:
        if q in self._subscriber_queues:
            self._subscriber_queues.remove(q)

def _publish(self, update: BookUpdate) -> None:
    for q in list(self._subscriber_queues):
        try:
            q.put_nowait(update)
        except asyncio.QueueFull:
            self._drop_count += 1
```

**`stream_updates()`** — public API. Note it's `def`, not `async def`,
same reason as `VenueConnector.stream_diffs` (Section 8.4): it returns
an `AsyncIterator` directly, no `await` needed at the call site. The
implementation:

1. Create a fresh bounded `asyncio.Queue` for this subscriber.
2. Append it to `_subscriber_queues` so `_publish` can find it.
3. Return `_iterate_queue(q)`, which is an async generator that drains
   the queue.

Subscribers call `async for update in maintainer.stream_updates(): ...`
and receive updates as the maintainer applies them. Different
subscribers get independent queues — slow ones can't slow down fast
ones, and a backpressure event in one doesn't affect the other.

**`_iterate_queue`** — the per-subscriber consumer loop. While running,
pulls from the queue; on `None`, returns (which exits the `async for`
loop in the caller). The `finally` clause removes the queue from the
maintainer's list so terminated subscribers don't get publication
attempts. The `if q in self._subscriber_queues` check handles the case
where the queue was already removed (e.g. during `stop()` shutdown).

**`_publish`** — called by `_run` after each applied diff. Iterates a
snapshot of `_subscriber_queues` (so concurrent removals don't break
iteration) and attempts `put_nowait` on each. If a queue is full,
`QueueFull` is caught and `_drop_count` is incremented.

The use of `put_nowait` (rather than `await q.put(update)`) is the key
choice that prevents the maintainer from blocking. If a subscriber is
slow, its queue fills, and the maintainer drops updates for that
subscriber. The maintainer never waits for a slow subscriber. This
implements the architecture's "the recorder never blocks the maintainer"
invariant — drops are preferable to backpressure on the live data path.

The dropped updates show up as `drop_count` increments, monitorable via
the `drop_count` property. A persistent non-zero drop rate means a
subscriber is too slow to keep up — diagnosable from outside.

### 10.7 `_run` — the actual loop

This is the heart of the maintainer. The whole correctness of the live
book depends on this loop's behaviour.

```python
async def _run(self) -> None:
    await self._connector.connect()
    while True:
        try:
            snapshot = await self._connector.fetch_snapshot(self._symbol)
            self._book.apply_snapshot(snapshot)
            self._last_update_at = time.monotonic()
            async for diff in self._connector.stream_diffs(self._symbol):
                self._book.apply_diff(diff)
                self._publish(
                    BookUpdate(
                        venue=self._venue, symbol=self._symbol, diff=diff
                    )
                )
                self._last_update_at = time.monotonic()
            # Stream ended with no exception (typically a WS disconnect).
            # In production the stream should never end of its own accord,
            # so treat this as a resync trigger and rebuild the book.
            self._resync_count += 1
            log.info("%s/%s: stream ended cleanly, rebootstrapping in %.1fs",
                     self._venue, self._symbol, _RECONNECT_BACKOFF_SECONDS)
            await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
        except asyncio.CancelledError:
            raise
        except SequenceGapError as e:
            self._resync_count += 1
            log.info("%s/%s: sequence gap (%s), rebootstrapping in %.1fs",
                     self._venue, self._symbol, e, _RECONNECT_BACKOFF_SECONDS)
            await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
        except Exception as e:
            self._resync_count += 1
            log.warning("%s/%s: unexpected error %r, rebootstrapping in %.1fs",
                        self._venue, self._symbol, e, _RECONNECT_BACKOFF_SECONDS)
            await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
```

The structure is an outer `while True` with three exception handlers.
This is the auto-reconnect loop, the piece we added during the
preparation for unattended VPS deployment (it had been a simple
single-pass before, which broke on any transient failure).

**The happy path.**

```python
snapshot = await self._connector.fetch_snapshot(self._symbol)
self._book.apply_snapshot(snapshot)
self._last_update_at = time.monotonic()
async for diff in self._connector.stream_diffs(self._symbol):
    self._book.apply_diff(diff)
    self._publish(BookUpdate(venue=self._venue, symbol=self._symbol, diff=diff))
    self._last_update_at = time.monotonic()
```

Fetch the snapshot. Apply it. Record `last_update_at`. Then loop over
the diff stream — for each `Diff`, apply to the book and publish a
`BookUpdate` to subscribers, updating `last_update_at` after each.

The `_publish` call wraps the bare `Diff` in a `BookUpdate` that adds
`venue` and `symbol` context, so subscribers know which book the diff
came from.

`_last_update_at` is bumped after every apply, which is what
`is_healthy()` keys off. As long as diffs are flowing, the maintainer
is healthy. If the stream stalls for more than 5 seconds (the staleness
threshold), `is_healthy()` returns False until the stall is resolved.

The `async for` over `stream_diffs` is what suspends and resumes as
diffs arrive. In normal operation, this loop runs *forever* — diffs
keep arriving, each one is applied and published.

**The clean-end branch.** What happens if `stream_diffs` returns
without raising? Per Section 9.10, that means the WS task ended (sent
the `None` sentinel) — most commonly because Binance closed the WS
connection, less commonly because of a network blip on our side.

```python
self._resync_count += 1
log.info("%s/%s: stream ended cleanly, rebootstrapping in %.1fs", ...)
await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
```

In production, a clean end is *not normal* — Binance doesn't close
healthy connections. We treat it as a recoverable failure: increment
`resync_count`, log it, sleep 1 second to back off, then loop back to
the top and re-bootstrap. The next iteration calls `fetch_snapshot`,
which starts a fresh WS task and a fresh snapshot.

This was the *one real bug* we had to fix before VPS deployment. The
original code had `return` here instead of the `continue` semantics —
when the WS disconnected cleanly, the maintainer would silently exit
the loop and stop maintaining the book, with no recovery and no log.
The maintainer task would complete normally, no exception, no alarm —
and the book would silently freeze. The current code treats this as
"unexpected stream end, re-bootstrap" and recovers automatically.

**The `CancelledError` branch.** `except asyncio.CancelledError: raise`.

When `stop()` calls `self._task.cancel()`, the cancellation arrives at
whatever `await` point is currently suspended (the `async for` loop or
one of the `await asyncio.sleep` calls). The cancellation manifests as
a `CancelledError` exception.

We catch it explicitly *only to re-raise it*. This is so the `except
Exception:` clause below doesn't accidentally catch it — `CancelledError`
is a subclass of `BaseException` in Python 3.8+, not `Exception`, so
strictly speaking we don't need the explicit re-raise. But being
explicit makes the intent obvious to a reader: "we *want* the
cancellation to propagate out of `_run`, so that the task ends cleanly
and `await self._task` in `stop()` completes."

Without this, future maintenance might introduce something that catches
`BaseException` accidentally, and we'd lose the ability to cancel the
task. The explicit re-raise is a small cost for a real correctness
guarantee.

**The `SequenceGapError` branch.**

```python
self._resync_count += 1
log.info("%s/%s: sequence gap (%s), rebootstrapping in %.1fs", ...)
await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
```

Same behaviour as the clean-end branch but with a different log
message. A gap is the most common kind of error worth re-bootstrapping
on — it's a protocol-level event that means "the book is now wrong,
discard and start over".

**The generic exception branch.**

```python
self._resync_count += 1
log.warning("%s/%s: unexpected error %r, rebootstrapping in %.1fs", ...)
await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
```

Anything else — `aiohttp.ClientError`, `json.JSONDecodeError`,
`websockets.exceptions.ConnectionClosed`, you name it — is treated as
a transient failure. Log at `warning` level (not `info`, because it's
unexpected) and retry. This is the catch-all that makes the maintainer
*genuinely* indestructible — short of `CancelledError` (which means we
explicitly asked it to stop), nothing makes the loop exit.

This is belt-and-suspenders with the systemd `Restart=always` policy
that supervises the process. systemd handles "the whole process died";
the maintainer's catch handles "the run loop hit a transient error".
Two layers of resilience for two different failure modes.

### 10.8 Why a single `_run` and not separate "bootstrap" / "consume" methods

A more conventional decomposition would be:

```python
async def _run(self):
    await self._connector.connect()
    while True:
        try:
            await self._bootstrap()
            await self._consume_diffs()
        except ... :
            ...

async def _bootstrap(self):
    snapshot = await ...
    self._book.apply_snapshot(snapshot)
    self._last_update_at = time.monotonic()

async def _consume_diffs(self):
    async for diff in self._connector.stream_diffs(self._symbol):
        ...
```

We didn't do this. Reason: the entire loop body is short enough that
breaking it up obscures rather than clarifies. The reader sees the
whole arc — "fetch snapshot, apply, then consume diffs forever, then
on any failure re-do all of that" — in one place. Pulling out
sub-methods would require the reader to jump around to understand
the control flow, and the sub-methods would each be too short to
justify their own functions.

The exception handling is also flattened intentionally. A three-arm
match (clean end / SequenceGapError / generic Exception) all leading
to the same outcome (sleep + retry) could be expressed with a single
`except Exception:`, but distinguishing them in logs is valuable.
Different failure modes produce different log messages, which makes
post-hoc diagnosis easier.

### 10.9 Tests

`tests/unit/test_book_maintainer.py` has 17 tests, covering:

- **Lifecycle**: snapshot applied on start, diffs applied after snapshot,
  zero-quantity diff removes a level, `start()` is idempotent, `stop()`
  cancels a hung stream, `stop()` without `start()` still disconnects.
- **Pub/sub**: subscriber receives `BookUpdate`s for each diff, multiple
  subscribers each get all updates independently, subscriber
  unsubscribes when iteration completes (queue removed from list), no
  blocking when there are no subscribers, slow subscriber drops excess
  updates.
- **Health**: `is_healthy()` returns False before any updates, True
  after recent update, False after staleness threshold (using
  `monkeypatch` on `time.monotonic` to simulate elapsed time
  deterministically).
- **Resync**: book rebuilt from new snapshot after sequence gap,
  updates from both pre- and post-gap sessions reach subscribers,
  multiple resyncs increment the counter.

The tests use an in-test `FakeVenueConnector` that implements the
`VenueConnector` ABC. It's driven by a "script" — a list of
`FakeSession` records, each describing what one fetch/stream cycle
should produce (snapshot, diffs, end behaviour). The `end` field
controls how the session ends: `"complete"` (stream exhausts
naturally), `"gap"` (raise `SequenceGapError`), `"hang"` (block forever
after diffs).

The default `end` is `"hang"` — this matches production semantics
where the stream should never end of its own accord. Tests that want a
clean end or a gap pass it explicitly.

The tests reach into `_subscriber_queues` and `_last_update_at`
directly in some cases — testing internal state. This is acceptable
in same-package tests; we're verifying the implementation, not just
the public contract.

The monkeypatching of `time.monotonic` is worth dwelling on. To test
that `is_healthy()` becomes False after the staleness threshold, we
need to make time *pass* without actually sleeping. The test patches
`vortexec.maintainer.book_maintainer.time.monotonic` to return values
from a controlled list, advances the list between assertions, and
verifies the health flag flips at the right moment. This gives
deterministic time-based tests with no `sleep` calls.

---

# Part V — Persistence (`src/vortexec/recorder/`)

The recorder takes diff events from the maintainer's pub/sub channel
and writes them to disk as Parquet files. It also periodically writes
full-book snapshots. The whole module is ~200 lines.

The reasons it exists:

1. **Replay.** With both incremental diffs and periodic snapshots, you
   can reconstruct the book state at any historical timestamp by
   starting from the most recent snapshot before that timestamp and
   applying diffs forward.
2. **Training data.** Walking each historical book and simulating a
   hypothetical trade produces (features, slippage) pairs — the
   training data for the future quantile model.
3. **Post-trade analysis.** A trader reports a real fill at time $T$;
   we look up the historical book at time $T$, walk it for what was
   *achievable* at that moment, and compute the gap between actual and
   optimal. This is the `POST /v1/analyse` endpoint (not yet built;
   Section 17).

The recorder doesn't do any of the *analysis* — it just preserves the
data so analytical code (in `research/` or in future endpoints) can
read it.

## 11. `recorder/parquet_recorder.py`

### 11.1 Why Parquet

Parquet is a columnar binary storage format. Three properties that
matter for our use case:

- **Compact.** Numeric columns get specialised encodings (delta, RLE,
  dictionary). A million BTCUSDT diff events fit in ~10 MB; the same
  data as JSON Lines is ~250 MB.
- **Column-oriented reads.** Analytical queries that only need
  `(timestamp, side, price)` can read just those three columns from
  disk without touching `quantity` or `venue`. For row-oriented
  formats you'd have to read everything.
- **Universally readable.** Pandas, polars, pyarrow, R, Spark, DuckDB,
  Athena, BigQuery all read Parquet natively. Research code in
  `research/` can read the recordings directly with `pandas.read_parquet(path)`.

The cost: writing Parquet is heavier than writing JSON Lines or CSV.
We mitigate by buffering — accumulate updates in memory, flush in
batches as row groups.

### 11.2 The full file (excerpt — full code in source)

```python
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from vortexec.core.types import BookUpdate
from vortexec.maintainer.book_maintainer import BookMaintainer

log = logging.getLogger("vortexec.recorder")

DEFAULT_FLUSH_INTERVAL_SECONDS = 60.0
DEFAULT_FLUSH_AFTER_UPDATES = 5000
DEFAULT_SNAPSHOT_INTERVAL_SECONDS = 600.0

SCHEMA: pa.Schema = pa.schema([
    pa.field("venue", pa.string()),
    pa.field("symbol", pa.string()),
    pa.field("timestamp_ms", pa.int64()),
    pa.field("side", pa.string()),
    pa.field("price", pa.float64()),
    pa.field("quantity", pa.float64()),
])

SNAPSHOT_SCHEMA: pa.Schema = pa.schema([
    pa.field("venue", pa.string()),
    pa.field("symbol", pa.string()),
    pa.field("snapshot_ts_ms", pa.int64()),
    pa.field("side", pa.string()),
    pa.field("price", pa.float64()),
    pa.field("quantity", pa.float64()),
])

_WriterKey = tuple[str, str, str, int]


def _hour_path(base_dir: Path, venue: str, symbol: str, ts_ms: int) -> Path:
    dt = _dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=_dt.timezone.utc)
    return base_dir / venue / symbol / dt.strftime("%Y-%m-%d") / f"{dt.hour:02d}.parquet"


def _writer_key_for(update: BookUpdate) -> _WriterKey:
    ts_ms = int(update.diff.timestamp.timestamp() * 1000)
    dt = _dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=_dt.timezone.utc)
    return (update.venue, update.symbol, dt.strftime("%Y-%m-%d"), dt.hour)


def _to_table(updates: list[BookUpdate]) -> pa.Table:
    return pa.table({
        "venue": [u.venue for u in updates],
        "symbol": [u.symbol for u in updates],
        "timestamp_ms": [int(u.diff.timestamp.timestamp() * 1000) for u in updates],
        "side": [u.diff.side.value for u in updates],
        "price": [u.diff.price for u in updates],
        "quantity": [u.diff.quantity for u in updates],
    }, schema=SCHEMA)


class ParquetRecorder:
    def __init__(
        self,
        base_dir: Path,
        flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        flush_after_updates: int = DEFAULT_FLUSH_AFTER_UPDATES,
        snapshot_interval_seconds: float = DEFAULT_SNAPSHOT_INTERVAL_SECONDS,
    ) -> None:
        self._base_dir = base_dir
        self._flush_interval = flush_interval_seconds
        self._flush_after = flush_after_updates
        self._snapshot_interval = snapshot_interval_seconds
        self._buffer: list[BookUpdate] = []
        self._writers: dict[_WriterKey, pq.ParquetWriter] = {}
        self._task: asyncio.Task[None] | None = None
        self._snapshot_task: asyncio.Task[None] | None = None
        self._maintainer: BookMaintainer | None = None
        self._last_flush_at: float = time.monotonic()
        self._total_recorded = 0
        self._total_flushed = 0
        self._total_snapshots = 0
    # ...
```

(The full file is in `src/vortexec/recorder/parquet_recorder.py`; I've
shown the schemas and key helpers above.)

### 11.3 The two schemas

**Diff schema.** One row per applied `Diff`:

| Column | Type | Meaning |
|---|---|---|
| `venue` | string | "binance" |
| `symbol` | string | "BTCUSDT" |
| `timestamp_ms` | int64 | Unix epoch ms |
| `side` | string | "buy" or "sell" |
| `price` | float64 | level price |
| `quantity` | float64 | new quantity at that level (0 means deleted) |

**Snapshot schema.** One row per level in a snapshot:

| Column | Type | Meaning |
|---|---|---|
| `venue` | string | "binance" |
| `symbol` | string | "BTCUSDT" |
| `snapshot_ts_ms` | int64 | when the snapshot was captured (same for all rows in one snapshot) |
| `side` | string | "buy" for bids, "sell" for asks |
| `price` | float64 | level price |
| `quantity` | float64 | level quantity |

Both schemas are intentionally similar — same column names and types
where they overlap (`venue`, `symbol`, `side`, `price`, `quantity`).
The only structural difference is `timestamp_ms` (per-event in the diff
file) vs `snapshot_ts_ms` (shared across all rows of one snapshot file).
This similarity makes downstream code that consumes both feel uniform.

### 11.4 The file layout

```
{base_dir}/
  binance/
    BTCUSDT/
      2026-05-14/
        09.parquet           ← diffs from 09:00-10:00 UTC
        10.parquet           ← diffs from 10:00-11:00 UTC
        ...
        snapshots/
          09-30-12.parquet   ← snapshot at 09:30:12
          09-40-12.parquet   ← snapshot at 09:40:12
          ...
      2026-05-15/
        ...
    ETHUSDT/
      ...
  okx/
    ...
```

The directory structure encodes `(venue, symbol, date, hour)` as path
components. Diffs for one hour land in one Parquet file. Snapshots get
their own subdirectory with per-snapshot files named by HH-MM-SS.

This layout has several useful properties:

- **Easy filtering.** "Read all of BTCUSDT for 2026-05-14" is a glob:
  `base_dir/binance/BTCUSDT/2026-05-14/*.parquet`.
- **Append-friendly.** New data lands in new files; no rewrites of
  existing files.
- **Partitioned by Hive convention.** Tools like Spark, Athena, DuckDB
  can use the path components as partition columns automatically.

### 11.5 The `_hour_path` helper

```python
def _hour_path(base_dir: Path, venue: str, symbol: str, ts_ms: int) -> Path:
    dt = _dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=_dt.timezone.utc)
    return base_dir / venue / symbol / dt.strftime("%Y-%m-%d") / f"{dt.hour:02d}.parquet"
```

Given a timestamp, compute which Parquet file a diff at that time
should go into. Convert ms epoch to UTC datetime, format date and
hour, join with the base path.

UTC always. We never use local time for anything — wall-clock
timestamps from exchanges are in UTC (epoch ms), and partitioning by
UTC means all servers across the world produce the same partition
boundaries.

### 11.6 The `_writer_key_for` helper

```python
def _writer_key_for(update: BookUpdate) -> _WriterKey:
    ts_ms = int(update.diff.timestamp.timestamp() * 1000)
    dt = _dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=_dt.timezone.utc)
    return (update.venue, update.symbol, dt.strftime("%Y-%m-%d"), dt.hour)
```

A `_WriterKey` is a tuple uniquely identifying which Parquet file an
update belongs to: `(venue, symbol, date, hour)`. We keep a dict of
open writers keyed by this tuple, so concurrent multi-symbol recording
keeps multiple files open and routes each update to the right one.

The tuple is hashable (strings + int), so it works as a dict key.

### 11.7 The `_to_table` helper

```python
def _to_table(updates: list[BookUpdate]) -> pa.Table:
    return pa.table({
        "venue": [u.venue for u in updates],
        "symbol": [u.symbol for u in updates],
        "timestamp_ms": [int(u.diff.timestamp.timestamp() * 1000) for u in updates],
        "side": [u.diff.side.value for u in updates],
        "price": [u.diff.price for u in updates],
        "quantity": [u.diff.quantity for u in updates],
    }, schema=SCHEMA)
```

Convert a list of `BookUpdate` objects into a pyarrow `Table` ready
to be written as a Parquet row group. Each list comprehension extracts
one column.

`u.diff.side.value` is the *string* value of the `Side` enum
(`"buy"` or `"sell"`). We serialise as the string rather than the
integer enum index so the Parquet files are self-describing — anyone
opening one in DuckDB or pandas sees readable values.

The conversion from `datetime` to ms epoch is `dt.timestamp() * 1000`.
The `datetime.timestamp()` method returns seconds (float); we multiply
by 1000 and round to int.

Passing `schema=SCHEMA` enforces the type schema — pyarrow will raise
a type error if any column's data doesn't match. This is a useful
safety net against accidentally writing a wrong type.

### 11.8 Buffering and flush triggers

The recorder buffers updates in `_buffer` and flushes (writes a Parquet
row group) when either:

1. **Buffer size threshold reached.** Default 5,000 updates.
2. **Time interval elapsed.** Default 60 seconds since last flush.

```python
def record(self, update: BookUpdate) -> None:
    self._buffer.append(update)
    self._total_recorded += 1
    if len(self._buffer) >= self._flush_after:
        self._flush()
```

`record()` is the sync entry point. Used by the async `_consume` task
which iterates `maintainer.stream_updates()` and calls `record(update)`
for each one.

```python
async def _consume(self, maintainer: BookMaintainer) -> None:
    log.info("recorder consuming from maintainer, writing into %s", self._base_dir)
    async for update in maintainer.stream_updates():
        self.record(update)
        if (time.monotonic() - self._last_flush_at) >= self._flush_interval:
            self._flush()
```

The consumer task iterates updates, calls `record`, and additionally
checks the time-based flush trigger between updates. So a flush fires
when either threshold is hit.

The time check happens on every update rather than via a separate
periodic timer because it's cheap (a single subtraction and compare)
and avoids coordinating two tasks for flushing.

### 11.9 The `_flush` method

```python
def _flush(self) -> None:
    if not self._buffer:
        self._last_flush_at = time.monotonic()
        return

    groups: dict[_WriterKey, list[BookUpdate]] = {}
    for u in self._buffer:
        groups.setdefault(_writer_key_for(u), []).append(u)

    for key, updates in groups.items():
        writer = self._writers.get(key)
        if writer is None:
            ts_ms = int(updates[0].diff.timestamp.timestamp() * 1000)
            path = _hour_path(self._base_dir, key[0], key[1], ts_ms)
            path.parent.mkdir(parents=True, exist_ok=True)
            writer = pq.ParquetWriter(path, SCHEMA)
            self._writers[key] = writer
            log.info("recorder opened %s", path)
        writer.write_table(_to_table(updates))
        self._total_flushed += len(updates)

    self._buffer.clear()
    self._last_flush_at = time.monotonic()
```

Three phases:

**Group by destination file.** Iterate the buffer, group each update
by its `_WriterKey`. Most flushes only produce one group (all updates
in a 60-second window typically belong to the same hour), but if the
flush straddles an hour boundary, you get two groups, one for each
hour.

**Open/use writers.** For each group, look up the writer in
`_writers`. If it doesn't exist, create the parent directory and a new
`ParquetWriter`. The writer stays open across flushes within the same
hour — successive flushes write additional row groups to the same
file, building up over the hour.

**Write the row group.** `writer.write_table(_to_table(updates))`
writes one row group per flush. A Parquet file is internally a
sequence of row groups; each row group is independently compressed
and indexable. By writing each flush as one row group, we get
incremental durability — readers can open the file at any time and
read complete row groups even if more are coming.

### 11.10 Hour rollover

When the maintainer crosses an hour boundary, the new updates land in
a different `_WriterKey` (different hour). The flush opens a *new*
writer for the new hour file — but the old writer is *still open*
because nothing has explicitly closed it.

This was a design choice. We could:

1. Detect the hour rollover at flush time and explicitly close the old
   writer.
2. Leave the old writer open and rely on `stop()` to close all writers
   at shutdown.

We chose option 2 for simplicity. The cost is that until shutdown, an
hour's Parquet file doesn't have its footer written (Parquet writes
the footer on `writer.close()`). Without the footer, the file isn't
fully readable — Parquet readers depend on the footer to find row
group offsets.

So in the running system, the *most recent* hour file is always
"open" and not yet readable as Parquet. Once shutdown happens or the
process is restarted (which closes all writers), the file becomes
readable. For unattended VPS deployment this is fine — the data is
always at most one hour stale at the readable boundary.

For research code that needs the most-recent data, the rsync-to-R2
backup script (`deploy/backup.sh`) handles this by syncing only files
that are fully written (it skips the active hour's file). The
`rclone sync` command's `--exclude '*.tmp'` doesn't catch open Parquet
files, but the next sync after shutdown picks them up.

A future improvement would be to detect hour rollover and explicitly
close the previous hour's writer at the rollover moment. This would
make every closed-hour file readable instantly. We haven't done this
yet because it's not needed for the current workflow.

### 11.11 Snapshots

Every `snapshot_interval_seconds` (default 600 = 10 minutes), a
background task writes a full-book snapshot to a Parquet file.

```python
async def _snapshot_loop(self) -> None:
    while True:
        await asyncio.sleep(self._snapshot_interval)
        try:
            self._write_snapshot()
        except Exception:
            log.exception("snapshot write failed; continuing")
```

```python
def _write_snapshot(self) -> None:
    if self._maintainer is None:
        return
    book = self._maintainer.get_book()
    venue = self._maintainer.venue
    symbol = self._maintainer.symbol

    now = _dt.datetime.now(_dt.timezone.utc)
    ts_ms = int(now.timestamp() * 1000)
    prices_bids = list(book._bids)
    prices_asks = list(book._asks)
    if not prices_bids and not prices_asks:
        return

    n = len(prices_bids) + len(prices_asks)
    venues = [venue] * n
    symbols = [symbol] * n
    timestamps = [ts_ms] * n
    sides = ["buy"] * len(prices_bids) + ["sell"] * len(prices_asks)
    prices = [float(p) for p in prices_bids] + [float(p) for p in prices_asks]
    quantities = [float(book._bids[p]) for p in prices_bids] + [
        float(book._asks[p]) for p in prices_asks
    ]

    table = pa.table({
        "venue": venues, "symbol": symbols, "snapshot_ts_ms": timestamps,
        "side": sides, "price": prices, "quantity": quantities,
    }, schema=SNAPSHOT_SCHEMA)

    path = (
        self._base_dir / venue / symbol / now.strftime("%Y-%m-%d")
        / "snapshots" / f"{now.strftime('%H-%M-%S')}.parquet"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    self._total_snapshots += 1
    log.info("recorder wrote snapshot: %s (%d levels)", path, n)
```

The snapshot task is independent of the diff stream. Every 10 minutes
it wakes up, asks the maintainer for the current book, walks both
sides, builds a `pa.Table` with one row per level, and writes a fresh
Parquet file. Unlike the diff files (which are appended-to across an
hour), each snapshot is its own file written with `pq.write_table`
(one-shot write that includes the footer).

The filename encodes the time of capture to second precision:
`HH-MM-SS.parquet`. Replay code can locate the most recent snapshot
before any target timestamp by listing the `snapshots/` directory and
filtering by filename.

The `if not prices_bids and not prices_asks` guard skips empty books
(don't write zero-level snapshots).

The synchronous `book._bids` access is safe for the same reason
discussed elsewhere — single-threaded asyncio means no concurrent
mutation during the sync block.

`pq.write_table` writes a complete Parquet file with a footer in one
call. No streaming, no row groups, no open-writer state. Snapshots are
small enough (a few hundred KB for 10,000 levels) that batch writing is
fine.

### 11.12 Shutdown

```python
async def stop(self) -> None:
    for task in (self._task, self._snapshot_task):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    self._task = None
    self._snapshot_task = None
    self._flush()
    for writer in self._writers.values():
        writer.close()
    self._writers.clear()
    log.info(
        "recorder stopped: %d updates recorded, %d flushed to disk, "
        "%d snapshots written",
        self._total_recorded, self._total_flushed, self._total_snapshots,
    )
```

Cancel both background tasks, await them, then do a final flush
(captures anything in the buffer that hadn't been flushed yet), then
close all open writers. Closing each writer is what writes the Parquet
file's footer — the file becomes fully readable.

The shutdown is what makes the deferred-footer design (Section 11.10)
work: as long as `stop()` is called cleanly, every file ends up
readable. The systemd `KillSignal=SIGINT` + `TimeoutStopSec=30`
ensures the service has time to do this on shutdown.

### 11.13 Tests

11 tests in `tests/unit/test_parquet_recorder.py`:

- `_hour_path` produces the right layout.
- `record` doesn't flush below threshold.
- `record` flushes at threshold.
- `stop` flushes remaining buffer and closes files.
- Diffs routed to correct hour files when straddling an hour boundary.
- Multi-venue, multi-symbol routing to separate files.
- Full integration: maintainer + recorder produces readable Parquet.
- `stop` with empty buffer is safe.
- Snapshot writes full book state.
- Snapshot skipped when book is empty.
- Snapshot disabled when `snapshot_interval=0`.

Tests use `tmp_path` for isolated filesystems and `pyarrow.parquet.read_table`
to verify content. The full-integration test confirms the
maintainer-to-recorder pipeline works end-to-end with a `FakeVenueConnector`
feeding synthetic data.

---

# Part VI — The API (`src/vortexec/api/`)

The API layer exposes the maintained book to external clients over HTTP.
Two endpoints today: `GET /health` and `POST /v1/estimate`. Both are
small — most of the work was done in `core/` (the simulator and feature
extractor); the API layer is mostly translation between HTTP and the
underlying Python types.

The package has five files: `models.py`, `deps.py`, `server.py`, and
two route files. Total ~150 lines of Python. We'll walk through each.

## 12. `api/models.py`, `api/deps.py`, `api/server.py`

### 12.1 `api/models.py` — Pydantic schemas

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ─── /health ────────────────────────────────────────────────────────────

class MaintainerHealth(BaseModel):
    venue: str
    symbol: str
    healthy: bool
    resync_count: int
    drop_count: int


class HealthResponse(BaseModel):
    status: Literal["healthy", "unhealthy"]
    maintainers: list[MaintainerHealth]


# ─── /v1/estimate ───────────────────────────────────────────────────────

class EstimateRequest(BaseModel):
    venue: Literal["binance"] = "binance"
    symbol: str = Field(min_length=1)
    side: Literal["buy", "sell"]
    size: float = Field(gt=0)


class SimResultModel(BaseModel):
    avg_price: float | None
    slippage_bps: float | None
    unfilled_qty: float
    levels_consumed: int


class FeaturesModel(BaseModel):
    mid_price: float | None
    spread_bps: float | None
    depth_top_5_bids: float
    depth_top_5_asks: float
    depth_top_10_bids: float
    depth_top_10_asks: float
    imbalance: float | None


class EstimateResponse(BaseModel):
    venue: str
    symbol: str
    side: Literal["buy", "sell"]
    size: float
    deterministic: SimResultModel
    features: FeaturesModel
```

These are Pydantic v2 models. FastAPI uses them for both request
validation and response serialisation.

**Why duplicate the core types?** A natural question is: we already
have `SimResult` and `Features` dataclasses in `core/`; why define
`SimResultModel` and `FeaturesModel` again here?

The answer is *boundary clarity*. The core dataclasses are the
internal Python representation. The Pydantic models are the *HTTP
contract*. They happen to have the same fields today, but they don't
have to — when we add ML predictions to the response (Phase 5a), the
`EstimateResponse` will have a `ml_predictions` field that has no
counterpart in `core/`. Keeping the boundary types separate means the
internal representation can evolve without affecting the wire contract,
and vice versa.

This pattern — "internal type" vs "DTO" (data transfer object) — is
controversial in some circles but valuable when the boundary matters.
For us, the boundary is the long-term API contract that customers
depend on, so we keep it separate.

**`Literal` types.** `Literal["binance"]` constrains the `venue` field
to exactly the string `"binance"`. Anything else triggers a 422
validation error before our code runs. As more venues come online,
this becomes `Literal["binance", "okx", "bybit"]`.

**`Field(min_length=1)` and `Field(gt=0)`.** Pydantic constraints
that produce 422 errors for invalid inputs. `size=0` or empty `symbol`
fail at the framework layer, before reaching our route handler.

**The `| None` types.** `avg_price: float | None`, `slippage_bps: float | None`,
etc. — Pydantic preserves these and serialises `None` as JSON `null`.
This is the wire equivalent of the "undefined is `None`" convention
discussed in earlier sections.

### 12.2 `api/deps.py` — FastAPI dependency injection

```python
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from vortexec.maintainer.book_maintainer import BookMaintainer

MaintainerKey = tuple[str, str]
MaintainersMap = dict[MaintainerKey, BookMaintainer]


def get_maintainers(request: Request) -> MaintainersMap:
    maintainers: MaintainersMap = request.app.state.maintainers
    return maintainers


MaintainersDep = Annotated[MaintainersMap, Depends(get_maintainers)]
```

This is the wire that lets route handlers get a reference to the
maintainers dict.

The maintainers dict is keyed by `(venue, symbol)` — a tuple of two
strings — and maps to the corresponding `BookMaintainer`. It's stored
on `app.state.maintainers` by `create_app` (Section 12.3). The
`get_maintainers` dependency reads it back out of the request's app
state.

`MaintainersDep` is a type alias that combines the type
`MaintainersMap` with FastAPI's `Depends(get_maintainers)` annotation.
Routes that declare `maintainers: MaintainersDep` get the dict
injected by FastAPI at call time.

This is the modern Pydantic-v2 / FastAPI dependency pattern (using
`Annotated[..., Depends(...)]`). Older FastAPI code used
`maintainers: MaintainersMap = Depends(get_maintainers)` directly in
the function signature; the `Annotated` form is preferred because it's
more composable (the type and the dependency can be reused independently).

### 12.3 `api/server.py` — the FastAPI app factory

```python
from __future__ import annotations

from fastapi import FastAPI

from vortexec.api.deps import MaintainersMap
from vortexec.api.routes import health, pretrade


def create_app(maintainers: MaintainersMap) -> FastAPI:
    app = FastAPI(
        title="VortExec",
        version="0.1.0",
        description="Live execution analytics for crypto algo traders.",
    )
    app.state.maintainers = maintainers
    app.include_router(health.router)
    app.include_router(pretrade.router)
    return app
```

A factory rather than a module-level `app = FastAPI(...)`. The
factory takes the maintainers dict as input and stores it on
`app.state`, which is where `get_maintainers` reads from.

The factory pattern is what enables clean testing — each test creates
its own app with its own (mock) maintainers dict, no global state.
For production, `service.py` calls `create_app` once with the real
maintainers.

The maintainers are *injected*, not *owned*, by the app. The app
doesn't manage their lifecycle — it just borrows references. The
actual lifecycle (start, stop) is owned by `service.py` (Section 14).
This separation keeps the FastAPI lifespan hooks simple and avoids
the awkward dance of starting/stopping maintainers as part of the
HTTP server's lifecycle.

## 13. `api/routes/health.py` and `api/routes/pretrade.py`

### 13.1 Health route

```python
from __future__ import annotations

from fastapi import APIRouter, Response, status

from vortexec.api.deps import MaintainersDep
from vortexec.api.models import HealthResponse, MaintainerHealth

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health(maintainers: MaintainersDep, response: Response) -> HealthResponse:
    statuses = [
        MaintainerHealth(
            venue=venue, symbol=symbol,
            healthy=m.is_healthy(),
            resync_count=m.resync_count,
            drop_count=m.drop_count,
        )
        for (venue, symbol), m in maintainers.items()
    ]
    overall = bool(statuses) and all(s.healthy for s in statuses)
    if not overall:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="healthy" if overall else "unhealthy",
        maintainers=statuses,
    )
```

`GET /health` returns 200 with `{"status": "healthy", ...}` if every
maintainer reports healthy, or 503 with `{"status": "unhealthy", ...}`
if any is unhealthy.

The 503 vs 200 distinction is what external monitoring keys off. A
load balancer or uptime checker that sees 503 considers the service
down; 200 considers it up.

The handler iterates `maintainers.items()` to build the per-maintainer
status list. For each maintainer, it reads three fields: `is_healthy()`,
`resync_count`, `drop_count`. These are the load-bearing signals.

`bool(statuses)` is True when there's at least one maintainer; an
empty list short-circuits to overall unhealthy (no maintainers = no
service).

`response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE` mutates
the response's status code in-place. FastAPI's pattern for "default
status but conditionally change it" is to take a `Response` object as
a dependency and mutate its status_code field — preferable to raising
an HTTPException because we want to return a real body, not just an
error.

### 13.2 Pretrade route — `POST /v1/estimate`

```python
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from vortexec.api.deps import MaintainersDep
from vortexec.api.models import (
    EstimateRequest, EstimateResponse, FeaturesModel, SimResultModel,
)
from vortexec.core.features import extract_features
from vortexec.core.simulator import simulate_market_order
from vortexec.core.types import Side

router = APIRouter()


@router.post("/v1/estimate", response_model=EstimateResponse)
async def estimate(
    req: EstimateRequest, maintainers: MaintainersDep
) -> EstimateResponse:
    key = (req.venue, req.symbol)
    maintainer = maintainers.get(key)
    if maintainer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no maintainer for {req.venue}/{req.symbol}",
        )
    if not maintainer.is_healthy():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"maintainer for {req.venue}/{req.symbol} is unhealthy",
        )

    book = maintainer.get_book()
    side = Side.BUY if req.side == "buy" else Side.SELL
    sim = simulate_market_order(book, side, req.size)
    feats = extract_features(book)

    return EstimateResponse(
        venue=req.venue, symbol=req.symbol, side=req.side, size=req.size,
        deterministic=SimResultModel(
            avg_price=sim.avg_price, slippage_bps=sim.slippage_bps,
            unfilled_qty=sim.unfilled_qty, levels_consumed=sim.levels_consumed,
        ),
        features=FeaturesModel(
            mid_price=feats.mid_price, spread_bps=feats.spread_bps,
            depth_top_5_bids=feats.depth_top_5_bids,
            depth_top_5_asks=feats.depth_top_5_asks,
            depth_top_10_bids=feats.depth_top_10_bids,
            depth_top_10_asks=feats.depth_top_10_asks,
            imbalance=feats.imbalance,
        ),
    )
```

Five things happen.

**Validation.** Pydantic has already validated `req` by the time we
get here — `side` is `"buy"` or `"sell"`, `size > 0`, `symbol` is
non-empty. Type errors are returned as 422 automatically by the
framework.

**Lookup.** `maintainers.get((req.venue, req.symbol))`. Returns
`None` if the maintainer doesn't exist; in that case raise a 404 with
a descriptive message. The 404 is correct because the requested
resource (this specific venue/symbol's maintainer) doesn't exist on
this service; not "the request was malformed" (422) and not "the
service is broken" (503).

**Health check.** If the maintainer is unhealthy (stale, hasn't seen
an update recently), refuse to serve the request — return 503. This
is the load-bearing safety net: a slippage estimate from a stale book
is worse than no estimate, because the caller takes it at face value.
Better to fail loudly than to serve wrong data.

**Computation.** Get the book, convert the side string to a `Side`
enum, run the simulator, run the feature extractor. Both are
synchronous and complete in ~1 ms total. This is the meat of the
endpoint.

**Response construction.** Pack the simulator and features results
into the response Pydantic models, return. FastAPI serialises to JSON
automatically.

The whole handler is ~25 lines. Most of the work is in `core/`; this
is a thin HTTP wrapper.

### 13.3 Tests

`tests/unit/test_api_health.py` (4 tests) and
`tests/unit/test_api_pretrade.py` (6 tests).

The tests use FastAPI's `TestClient`, which is a synchronous wrapper
around the app that doesn't actually start a server. It calls the
ASGI handler directly. The maintainers are constructed manually
(`BookMaintainer(connector=FakeVenueConnector(...), ...)`), the
`_last_update_at` is poked to make `is_healthy()` return True, and
the maintainers dict is passed to `create_app`.

Tests verify:

Health:
- 200 with all-healthy maintainers, status field is `"healthy"`.
- 503 with any-unhealthy maintainer, status field is `"unhealthy"`.
- 503 with no maintainers registered (degenerate but possible state).
- Resync and drop counters surface in the response.

Pretrade:
- Returns simulator + features correctly for a valid request.
- Multi-level walk (size exceeds top-of-book) produces correct avg price.
- 404 for unknown venue/symbol.
- 503 for unhealthy maintainer.
- 422 for invalid size (Pydantic validation).
- 422 for invalid side (Pydantic Literal validation).

These tests cover the HTTP-shape behaviours; the underlying simulator
and feature math is tested in `core/` tests.

---

# Part VII — Orchestration (`src/vortexec/service.py`)

`service.py` is the top-level wiring. It instantiates connectors,
maintainers, and recorders; it creates the FastAPI app and wires it to
the maintainers; it runs uvicorn in the same event loop as the
maintainers; it handles SIGINT/SIGTERM for clean shutdown; it logs
periodic stats; it optionally pings Healthchecks.io for external
monitoring.

It is ~180 lines of *coordination*, not logic. The coordination is
fiddly but conceptually simple.

## 14. `service.py` and `__main__.py`

### 14.1 `__main__.py`

```python
from vortexec.service import main

if __name__ == "__main__":
    main()
```

Three lines. Exists so that `python -m vortexec` invokes
`service.main()`. This is the Python package convention for "module
as a script". Setting up a `[project.scripts]` entry in
`pyproject.toml` provides the `vortexec` command-line alias too.

### 14.2 `service.py` — overview

The module exposes two functions:

- **`main()`** (sync) — parses CLI args, calls `asyncio.run(run(...))`.
  Plus signal-related setup.
- **`run(symbols, venue, verify_ssl, record_to, snapshot_interval, api_host, api_port, healthchecks_url)`** (async) — the actual orchestration coroutine.

Plus helpers:

- `_log_stats(trios, stop)` — periodic per-symbol stats logger.
- `_healthchecks_ping_loop(url, trios, stop)` — periodic external uptime ping.
- `_on_signal(stop, sig_name)` — signal handler that sets the stop event.

And one dataclass:

- `_Trio` — bundles together one symbol's connector, maintainer, recorder.

### 14.3 The `_Trio` bundle

```python
@dataclass
class _Trio:
    connector: BinanceConnector
    maintainer: BookMaintainer
    recorder: ParquetRecorder | None
```

For each symbol, we manage three objects: connector, maintainer,
recorder. They're closely related and almost always handled together
in `run()`. The dataclass exists purely so we can pass them around as
a unit and iterate the list with self-documenting field names. Not a
public type — single-underscore internal.

`recorder` is `Optional` because the service can run without recording
(`--record-to` flag not set means no recorder).

### 14.4 The `run` function — start-up

```python
async def run(
    symbols: list[str],
    venue: str,
    verify_ssl: bool,
    record_to: Path | None,
    snapshot_interval_seconds: float,
    api_host: str,
    api_port: int,
    healthchecks_url: str | None,
) -> None:
    trios: list[_Trio] = []
    for symbol in symbols:
        connector = BinanceConnector(verify_ssl=verify_ssl)
        maintainer = BookMaintainer(connector, venue, symbol)
        recorder = (
            ParquetRecorder(record_to, snapshot_interval_seconds=snapshot_interval_seconds)
            if record_to is not None
            else None
        )
        trios.append(_Trio(connector, maintainer, recorder))
```

For each requested symbol, build a connector, a maintainer that wraps
it, and a recorder (if recording was requested). Collect into the
list of trios.

The recorder constructor takes the *directory* (not a per-symbol
path); it computes the per-symbol path internally from each
`BookUpdate`'s venue and symbol fields. So one `ParquetRecorder`
instance per maintainer; each one writes to its symbol's subtree.

```python
    log.info(
        "starting %d symbol(s) on venue=%s: %s (verify_ssl=%s, record_to=%s, "
        "snapshot_every=%.0fs, api=%s:%d)",
        len(symbols), venue, ",".join(symbols), verify_ssl, record_to,
        snapshot_interval_seconds, api_host, api_port,
    )
```

One info log line summarising the configuration. Helps when reading
journalctl after-the-fact.

```python
    for trio in trios:
        if trio.recorder is not None:
            await trio.recorder.start(trio.maintainer)
    for trio in trios:
        await trio.maintainer.start()
```

Two loops, not one. Recorders are started *first*; maintainers are
started *second*.

Why? The recorder subscribes to the maintainer's `stream_updates`
channel. If we started the maintainer first, it might publish a few
diffs before the recorder subscribed, and those would be lost. Starting
the recorder first ensures it's already subscribed when the maintainer
begins producing.

In practice the race is tiny (the maintainer doesn't start producing
until it has fetched a snapshot, which takes ~100 ms), but the
explicit ordering closes the race rigorously.

### 14.5 FastAPI + uvicorn integration

```python
    maintainers_map: MaintainersMap = {
        (t.maintainer.venue, t.maintainer.symbol): t.maintainer for t in trios
    }
    app = create_app(maintainers_map)
    config = uvicorn.Config(
        app, host=api_host, port=api_port, log_level="info", access_log=False
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
```

Build the maintainers dict from the trios list (keyed by `(venue,
symbol)` tuples). Pass it to `create_app` which gives back a FastAPI
instance with the maintainers stored on `app.state`.

Wrap the FastAPI app in a `uvicorn.Server` configured to listen on
the requested host/port. The `access_log=False` disables uvicorn's
default per-request logging — a few hundred requests per second would
otherwise spam the journal. Application-level logs (the maintainer's
stats, etc.) are still on.

`server.serve()` is the coroutine that runs uvicorn — accepting
connections, dispatching requests. We schedule it as a separate task
so it runs concurrently with the maintainers. All in the same event
loop, no threading.

### 14.6 Signal handling

```python
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, partial(_on_signal, stop, sig.name))
```

```python
def _on_signal(stop: asyncio.Event, sig_name: str) -> None:
    log.info("received %s, shutting down", sig_name)
    stop.set()
```

Both SIGINT (Ctrl+C, or `kill -INT`) and SIGTERM (the default `kill`
or systemd's stop signal) are routed to `_on_signal`, which logs and
sets the `stop` event.

`asyncio.get_running_loop().add_signal_handler` is the asyncio-native
way to install signal handlers — they run in the event loop rather
than interrupting whatever is currently executing. This is safer than
the stdlib `signal.signal` for asyncio code because asyncio coroutines
can't be safely interrupted mid-execution.

The `partial(_on_signal, stop, sig.name)` binds the stop event and
signal name into the handler so it has what it needs to log and
trigger shutdown.

### 14.7 Background tasks: stats and healthchecks

```python
    stats_task = asyncio.create_task(_log_stats(trios, stop))
    hc_task: asyncio.Task[None] | None = None
    if healthchecks_url:
        hc_task = asyncio.create_task(
            _healthchecks_ping_loop(healthchecks_url, trios, stop)
        )
        log.info("healthchecks ping enabled (every %.0fs)", HEALTHCHECKS_PING_INTERVAL_SECONDS)
```

Two optional periodic tasks.

**`_log_stats`** logs a per-symbol stats line every 10 seconds:

```python
async def _log_stats(trios: list[_Trio], stop: asyncio.Event) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=STATS_INTERVAL_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        for trio in trios:
            m = trio.maintainer
            book = m.get_book()
            log.info(
                "%-8s bid=%s ask=%s spread=%s mid=%s  healthy=%s  resync=%d  drop=%d",
                m.symbol, _fmt(book.best_bid()), _fmt(book.best_ask()),
                _fmt(book.spread(), 4), _fmt(book.mid()),
                m.is_healthy(), m.resync_count, m.drop_count,
            )
```

The loop pattern is `asyncio.wait_for(stop.wait(), timeout=N)` — wait
up to N seconds for the stop event, or fire on timeout. If stop fires
before the timeout, return. If timeout fires, log a stats line and
loop. This is the idiomatic asyncio way to do "every N seconds, do X,
until stop".

**`_healthchecks_ping_loop`** is similar but pings a URL instead of
logging:

```python
async def _healthchecks_ping_loop(
    url: str, trios: list[_Trio], stop: asyncio.Event
) -> None:
    timeout = aiohttp.ClientTimeout(total=HEALTHCHECKS_PING_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=HEALTHCHECKS_PING_INTERVAL_SECONDS
                )
                return
            except asyncio.TimeoutError:
                pass
            healthy = all(t.maintainer.is_healthy() for t in trios) if trios else False
            if not healthy:
                log.warning("skipping healthchecks ping: one or more maintainers unhealthy")
                continue
            try:
                async with session.get(url) as resp:
                    if resp.status >= 400:
                        log.warning("healthchecks ping returned HTTP %d", resp.status)
            except Exception as e:
                log.warning("healthchecks ping failed: %r", e)
```

Every 60 seconds, if all maintainers are healthy, GET the Healthchecks.io
URL. If any maintainer is unhealthy, *deliberately skip the ping* so
the external alert fires.

This is the key design choice for the healthchecks integration. It would
be tempting to ping unconditionally, but that would mean the external
"is the service alive" check stays green even when the service is
serving wrong data. By skipping the ping when internal state is bad,
the external alert correctly fires for "this service is broken"
scenarios, not just "this service crashed" scenarios.

The aiohttp session is created with a 10-second timeout so a hanging
Healthchecks.io endpoint can't block this task forever.

### 14.8 The main wait

```python
    try:
        await stop.wait()
    finally:
        log.info("stopping %d symbol(s) and HTTP server", len(trios))
        for t in (stats_task, hc_task):
            if t is None:
                continue
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=10.0)
        except asyncio.TimeoutError:
            log.warning("uvicorn did not exit cleanly within 10s, cancelling")
            server_task.cancel()
        coros = []
        for trio in trios:
            coros.append(trio.maintainer.stop())
            if trio.recorder is not None:
                coros.append(trio.recorder.stop())
        await asyncio.gather(*coros, return_exceptions=True)
        log.info("clean shutdown complete")
```

The main coroutine awaits the stop event — i.e. it sleeps until a
signal arrives. When SIGINT/SIGTERM fires, the signal handler sets
`stop`, the `await stop.wait()` returns, and we enter the `finally`
block.

Shutdown sequence:

1. **Cancel the stats and healthchecks tasks.** They're just loggers;
   tearing them down first means subsequent shutdown log lines aren't
   interleaved with stats lines.

2. **Stop uvicorn.** `server.should_exit = True` is uvicorn's
   cooperative-shutdown flag. The server completes any in-flight
   requests and exits. We `await server_task` with a 10-second timeout
   to give it time to finish; if it doesn't, we cancel.

3. **Stop maintainers and recorders in parallel.** Each `maintainer.stop()`
   takes ~7 seconds (cancelling the run task, signalling subscribers,
   closing the WS task, closing the aiohttp session). If we did them
   serially for 3 maintainers, shutdown would take 21 seconds. With
   `asyncio.gather`, they run concurrently — total shutdown is ~7
   seconds regardless of how many maintainers.

   `return_exceptions=True` ensures one maintainer's exception doesn't
   cancel the others. We log a clean-shutdown message either way.

4. **`log.info("clean shutdown complete")`** — final confirmation.
   systemd uses this as the signal that the service has finished its
   cleanup; the `TimeoutStopSec=30` ensures systemd waits.

### 14.9 `main()` — CLI and entry point

```python
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT"], help="...")
    p.add_argument("--venue", default="binance", choices=["binance"])
    p.add_argument("--no-verify-ssl", dest="verify_ssl", action="store_false",
                   default=True, help="...")
    p.add_argument("--record-to", type=Path, default=None, help="...")
    p.add_argument("--snapshot-interval", type=float, default=600.0, help="...")
    p.add_argument("--api-host", default="127.0.0.1", help="...")
    p.add_argument("--api-port", type=int, default=8000, help="...")
    p.add_argument("--healthchecks-url", default=None, help="...")
    args = p.parse_args()
    try:
        asyncio.run(run(args.symbols, args.venue, args.verify_ssl,
                        args.record_to, args.snapshot_interval,
                        args.api_host, args.api_port, args.healthchecks_url))
    except KeyboardInterrupt:
        pass
```

Set up logging at INFO level with a standardised format. Parse CLI
args. Call `asyncio.run(run(...))`.

The `except KeyboardInterrupt: pass` is a safety net. The signal
handler should normally translate SIGINT into a clean stop via the
stop event. But if something goes wrong (e.g. SIGINT during startup
before the signal handler is installed), `asyncio.run` propagates
`KeyboardInterrupt` out. Catching it lets the process exit with code 0
rather than dumping a stack trace.

`args.symbols` accepts multiple values: `--symbols BTCUSDT ETHUSDT SOLUSDT`
or just `--symbols BTCUSDT`. The default is `["BTCUSDT"]`.

`--no-verify-ssl` is `action="store_false"` with `dest="verify_ssl"`
— flag presence sets `verify_ssl=False`, absence keeps the default
`True`. This is the "negate a default-true flag" pattern.

### 14.10 What service.py doesn't do

Worth being explicit about the scope.

**It doesn't load config from a file.** All configuration is via CLI
args. A future `config.py` (Phase 7) would replace this with YAML/TOML
loading. For now, the CLI surface is small enough that args are fine.

**It doesn't manage credentials.** No API keys, no secrets. The
recorder's R2 backup is configured outside the service (via rclone).

**It doesn't restart on its own failures.** That's systemd's job. The
maintainer's run loop is internally resilient (Section 10.7), but if
the *whole service* dies (e.g. memory leak, segfault), systemd handles
restart.

**It doesn't expose metrics.** The stats logger writes to journald,
but there's no Prometheus endpoint or similar. If we needed proper
metrics, we'd add a `/metrics` endpoint that exports counters; for now,
journald + Healthchecks.io is enough.

**It doesn't have a graceful-reload (SIGHUP).** Changing the config
means stopping and starting the service. Reload is a feature, not
required for our current scale.

These are all conscious deferrals — the service does exactly what it
needs to do, and adds nothing speculative.

---

# Part VIII — Operations (`deploy/`)

Everything so far has been about the Python process. This Part is about
*running* that process on a server for months without you watching it.
The artefacts in `deploy/` are the operational scaffolding: a systemd
unit that supervises the process, a bash wrapper that translates an
environment file into CLI flags, a bootstrap script that turns a fresh
Ubuntu VPS into a running VortExec node, a backup script that ships
recordings to off-site storage daily, and a sanity-check script that
verifies yesterday's recordings look sensible.

None of these are clever. They are clear, defensive, and explicit about
their assumptions. The intent is that the *whole deployment* is
reproducible from `deploy/`: if a VPS dies, you spin up a new one and
run `setup.sh`, and you're back where you were. The "irreproducible
state" of the deployment is reduced to two things: the contents of
`/etc/vortexec/env` (which holds non-secret configuration and a few
public Healthchecks URLs) and the rclone R2 credentials. Everything
else is in the repo.

## 15. The systemd unit and the run wrapper

### 15.1 `deploy/vortexec.service` — the systemd unit

```ini
[Unit]
Description=VortExec live order-book maintainer + Parquet recorder
Documentation=https://github.com/anshshetty/vortexec
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=vortexec
Group=vortexec
WorkingDirectory=/opt/vortexec
ExecStart=/opt/vortexec/deploy/run.sh

# Resilience
Restart=always
RestartSec=10
TimeoutStartSec=30
TimeoutStopSec=30
KillSignal=SIGINT
KillMode=mixed

# Memory bounds — kill if leaks (systemd then restarts).
MemoryHigh=512M
MemoryMax=1G

# Logging — let journald handle it, with file fallback.
StandardOutput=journal
StandardError=journal
SyslogIdentifier=vortexec
Environment=PYTHONUNBUFFERED=1

# Hardening — limit blast radius if something goes wrong.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/vortexec /var/log/vortexec

[Install]
WantedBy=multi-user.target
```

systemd unit files are declarative. Each section declares constraints
or properties; systemd does the work of starting, supervising, and
restarting the service. Let me walk through each block.

**`[Unit]`** — service identification and ordering.

- `Description=` and `Documentation=` are metadata visible in
  `systemctl status` output and `systemd-cgls`. Useful for any future
  operator who didn't write this.
- `After=network-online.target` — don't start until the network is up.
  Critical because the maintainer needs to reach Binance immediately
  on startup; starting before networking is configured would just fail
  and trigger a restart loop.
- `Wants=network-online.target` — pulls `network-online.target` into
  the boot sequence if it isn't already. Subtly different from
  `Requires`: `Wants` is a soft dependency (failure to bring up
  network doesn't stop us; we'll just keep failing and getting
  restarted). `Requires` would make our service fail-stop if network
  fails. `Wants` is more forgiving.

**`[Service]` — process management.**

- `Type=simple` — the simplest mode. systemd considers the service
  "started" as soon as `ExecStart` runs (no fork, no readiness
  signalling). We don't have a complex startup sequence that needs to
  signal "I'm ready"; the maintainer's readiness is checked separately
  via the Healthchecks ping.
- `User=vortexec, Group=vortexec` — run as the unprivileged
  `vortexec` system user, not as root. Limits blast radius — even if
  the Python process is exploited, it can only write to directories
  the `vortexec` user owns.
- `WorkingDirectory=/opt/vortexec` — sets cwd. Relative paths in our
  code (none currently, but defensively) would resolve against this.
- `ExecStart=/opt/vortexec/deploy/run.sh` — the command systemd
  actually runs. A wrapper script rather than the Python command
  directly, because the command involves environment-file expansion
  that systemd's `ExecStart` syntax handles awkwardly. The wrapper is
  cleaner (Section 15.2).

**Resilience.**

- `Restart=always` — restart the process whenever it exits, regardless
  of exit code. The maintainer is designed to never exit voluntarily;
  any exit means something went wrong, and we want to recover.
- `RestartSec=10` — wait 10 seconds before restarting. Prevents
  tight-loop restarts if something is fundamentally broken (e.g. bad
  config) by rate-limiting the failure-and-restart cycle.
- `TimeoutStartSec=30` — if `ExecStart` hasn't returned after 30
  seconds without exiting, consider it failed. We use `Type=simple`
  so this barely applies — but it sets a sane upper bound on startup
  hangs.
- `TimeoutStopSec=30` — give the service 30 seconds to shut down after
  we send the stop signal. Our shutdown sequence (Section 14.8) takes
  ~7 seconds in practice. 30 seconds is a comfortable buffer.
- `KillSignal=SIGINT` — when systemd wants to stop the service, send
  SIGINT instead of the default SIGTERM. Our signal handler is wired to
  SIGINT and translates it into a graceful stop (flush buffers, close
  WS, close aiohttp session, etc.). SIGTERM would also work — we
  handle both — but using SIGINT is consistent with the Ctrl+C-style
  interaction we use during development.
- `KillMode=mixed` — send the stop signal to the main process first;
  if it hasn't exited within `TimeoutStopSec`, send SIGKILL to all
  remaining processes. This handles the (unlikely) case where the
  Python process spawns child processes that don't respond to SIGINT.

**Resource limits.**

- `MemoryHigh=512M` — soft memory limit. The kernel will throttle the
  process (via memcg pressure) if it exceeds 512 MB. Doesn't kill;
  encourages.
- `MemoryMax=1G` — hard memory limit. The kernel kills the process via
  the OOM killer if it exceeds 1 GB. systemd's `Restart=always` then
  brings it back. This is the belt-and-suspenders defence against a
  memory leak — even if we have a slow leak we haven't noticed, the
  process gets killed and restarted before consuming the entire VPS's
  RAM.

  The choice of 1G is conservative — the maintainer's actual memory
  usage in steady state is around 50-100 MB per symbol (the bulk is
  the in-memory OrderBook with ~10,000 levels per side, plus pyarrow
  buffers awaiting flush). Three symbols comfortably fit in 200 MB,
  so 1G is ~5× headroom.

**Logging.**

- `StandardOutput=journal, StandardError=journal` — pipe both streams
  to systemd's journal. `journalctl -u vortexec` retrieves them.
- `SyslogIdentifier=vortexec` — the tag that appears in journal
  entries. `journalctl -t vortexec` is another way to filter.
- `Environment=PYTHONUNBUFFERED=1` — disable Python's stdout/stderr
  buffering so log lines appear in the journal immediately rather than
  batched. Critical for tailing live with `journalctl -f`.

**Hardening.**

- `NoNewPrivileges=true` — the process cannot acquire new privileges
  via setuid/setgid/capabilities. Defence-in-depth.
- `ProtectSystem=strict` — most of the filesystem is read-only to the
  process. Combined with `ReadWritePaths` below, the process can
  only write to specific allowed paths.
- `ProtectHome=true` — `/home`, `/root`, `/run/user` are inaccessible
  to the process. No reason for our service to touch these.
- `PrivateTmp=true` — the process gets its own `/tmp` namespace,
  isolated from other processes. Defends against tmp-file races and
  symlink attacks.
- `ReadWritePaths=/var/lib/vortexec /var/log/vortexec` — explicitly
  allows writes to the data and log directories. Everything else is
  read-only via `ProtectSystem=strict`.

These hardening directives are systemd's standard service-isolation
toolkit. They cost nothing — no code change required — and substantially
reduce the impact of any future compromise.

**`[Install]` — when to start.**

- `WantedBy=multi-user.target` — start when the system reaches the
  multi-user runlevel (the normal "logged in, fully booted" state).
  `systemctl enable vortexec` adds this service to that target's
  dependency set; the service auto-starts on every boot afterwards.

### 15.2 `deploy/run.sh` — the ExecStart wrapper

```bash
#!/bin/bash
# Wrapper that systemd ExecStart points at. Reads /etc/vortexec/env and
# builds the python -m vortexec invocation. Keeps the systemd unit free of
# argument-parsing concerns.
set -euo pipefail

# shellcheck disable=SC1091
source /etc/vortexec/env

cd /opt/vortexec

# Symbols expand unquoted so each becomes its own argv item.
args=(
    --symbols ${VORTEXEC_SYMBOLS}
    --record-to "${VORTEXEC_DATA_DIR}"
    --snapshot-interval "${VORTEXEC_SNAPSHOT_INTERVAL:-600}"
)

if [ -n "${VORTEXEC_HEALTHCHECKS_URL:-}" ]; then
    args+=(--healthchecks-url "${VORTEXEC_HEALTHCHECKS_URL}")
fi

exec .venv/bin/python -m vortexec "${args[@]}"
```

A small bash script that does three things: sources the config file,
builds the argument list, and execs Python.

**`set -euo pipefail`** — bash safety flags.
- `-e`: exit on any command failure.
- `-u`: treat undefined variables as errors.
- `-o pipefail`: a pipeline fails if any command in it fails (not just
  the last).

Together, these make the script fail loudly on any error rather than
silently continuing with broken state. Standard defensive bash header.

**`source /etc/vortexec/env`** — read the environment file into the
current shell. This is what populates `VORTEXEC_SYMBOLS`,
`VORTEXEC_DATA_DIR`, `VORTEXEC_HEALTHCHECKS_URL`, etc.

The `# shellcheck disable=SC1091` comment silences ShellCheck's
warning about sourcing a file it can't find at lint time (because the
file lives at `/etc/vortexec/env`, outside the repo).

**`cd /opt/vortexec`** — change to the install directory so relative
paths resolve correctly (the venv is at `.venv/`).

**Argument assembly.** The bash array syntax:

```bash
args=(
    --symbols ${VORTEXEC_SYMBOLS}
    --record-to "${VORTEXEC_DATA_DIR}"
    --snapshot-interval "${VORTEXEC_SNAPSHOT_INTERVAL:-600}"
)
```

builds the argument list piece by piece. Note the *un*quoted
`${VORTEXEC_SYMBOLS}` — bash word-splits the value `"BTCUSDT ETHUSDT
SOLUSDT"` into three separate argv items. The quoted ones
(`"${VORTEXEC_DATA_DIR}"`) are passed as single items even if they
contain spaces.

The `${VORTEXEC_SNAPSHOT_INTERVAL:-600}` syntax is "use the variable,
or default to 600 if unset". Means the env file can leave
`SNAPSHOT_INTERVAL` unset and the script still works.

**Conditional Healthchecks flag.** The Healthchecks URL might be empty
(if the operator hasn't set up monitoring yet). The `if [ -n "${X:-}"
]` test is "is the variable non-empty". When the URL is set, append
the flag and value; when unset, skip it (which makes the Python service
default to no Healthchecks pinging).

The `${X:-}` form with an empty default avoids triggering the `-u`
flag's "undefined variable" error.

**`exec .venv/bin/python -m vortexec "${args[@]}"`** — `exec` replaces
the current shell with the Python process. This is important: without
`exec`, the shell would be the parent of the Python process, and
signals from systemd (SIGINT) would hit the shell first. With `exec`,
Python *is* the process — signals go directly to it.

`"${args[@]}"` expands the array as separate quoted items — the
standard idiom for "expand this array correctly into argv".

### 15.3 Why a separate wrapper

A natural alternative is to put the Python command directly in
`ExecStart` and use systemd's environment-file features:

```ini
EnvironmentFile=/etc/vortexec/env
ExecStart=/opt/vortexec/.venv/bin/python -m vortexec --symbols ${VORTEXEC_SYMBOLS} ...
```

systemd does support this, but the variable expansion has limitations
that make it awkward for our case. Specifically, systemd doesn't do
shell-style word splitting on unquoted variables — so
`${VORTEXEC_SYMBOLS}` with value `"BTCUSDT ETHUSDT SOLUSDT"` is
passed as a *single* argument `"BTCUSDT ETHUSDT SOLUSDT"`, not three
separate arguments. Working around this requires either listing each
symbol as a separate variable (`SYMBOL1`, `SYMBOL2`, ...) or
preprocessing in a wrapper.

The bash wrapper is simpler. It also gives us conditional argument
inclusion (the `if [ -n "${HEALTHCHECKS_URL:-}" ]` block), which
systemd's `ExecStart` syntax doesn't support cleanly.

The cost is one extra process (bash) sitting between systemd and
Python. The `exec` at the end makes this temporary — once Python
starts, bash is replaced and uses no resources.

---

## 16. The bootstrap script, backup, and daily check

### 16.1 `deploy/vortexec.env.example` — the config template

```bash
# VortExec service configuration. Copied to /etc/vortexec/env on the VPS.
# Edit before starting the service for the first time.

# Required
VORTEXEC_SYMBOLS="BTCUSDT ETHUSDT SOLUSDT"
VORTEXEC_DATA_DIR=/var/lib/vortexec/data

# Recording cadence
VORTEXEC_SNAPSHOT_INTERVAL=600

# Liveness monitoring
VORTEXEC_HEALTHCHECKS_URL=
VORTEXEC_DIFFCOUNT_HC_URL=
VORTEXEC_MIN_DIFFS_PER_SYMBOL_DAY=100000

# Cloudflare R2 backup
R2_BUCKET=vortexec-backup
R2_PREFIX=production
```

A template that gets copied to `/etc/vortexec/env` on first install.
The operator edits this file with their specific values (Healthchecks
URLs, R2 bucket name) before starting the service.

Comments document each variable inline. The template values for
required fields (`VORTEXEC_SYMBOLS`, `VORTEXEC_DATA_DIR`) are
reasonable defaults; the variables for monitoring (`*_HC_URL`) are
intentionally empty so the operator has to fill them in (or knowingly
leave them empty to disable monitoring).

The Healthchecks-related variables are split: one for the every-minute
liveness ping (`VORTEXEC_HEALTHCHECKS_URL`), one for the daily
diff-count cron ping (`VORTEXEC_DIFFCOUNT_HC_URL`). Two separate
Healthchecks accounts/checks because they have different cadences and
different failure semantics — the live one fires on "service crashed
or stopped pinging", the daily one fires on "data is missing or
abnormally low even though service appears alive".

`VORTEXEC_MIN_DIFFS_PER_SYMBOL_DAY=100000` is the threshold used by
the daily check (Section 16.4). Default 100,000 diffs/symbol/day —
conservative; real volumes are typically 1-10M diffs/symbol/day on
BTCUSDT.

### 16.2 `deploy/setup.sh` — VPS bootstrap

```bash
#!/bin/bash
# VPS bootstrap for VortExec. Idempotent — safe to re-run after code update.

set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
    echo "ERROR: must run as root (sudo bash ...)" >&2
    exit 1
fi

INSTALL_DIR="${INSTALL_DIR:-/opt/vortexec}"
DATA_DIR="${DATA_DIR:-/var/lib/vortexec/data}"
LOG_DIR="${LOG_DIR:-/var/log/vortexec}"
SERVICE_USER="${SERVICE_USER:-vortexec}"
PY=python3.12

echo "─── 1/8  Installing OS packages ───"
apt-get update -y
apt-get install -y \
    "${PY}" "${PY}-venv" "${PY}-dev" \
    build-essential git rsync curl ca-certificates \
    systemd-timesyncd cron rclone

echo "─── 2/8  Ensuring NTP sync is active ───"
timedatectl set-ntp true
timedatectl status | grep -E "(NTP service|System clock synchronized)"

echo "─── 3/8  Creating service user ───"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/bash "${SERVICE_USER}"
fi

echo "─── 4/8  Creating data + log directories ───"
mkdir -p "${DATA_DIR}" "${LOG_DIR}" /etc/vortexec
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}" "${LOG_DIR}"

echo "─── 5/8  Installing Python deps in venv ───"
if [ ! -d "${INSTALL_DIR}/.venv" ]; then
    sudo -u "${SERVICE_USER}" "${PY}" -m venv "${INSTALL_DIR}/.venv"
fi
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -e "${INSTALL_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

echo "─── 6/8  Installing config + systemd unit ───"
chmod +x "${INSTALL_DIR}/deploy/run.sh"
if [ ! -f /etc/vortexec/env ]; then
    cp "${INSTALL_DIR}/deploy/vortexec.env.example" /etc/vortexec/env
    chmod 640 /etc/vortexec/env
    chown root:"${SERVICE_USER}" /etc/vortexec/env
    echo "  → wrote /etc/vortexec/env (template — EDIT before starting service)"
else
    echo "  → /etc/vortexec/env already exists; leaving alone"
fi
cp "${INSTALL_DIR}/deploy/vortexec.service" /etc/systemd/system/
systemctl daemon-reload

echo "─── 7/8  Installing cron jobs (backup + daily check) ───"
cat >/etc/cron.d/vortexec <<'EOF'
0 4 * * * vortexec /opt/vortexec/deploy/backup.sh >>/var/log/vortexec/backup.log 2>&1
0 5 * * * vortexec /opt/vortexec/.venv/bin/python /opt/vortexec/deploy/diff_count_check.py >>/var/log/vortexec/diff_count.log 2>&1
EOF
chmod 644 /etc/cron.d/vortexec

echo "─── 8/8  Done ───"
echo "Next steps:"
echo "  1. EDIT /etc/vortexec/env"
echo "  2. sudo -u vortexec rclone config"
echo "  3. systemctl enable --now vortexec"
echo "  4. journalctl -u vortexec -f"
```

Eight numbered phases. The phases:

**1. Install OS packages.** `apt-get install` everything we need —
Python 3.12, build tools (for any C extensions in our deps), git,
rsync, rclone (for R2 backup), NTP, cron. Run `apt-get update` first
to get current package lists.

The choice of `python3.12` over `python3` is deliberate. Ubuntu 22.04
ships `python3` pointing at 3.10; we need 3.11+. Ubuntu 24.04 ships
`python3` at 3.12. Specifying `python3.12` explicitly means the
script works on Ubuntu 24.04 (the recommended base image) and fails
loudly on older Ubuntus rather than silently using the wrong Python.

**2. NTP sync.** `timedatectl set-ntp true` enables systemd-timesyncd,
which keeps the system clock synced to NTP servers. Critical for
recording: every timestamp we write to Parquet is wall-clock UTC, and
a drifted clock would make recordings useless for replay or
cross-correlation with other data.

The `timedatectl status | grep` line is informational — prints the
sync status so the operator can see it in the output.

**3. Service user.** Create the `vortexec` system user if it doesn't
exist. `--system` makes it a low-UID system user, not interactive.
`--create-home` gives it a home directory (needed for rclone config
storage). `--shell /bin/bash` makes the user able to run shell
commands when we `sudo -u vortexec` later.

The `if ! id -u ${SERVICE_USER} ...` guard makes this step idempotent
— re-running the script doesn't try to recreate an existing user.

**4. Directories.** Create `/var/lib/vortexec/data`,
`/var/log/vortexec`, `/etc/vortexec`. `chown` the data and log
directories to the service user (they need write access);
`/etc/vortexec` stays root-owned (it holds the config file, which we
want to be unwritable by the service user as a defence-in-depth
measure).

**5. Python venv.** Create a virtualenv at `/opt/vortexec/.venv` owned
by the service user. Upgrade pip first (the system pip can be old).
Install the project in editable mode (`pip install -e .`). The `-e`
flag means changes to the source code are picked up on the next process
restart without needing a reinstall — useful for in-place updates via
`git pull && systemctl restart`.

The `chown -R` at the end ensures the venv and code are owned by the
service user, so the running process can read them.

**6. Config and systemd unit.** Copy the env template to
`/etc/vortexec/env` *only if it doesn't already exist* — preserves any
operator edits across re-runs. Set ownership to `root:vortexec` and
permissions `640` so the service user can read but not write the
config.

Copy the systemd unit file into place and reload systemd's unit
database (`systemctl daemon-reload`). The service is now installed but
not started.

**7. Cron jobs.** Write `/etc/cron.d/vortexec` with two cron lines:
one for the daily backup at 04:00 UTC, one for the daily diff-count
check at 05:00 UTC. Both run as the `vortexec` user. Both append their
output to log files in `/var/log/vortexec/`.

The here-doc syntax (`<<'EOF' ... EOF`) makes the content literal —
no variable expansion inside, which is what we want for cron files.

**8. Final instructions.** Print the operator's next steps:
- Edit the config file.
- Configure rclone interactively (the R2 credentials can't be
  pre-baked; the operator pastes them in).
- Enable and start the service.
- Watch the logs.

### 16.3 `deploy/backup.sh` — daily R2 sync

```bash
#!/bin/bash
# Daily backup of VortExec data to Cloudflare R2.

set -euo pipefail

# shellcheck disable=SC1091
source /etc/vortexec/env

: "${R2_BUCKET:?R2_BUCKET must be set in /etc/vortexec/env}"
: "${VORTEXEC_DATA_DIR:?VORTEXEC_DATA_DIR must be set in /etc/vortexec/env}"
R2_PREFIX="${R2_PREFIX:-vortexec}"

echo "[$(date -u +%FT%TZ)] starting backup → r2:${R2_BUCKET}/${R2_PREFIX}/"

rclone sync \
    "${VORTEXEC_DATA_DIR}/" \
    "r2:${R2_BUCKET}/${R2_PREFIX}/" \
    --exclude '*.tmp' \
    --transfers 4 \
    --checkers 8 \
    --stats-one-line --stats 30s

echo "[$(date -u +%FT%TZ)] backup complete"
```

One-shot bash script that rsyncs the data directory to a Cloudflare
R2 bucket. Runs daily via cron.

**Config loading.** `source /etc/vortexec/env` populates the
environment. The `: "${X:?msg}"` syntax (`:` is the no-op command) is
shorthand for "if X is unset, print msg and exit non-zero". This makes
required variables explicit at script-start rather than letting them
silently default to empty strings.

**rclone sync.** `rclone` is a multi-cloud rsync-like tool that
supports S3-compatible APIs (which R2 implements). The command:

- `sync` mode (not `copy`): make the destination match the source.
  Files deleted locally are also deleted remotely. We use `sync` rather
  than `copy` because nothing should ever be deleted from the data
  directory under normal operation; if something is, it's deliberate
  and we want R2 to reflect it.
- `--exclude '*.tmp'` — skip temporary files (none of our code
  creates `.tmp` files, but defensive).
- `--transfers 4 --checkers 8` — parallelism. 4 concurrent file
  transfers, 8 concurrent existence-checks. Tuned for a small VPS;
  not bandwidth-limited at our scale.
- `--stats-one-line --stats 30s` — log a compact stats line every
  30 seconds so the backup log shows progress.

**rclone's remote**. The `r2:` prefix in the destination refers to a
remote named `r2` in rclone's config (`~/.config/rclone/rclone.conf`
or `/root/.config/rclone/rclone.conf`). The operator runs `rclone
config` interactively once on the VPS to set this up, pasting in the
R2 account ID, access key, and secret. The config file then holds
those credentials, and the script can run unattended.

The interactive configuration step is the *only* part of the
deployment that isn't reproducible from the repo. It exists because
credentials shouldn't be in the repo, and there's no clean way to
pre-bake them.

### 16.4 `deploy/diff_count_check.py` — daily sanity check

```python
"""Daily sanity check: count yesterday's recorded diffs per (venue, symbol)
and ping Healthchecks.io if everything looks healthy. Silence (no ping) means
the alert fires."""

from __future__ import annotations

import datetime as dt
import os
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq

DATA_DIR = Path(os.environ.get("VORTEXEC_DATA_DIR", "/var/lib/vortexec/data"))
HEALTHCHECKS_URL = os.environ.get("VORTEXEC_DIFFCOUNT_HC_URL", "").strip()
MIN_DIFFS_PER_SYMBOL_DAY = int(
    os.environ.get("VORTEXEC_MIN_DIFFS_PER_SYMBOL_DAY", "100000")
)


def yesterday_str() -> str:
    return (dt.datetime.utcnow() - dt.timedelta(days=1)).strftime("%Y-%m-%d")


def main() -> int:
    yday = yesterday_str()
    files = list(DATA_DIR.glob(f"*/*/{yday}/*.parquet"))
    if not files:
        print(f"FAIL: no diff files found under {DATA_DIR} for {yday}")
        return 1

    totals: dict[tuple[str, str], int] = defaultdict(int)
    for f in files:
        try:
            n = pq.ParquetFile(f).metadata.num_rows
        except Exception as e:
            print(f"FAIL: unreadable parquet file {f}: {e}")
            return 1
        symbol = f.parent.parent.name
        venue = f.parent.parent.parent.name
        totals[(venue, symbol)] += n

    print(f"Diff counts for {yday} (threshold: {MIN_DIFFS_PER_SYMBOL_DAY:,}/symbol):")
    failures: list[str] = []
    for (venue, symbol), n in sorted(totals.items()):
        marker = "OK " if n >= MIN_DIFFS_PER_SYMBOL_DAY else "LOW"
        print(f"  [{marker}] {venue}/{symbol}: {n:,} diffs")
        if n < MIN_DIFFS_PER_SYMBOL_DAY:
            failures.append(f"{venue}/{symbol}")

    if failures:
        print(f"FAIL: low diff counts: {', '.join(failures)}")
        return 1

    if HEALTHCHECKS_URL:
        try:
            with urllib.request.urlopen(HEALTHCHECKS_URL, timeout=10) as resp:
                if resp.status >= 400:
                    print(f"WARN: healthchecks ping returned HTTP {resp.status}")
        except Exception as e:
            print(f"WARN: healthchecks ping failed: {e!r}")
    else:
        print("(no VORTEXEC_DIFFCOUNT_HC_URL set; skipping ping)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

A Python script (run via cron at 05:00 UTC daily) that verifies
yesterday's recordings look sensible.

The algorithm:

1. Compute yesterday's UTC date string.
2. Find every Parquet file under `DATA_DIR` matching the glob
   `*/*/{yesterday}/*.parquet` — i.e. `venue/symbol/yesterday/*.parquet`.
3. For each file, read its Parquet metadata (much cheaper than reading
   the actual data — just opens the file's footer) and extract the row
   count.
4. Aggregate row counts per `(venue, symbol)`.
5. Compare each total to `MIN_DIFFS_PER_SYMBOL_DAY` (default 100,000).
   If any (venue, symbol) is below the threshold, print failures and
   exit non-zero (which means *don't* ping Healthchecks).
6. If everything is OK, ping the Healthchecks URL.

**The "ping or don't ping" pattern.** The check pings Healthchecks
*only when everything is healthy*. Healthchecks.io's grace-period
mechanism alerts when expected pings stop arriving. So:

- Healthy data → check pings → Healthchecks sees the ping → no alert.
- Low data → check skips ping → Healthchecks sees no ping → alert
  fires after the grace period.
- Service is down (cron didn't run) → no ping → alert fires.

The same single mechanism catches both "data is bad" and "the check
itself stopped running". Elegant.

**Why open just the footer.** `pq.ParquetFile(f).metadata.num_rows`
reads only the file's footer (a small fixed-position chunk at the end
of the file) and gets the row count from metadata. We don't load any
data into memory. For a 100 MB Parquet file, reading the footer is a
~10 KB disk read. With hundreds of hour-files per day, this is the
difference between the check taking ~1 second and ~minutes.

**The path parsing.** `f.parent.parent.name` and
`f.parent.parent.parent.name` extract the symbol and venue from the
path. The layout is
`DATA_DIR/{venue}/{symbol}/{date}/{HH}.parquet`, so for a file
`/var/lib/vortexec/data/binance/BTCUSDT/2026-05-14/09.parquet`:

- `f.parent` = `.../2026-05-14`
- `f.parent.parent` = `.../BTCUSDT` → `.name` = `"BTCUSDT"` (symbol)
- `f.parent.parent.parent` = `.../binance` → `.name` = `"binance"` (venue)

This is fragile to layout changes — if the directory structure ever
shifts, the parsing breaks. Since the layout is established in the
recorder code (Section 11.4) and unlikely to change, the fragility is
acceptable.

**Error handling.** If any Parquet file is unreadable (corrupted,
truncated mid-write), the check exits non-zero with a clear error
message. This is a real failure mode: if the recorder process crashed
while writing a row group, the file might lack a complete footer.
Catching this at the daily check is good — it surfaces the problem
quickly and via the alert.

### 16.5 The whole operational picture

Together, these artefacts give:

- **systemd unit + run.sh** — the service itself, supervised, with
  resource limits, hardening, and auto-restart.
- **vortexec.env** — operator-tunable config, kept out of the repo.
- **setup.sh** — reproducible bootstrap from a fresh VPS.
- **backup.sh** — daily off-site backup, handles "VPS dies, lose
  everything locally".
- **diff_count_check.py** — daily sanity check, handles "service is
  alive but producing wrong data".
- **Healthchecks.io integration** (live ping from the service + daily
  ping from the cron) — external monitoring, handles "I forgot to
  check on the service for two weeks".

This is the minimum operational surface for an unattended multi-month
deployment. Each artefact handles one specific failure mode:

| Failure | Handled by |
|---|---|
| Process crashes (any reason) | systemd `Restart=always` |
| Memory leak | systemd `MemoryMax=1G` + restart |
| Network drop, transient error | maintainer's auto-reconnect (Section 10.7) |
| Sequence gap | maintainer's auto-reconnect |
| Disk fills up | (none — would need monitoring; defer until problem appears) |
| VPS hardware failure | R2 backup recovers data |
| Operator stops watching | Healthchecks alerts on missing ping |
| Service runs but data is broken | Daily diff-count check |
| Bad code deploy | Manual rollback via git |

We don't have automated rollback (a code change you deploy and that
breaks recording requires manual `git checkout HEAD~1; systemctl
restart vortexec`). That's a deliberate choice — the deploy cadence is
slow enough (we don't deploy continuously) that auto-rollback is
ceremony beyond what we need. If we ever moved to continuous deployment,
we'd add it.

---

# Part IX — What isn't built yet, and why

The system as documented above does one thing well: it maintains a
live order book and serves a deterministic slippage estimate over HTTP.
That is the foundation. It is also less than the full product
described in `ARCHITECTURE.md`. This Part is the honest accounting of
what's deferred and why each deferral is correct.

## 17. Deferred features

The architecture has Phases 1 through 7. We have done 1, 2 (both
sub-phases), 3, and 4 — the core engineering and the basic HTTP
surface. The rest:

### 17.1 ML quantile model (Phase 5a)

**What it would be.** A `model/quantile_model.py` that loads a
pre-trained pickle (scikit-learn quantile regressor) and a
`predict(features, side, size) -> QuantilePredictions` method
returning P50/P90/P95 slippage estimates. Plumbed into `/v1/estimate`
so the response includes both deterministic and quantile predictions.

**Why deferred.** The model itself needs training data. The training
data needs to be representative — i.e. drawn from the same
distribution as the conditions under which the model will be queried.
The most defensible source of that data is *our own recordings* on
the same connector code we run in production, because then there's
zero distribution shift between training and inference.

We have ~16 hours of recorded BTCUSDT depth from a legacy run (top-100
only, which makes deep-trade simulation unreliable) and a few hours of
multi-symbol recordings from this development cycle. That's not
enough. Calibrated tail estimation needs *months* of data, partly
because the tails by definition are rare events and partly because
markets cycle through regimes that any one short recording will fail
to capture.

The alternative — buying historical data from Tardis or Kaiko — costs
£200-500 for a few months of one symbol, with the caveat that the
training distribution might still not match our connector's exact
parsing.

The structural decision was to **launch the recorder unattended on a
VPS for 2-3 months** while doing the other engineering work in
parallel. The recorder is now built and validated; the VPS deployment
is the immediate next step (Part VIII covers the prep). By the time
the rest of the product surface is built, the data will exist and the
model can be trained on it.

If we'd built the ML layer first — wired the legacy pickle into the
API — we'd be serving predictions whose training data we don't know.
A trader using the system would take the P95 number at face value and
get burned. The product would have a feature that looked meaningful
but was actually noise. That's worse than no feature.

### 17.2 API authentication (Phase 5c)

**What it would be.** An `api/auth.py` with API-key-based
authentication: each request carries a header like `X-API-Key:
<key>`; we look up the key in a SQLite database, verify it's valid,
attach the customer to the request. Stripe webhook integration for
billing lifecycle (key issuance, suspension on payment failure).

**Why deferred.** The API is currently bound to `127.0.0.1` — only
accessible from inside the VPS. Adding auth before exposing it
externally is technically unnecessary. Auth becomes necessary
*the moment* the API is reachable from outside, and not before.

The reason this is "deferred" rather than "trivial to add when needed"
is that auth is a structural change: every route needs to declare its
auth dependency, error responses need to be normalised, customer
context needs to flow through into logs and rate-limiting. This is
~200 LOC of careful work, plus the SQLite schema and the Stripe
integration on top.

When we open the API to external clients (the "find first 10
customers" step), auth is the gating piece. Until then, building it
is premature.

### 17.3 Post-trade analysis endpoint (Phase 5b)

**What it would be.** `POST /v1/analyse`. The client sends a real fill
they executed (price, size, side, venue, timestamp). The service reads
the historical book at that timestamp from the recordings, walks it
for the *achievable* slippage at that moment, and returns the gap —
"you paid 2.5 bps, the optimal achievable was 1.7 bps, you were 0.8
bps above the floor."

**Why deferred.** Two reasons.

First, the post-trade analysis is meaningfully useful only once enough
data has been recorded. With ~16 hours of legacy recordings, the
chance that a customer's fill happened during a recorded window is
near zero. Once we have months of continuous data, the analyser
becomes useful for any fill from any time in our recording window.

Second, the post-trade analyser is the *natural next API endpoint
after auth*, and there's no value in building it before customers can
use it. The product story "analyse your fills against what was
achievable" is a real second feature beyond the pre-trade estimator.
Building it gives the product a second endpoint that justifies a
subscription.

The implementation is straightforward: glob the recordings directory
to find the right Parquet file, find the snapshot nearest to the
fill's timestamp, replay diffs forward to the exact timestamp,
construct an `OrderBook`, run `simulate_market_order`. The whole
endpoint is probably ~150 LOC plus the file-locating glue.

### 17.4 Multi-venue (Phase 6)

**What it would be.** `venues/okx.py` and `venues/bybit.py` — connectors
for OKX and Bybit using the same `VenueConnector` ABC. Plus
cross-venue logic in `pretrade.py` (`POST /v1/estimate` with a list of
venues splits the order across them for optimal execution).

**Why deferred.** Each new venue is ~300 LOC of connector + tests +
live validation. Same engineering pattern as Binance, but each venue's
protocol has its own quirks (OKX uses checksums to verify book
state; Bybit has a different reconnect protocol; both have different
snapshot endpoints). The work isn't hard; it's just *more of the same
work*, multiplied.

The product reason to add OKX or Bybit is that cross-venue routing is
where execution analytics gets *interesting*. Pre-trade estimation
that says "Binance: 2.1 bps, OKX: 1.4 bps, Bybit: 1.8 bps — route to
OKX" is a meaningfully different product proposition than "Binance:
2.1 bps". This is what sophisticated trading desks already do
manually via their broker software; offering it as an API is the
moat.

Doing this before the first customer would be premature. Doing it
once the first 10 customers are using single-venue is the right
expansion move.

### 17.5 Configuration management (`config.py`)

**What it would be.** Loading configuration from YAML files in
`config/` (per the architecture's vision: `config/default.yaml`,
`config/dev.yaml`, `config/prod.yaml`), via pydantic-settings or
similar. Replace the current CLI-args approach.

**Why deferred.** The current CLI surface is small (eight arguments).
A config file becomes valuable when the config grows past what fits
comfortably on a command line — typically 15-20 settings. We're not
there.

Adding `config.py` prematurely would introduce indirection without
benefit: instead of reading the systemd unit and `/etc/vortexec/env`
to see what's configured, an operator would have to also read a YAML
file. Two surfaces for one thing.

When we have venue-specific configuration (different API endpoints
per venue, per-symbol overrides), authentication configuration, model
configuration, then YAML is the right shape and `config.py` arrives.

### 17.6 Structured logging (`logging.py`)

**What it would be.** The architecture mentions a `vortexec/logging.py`
module that exposes a structured logger — every log line includes
context like venue, symbol, request_id automatically. Probably built
on `structlog`.

**Why deferred.** Current logging via stdlib `logging` is good enough.
Log lines are written to journald, journald supports filtering by
fields (`journalctl -u vortexec --grep "BTCUSDT"`), and we don't yet
have downstream log aggregation (Loki, ELK, etc.) that would benefit
from structured fields.

If we later ship to a stack that consumes structured logs, swapping
out the logger is contained — we have one logger initialization per
module, and replacing them is mechanical.

### 17.7 Why this list, in this order

The thread connecting all these deferrals is the same: build what's
necessary now, defer what's premature, and never build something
whose value depends on data or customers we don't yet have.

The current build sequence is consistent with this:

1. Phase 1: core math — required for everything.
2. Phase 2: live book maintenance — required for pre-trade estimates
   to be meaningful.
3. Phase 3: recording — required so the data clock starts running, and
   required for any future ML or post-trade analysis.
4. Phase 4: basic API — required to have something to expose.

Next, in approximate order:

- **VPS deployment** (Part VIII, ready to execute) — the *data
  clock* is the longest-pole task. Start it as early as possible.
- **Auth** — gate the API, expose externally, find first customers.
- **Post-trade endpoint** — second feature, justifies subscription
  pricing. Uses the recordings as they accumulate.
- **OKX or Bybit connector** — broader value proposition (cross-venue
  routing).
- **ML quantile model** — once the recorder has been running long
  enough.
- **`config.py`, `logging.py`** — when scale demands.

The defence of each "we haven't built X yet" is the same: X's value
depends on something downstream we don't yet have. Build X when X's
prerequisites are real.

---

# Final notes

This document is a snapshot of the codebase as of 2026-05-15. The
project has been built layer by layer over several development cycles;
each layer has been correctness-validated before the next was added.
The bottom of the stack — the deterministic walk on a live, maintained
order book — is the property the rest of the product will sit on, and
it has been validated against real Binance under real network
conditions. The remaining work is largely productisation: gating,
exposing, billing, and broadening — all things that depend on the
foundation being correct, and none of which are hard.

The single most important thing to take from reading this end-to-end is
that *every layer has a clearly defined contract and a clearly defined
failure mode*. The book never gets read mid-update because of how
asyncio runs single-threaded code. The connector never silently drifts
because the aligner enforces sequence numbers. The maintainer never
silently dies because the run loop is exhaustively defensive. The
recorder never silently loses data because writers are explicitly
closed on shutdown. The service never silently fails because systemd
restarts on death and Healthchecks alerts on prolonged silence. Each
layer's correctness rests on the layer below, and each layer adds an
explicit contract that the layer above can rely on.

When something breaks in the future — and something will — the layered
contracts make diagnosis tractable: you find the lowest layer whose
contract is violated, fix the cause there, and verify each higher
layer's expectations still hold. Without this discipline, debugging a
system that's wrong about basis points of cost across thousands of
fills is approximately impossible. With it, every wrong number traces
to one specific layer.
