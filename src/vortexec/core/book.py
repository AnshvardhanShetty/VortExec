from __future__ import annotations

from typing import cast

from sortedcontainers import SortedDict

from vortexec.core.types import Diff, Side, Snapshot


class OrderBook:
    def __init__(self) -> None:
        self._bids: SortedDict = SortedDict()
        self._asks: SortedDict = SortedDict()

    def apply_snapshot(self, snapshot: Snapshot) -> None:
        self._bids.clear()
        self._asks.clear()
        for level in snapshot.bids:
            if level.quantity != 0:
                self._bids[level.price] = level.quantity
        for level in snapshot.asks:
            if level.quantity != 0:
                self._asks[level.price] = level.quantity

    def apply_diff(self, diff: Diff) -> None:
        side_dict = self._bids if diff.side is Side.BUY else self._asks
        if diff.quantity == 0:
            side_dict.pop(diff.price, None)
        else:
            side_dict[diff.price] = diff.quantity

    def best_bid(self) -> float | None:
        if not self._bids:
            return None
        return cast(float, self._bids.peekitem(-1)[0])

    def best_ask(self) -> float | None:
        if not self._asks:
            return None
        return cast(float, self._asks.peekitem(0)[0])

    def mid(self) -> float | None:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    def spread(self) -> float | None:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return None
        return ask - bid
