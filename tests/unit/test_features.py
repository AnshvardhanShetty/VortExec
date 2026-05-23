from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from vortexec.core.book import OrderBook
from vortexec.core.features import Features, extract_features
from vortexec.core.types import Level, Snapshot


def _ts() -> datetime:
    return datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)


def _book(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
) -> OrderBook:
    book = OrderBook()
    book.apply_snapshot(
        Snapshot(
            bids=[Level(price=p, quantity=q) for p, q in bids],
            asks=[Level(price=p, quantity=q) for p, q in asks],
            timestamp=_ts(),
        )
    )
    return book


def test_features_is_frozen() -> None:
    f = Features(
        spread_bps=1.0,
        mid_price=100.0,
        depth_top_5_bids=1.0,
        depth_top_5_asks=1.0,
        depth_top_10_bids=2.0,
        depth_top_10_asks=2.0,
        imbalance=0.0,
    )
    with pytest.raises(FrozenInstanceError):
        f.mid_price = 200.0  # type: ignore[misc]


def test_empty_book_features_are_zero_or_none() -> None:
    f = extract_features(OrderBook())
    assert f.mid_price is None
    assert f.spread_bps is None
    assert f.imbalance is None
    assert f.depth_top_5_bids == pytest.approx(0.0)
    assert f.depth_top_5_asks == pytest.approx(0.0)
    assert f.depth_top_10_bids == pytest.approx(0.0)
    assert f.depth_top_10_asks == pytest.approx(0.0)


def test_bid_only_book_imbalance_is_plus_one() -> None:
    book = _book(bids=[(99.0, 1.0), (98.0, 2.0)], asks=[])
    f = extract_features(book)
    assert f.mid_price is None
    assert f.spread_bps is None
    assert f.depth_top_5_bids == pytest.approx(3.0)
    assert f.depth_top_5_asks == pytest.approx(0.0)
    assert f.imbalance == pytest.approx(1.0)


def test_ask_only_book_imbalance_is_minus_one() -> None:
    book = _book(bids=[], asks=[(101.0, 1.0), (102.0, 2.0)])
    f = extract_features(book)
    assert f.mid_price is None
    assert f.spread_bps is None
    assert f.depth_top_5_asks == pytest.approx(3.0)
    assert f.depth_top_5_bids == pytest.approx(0.0)
    assert f.imbalance == pytest.approx(-1.0)


def test_mid_and_spread_bps_on_two_sided_book() -> None:
    # spread 2 over mid 100 → 200 bps
    book = _book(bids=[(99.0, 5.0)], asks=[(101.0, 5.0)])
    f = extract_features(book)
    assert f.mid_price == pytest.approx(100.0)
    assert f.spread_bps == pytest.approx(200.0)


def test_depth_top_5_and_10_sum_correctly() -> None:
    # 7 bid levels at qty 1, plus 2 deeper levels at higher qty
    book = _book(
        bids=[
            (99.0, 1.0),
            (98.0, 1.0),
            (97.0, 1.0),
            (96.0, 1.0),
            (95.0, 1.0),  # ← top 5 ends here
            (94.0, 10.0),
            (93.0, 20.0),
        ],
        asks=[(101.0, 1.0)],
    )
    f = extract_features(book)
    assert f.depth_top_5_bids == pytest.approx(5.0)
    assert f.depth_top_10_bids == pytest.approx(5.0 + 10.0 + 20.0)


def test_depth_when_fewer_than_n_levels_present() -> None:
    book = _book(
        bids=[(99.0, 1.5), (98.0, 0.5)],
        asks=[(101.0, 2.0)],
    )
    f = extract_features(book)
    assert f.depth_top_5_bids == pytest.approx(2.0)
    assert f.depth_top_10_bids == pytest.approx(2.0)
    assert f.depth_top_5_asks == pytest.approx(2.0)
    assert f.depth_top_10_asks == pytest.approx(2.0)


def test_imbalance_skewed_to_bids() -> None:
    # bid depth 10, ask depth 1 → (10 - 1) / 11
    book = _book(bids=[(99.0, 10.0)], asks=[(101.0, 1.0)])
    f = extract_features(book)
    assert f.imbalance == pytest.approx(9.0 / 11.0)


def test_imbalance_uses_top_10_window() -> None:
    # Designed so top-5 imbalance ≠ top-10 imbalance, to pin down the choice of window.
    # Top 5 bids: 5*1.0 = 5; Top 5 asks: 5*1.0 = 5 → top-5 imbalance = 0
    # Top 10 bids: 5*1.0 + 5*100.0 = 505; Top 10 asks: 10*1.0 = 10 → far from 0
    bid_levels = [(100.0 - i, 1.0) for i in range(5)] + [
        (95.0 - i, 100.0) for i in range(5)
    ]
    ask_levels = [(101.0 + i, 1.0) for i in range(10)]
    book = _book(bids=bid_levels, asks=ask_levels)
    f = extract_features(book)
    expected = (505.0 - 10.0) / (505.0 + 10.0)
    assert f.imbalance == pytest.approx(expected)
