from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import aiohttp
import websockets

from vortexec.core.types import Diff, Level, Side, Snapshot
from vortexec.venues.base import SequenceGapError, VenueConnector

DEFAULT_REST_BASE_URL = "https://api.binance.com"
DEFAULT_WS_BASE_URL = "wss://stream.binance.com:9443"
WS_READY_TIMEOUT = 10.0


def _parse_depth_response(data: dict[str, Any], timestamp: datetime) -> Snapshot:
    bids = [Level(price=float(p), quantity=float(q)) for p, q in data["bids"]]
    asks = [Level(price=float(p), quantity=float(q)) for p, q in data["asks"]]
    return Snapshot(bids=bids, asks=asks, timestamp=timestamp)


class _Aligner:
    """Sequence-number validator for Binance depth diffs post-snapshot.

    Drops messages already covered by the snapshot (u <= last_update_id),
    requires the first kept message to bridge last_update_id+1 (U <= last_update_id+1),
    and requires subsequent messages to be contiguous (U == prev_u + 1).
    Raises SequenceGapError on any violation.
    """

    def __init__(self, last_update_id: int) -> None:
        self._snap_id = last_update_id
        self._prev_u: int | None = None

    def should_emit(self, msg: dict[str, Any]) -> bool:
        first_id = int(msg["U"])
        final_id = int(msg["u"])

        if final_id <= self._snap_id:
            return False

        if self._prev_u is None:
            if first_id > self._snap_id + 1:
                raise SequenceGapError(
                    f"snapshot last_update_id={self._snap_id}, "
                    f"first ws message U={first_id} "
                    f"(expected U <= {self._snap_id + 1})"
                )
            self._prev_u = final_id
            return True

        if first_id != self._prev_u + 1:
            raise SequenceGapError(
                f"sequence gap: expected U={self._prev_u + 1}, got U={first_id}"
            )
        self._prev_u = final_id
        return True


def _parse_diff_message(data: dict[str, Any]) -> list[Diff]:
    event_ms: int = data["E"]
    timestamp = datetime.fromtimestamp(event_ms / 1000.0, tz=timezone.utc)
    diffs: list[Diff] = []
    for price_str, qty_str in data.get("b", []):
        diffs.append(
            Diff(
                side=Side.BUY,
                price=float(price_str),
                quantity=float(qty_str),
                timestamp=timestamp,
            )
        )
    for price_str, qty_str in data.get("a", []):
        diffs.append(
            Diff(
                side=Side.SELL,
                price=float(price_str),
                quantity=float(qty_str),
                timestamp=timestamp,
            )
        )
    return diffs


class BinanceConnector(VenueConnector):
    def __init__(
        self,
        rest_base_url: str = DEFAULT_REST_BASE_URL,
        ws_base_url: str = DEFAULT_WS_BASE_URL,
        verify_ssl: bool = True,
    ) -> None:
        self._rest_base_url = rest_base_url
        self._ws_base_url = ws_base_url
        self._verify_ssl = verify_ssl
        self._session: aiohttp.ClientSession | None = None
        self._last_update_id: int | None = None
        # WS buffer state — populated by fetch_snapshot, drained by stream_diffs.
        self._ws_task: asyncio.Task[None] | None = None
        self._ws_queue: asyncio.Queue[dict[str, Any] | None] | None = None
        self._ws_ready: asyncio.Event | None = None

    async def connect(self) -> None:
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=self._verify_ssl)
            self._session = aiohttp.ClientSession(connector=connector)

    async def disconnect(self) -> None:
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_task = None
            self._ws_queue = None
            self._ws_ready = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch_snapshot(self, symbol: str) -> Snapshot:
        """Bootstrap protocol: start the WS buffer first, then fetch the REST
        snapshot. By the time the REST call returns, the queue already holds
        the diffs that arrived during the fetch — the aligner in stream_diffs
        drops the ones already covered by the snapshot and bridges from there.
        """
        assert self._session is not None, "call connect() before fetch_snapshot()"

        if self._ws_task is None or self._ws_task.done():
            self._ws_queue = asyncio.Queue()
            self._ws_ready = asyncio.Event()
            self._ws_task = asyncio.create_task(self._buffer_ws(symbol))
            await asyncio.wait_for(self._ws_ready.wait(), timeout=WS_READY_TIMEOUT)

        url = f"{self._rest_base_url}/api/v3/depth"
        params: dict[str, Any] = {"symbol": symbol.upper(), "limit": 5000}
        async with self._session.get(url, params=params) as response:
            response.raise_for_status()
            data: dict[str, Any] = await response.json()
        self._last_update_id = int(data["lastUpdateId"])
        return _parse_depth_response(data, datetime.now(timezone.utc))

    async def _buffer_ws(self, symbol: str) -> None:
        """Background task: connect to the WS depth stream and queue every
        message. Sets _ws_ready as soon as the connection is established so
        fetch_snapshot can proceed knowing the buffer is live. Always puts
        a None sentinel on exit so consumers know the stream is closed.
        """
        assert self._ws_queue is not None
        assert self._ws_ready is not None
        uri = f"{self._ws_base_url}/ws/{symbol.lower()}@depth@100ms"
        ssl_ctx = ssl.create_default_context()
        if not self._verify_ssl:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        try:
            async with websockets.connect(uri, ssl=ssl_ctx) as ws:
                self._ws_ready.set()
                async for raw in ws:
                    msg: dict[str, Any] = json.loads(raw)
                    await self._ws_queue.put(msg)
        finally:
            await self._ws_queue.put(None)

    async def stream_diffs(self, symbol: str) -> AsyncIterator[Diff]:
        assert self._last_update_id is not None, (
            "fetch_snapshot() must be called before stream_diffs() "
            "to establish the bootstrap sequence baseline"
        )
        assert self._ws_queue is not None, (
            "fetch_snapshot() must be called before stream_diffs() "
            "to start the WS buffer"
        )
        aligner = _Aligner(self._last_update_id)
        while True:
            msg = await self._ws_queue.get()
            if msg is None:  # WS task ended; signal end of stream
                return
            if aligner.should_emit(msg):
                for diff in _parse_diff_message(msg):
                    yield diff
