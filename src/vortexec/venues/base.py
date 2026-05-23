from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from vortexec.core.types import Diff, Snapshot


class SequenceGapError(Exception):
    """Raised when a diff stream's sequence numbers indicate a gap."""


class VenueConnector(ABC):
    @abstractmethod
    async def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def fetch_snapshot(self, symbol: str) -> Snapshot:
        raise NotImplementedError

    @abstractmethod
    def stream_diffs(self, symbol: str) -> AsyncIterator[Diff]:
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        raise NotImplementedError
