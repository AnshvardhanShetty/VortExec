import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from vortexec.api.deps import MaintainersMap
from vortexec.api.server import create_app
from vortexec.core.types import Level, Snapshot
from vortexec.maintainer.book_maintainer import BookMaintainer

from tests.unit.test_book_maintainer import FakeSession, FakeVenueConnector


def _build_maintainer(
    venue: str,
    symbol: str,
    *,
    healthy: bool = True,
    bids: list[Level] | None = None,
    asks: list[Level] | None = None,
) -> BookMaintainer:
    if bids is None:
        bids = [Level(price=99.0, quantity=5.0), Level(price=98.0, quantity=10.0)]
    if asks is None:
        asks = [Level(price=101.0, quantity=5.0), Level(price=102.0, quantity=10.0)]
    snap = Snapshot(
        bids=bids, asks=asks,
        timestamp=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
    )
    fake = FakeVenueConnector(sessions=[FakeSession(snapshot=snap, diffs=[])])
    m = BookMaintainer(
        connector=fake, venue=venue, symbol=symbol,
        staleness_threshold_seconds=60.0,
    )
    m.get_book().apply_snapshot(snap)
    if healthy:
        m._last_update_at = time.monotonic()
    return m


def _client(maintainers: MaintainersMap) -> TestClient:
    return TestClient(create_app(maintainers))


def test_estimate_returns_simulator_and_features() -> None:
    m = _build_maintainer("binance", "BTCUSDT")
    with _client({("binance", "BTCUSDT"): m}) as c:
        r = c.post(
            "/v1/estimate",
            json={"venue": "binance", "symbol": "BTCUSDT", "side": "buy", "size": 3.0},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["venue"] == "binance"
    assert body["symbol"] == "BTCUSDT"
    assert body["side"] == "buy"
    assert body["size"] == 3.0

    det = body["deterministic"]
    # Buy 3 at top ask (5 available @ 101) → all fills at 101
    assert det["avg_price"] == 101.0
    assert det["unfilled_qty"] == 0.0
    assert det["levels_consumed"] == 1
    # Slippage vs mid 100: (101-100)/100 * 10000 = 100 bps
    assert det["slippage_bps"] == 100.0

    feats = body["features"]
    assert feats["mid_price"] == 100.0
    # Top 5 bids = 5 + 10 = 15; top 5 asks = 5 + 10 = 15
    assert feats["depth_top_5_bids"] == 15.0
    assert feats["depth_top_5_asks"] == 15.0
    # Symmetric book → imbalance 0
    assert feats["imbalance"] == 0.0


def test_estimate_walks_multiple_levels_when_top_insufficient() -> None:
    m = _build_maintainer(
        "binance", "BTCUSDT",
        bids=[Level(price=99.0, quantity=1.0)],
        asks=[Level(price=101.0, quantity=2.0), Level(price=102.0, quantity=3.0)],
    )
    with _client({("binance", "BTCUSDT"): m}) as c:
        r = c.post(
            "/v1/estimate",
            json={"venue": "binance", "symbol": "BTCUSDT", "side": "buy", "size": 4.0},
        )
    assert r.status_code == 200
    body = r.json()["deterministic"]
    # 2 @ 101 + 2 @ 102 = 406 / 4 = 101.5
    assert body["avg_price"] == 101.5
    assert body["levels_consumed"] == 2


def test_estimate_404_for_unknown_venue_or_symbol() -> None:
    m = _build_maintainer("binance", "BTCUSDT")
    with _client({("binance", "BTCUSDT"): m}) as c:
        r = c.post(
            "/v1/estimate",
            json={"venue": "binance", "symbol": "ETHUSDT", "side": "buy", "size": 1.0},
        )
    assert r.status_code == 404
    assert "no maintainer for binance/ETHUSDT" in r.json()["detail"]


def test_estimate_503_for_unhealthy_maintainer() -> None:
    m = _build_maintainer("binance", "BTCUSDT", healthy=False)
    with _client({("binance", "BTCUSDT"): m}) as c:
        r = c.post(
            "/v1/estimate",
            json={"venue": "binance", "symbol": "BTCUSDT", "side": "buy", "size": 1.0},
        )
    assert r.status_code == 503
    assert "unhealthy" in r.json()["detail"]


def test_estimate_422_for_invalid_size() -> None:
    m = _build_maintainer("binance", "BTCUSDT")
    with _client({("binance", "BTCUSDT"): m}) as c:
        r = c.post(
            "/v1/estimate",
            json={"venue": "binance", "symbol": "BTCUSDT", "side": "buy", "size": 0.0},
        )
    # Pydantic validation rejects size=0 (Field(gt=0))
    assert r.status_code == 422


def test_estimate_422_for_invalid_side() -> None:
    m = _build_maintainer("binance", "BTCUSDT")
    with _client({("binance", "BTCUSDT"): m}) as c:
        r = c.post(
            "/v1/estimate",
            json={"venue": "binance", "symbol": "BTCUSDT", "side": "long", "size": 1.0},
        )
    assert r.status_code == 422
