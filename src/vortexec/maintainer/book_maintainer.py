from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from vortexec.core.book import OrderBook
from vortexec.core.types import BookUpdate
from vortexec.venues.base import SequenceGapError, VenueConnector

log = logging.getLogger("vortexec.maintainer")

# Default bumped from 1024 → 8192 after observing real-world BTCUSDT drops
# in production. During flush bursts, the recorder pauses to write 5000 rows
# to disk (~100-200ms); incoming diffs accumulate in the subscriber queue
# during that pause. At BTC peak rates of several hundred diffs/sec, 1024
# slots can fill before the consumer resumes. 8192 gives ~8× headroom for
# unmeasured-volume cost (a few MB of memory per maintainer).
_DEFAULT_SUBSCRIBER_QUEUE_SIZE = 8192
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
        """Maintainer loop. Re-bootstraps indefinitely on any non-cancellation
        failure so unattended deployments survive transient WS drops, network
        blips, and gap-driven resyncs without external intervention.
        """
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
                # stop() cancelled us — propagate cleanly, do NOT retry.
                raise
            except SequenceGapError as e:
                self._resync_count += 1
                log.info(
                    "%s/%s: sequence gap (%s), rebootstrapping in %.1fs",
                    self._venue, self._symbol, e, _RECONNECT_BACKOFF_SECONDS,
                )
                await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
            except Exception as e:
                # Any other transient error (network blip, parse error, etc.):
                # log and retry rather than letting the task die. systemd
                # restart-on-death is the outer safety net for things this
                # can't recover from.
                self._resync_count += 1
                log.warning(
                    "%s/%s: unexpected error %r, rebootstrapping in %.1fs",
                    self._venue, self._symbol, e, _RECONNECT_BACKOFF_SECONDS,
                )
                await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
