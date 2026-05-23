from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from vortexec.core.book import OrderBook


@dataclass(frozen=True)
class Features:
    spread_bps: float | None
    mid_price: float | None
    depth_top_5_bids: float
    depth_top_5_asks: float
    depth_top_10_bids: float
    depth_top_10_asks: float
    imbalance: float | None


def extract_features(book: OrderBook) -> Features:
    bid_prices = list(reversed(book._bids))
    ask_prices = list(book._asks)

    depth_top_5_bids = sum(
        (cast(float, book._bids[p]) for p in bid_prices[:5]), 0.0
    )
    depth_top_5_asks = sum(
        (cast(float, book._asks[p]) for p in ask_prices[:5]), 0.0
    )
    depth_top_10_bids = sum(
        (cast(float, book._bids[p]) for p in bid_prices[:10]), 0.0
    )
    depth_top_10_asks = sum(
        (cast(float, book._asks[p]) for p in ask_prices[:10]), 0.0
    )

    mid = book.mid()
    spread = book.spread()
    spread_bps: float | None = None
    if mid is not None and spread is not None:
        spread_bps = spread / mid * 10_000

    total_top_10 = depth_top_10_bids + depth_top_10_asks
    imbalance: float | None = None
    if total_top_10 > 0:
        imbalance = (depth_top_10_bids - depth_top_10_asks) / total_top_10

    return Features(
        spread_bps=spread_bps,
        mid_price=mid,
        depth_top_5_bids=depth_top_5_bids,
        depth_top_5_asks=depth_top_5_asks,
        depth_top_10_bids=depth_top_10_bids,
        depth_top_10_asks=depth_top_10_asks,
        imbalance=imbalance,
    )
