"""Recorder that subscribes to a BookMaintainer's update stream and writes
the diffs (and periodic full snapshots) to Parquet files on disk.

Diff schema (one row per Diff):
    venue:        string
    symbol:       string
    timestamp_ms: int64   (Unix epoch milliseconds)
    side:         string  ("buy" or "sell")
    price:        float64
    quantity:     float64

Snapshot schema (one row per level at a point in time):
    venue:           string
    symbol:          string
    snapshot_ts_ms:  int64
    side:            string  ("buy" for bids, "sell" for asks)
    price:           float64
    quantity:        float64

Layout::

    {base_dir}/{venue}/{symbol}/{YYYY-MM-DD}/{HH}.parquet
    {base_dir}/{venue}/{symbol}/{YYYY-MM-DD}/snapshots/{HH-MM-SS}.parquet

Each diff Parquet file accumulates row groups as the hour progresses. Flushes
are triggered by either reaching ``flush_after_updates`` buffered rows or
``flush_interval_seconds`` elapsed since the last flush. Snapshots are one
file each, taken every ``snapshot_interval_seconds`` and starting one
interval after recording begins (so the maintainer has time to populate the
book).

On stop() the remaining buffer is flushed and all open ParquetWriters are
closed so the files end up with valid footers and are readable.
"""

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
DEFAULT_SNAPSHOT_INTERVAL_SECONDS = 600.0  # 10 minutes

SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("venue", pa.string()),
        pa.field("symbol", pa.string()),
        pa.field("timestamp_ms", pa.int64()),
        pa.field("side", pa.string()),
        pa.field("price", pa.float64()),
        pa.field("quantity", pa.float64()),
    ]
)

SNAPSHOT_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("venue", pa.string()),
        pa.field("symbol", pa.string()),
        pa.field("snapshot_ts_ms", pa.int64()),
        pa.field("side", pa.string()),
        pa.field("price", pa.float64()),
        pa.field("quantity", pa.float64()),
    ]
)

# (venue, symbol, date_str, hour) → open ParquetWriter
_WriterKey = tuple[str, str, str, int]


def _hour_path(base_dir: Path, venue: str, symbol: str, ts_ms: int) -> Path:
    # Convert ms epoch to year/month/day/hour using a UTC view.
    import datetime as _dt

    dt = _dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=_dt.timezone.utc)
    return base_dir / venue / symbol / dt.strftime("%Y-%m-%d") / f"{dt.hour:02d}.parquet"


def _writer_key_for(update: BookUpdate) -> _WriterKey:
    import datetime as _dt

    ts_ms = int(update.diff.timestamp.timestamp() * 1000)
    dt = _dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=_dt.timezone.utc)
    return (update.venue, update.symbol, dt.strftime("%Y-%m-%d"), dt.hour)


def _to_table(updates: list[BookUpdate]) -> pa.Table:
    return pa.table(
        {
            "venue": [u.venue for u in updates],
            "symbol": [u.symbol for u in updates],
            "timestamp_ms": [int(u.diff.timestamp.timestamp() * 1000) for u in updates],
            "side": [u.diff.side.value for u in updates],
            "price": [u.diff.price for u in updates],
            "quantity": [u.diff.quantity for u in updates],
        },
        schema=SCHEMA,
    )


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

    @property
    def total_recorded(self) -> int:
        return self._total_recorded

    @property
    def total_flushed(self) -> int:
        return self._total_flushed

    @property
    def total_snapshots(self) -> int:
        return self._total_snapshots

    def record(self, update: BookUpdate) -> None:
        """Append one update to the buffer. May trigger a flush."""
        self._buffer.append(update)
        self._total_recorded += 1
        if len(self._buffer) >= self._flush_after:
            self._flush()

    async def start(self, maintainer: BookMaintainer) -> None:
        """Subscribe to the maintainer and start the background consume and
        periodic-snapshot tasks.
        """
        if self._task is not None:
            return
        self._maintainer = maintainer
        self._task = asyncio.create_task(self._consume(maintainer))
        if self._snapshot_interval > 0:
            self._snapshot_task = asyncio.create_task(self._snapshot_loop())

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
        # Final flush + close all writers so files have valid footers.
        self._flush()
        for writer in self._writers.values():
            writer.close()
        self._writers.clear()
        log.info(
            "recorder stopped: %d updates recorded, %d flushed to disk, "
            "%d snapshots written",
            self._total_recorded,
            self._total_flushed,
            self._total_snapshots,
        )

    async def _consume(self, maintainer: BookMaintainer) -> None:
        log.info("recorder consuming from maintainer, writing into %s", self._base_dir)
        async for update in maintainer.stream_updates():
            self.record(update)
            # Time-based flush check between updates (every update is cheap)
            if (time.monotonic() - self._last_flush_at) >= self._flush_interval:
                self._flush()

    async def _snapshot_loop(self) -> None:
        """Periodically write a full-book snapshot for replay bootstrap."""
        while True:
            await asyncio.sleep(self._snapshot_interval)
            try:
                self._write_snapshot()
            except Exception:
                log.exception("snapshot write failed; continuing")

    def _write_snapshot(self) -> None:
        if self._maintainer is None:
            return
        book = self._maintainer.get_book()
        venue = self._maintainer.venue
        symbol = self._maintainer.symbol

        # Build rows from the live SortedDicts. Sync block — no awaits — so
        # the maintainer can't apply a diff partway through.
        now = _dt.datetime.now(_dt.timezone.utc)
        ts_ms = int(now.timestamp() * 1000)
        prices_bids = list(book._bids)
        prices_asks = list(book._asks)
        if not prices_bids and not prices_asks:
            return  # nothing to snapshot yet

        n = len(prices_bids) + len(prices_asks)
        venues = [venue] * n
        symbols = [symbol] * n
        timestamps = [ts_ms] * n
        sides = ["buy"] * len(prices_bids) + ["sell"] * len(prices_asks)
        prices = [float(p) for p in prices_bids] + [
            float(p) for p in prices_asks
        ]
        quantities = [float(book._bids[p]) for p in prices_bids] + [
            float(book._asks[p]) for p in prices_asks
        ]

        table = pa.table(
            {
                "venue": venues,
                "symbol": symbols,
                "snapshot_ts_ms": timestamps,
                "side": sides,
                "price": prices,
                "quantity": quantities,
            },
            schema=SNAPSHOT_SCHEMA,
        )

        path = (
            self._base_dir
            / venue
            / symbol
            / now.strftime("%Y-%m-%d")
            / "snapshots"
            / f"{now.strftime('%H-%M-%S')}.parquet"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, path)
        self._total_snapshots += 1
        log.info("recorder wrote snapshot: %s (%d levels)", path, n)

    def _flush(self) -> None:
        if not self._buffer:
            self._last_flush_at = time.monotonic()
            return

        # Group buffered updates by (venue, symbol, hour). Almost always all
        # updates in a 60-second window land in the same group, but split if
        # the buffer happens to straddle an hour boundary.
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
                # A new hour writer for (venue, symbol) means any previously-
                # open writer for the same (venue, symbol) at an older hour
                # should be closed now — its hour is over. Without this,
                # writers stay open until stop(), and an ungraceful kill
                # (unattended-upgrades restart, OOM, etc.) leaves every
                # open file without a Parquet footer. Closing eagerly at
                # hour rollover limits the at-risk window to the currently-
                # being-written hour for any single (venue, symbol).
                self._close_superseded_writers(key)
            writer.write_table(_to_table(updates))
            self._total_flushed += len(updates)

        self._buffer.clear()
        self._last_flush_at = time.monotonic()

    def _close_superseded_writers(self, current: _WriterKey) -> None:
        cur_venue, cur_symbol, cur_date, cur_hour = current
        to_close: list[_WriterKey] = []
        for key in self._writers:
            if key == current:
                continue
            venue, symbol, date, hour = key
            if venue != cur_venue or symbol != cur_symbol:
                continue
            if (date, hour) < (cur_date, cur_hour):
                to_close.append(key)
        for key in to_close:
            writer = self._writers.pop(key)
            writer.close()
            log.info(
                "recorder closed %s/%s/%s/%02d.parquet (hour rollover)",
                key[0], key[1], key[2], key[3],
            )
