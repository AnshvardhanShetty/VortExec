from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from vortexec.core.types import BookUpdate, Diff, Level, Side, Snapshot


def test_side_has_buy_and_sell() -> None:
    assert Side.BUY.value == "buy"
    assert Side.SELL.value == "sell"


def test_level_holds_price_and_quantity() -> None:
    level = Level(price=100.5, quantity=2.0)
    assert level.price == 100.5
    assert level.quantity == 2.0


def test_level_is_frozen() -> None:
    level = Level(price=100.0, quantity=1.0)
    with pytest.raises(FrozenInstanceError):
        level.price = 200.0  # type: ignore[misc]


def test_level_equality() -> None:
    assert Level(price=100.0, quantity=1.0) == Level(price=100.0, quantity=1.0)
    assert Level(price=100.0, quantity=1.0) != Level(price=100.0, quantity=2.0)


def test_snapshot_holds_bids_asks_and_timestamp() -> None:
    ts = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    bids = [Level(price=99.0, quantity=1.0), Level(price=98.0, quantity=2.0)]
    asks = [Level(price=101.0, quantity=1.5)]
    snap = Snapshot(bids=bids, asks=asks, timestamp=ts)
    assert snap.bids == bids
    assert snap.asks == asks
    assert snap.timestamp == ts


def test_snapshot_is_frozen() -> None:
    snap = Snapshot(bids=[], asks=[], timestamp=datetime(2026, 5, 8, tzinfo=timezone.utc))
    with pytest.raises(FrozenInstanceError):
        snap.bids = [Level(price=1.0, quantity=1.0)]  # type: ignore[misc]


def test_diff_holds_all_fields() -> None:
    ts = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    diff = Diff(side=Side.BUY, price=100.0, quantity=0.5, timestamp=ts)
    assert diff.side is Side.BUY
    assert diff.price == 100.0
    assert diff.quantity == 0.5
    assert diff.timestamp == ts


def test_diff_is_frozen() -> None:
    diff = Diff(
        side=Side.SELL,
        price=100.0,
        quantity=1.0,
        timestamp=datetime(2026, 5, 8, tzinfo=timezone.utc),
    )
    with pytest.raises(FrozenInstanceError):
        diff.price = 200.0  # type: ignore[misc]


def test_book_update_holds_fields() -> None:
    diff = Diff(
        side=Side.BUY,
        price=100.0,
        quantity=1.0,
        timestamp=datetime(2026, 5, 8, tzinfo=timezone.utc),
    )
    update = BookUpdate(venue="binance", symbol="BTCUSDT", diff=diff)
    assert update.venue == "binance"
    assert update.symbol == "BTCUSDT"
    assert update.diff == diff


def test_book_update_is_frozen() -> None:
    diff = Diff(
        side=Side.BUY,
        price=100.0,
        quantity=1.0,
        timestamp=datetime(2026, 5, 8, tzinfo=timezone.utc),
    )
    update = BookUpdate(venue="binance", symbol="BTCUSDT", diff=diff)
    with pytest.raises(FrozenInstanceError):
        update.venue = "okx"  # type: ignore[misc]
