from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Level:
    price: float
    quantity: float


@dataclass(frozen=True)
class Snapshot:
    bids: list[Level]
    asks: list[Level]
    timestamp: datetime


@dataclass(frozen=True)
class Diff:
    side: Side
    price: float
    quantity: float
    timestamp: datetime


@dataclass(frozen=True)
class BookUpdate:
    venue: str
    symbol: str
    diff: Diff
