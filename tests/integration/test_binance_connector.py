import json
from pathlib import Path
from typing import Any

import pytest

from vortexec.core.types import Level
from vortexec.venues.base import SequenceGapError
from vortexec.venues.binance import BinanceConnector

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


class _FakeResponse:
    def __init__(self, json_data: dict[str, Any]) -> None:
        self._json_data = json_data

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> dict[str, Any]:
        return self._json_data

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _FakeSession:
    def __init__(self, json_data: dict[str, Any]) -> None:
        self._json_data = json_data
        self.requests: list[tuple[str, dict[str, Any] | None]] = []
        self.closed = False

    def get(
        self, url: str, params: dict[str, Any] | None = None
    ) -> _FakeResponse:
        self.requests.append((url, params))
        return _FakeResponse(self._json_data)

    async def close(self) -> None:
        self.closed = True


class _FakeWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self._messages = messages
        self._idx = 0

    async def __aenter__(self) -> "_FakeWebSocket":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def __aiter__(self) -> "_FakeWebSocket":
        return self

    async def __anext__(self) -> str:
        if self._idx >= len(self._messages):
            raise StopAsyncIteration
        msg = self._messages[self._idx]
        self._idx += 1
        return msg


def _patch_ws(
    monkeypatch: pytest.MonkeyPatch, messages: list[str] | None = None
) -> list[str]:
    """Patch ``websockets.connect`` to return a _FakeWebSocket. Pass an empty
    list (the default) when the test only exercises fetch_snapshot and doesn't
    care about WS messages — the buffered bootstrap still needs the WS to
    open successfully so the snapshot fetch can proceed.
    """
    if messages is None:
        messages = []
    captured_uris: list[str] = []

    def fake_connect(uri: str, *args: Any, **kwargs: Any) -> _FakeWebSocket:
        captured_uris.append(uri)
        return _FakeWebSocket(messages)

    monkeypatch.setattr("vortexec.venues.binance.websockets.connect", fake_connect)
    return captured_uris


async def test_fetch_snapshot_parses_binance_depth_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture: dict[str, Any] = json.loads(
        (FIXTURE_DIR / "binance_btcusdt_depth.json").read_text()
    )
    fake_session = _FakeSession(fixture)
    _patch_ws(monkeypatch)

    connector = BinanceConnector()
    connector._session = fake_session  # type: ignore[assignment]

    snapshot = await connector.fetch_snapshot("BTCUSDT")

    assert snapshot.bids == [
        Level(price=100.50, quantity=1.5),
        Level(price=100.40, quantity=2.0),
        Level(price=100.30, quantity=3.0),
    ]
    assert snapshot.asks == [
        Level(price=100.60, quantity=1.0),
        Level(price=100.70, quantity=2.5),
        Level(price=100.80, quantity=4.0),
    ]

    await connector.disconnect()


async def test_fetch_snapshot_calls_depth_endpoint_with_correct_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture: dict[str, Any] = json.loads(
        (FIXTURE_DIR / "binance_btcusdt_depth.json").read_text()
    )
    fake_session = _FakeSession(fixture)
    _patch_ws(monkeypatch)

    connector = BinanceConnector(rest_base_url="https://test.example")
    connector._session = fake_session  # type: ignore[assignment]

    await connector.fetch_snapshot("btcusdt")

    assert len(fake_session.requests) == 1
    url, params = fake_session.requests[0]
    assert url == "https://test.example/api/v3/depth"
    assert params == {"symbol": "BTCUSDT", "limit": 5000}

    await connector.disconnect()


async def test_fetch_snapshot_without_connect_raises() -> None:
    connector = BinanceConnector()
    with pytest.raises(AssertionError):
        await connector.fetch_snapshot("BTCUSDT")


async def test_stream_diffs_yields_aligned_diffs_after_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_fixture: dict[str, Any] = json.loads(
        (FIXTURE_DIR / "binance_btcusdt_depth.json").read_text()
    )
    raw_messages = (
        (FIXTURE_DIR / "binance_btcusdt_diffs.jsonl")
        .read_text()
        .strip()
        .split("\n")
    )
    fake_session = _FakeSession(snapshot_fixture)
    captured_uris = _patch_ws(monkeypatch, raw_messages)

    connector = BinanceConnector(ws_base_url="wss://test.example")
    connector._session = fake_session  # type: ignore[assignment]
    await connector.fetch_snapshot("BTCUSDT")

    collected = [diff async for diff in connector.stream_diffs("BTCUSDT")]

    # Fixture U/u values straddle the snapshot's lastUpdateId; both messages emit.
    # 3 bid-side + 3 ask-side level updates across 2 messages.
    assert len(collected) == 6
    assert captured_uris == ["wss://test.example/ws/btcusdt@depth@100ms"]
    assert collected[0].price == 100.50  # first bid of message 1
    assert collected[3].price == 100.55  # first bid of message 2

    await connector.disconnect()


async def test_fetch_snapshot_stores_last_update_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture: dict[str, Any] = json.loads(
        (FIXTURE_DIR / "binance_btcusdt_depth.json").read_text()
    )
    _patch_ws(monkeypatch)
    connector = BinanceConnector()
    connector._session = _FakeSession(fixture)  # type: ignore[assignment]
    await connector.fetch_snapshot("BTCUSDT")
    assert connector._last_update_id == 1027024
    await connector.disconnect()


async def test_stream_diffs_without_fetch_snapshot_raises() -> None:
    connector = BinanceConnector()
    with pytest.raises(AssertionError):
        async for _ in connector.stream_diffs("BTCUSDT"):
            pass


async def test_stream_diffs_drops_messages_already_in_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_fixture: dict[str, Any] = {
        "lastUpdateId": 1000,
        "bids": [],
        "asks": [],
    }
    messages = [
        # u <= lastUpdateId — already in snapshot, drop entirely
        '{"E":1700000000000,"U":990,"u":995,"b":[["100.00","1.0"]],"a":[]}',
        # bridges lastUpdateId+1: U=996 <= 1001 <= u=1005
        '{"E":1700000000100,"U":996,"u":1005,"b":[["101.00","2.0"]],"a":[]}',
    ]
    fake_session = _FakeSession(snapshot_fixture)
    _patch_ws(monkeypatch, messages)

    connector = BinanceConnector()
    connector._session = fake_session  # type: ignore[assignment]
    await connector.fetch_snapshot("BTCUSDT")

    collected = [diff async for diff in connector.stream_diffs("BTCUSDT")]
    assert len(collected) == 1
    assert collected[0].price == 101.00

    await connector.disconnect()


async def test_stream_diffs_raises_on_first_message_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_fixture: dict[str, Any] = {
        "lastUpdateId": 1000,
        "bids": [],
        "asks": [],
    }
    # First message starts at U=1002 — missed 1001
    messages = ['{"E":1700000000000,"U":1002,"u":1010,"b":[["100","1"]],"a":[]}']
    fake_session = _FakeSession(snapshot_fixture)
    _patch_ws(monkeypatch, messages)

    connector = BinanceConnector()
    connector._session = fake_session  # type: ignore[assignment]
    await connector.fetch_snapshot("BTCUSDT")

    with pytest.raises(SequenceGapError):
        async for _ in connector.stream_diffs("BTCUSDT"):
            pass

    await connector.disconnect()


async def test_stream_diffs_raises_on_mid_stream_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_fixture: dict[str, Any] = {
        "lastUpdateId": 1000,
        "bids": [],
        "asks": [],
    }
    messages = [
        '{"E":1700000000000,"U":1001,"u":1005,"b":[["100","1"]],"a":[]}',
        # Gap: expected U=1006, got U=1008
        '{"E":1700000000100,"U":1008,"u":1010,"b":[["101","2"]],"a":[]}',
    ]
    fake_session = _FakeSession(snapshot_fixture)
    _patch_ws(monkeypatch, messages)

    connector = BinanceConnector()
    connector._session = fake_session  # type: ignore[assignment]
    await connector.fetch_snapshot("BTCUSDT")

    collected = []
    with pytest.raises(SequenceGapError):
        async for diff in connector.stream_diffs("BTCUSDT"):
            collected.append(diff)
    # First message succeeded before the gap was detected
    assert len(collected) == 1

    await connector.disconnect()
