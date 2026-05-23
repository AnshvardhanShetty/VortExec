from datetime import datetime, timezone

import pytest

from vortexec.core.book import OrderBook
from vortexec.core.types import Diff, Level, Side, Snapshot


def _ts() -> datetime:
    return datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)


def test_empty_book_has_no_bids_or_asks() -> None:
    book = OrderBook()
    assert len(book._bids) == 0
    assert len(book._asks) == 0


def test_apply_snapshot_loads_bids_and_asks() -> None:
    book = OrderBook()
    snap = Snapshot(
        bids=[Level(price=99.0, quantity=1.0), Level(price=98.0, quantity=2.0)],
        asks=[Level(price=101.0, quantity=1.5), Level(price=102.0, quantity=3.0)],
        timestamp=_ts(),
    )
    book.apply_snapshot(snap)
    assert dict(book._bids) == {99.0: 1.0, 98.0: 2.0}
    assert dict(book._asks) == {101.0: 1.5, 102.0: 3.0}


def test_apply_snapshot_ignores_zero_quantity_levels() -> None:
    book = OrderBook()
    snap = Snapshot(
        bids=[Level(price=99.0, quantity=1.0), Level(price=98.0, quantity=0.0)],
        asks=[Level(price=101.0, quantity=0.0), Level(price=102.0, quantity=3.0)],
        timestamp=_ts(),
    )
    book.apply_snapshot(snap)
    assert dict(book._bids) == {99.0: 1.0}
    assert dict(book._asks) == {102.0: 3.0}


def test_apply_snapshot_replaces_previous_data() -> None:
    book = OrderBook()
    first = Snapshot(
        bids=[Level(price=99.0, quantity=1.0), Level(price=98.0, quantity=2.0)],
        asks=[Level(price=101.0, quantity=1.0)],
        timestamp=_ts(),
    )
    book.apply_snapshot(first)
    second = Snapshot(
        bids=[Level(price=95.0, quantity=5.0)],
        asks=[Level(price=105.0, quantity=5.0), Level(price=106.0, quantity=1.0)],
        timestamp=_ts(),
    )
    book.apply_snapshot(second)
    assert dict(book._bids) == {95.0: 5.0}
    assert dict(book._asks) == {105.0: 5.0, 106.0: 1.0}


def test_apply_empty_snapshot_leaves_empty_book() -> None:
    book = OrderBook()
    book.apply_snapshot(Snapshot(bids=[], asks=[], timestamp=_ts()))
    assert len(book._bids) == 0
    assert len(book._asks) == 0


def test_empty_book_best_bid_and_ask_are_none() -> None:
    book = OrderBook()
    assert book.best_bid() is None
    assert book.best_ask() is None


def test_empty_book_mid_and_spread_are_none() -> None:
    book = OrderBook()
    assert book.mid() is None
    assert book.spread() is None


def test_best_bid_returns_highest_bid_price() -> None:
    book = OrderBook()
    book.apply_snapshot(
        Snapshot(
            bids=[
                Level(price=98.0, quantity=1.0),
                Level(price=99.5, quantity=2.0),
                Level(price=97.0, quantity=3.0),
            ],
            asks=[Level(price=101.0, quantity=1.0)],
            timestamp=_ts(),
        )
    )
    assert book.best_bid() == 99.5


def test_best_ask_returns_lowest_ask_price() -> None:
    book = OrderBook()
    book.apply_snapshot(
        Snapshot(
            bids=[Level(price=99.0, quantity=1.0)],
            asks=[
                Level(price=102.0, quantity=1.0),
                Level(price=100.5, quantity=2.0),
                Level(price=103.0, quantity=3.0),
            ],
            timestamp=_ts(),
        )
    )
    assert book.best_ask() == 100.5


def test_mid_is_average_of_best_bid_and_ask() -> None:
    book = OrderBook()
    book.apply_snapshot(
        Snapshot(
            bids=[Level(price=99.0, quantity=1.0)],
            asks=[Level(price=101.0, quantity=1.0)],
            timestamp=_ts(),
        )
    )
    assert book.mid() == 100.0


def test_spread_is_best_ask_minus_best_bid() -> None:
    book = OrderBook()
    book.apply_snapshot(
        Snapshot(
            bids=[Level(price=99.0, quantity=1.0)],
            asks=[Level(price=101.5, quantity=1.0)],
            timestamp=_ts(),
        )
    )
    assert book.spread() == pytest.approx(2.5)


def test_mid_and_spread_are_none_when_only_bids_present() -> None:
    book = OrderBook()
    book.apply_snapshot(
        Snapshot(
            bids=[Level(price=99.0, quantity=1.0)],
            asks=[],
            timestamp=_ts(),
        )
    )
    assert book.best_bid() == 99.0
    assert book.best_ask() is None
    assert book.mid() is None
    assert book.spread() is None


def test_mid_and_spread_are_none_when_only_asks_present() -> None:
    book = OrderBook()
    book.apply_snapshot(
        Snapshot(
            bids=[],
            asks=[Level(price=101.0, quantity=1.0)],
            timestamp=_ts(),
        )
    )
    assert book.best_bid() is None
    assert book.best_ask() == 101.0
    assert book.mid() is None
    assert book.spread() is None


def _diff(side: Side, price: float, quantity: float) -> Diff:
    return Diff(side=side, price=price, quantity=quantity, timestamp=_ts())


def test_apply_diff_adds_new_bid_level() -> None:
    book = OrderBook()
    book.apply_diff(_diff(Side.BUY, 100.0, 1.5))
    assert dict(book._bids) == {100.0: 1.5}
    assert len(book._asks) == 0


def test_apply_diff_adds_new_ask_level() -> None:
    book = OrderBook()
    book.apply_diff(_diff(Side.SELL, 101.0, 2.0))
    assert dict(book._asks) == {101.0: 2.0}
    assert len(book._bids) == 0


def test_apply_diff_replaces_quantity_at_existing_level() -> None:
    book = OrderBook()
    book.apply_snapshot(
        Snapshot(bids=[Level(price=99.0, quantity=1.0)], asks=[], timestamp=_ts())
    )
    book.apply_diff(_diff(Side.BUY, 99.0, 5.0))
    assert dict(book._bids) == {99.0: 5.0}


def test_apply_diff_zero_quantity_deletes_existing_level() -> None:
    book = OrderBook()
    book.apply_snapshot(
        Snapshot(
            bids=[Level(price=99.0, quantity=1.0), Level(price=98.0, quantity=2.0)],
            asks=[],
            timestamp=_ts(),
        )
    )
    book.apply_diff(_diff(Side.BUY, 99.0, 0.0))
    assert dict(book._bids) == {98.0: 2.0}


def test_apply_diff_zero_quantity_on_missing_level_is_noop() -> None:
    book = OrderBook()
    book.apply_diff(_diff(Side.BUY, 99.0, 0.0))
    book.apply_diff(_diff(Side.SELL, 101.0, 0.0))
    assert len(book._bids) == 0
    assert len(book._asks) == 0


def test_apply_diff_buy_does_not_affect_asks() -> None:
    book = OrderBook()
    book.apply_snapshot(
        Snapshot(
            bids=[],
            asks=[Level(price=101.0, quantity=1.0)],
            timestamp=_ts(),
        )
    )
    book.apply_diff(_diff(Side.BUY, 101.0, 5.0))
    assert dict(book._bids) == {101.0: 5.0}
    assert dict(book._asks) == {101.0: 1.0}
