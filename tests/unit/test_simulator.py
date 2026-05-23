from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from vortexec.core.book import OrderBook
from vortexec.core.simulator import SimResult, simulate_market_order
from vortexec.core.types import Level, Side, Snapshot


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


def test_sim_result_is_frozen() -> None:
    result = SimResult(avg_price=100.0, slippage_bps=1.0, unfilled_qty=0.0, levels_consumed=1)
    with pytest.raises(FrozenInstanceError):
        result.avg_price = 200.0  # type: ignore[misc]


def test_buy_fills_first_ask_level() -> None:
    book = _book(bids=[(99.0, 1.0)], asks=[(101.0, 5.0), (102.0, 3.0)])
    result = simulate_market_order(book, Side.BUY, 2.0)
    assert result.avg_price == pytest.approx(101.0)
    assert result.unfilled_qty == pytest.approx(0.0)
    assert result.levels_consumed == 1


def test_buy_walks_multiple_ask_levels() -> None:
    book = _book(bids=[(99.0, 1.0)], asks=[(101.0, 1.0), (102.0, 2.0), (103.0, 5.0)])
    result = simulate_market_order(book, Side.BUY, 4.0)
    # 1@101 + 2@102 + 1@103 = 408 over 4 units = 102.0
    assert result.avg_price == pytest.approx(102.0)
    assert result.unfilled_qty == pytest.approx(0.0)
    assert result.levels_consumed == 3


def test_buy_partially_fills_when_book_runs_out() -> None:
    book = _book(bids=[(99.0, 1.0)], asks=[(101.0, 1.0), (102.0, 2.0)])
    result = simulate_market_order(book, Side.BUY, 10.0)
    # 1@101 + 2@102 = 305 over 3 units
    assert result.avg_price == pytest.approx(305.0 / 3)
    assert result.unfilled_qty == pytest.approx(7.0)
    assert result.levels_consumed == 2


def test_buy_against_empty_asks_returns_no_fill() -> None:
    book = _book(bids=[(99.0, 1.0)], asks=[])
    result = simulate_market_order(book, Side.BUY, 1.0)
    assert result.avg_price is None
    assert result.slippage_bps is None
    assert result.unfilled_qty == pytest.approx(1.0)
    assert result.levels_consumed == 0


def test_sell_walks_bids_highest_first() -> None:
    book = _book(
        bids=[(98.0, 1.0), (99.0, 1.0), (97.0, 5.0)],
        asks=[(101.0, 1.0)],
    )
    result = simulate_market_order(book, Side.SELL, 2.5)
    # 1@99 + 1@98 + 0.5@97 = 245.5 over 2.5 units = 98.2
    assert result.avg_price == pytest.approx(98.2)
    assert result.unfilled_qty == pytest.approx(0.0)
    assert result.levels_consumed == 3


def test_sell_against_empty_bids_returns_no_fill() -> None:
    book = _book(bids=[], asks=[(101.0, 1.0)])
    result = simulate_market_order(book, Side.SELL, 1.0)
    assert result.avg_price is None
    assert result.slippage_bps is None
    assert result.unfilled_qty == pytest.approx(1.0)
    assert result.levels_consumed == 0


def test_buy_slippage_is_positive_above_mid() -> None:
    # mid = 100, buy at 101 → (101 - 100) / 100 * 10000 = 100 bps
    book = _book(bids=[(99.0, 5.0)], asks=[(101.0, 5.0)])
    result = simulate_market_order(book, Side.BUY, 1.0)
    assert result.avg_price == pytest.approx(101.0)
    assert result.slippage_bps == pytest.approx(100.0)


def test_sell_slippage_is_positive_below_mid() -> None:
    # mid = 100, sell at 99 → (100 - 99) / 100 * 10000 = 100 bps
    book = _book(bids=[(99.0, 5.0)], asks=[(101.0, 5.0)])
    result = simulate_market_order(book, Side.SELL, 1.0)
    assert result.avg_price == pytest.approx(99.0)
    assert result.slippage_bps == pytest.approx(100.0)


def test_slippage_is_none_when_other_side_is_empty() -> None:
    # No bids → mid is None; buy fills against asks but slippage cannot be computed
    book = _book(bids=[], asks=[(101.0, 5.0)])
    result = simulate_market_order(book, Side.BUY, 1.0)
    assert result.avg_price == pytest.approx(101.0)
    assert result.slippage_bps is None


def test_size_zero_returns_no_fill() -> None:
    book = _book(bids=[(99.0, 1.0)], asks=[(101.0, 1.0)])
    result = simulate_market_order(book, Side.BUY, 0.0)
    assert result.avg_price is None
    assert result.slippage_bps is None
    assert result.unfilled_qty == pytest.approx(0.0)
    assert result.levels_consumed == 0
