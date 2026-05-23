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
