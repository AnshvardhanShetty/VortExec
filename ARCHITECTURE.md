# VortExec — Architecture

This document describes the structure of the VortExec codebase. It exists so that anyone working on the project — including AI coding assistants — has the architectural context to make changes that fit the system rather than fighting it.

If you're an AI assistant reading this: the structure below is deliberate. When in doubt, prefer extending existing modules in the patterns shown here over introducing new patterns. If a piece of work doesn't seem to fit the structure, surface that to the human rather than inventing a new module silently.

## What the project is

VortExec is a single Python service that:

1. Maintains live order books from multiple cryptocurrency exchanges in memory by consuming WebSocket diff streams and reconciling against periodic REST snapshots.
2. Continuously records all incoming market data to disk in a replayable format, so historical book state can be reconstructed at any timestamp.
3. Serves an HTTP API that customers call to get pre-trade cost estimates (deterministic walk + ML-based tail estimates) and post-trade fill analysis (comparing realised fills to the optimal at the time of execution).

The deterministic walk on the live book is the source of truth for point estimates. A quantile regression model layered on book features provides calibrated tail estimates and anomaly scoring. Eventually the quantile model will be retrained as a true forecaster on accumulated live data; until then, it provides conditional tail context on top of the deterministic walk.

The whole thing runs as one process, in one language (Python), with all components communicating in-process. No microservices, no message brokers, no separate ML serving infrastructure. Async throughout (asyncio).

## Repository layout

```
vortexec/
├── pyproject.toml
├── README.md
├── ARCHITECTURE.md          # this file
├── DECISIONS.md             # running log of design decisions and rationale
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── config/
│   ├── default.yaml         # base config
│   ├── dev.yaml             # local dev overrides
│   └── prod.yaml            # production overrides
├── src/
│   └── vortexec/
│       ├── __init__.py
│       ├── core/            # pure logic, no I/O
│       ├── venues/          # exchange-specific protocol handlers
│       ├── maintainer/      # live book lifecycle management
│       ├── recorder/        # persistence layer
│       ├── model/           # ML model loading and inference
│       ├── api/             # HTTP server and routes
│       ├── service.py       # top-level orchestration
│       ├── config.py        # config loading
│       └── logging.py       # structured logging setup
├── tests/
│   ├── unit/                # fast, pure-function tests
│   ├── integration/         # tests against live exchanges or fixtures
│   └── fixtures/            # sample books, sample diff sequences
├── scripts/                 # one-off utilities (replay, inspect, etc.)
├── research/                # ML research, separate from production
│   ├── pyproject.toml       # research has its own deps
│   ├── notebooks/
│   └── README.md
└── data/                    # gitignored: recorded market data
    └── {venue}/{symbol}/{date}.parquet
```

## Module responsibilities

### `core/` — pure logic

No I/O, no async, no network, no database. Just data structures and pure functions. Every function in this module is deterministic and unit-testable in microseconds.

**`core/book.py`** — The `OrderBook` class, the canonical in-memory representation. Stores bids and asks as `SortedDict` keyed by price (using `sortedcontainers`). Provides:
- `apply_snapshot(snapshot)` — replace book contents with a full snapshot
- `apply_diff(diff)` — apply a single incremental update (level set or delete)
- `best_bid()`, `best_ask()`, `mid()`, `spread()` — basic accessors
- `walk(side, size)` — used by the simulator (defined in simulator.py)

The class is plain Python, fully synchronous. About 200 lines.

**`core/simulator.py`** — The deterministic walk. Single function:
```
simulate_market_order(book: OrderBook, side: Side, size: float) -> SimResult
```
where `SimResult` is a frozen dataclass with `avg_price`, `slippage_bps`, `unfilled_qty`, `levels_consumed`. Pure function. About 80 lines.

**`core/features.py`** — Feature extraction. Single function:
```
extract_features(book: OrderBook) -> Features
```
where `Features` is a frozen dataclass with `spread_bps`, `mid_price`, `depth_top_5_bids`, `depth_top_5_asks`, `depth_top_10_bids`, `depth_top_10_asks`, `imbalance`. Pure function. About 60 lines.

**`core/types.py`** — Shared dataclasses: `Side` (enum: BUY/SELL), `Snapshot`, `Diff`, `Level`, `BookUpdate`. These are the canonical normalised types the rest of the system uses.

### `venues/` — exchange protocol handlers

Each exchange has different WebSocket message formats, snapshot endpoints, sequence number conventions, and reconnection requirements. The `venues/` module isolates this specificity behind a common interface.

**`venues/base.py`** — Abstract base class `VenueConnector` defining the interface every exchange implementation must satisfy:
```
async def connect() -> None
async def fetch_snapshot(symbol: str) -> Snapshot
async def stream_diffs(symbol: str) -> AsyncIterator[Diff]
async def disconnect() -> None
```

The `Snapshot` and `Diff` types are normalised — every connector translates the exchange's native format into these. The rest of the system never sees exchange-specific message formats.

**`venues/binance.py`** — Implements `VenueConnector` for Binance. Handles:
- WebSocket connection to `wss://stream.binance.com:9443/ws/{symbol}@depth@100ms`
- REST snapshot fetch from `/api/v3/depth?limit=5000`
- Bootstrap protocol: fetch snapshot, buffer diffs that arrived during fetch, replay buffered diffs from the right `lastUpdateId`
- Sequence number tracking via `U`/`u` fields, with resync on gap detection

This is the most complex of the connector implementations because of the bootstrap protocol. Get it right and everything else is easier.

**`venues/okx.py`, `venues/bybit.py`** — Same pattern, different specifics. Implemented after Binance is solid.

### `maintainer/` — live book lifecycle

The keystone module. One class that owns the live state for one (venue, symbol) pair.

**`maintainer/book_maintainer.py`** — `BookMaintainer` class. Wraps a `VenueConnector` and an `OrderBook`. Runs an async task that:

1. On startup: calls `fetch_snapshot()`, applies it to a fresh book, begins consuming diffs.
2. Normal operation: applies each diff, validates sequence numbers, broadcasts updates to subscribers.
3. On gap or disconnect: stops applying diffs, fetches fresh snapshot, resyncs.

Exposes:
- `get_book() -> OrderBook` — current book state (read-locked during the call)
- `stream_updates() -> AsyncIterator[BookUpdate]` — async generator for subscribers
- `is_healthy() -> bool` — for the health endpoint

This module is the only thing in the system that knows about the live book's lifecycle. The API doesn't know how the book gets maintained, just that it can ask for the current state. The recorder doesn't know how diffs arrive, just that it gets `BookUpdate` events.

About 400 lines including resync logic and lifecycle management.

### `recorder/` — persistence

Subscribes to `BookMaintainer` update streams. Buffers updates and writes them to disk in Parquet format.

**`recorder/parquet_recorder.py`** — `ParquetRecorder` class. Buffers in memory (in 60-second windows or N updates, whichever first), then flushes to:
```
data/{venue}/{symbol}/{YYYY-MM-DD}/{HH}.parquet
```

Files are columnar and compressed. The research code reads these directly with pandas/polars.

Also writes periodic full snapshots (every 10 minutes) so replay can start from any point without going back to the beginning.

About 200 lines.

### `model/` — ML inference

Loads the trained quantile model at startup. Provides inference for the API.

**`model/quantile_model.py`** — `QuantileModel` class. Loads pickled scikit-learn models (P50, P90, P95) at startup. Provides:
```
predict(features: Features, side: Side, size: float) -> QuantilePredictions
```
where `QuantilePredictions` has `p50_bps`, `p90_bps`, `p95_bps`.

The model file lives at `data/models/quantile_v{n}.pkl`. Hot-swapping happens by writing a new file and reloading — handled by a separate concern, not this class.

About 100 lines.

### `api/` — HTTP server

FastAPI server. All routes are async and depend on the `BookMaintainer` and `QuantileModel` via FastAPI's dependency injection.

**`api/server.py`** — FastAPI app instance, middleware (CORS, request logging, error handling), startup/shutdown hooks. About 100 lines.

**`api/auth.py`** — API key authentication via header. Looks up keys in SQLite (`data/auth.db`). Stripe webhook handler for subscription lifecycle. About 200 lines.

**`api/models.py`** — Pydantic models for request and response shapes. The API contract lives here. About 100 lines.

**`api/routes/pretrade.py`** — `POST /v1/estimate`. Takes `(side, size, symbol, venues=optional)`. For each requested venue: gets the live book, runs the simulator, runs the model, returns combined estimate. Computes optimal split if multiple venues requested. About 100 lines.

**`api/routes/posttrade.py`** — `POST /v1/analyse`. Takes a fill `(price, size, side, venue, timestamp)`. Looks up the historical book at that timestamp from the Parquet recordings, walks it for the optimal, returns the gap. About 150 lines.

**`api/routes/health.py`** — `GET /health`. Returns 200 if all maintainers report healthy, 503 otherwise. About 30 lines.

### `service.py` — top-level orchestration

The entry point. `python -m vortexec` runs this.

1. Loads config.
2. Sets up logging.
3. Instantiates `VenueConnector` for each configured venue/symbol.
4. Wraps each in a `BookMaintainer`.
5. Instantiates the `Recorder` and subscribes it to all maintainers.
6. Loads the `QuantileModel`.
7. Starts the FastAPI app, injecting maintainers and model.
8. Runs the asyncio event loop.
9. Handles SIGINT/SIGTERM for graceful shutdown.

About 150 lines. Pure orchestration, no business logic.

### `research/` — ML research

Lives outside `src/vortexec/`. Has its own `pyproject.toml` with heavy dependencies (PyTorch, scikit-learn, statsmodels, matplotlib, jupyter). Production install never depends on these.

Reads from the same Parquet files the recorder writes. Never imports from production code (one-way dependency: research consumes recorded data, doesn't reach into the live system).

This is where the forecasting work happens — labelling pipeline, feature engineering, training, calibration analysis, baseline comparisons. Output is improved model files that get hot-swapped into production.

## Data flow

Live data path:
```
Exchange WebSocket
    ↓ (diffs as raw JSON)
VenueConnector
    ↓ (normalised Diff objects)
BookMaintainer
    ↓ (BookUpdate events)
    ├──→ in-memory OrderBook (read by API)
    └──→ Recorder (writes to Parquet)
```

API request path:
```
HTTP request
    ↓
api/routes/{pretrade,posttrade}
    ↓
BookMaintainer.get_book() → OrderBook
    ↓
core/simulator.simulate_market_order(book, side, size)
core/features.extract_features(book)
model.predict(features, side, size)
    ↓
HTTP response
```

Research path (separate from live system):
```
Parquet files on disk
    ↓
research/notebooks (analysis, training)
    ↓
new model pickle file
    ↓
hot-swap into production (file watcher reloads model)
```

## Key invariants

These hold throughout the system. Code that violates them is buggy, even if it appears to work.

1. **Books are never read mid-update.** `BookMaintainer.get_book()` returns a consistent snapshot. If a diff is being applied when `get_book()` is called, the call waits.

2. **Diffs are applied in sequence order.** If a sequence gap is detected, the maintainer stops applying diffs and triggers a resync. Out-of-order application would silently corrupt the book.

3. **The recorder never blocks the maintainer.** If the recorder falls behind, it drops updates rather than back-pressuring the live data path. Drops are logged.

4. **The model never makes the API slow.** Model inference is on the hot path; if it ever takes more than a few milliseconds, that's a bug. The point estimate from the simulator is always available even if the model is slow or fails.

5. **Pure functions in `core/` have no I/O.** No logging, no config reads, no time calls. They're pure functions of their inputs. This makes them trivially testable.

6. **Type boundaries between modules are stable.** `Snapshot`, `Diff`, `BookUpdate`, `Features`, `SimResult`, `QuantilePredictions` are the canonical inter-module types. Don't pass dicts or tuples across module boundaries; use the typed dataclasses.

## Conventions

**Async.** Everything I/O-related is async. Everything in `core/` is sync. Don't mix the two — sync I/O in async code blocks the event loop.

**Logging.** Use the structured logger from `vortexec/logging.py`. Each log line has a context (venue, symbol, request_id where applicable). Never use `print()`.

**Config.** Read from `config.py`, which loads from YAML files in `config/`. Never hardcode hostnames, paths, or magic numbers.

**Errors.** Custom exceptions for known conditions (`SequenceGapError`, `BookStaleError`, `VenueDisconnectError`). Catch broad `Exception` only at the top of long-running tasks for observability, then either restart or propagate.

**Tests.** Every PR adds tests. Pure functions in `core/` get unit tests with fixtures. Modules with I/O get integration tests against fixtures (recorded sequences) or against real exchanges (rate-limited, in CI behind a flag).

**Type hints.** Required everywhere. Run `mypy --strict` in CI.

## Build phases

The project is built in phases, each phase yielding something testable end-to-end.

**Phase 1: `core/` and tests.** Implement `book.py`, `simulator.py`, `features.py`, `types.py`. Validate against fixtures of recorded data. ~2 days.

**Phase 2: Single venue connector + maintainer.** Implement `venues/base.py`, `venues/binance.py`, `maintainer/book_maintainer.py`. Validate that a maintained book matches a fresh REST snapshot fetched independently. ~3-5 days.

**Phase 3: Recorder.** Implement `recorder/parquet_recorder.py`. Verify that a few hours of recording produces sensible Parquet files. ~1 day.

**Phase 4: API skeleton.** Implement `api/server.py`, `api/routes/pretrade.py`, `api/routes/health.py`. Wire `service.py` to start everything together. ~2-3 days.

**Phase 5: Auth, model, post-trade.** Implement `api/auth.py`, `model/quantile_model.py`, `api/routes/posttrade.py`. ~3-4 days.

**Phase 6: Multi-venue.** Implement `venues/okx.py`, `venues/bybit.py`. Add cross-venue logic to pretrade route. ~3-5 days per venue.

**Phase 7: Hardening.** Logging, metrics, deployment, monitoring. Ongoing.

## Things this project deliberately is not

To prevent scope creep and architectural drift:

- **Not a microservices system.** One process, one language, one event loop.
- **Not a low-latency HFT system.** Targeting algo traders on seconds-to-minutes timescales. Sub-100ms API response is the bar.
- **Not a frontend project.** Web UI lives in a separate project that talks to this API.
- **Not a research framework.** The research code in `research/` reads recorded data and produces models; it doesn't reach into the live system.
- **Not a backtesting engine.** Replay is supported (the recorded data makes it possible) but the primary product is live, not historical simulation.
