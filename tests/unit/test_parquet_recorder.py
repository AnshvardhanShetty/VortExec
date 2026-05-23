from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from vortexec.core.types import BookUpdate, Diff, Side
from vortexec.recorder.parquet_recorder import ParquetRecorder, _hour_path


def _update(
    *,
    venue: str = "binance",
    symbol: str = "BTCUSDT",
    side: Side = Side.BUY,
    price: float = 100.0,
    quantity: float = 1.0,
    timestamp: datetime | None = None,
) -> BookUpdate:
    if timestamp is None:
        timestamp = datetime(2026, 5, 13, 14, 30, tzinfo=timezone.utc)
    return BookUpdate(
        venue=venue,
        symbol=symbol,
        diff=Diff(side=side, price=price, quantity=quantity, timestamp=timestamp),
    )


def test_hour_path_layout(tmp_path: Path) -> None:
    ts_ms = int(datetime(2026, 5, 13, 14, 30, tzinfo=timezone.utc).timestamp() * 1000)
    path = _hour_path(tmp_path, "binance", "BTCUSDT", ts_ms)
    assert path == tmp_path / "binance" / "BTCUSDT" / "2026-05-13" / "14.parquet"


def test_record_does_not_flush_below_threshold(tmp_path: Path) -> None:
    rec = ParquetRecorder(
        tmp_path, flush_interval_seconds=10_000, flush_after_updates=10
    )
    rec.record(_update())
    rec.record(_update())
    rec.record(_update())
    assert rec.total_recorded == 3
    assert rec.total_flushed == 0
    # No file written yet
    assert list(tmp_path.rglob("*.parquet")) == []


def test_record_flushes_when_count_threshold_reached(tmp_path: Path) -> None:
    rec = ParquetRecorder(
        tmp_path, flush_interval_seconds=10_000, flush_after_updates=3
    )
    rec.record(_update(price=100.0))
    rec.record(_update(price=101.0))
    rec.record(_update(price=102.0))  # triggers flush
    assert rec.total_recorded == 3
    assert rec.total_flushed == 3


@pytest.mark.asyncio
async def test_stop_flushes_remaining_buffer_and_closes_files(tmp_path: Path) -> None:
    rec = ParquetRecorder(
        tmp_path, flush_interval_seconds=10_000, flush_after_updates=10_000
    )
    for i in range(5):
        rec.record(_update(price=100.0 + i))
    assert rec.total_flushed == 0  # not flushed yet

    await rec.stop()

    assert rec.total_flushed == 5
    # Exactly one file written, at the correct hour path
    files = list(tmp_path.rglob("*.parquet"))
    assert len(files) == 1
    expected = (
        tmp_path / "binance" / "BTCUSDT" / "2026-05-13" / "14.parquet"
    )
    assert files[0] == expected

    # File should be readable end-to-end (footer present)
    table = pq.read_table(expected)
    assert table.num_rows == 5
    assert table.column("price").to_pylist() == [100.0, 101.0, 102.0, 103.0, 104.0]
    assert table.column("venue").to_pylist() == ["binance"] * 5
    assert table.column("symbol").to_pylist() == ["BTCUSDT"] * 5
    assert table.column("side").to_pylist() == ["buy"] * 5


@pytest.mark.asyncio
async def test_recorder_routes_updates_to_correct_hour_files(tmp_path: Path) -> None:
    rec = ParquetRecorder(
        tmp_path, flush_interval_seconds=10_000, flush_after_updates=10_000
    )
    # Two updates in different hours
    rec.record(
        _update(
            price=100.0,
            timestamp=datetime(2026, 5, 13, 14, 59, tzinfo=timezone.utc),
        )
    )
    rec.record(
        _update(
            price=200.0,
            timestamp=datetime(2026, 5, 13, 15, 1, tzinfo=timezone.utc),
        )
    )
    await rec.stop()

    file_14 = tmp_path / "binance" / "BTCUSDT" / "2026-05-13" / "14.parquet"
    file_15 = tmp_path / "binance" / "BTCUSDT" / "2026-05-13" / "15.parquet"
    assert file_14.exists()
    assert file_15.exists()

    assert pq.read_table(file_14).column("price").to_pylist() == [100.0]
    assert pq.read_table(file_15).column("price").to_pylist() == [200.0]


@pytest.mark.asyncio
async def test_recorder_separates_by_venue_and_symbol(tmp_path: Path) -> None:
    rec = ParquetRecorder(
        tmp_path, flush_interval_seconds=10_000, flush_after_updates=10_000
    )
    rec.record(_update(venue="binance", symbol="BTCUSDT", price=100.0))
    rec.record(_update(venue="binance", symbol="ETHUSDT", price=3.0))
    rec.record(_update(venue="okx", symbol="BTCUSDT", price=101.0))
    await rec.stop()

    paths = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*.parquet"))
    assert paths == [
        Path("binance/BTCUSDT/2026-05-13/14.parquet"),
        Path("binance/ETHUSDT/2026-05-13/14.parquet"),
        Path("okx/BTCUSDT/2026-05-13/14.parquet"),
    ]


@pytest.mark.asyncio
async def test_recorder_consumes_from_maintainer(tmp_path: Path) -> None:
    # Integration-ish: spin up a real BookMaintainer fed by FakeVenueConnector,
    # subscribe the recorder, run until the fake stream ends, verify file.
    import asyncio

    from tests.unit.test_book_maintainer import FakeSession, FakeVenueConnector
    from vortexec.core.types import Level, Snapshot
    from vortexec.maintainer.book_maintainer import BookMaintainer

    snapshot = Snapshot(bids=[], asks=[], timestamp=datetime(2026, 5, 13, 14, 30, tzinfo=timezone.utc))
    diffs = [
        Diff(
            side=Side.BUY,
            price=100.0 + i,
            quantity=1.0,
            timestamp=datetime(2026, 5, 13, 14, 30, tzinfo=timezone.utc),
        )
        for i in range(7)
    ]
    fake = FakeVenueConnector(
        sessions=[FakeSession(snapshot=snapshot, diffs=diffs)]
    )
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")
    recorder = ParquetRecorder(
        tmp_path, flush_interval_seconds=10_000, flush_after_updates=10_000
    )

    # Subscriber must register before start so it sees every update.
    await recorder.start(maintainer)
    await maintainer.start()
    await fake.all_sessions_done.wait()
    # Give the recorder one event-loop tick to consume the final updates.
    await asyncio.sleep(0)
    await maintainer.stop()
    await recorder.stop()

    files = list(tmp_path.rglob("*.parquet"))
    assert len(files) == 1
    table = pq.read_table(files[0])
    # The recorder may or may not have caught the last update depending on
    # event-loop scheduling; allow for that race in the assertion.
    assert table.num_rows in (6, 7)
    assert table.column("price").to_pylist()[:6] == [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]


def test_stop_with_empty_buffer_is_safe(tmp_path: Path) -> None:
    import asyncio

    rec = ParquetRecorder(tmp_path)
    asyncio.run(rec.stop())
    assert list(tmp_path.rglob("*.parquet")) == []


@pytest.mark.asyncio
async def test_snapshot_writes_full_book_state(tmp_path: Path) -> None:
    """Run a maintainer for long enough for the snapshot task to fire at least
    once, then verify the on-disk snapshot reflects the maintained book.
    """
    import asyncio

    from tests.unit.test_book_maintainer import FakeSession, FakeVenueConnector
    from vortexec.core.types import Level, Snapshot
    from vortexec.maintainer.book_maintainer import BookMaintainer

    ts = datetime(2026, 5, 13, 14, 30, tzinfo=timezone.utc)
    snapshot = Snapshot(
        bids=[Level(price=99.0, quantity=1.0), Level(price=98.0, quantity=2.0)],
        asks=[Level(price=101.0, quantity=3.0)],
        timestamp=ts,
    )
    fake = FakeVenueConnector(
        sessions=[FakeSession(snapshot=snapshot, diffs=[], end="hang")]
    )
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")
    recorder = ParquetRecorder(
        tmp_path,
        flush_interval_seconds=10_000,
        flush_after_updates=10_000,
        snapshot_interval_seconds=0.05,  # 50ms — short enough for tests
    )

    await recorder.start(maintainer)
    await maintainer.start()
    await fake.all_sessions_done.wait()  # snapshot applied, ready
    await asyncio.sleep(0.12)  # let at least one snapshot task fire
    await maintainer.stop()
    await recorder.stop()

    snapshot_dir = tmp_path / "binance" / "BTCUSDT"
    snap_files = sorted(snapshot_dir.rglob("snapshots/*.parquet"))
    assert len(snap_files) >= 1, f"expected ≥1 snapshot file, got {snap_files}"
    assert recorder.total_snapshots >= 1

    table = pq.read_table(snap_files[0])
    # 2 bids + 1 ask = 3 rows
    assert table.num_rows == 3
    # Bids first, then asks (per our build order)
    sides = table.column("side").to_pylist()
    prices = table.column("price").to_pylist()
    qtys = table.column("quantity").to_pylist()
    assert sides == ["buy", "buy", "sell"]
    # SortedDict iterates ascending: bids 98.0, 99.0 ; asks 101.0
    assert prices == [98.0, 99.0, 101.0]
    assert qtys == [2.0, 1.0, 3.0]
    # All rows share the snapshot timestamp
    assert len(set(table.column("snapshot_ts_ms").to_pylist())) == 1


@pytest.mark.asyncio
async def test_snapshot_skipped_when_book_empty(tmp_path: Path) -> None:
    """If the book has no levels yet, the snapshot task should not write a file."""
    import asyncio

    from tests.unit.test_book_maintainer import FakeSession, FakeVenueConnector
    from vortexec.core.types import Snapshot
    from vortexec.maintainer.book_maintainer import BookMaintainer

    empty = Snapshot(
        bids=[], asks=[],
        timestamp=datetime(2026, 5, 13, 14, 30, tzinfo=timezone.utc),
    )
    fake = FakeVenueConnector(
        sessions=[FakeSession(snapshot=empty, diffs=[], end="hang")]
    )
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")
    recorder = ParquetRecorder(
        tmp_path,
        flush_interval_seconds=10_000,
        flush_after_updates=10_000,
        snapshot_interval_seconds=0.03,
    )

    await recorder.start(maintainer)
    await maintainer.start()
    await fake.all_sessions_done.wait()
    await asyncio.sleep(0.1)  # let a few snapshot tries happen
    await maintainer.stop()
    await recorder.stop()

    snap_files = list(tmp_path.rglob("snapshots/*.parquet"))
    assert snap_files == []
    assert recorder.total_snapshots == 0


def test_hour_rollover_closes_previous_writer(tmp_path: Path) -> None:
    """When a new hour's writer opens for a given (venue, symbol), the
    previous hour's writer must be closed eagerly so its file has a valid
    Parquet footer even if the process is later killed ungracefully.
    """
    rec = ParquetRecorder(
        tmp_path, flush_interval_seconds=10_000, flush_after_updates=10_000
    )
    # First flush writes 14:00 hour
    rec.record(
        _update(price=100.0, timestamp=datetime(2026, 5, 16, 14, 30, tzinfo=timezone.utc))
    )
    rec._flush()
    file_14 = tmp_path / "binance" / "BTCUSDT" / "2026-05-16" / "14.parquet"
    assert file_14.exists()

    # Second flush writes 15:00 hour. This should trigger the rollover.
    rec.record(
        _update(price=200.0, timestamp=datetime(2026, 5, 16, 15, 5, tzinfo=timezone.utc))
    )
    rec._flush()
    file_15 = tmp_path / "binance" / "BTCUSDT" / "2026-05-16" / "15.parquet"
    assert file_15.exists()

    # CRITICAL: the 14.parquet file should now be readable end-to-end
    # WITHOUT having to call rec.stop() first — because the rollover
    # closed its writer eagerly.
    table = pq.read_table(file_14)
    assert table.column("price").to_pylist() == [100.0]
    # And the 14:00 writer should no longer be in the open-writers dict
    assert all(k[3] != 14 for k in rec._writers)


@pytest.mark.asyncio
async def test_snapshot_disabled_when_interval_is_zero(tmp_path: Path) -> None:
    """snapshot_interval_seconds=0 should disable the snapshot task entirely."""
    import asyncio

    from tests.unit.test_book_maintainer import FakeSession, FakeVenueConnector
    from vortexec.core.types import Level, Snapshot
    from vortexec.maintainer.book_maintainer import BookMaintainer

    snapshot = Snapshot(
        bids=[Level(price=99.0, quantity=1.0)],
        asks=[Level(price=101.0, quantity=1.0)],
        timestamp=datetime(2026, 5, 13, 14, 30, tzinfo=timezone.utc),
    )
    fake = FakeVenueConnector(
        sessions=[FakeSession(snapshot=snapshot, diffs=[], end="hang")]
    )
    maintainer = BookMaintainer(connector=fake, venue="binance", symbol="BTCUSDT")
    recorder = ParquetRecorder(tmp_path, snapshot_interval_seconds=0)

    await recorder.start(maintainer)
    await maintainer.start()
    await fake.all_sessions_done.wait()
    await asyncio.sleep(0.1)
    await maintainer.stop()
    await recorder.stop()

    assert recorder.total_snapshots == 0
    assert list(tmp_path.rglob("snapshots/*.parquet")) == []
