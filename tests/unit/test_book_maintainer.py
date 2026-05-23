import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import pytest

from vortexec.core.types import BookUpdate, Diff, Level, Side, Snapshot
from vortexec.maintainer.book_maintainer import BookMaintainer
from vortexec.venues.base import SequenceGapError, VenueConnector


def _ts() -> datetime:
    return datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)


@dataclass
class FakeSession:
    snapshot: Snapshot
    diffs: list[Diff]
    # Default "hang" matches production semantics: the maintainer's _run loop
    # auto-reconnects on stream end, so a "complete" session would just trigger
    # endless re-bootstrap attempts against a fixture with no more sessions.
    # Tests assert state after diffs are applied; "hang" lets them stop()
    # cleanly without that retry storm. Set "gap" or "complete" explicitly.
    end: Literal["complete", "gap", "hang"] = "hang"


class FakeVenueConnector(VenueConnector):
    """In-memory VenueConnector driven by a script of sessions.

    Each session corresponds to one fetch_snapshot + one stream_diffs cycle.
    ``end`` controls how a session terminates:
      - "complete" (default): stream exhausts naturally
      - "gap": raise SequenceGapError after the diffs (triggers maintainer resync)
      - "hang": block forever after the diffs (for cancellation tests)
    """

    def __init__(self, sessions: list[FakeSession]) -> None:
        self._sessions = sessions
        self._fetch_idx = 0
        self._stream_idx = 0
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.all_sessions_done = asyncio.Event()

    @property
    def fetch_calls(self) -> int:
        return self._fetch_idx

    @property
    def stream_calls(self) -> int:
        return self._stream_idx

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def fetch_snapshot(self, symbol: str) -> Snapshot:
        idx = self._fetch_idx
        self._fetch_idx += 1
        return self._sessions[idx].snapshot

    async def _stream(self, idx: int) -> AsyncIterator[Diff]:
        session = self._sessions[idx]
        for d in session.diffs:
            yield d
        if idx == len(self._sessions) - 1:
            self.all_sessions_done.set()
        if session.end == "gap":
            raise SequenceGapError("simulated gap")
        if session.end == "hang":
            await asyncio.Event().wait()

    def stream_diffs(self, symbol: str) -> AsyncIterator[Diff]:
        idx = self._stream_idx
        self._stream_idx += 1
        return self._stream(idx)


def _single(
    snapshot: Snapshot,
    diffs: list[Diff],
    end: Literal["complete", "gap", "hang"] = "hang",
) -> FakeVenueConnector:
    return FakeVenueConnector(sessions=[FakeSession(snapshot, diffs, end=end)])


async def test_maintainer_applies_snapshot_on_start() -> None:
    snapshot = Snapshot(
        bids=[Level(price=99.0, quantity=1.0)],
        asks=[Level(price=101.0, quantity=1.0)],
        timestamp=_ts(),
    )
    fake = _single(snapshot=snapshot, diffs=[])
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")

    await maintainer.start()
    await fake.all_sessions_done.wait()
    await maintainer.stop()

    book = maintainer.get_book()
    assert book.best_bid() == 99.0
    assert book.best_ask() == 101.0
    assert fake.connect_calls == 1
    assert fake.fetch_calls == 1
    assert fake.disconnect_calls == 1


async def test_maintainer_applies_diffs_after_snapshot() -> None:
    snapshot = Snapshot(
        bids=[Level(price=99.0, quantity=1.0)],
        asks=[Level(price=101.0, quantity=1.0)],
        timestamp=_ts(),
    )
    diffs = [
        Diff(side=Side.BUY, price=100.0, quantity=2.0, timestamp=_ts()),
        Diff(side=Side.SELL, price=100.5, quantity=3.0, timestamp=_ts()),
    ]
    fake = _single(snapshot=snapshot, diffs=diffs)
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")

    await maintainer.start()
    await fake.all_sessions_done.wait()
    await maintainer.stop()

    book = maintainer.get_book()
    assert book.best_bid() == 100.0
    assert book.best_ask() == 100.5


async def test_maintainer_diff_zero_quantity_removes_level() -> None:
    snapshot = Snapshot(
        bids=[Level(price=99.0, quantity=1.0), Level(price=98.0, quantity=2.0)],
        asks=[Level(price=101.0, quantity=1.0)],
        timestamp=_ts(),
    )
    diffs = [Diff(side=Side.BUY, price=99.0, quantity=0.0, timestamp=_ts())]
    fake = _single(snapshot=snapshot, diffs=diffs)
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")

    await maintainer.start()
    await fake.all_sessions_done.wait()
    await maintainer.stop()

    assert maintainer.get_book().best_bid() == 98.0


async def test_maintainer_start_is_idempotent() -> None:
    snapshot = Snapshot(bids=[], asks=[], timestamp=_ts())
    fake = _single(snapshot=snapshot, diffs=[])
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")

    await maintainer.start()
    await maintainer.start()
    await fake.all_sessions_done.wait()
    await maintainer.stop()

    assert fake.connect_calls == 1
    assert fake.fetch_calls == 1


async def test_maintainer_stop_cancels_task_when_stream_is_open() -> None:
    snapshot = Snapshot(bids=[], asks=[], timestamp=_ts())
    fake = _single(snapshot=snapshot, diffs=[], end="hang")
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")

    await maintainer.start()
    await fake.all_sessions_done.wait()
    await maintainer.stop()

    assert fake.disconnect_calls == 1


async def test_maintainer_stop_without_start_still_disconnects() -> None:
    snapshot = Snapshot(bids=[], asks=[], timestamp=_ts())
    fake = _single(snapshot=snapshot, diffs=[])
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")

    await maintainer.stop()

    assert fake.connect_calls == 0
    assert fake.disconnect_calls == 1


async def test_subscriber_receives_book_updates_for_each_diff() -> None:
    snapshot = Snapshot(bids=[], asks=[], timestamp=_ts())
    diffs = [
        Diff(side=Side.BUY, price=100.0, quantity=1.0, timestamp=_ts()),
        Diff(side=Side.SELL, price=101.0, quantity=2.0, timestamp=_ts()),
    ]
    fake = _single(snapshot=snapshot, diffs=diffs)
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")

    sub = maintainer.stream_updates()

    await maintainer.start()
    await fake.all_sessions_done.wait()
    await maintainer.stop()

    received = [u async for u in sub]

    assert len(received) == 2
    assert received[0] == BookUpdate(venue="binance", symbol="BTCUSDT", diff=diffs[0])
    assert received[1] == BookUpdate(venue="binance", symbol="BTCUSDT", diff=diffs[1])


async def test_multiple_subscribers_each_receive_all_updates() -> None:
    snapshot = Snapshot(bids=[], asks=[], timestamp=_ts())
    diffs = [
        Diff(side=Side.BUY, price=100.0, quantity=1.0, timestamp=_ts()),
        Diff(side=Side.SELL, price=101.0, quantity=1.0, timestamp=_ts()),
        Diff(side=Side.BUY, price=99.5, quantity=2.0, timestamp=_ts()),
    ]
    fake = _single(snapshot=snapshot, diffs=diffs)
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")

    sub_a = maintainer.stream_updates()
    sub_b = maintainer.stream_updates()

    await maintainer.start()
    await fake.all_sessions_done.wait()
    await maintainer.stop()

    a_received = [u async for u in sub_a]
    b_received = [u async for u in sub_b]

    assert len(a_received) == 3
    assert len(b_received) == 3
    assert a_received == b_received


async def test_subscriber_unsubscribes_when_iteration_completes() -> None:
    snapshot = Snapshot(bids=[], asks=[], timestamp=_ts())
    diffs = [Diff(side=Side.BUY, price=100.0, quantity=1.0, timestamp=_ts())]
    fake = _single(snapshot=snapshot, diffs=diffs)
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")

    sub = maintainer.stream_updates()
    assert len(maintainer._subscriber_queues) == 1

    await maintainer.start()
    await fake.all_sessions_done.wait()
    await maintainer.stop()

    [u async for u in sub]

    assert len(maintainer._subscriber_queues) == 0


async def test_publish_does_not_block_when_no_subscribers() -> None:
    snapshot = Snapshot(bids=[], asks=[], timestamp=_ts())
    diffs = [
        Diff(side=Side.BUY, price=100.0, quantity=1.0, timestamp=_ts())
        for _ in range(5)
    ]
    fake = _single(snapshot=snapshot, diffs=diffs)
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")

    await maintainer.start()
    await fake.all_sessions_done.wait()
    await maintainer.stop()

    assert fake.disconnect_calls == 1


async def test_slow_subscriber_drops_excess_updates() -> None:
    snapshot = Snapshot(bids=[], asks=[], timestamp=_ts())
    diffs = [
        Diff(side=Side.BUY, price=100.0 + i, quantity=1.0, timestamp=_ts())
        for i in range(20)
    ]
    fake = _single(snapshot=snapshot, diffs=diffs)
    maintainer = BookMaintainer(
        connector=fake,
        venue="binance",
        symbol="BTCUSDT",
        subscriber_queue_size=3,
    )

    sub = maintainer.stream_updates()

    await maintainer.start()
    await fake.all_sessions_done.wait()
    await maintainer.stop()

    received = [u async for u in sub]
    assert len(received) <= 3
    # The remaining 17+ updates were dropped silently from this subscriber.
    assert maintainer.drop_count >= 17


async def test_is_healthy_false_before_any_updates() -> None:
    snapshot = Snapshot(bids=[], asks=[], timestamp=_ts())
    fake = _single(snapshot=snapshot, diffs=[])
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")

    assert not maintainer.is_healthy()


async def test_is_healthy_true_after_recent_update() -> None:
    snapshot = Snapshot(
        bids=[Level(price=99.0, quantity=1.0)],
        asks=[Level(price=101.0, quantity=1.0)],
        timestamp=_ts(),
    )
    fake = _single(snapshot=snapshot, diffs=[])
    maintainer = BookMaintainer(
        connector=fake,
        venue="binance",
        symbol="BTCUSDT",
        staleness_threshold_seconds=60.0,
    )

    await maintainer.start()
    await fake.all_sessions_done.wait()

    assert maintainer.is_healthy()

    await maintainer.stop()


async def test_is_healthy_false_after_staleness_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        "vortexec.maintainer.book_maintainer.time.monotonic",
        lambda: clock[0],
    )

    snapshot = Snapshot(
        bids=[Level(price=99.0, quantity=1.0)],
        asks=[Level(price=101.0, quantity=1.0)],
        timestamp=_ts(),
    )
    fake = _single(snapshot=snapshot, diffs=[])
    maintainer = BookMaintainer(
        connector=fake,
        venue="binance",
        symbol="BTCUSDT",
        staleness_threshold_seconds=5.0,
    )

    await maintainer.start()
    await fake.all_sessions_done.wait()

    assert maintainer.is_healthy()  # last update at fake time 0.0

    clock[0] = 10.0  # advance past the 5s threshold
    assert not maintainer.is_healthy()

    await maintainer.stop()


async def test_resync_after_sequence_gap_rebuilds_book_from_new_snapshot() -> None:
    snapshot1 = Snapshot(
        bids=[Level(price=99.0, quantity=1.0)],
        asks=[Level(price=101.0, quantity=1.0)],
        timestamp=_ts(),
    )
    diffs1 = [Diff(side=Side.BUY, price=100.0, quantity=1.0, timestamp=_ts())]

    # After the gap, a completely different snapshot resets the book.
    snapshot2 = Snapshot(
        bids=[Level(price=50.0, quantity=10.0)],
        asks=[Level(price=200.0, quantity=10.0)],
        timestamp=_ts(),
    )
    diffs2 = [Diff(side=Side.SELL, price=150.0, quantity=2.0, timestamp=_ts())]

    fake = FakeVenueConnector(
        sessions=[
            FakeSession(snapshot=snapshot1, diffs=diffs1, end="gap"),
            FakeSession(snapshot=snapshot2, diffs=diffs2),
        ]
    )
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")

    await maintainer.start()
    await fake.all_sessions_done.wait()
    await maintainer.stop()

    book = maintainer.get_book()
    # Book reflects snapshot2 + diffs2; nothing from session 1 should remain.
    assert book.best_bid() == 50.0
    assert book.best_ask() == 150.0  # diff2 tightened the ask
    assert maintainer.resync_count == 1
    assert fake.fetch_calls == 2  # initial + resync


async def test_resync_publishes_updates_from_both_sessions() -> None:
    snapshot1 = Snapshot(bids=[], asks=[], timestamp=_ts())
    diffs1 = [Diff(side=Side.BUY, price=100.0, quantity=1.0, timestamp=_ts())]
    snapshot2 = Snapshot(bids=[], asks=[], timestamp=_ts())
    diffs2 = [
        Diff(side=Side.BUY, price=200.0, quantity=2.0, timestamp=_ts()),
        Diff(side=Side.SELL, price=300.0, quantity=3.0, timestamp=_ts()),
    ]
    fake = FakeVenueConnector(
        sessions=[
            FakeSession(snapshot=snapshot1, diffs=diffs1, end="gap"),
            FakeSession(snapshot=snapshot2, diffs=diffs2),
        ]
    )
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")

    sub = maintainer.stream_updates()

    await maintainer.start()
    await fake.all_sessions_done.wait()
    await maintainer.stop()

    received = [u async for u in sub]
    # 1 diff before gap + 2 after = 3 updates total
    assert len(received) == 3
    assert received[0].diff.price == 100.0
    assert received[1].diff.price == 200.0
    assert received[2].diff.price == 300.0


async def test_multiple_resyncs_increment_counter() -> None:
    snapshot = Snapshot(bids=[], asks=[], timestamp=_ts())
    diffs = [Diff(side=Side.BUY, price=100.0, quantity=1.0, timestamp=_ts())]
    fake = FakeVenueConnector(
        sessions=[
            FakeSession(snapshot=snapshot, diffs=diffs, end="gap"),
            FakeSession(snapshot=snapshot, diffs=diffs, end="gap"),
            FakeSession(snapshot=snapshot, diffs=diffs),
        ]
    )
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")

    await maintainer.start()
    await fake.all_sessions_done.wait()
    await maintainer.stop()

    assert maintainer.resync_count == 2
    assert fake.fetch_calls == 3
