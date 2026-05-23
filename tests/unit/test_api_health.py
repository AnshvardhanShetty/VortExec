import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from vortexec.api.deps import MaintainersMap
from vortexec.api.server import create_app
from vortexec.core.types import Level, Snapshot
from vortexec.maintainer.book_maintainer import BookMaintainer

# Reuse the FakeVenueConnector helpers from the maintainer tests.
from tests.unit.test_book_maintainer import FakeSession, FakeVenueConnector


def _build_maintainer(venue: str, symbol: str, *, healthy: bool) -> BookMaintainer:
    """Construct a BookMaintainer without starting it. Optionally fake the
    last-update timestamp so is_healthy() returns True for tests that need it.
    """
    snapshot = Snapshot(
        bids=[Level(price=99.0, quantity=1.0)],
        asks=[Level(price=101.0, quantity=1.0)],
        timestamp=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
    )
    fake = FakeVenueConnector(sessions=[FakeSession(snapshot=snapshot, diffs=[])])
    m = BookMaintainer(
        connector=fake,
        venue=venue,
        symbol=symbol,
        staleness_threshold_seconds=60.0,
    )
    m.get_book().apply_snapshot(snapshot)
    if healthy:
        m._last_update_at = time.monotonic()
    return m


def _client(maintainers: MaintainersMap) -> TestClient:
    return TestClient(create_app(maintainers))


def test_health_returns_200_when_all_maintainers_healthy() -> None:
    maintainers: MaintainersMap = {
        ("binance", "BTCUSDT"): _build_maintainer("binance", "BTCUSDT", healthy=True),
        ("binance", "ETHUSDT"): _build_maintainer("binance", "ETHUSDT", healthy=True),
    }
    with _client(maintainers) as c:
        r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert len(body["maintainers"]) == 2
    assert all(m["healthy"] for m in body["maintainers"])
    assert all(m["resync_count"] == 0 for m in body["maintainers"])


def test_health_returns_503_when_any_maintainer_unhealthy() -> None:
    maintainers: MaintainersMap = {
        ("binance", "BTCUSDT"): _build_maintainer("binance", "BTCUSDT", healthy=True),
        ("binance", "ETHUSDT"): _build_maintainer("binance", "ETHUSDT", healthy=False),
    }
    with _client(maintainers) as c:
        r = c.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unhealthy"
    healthies = {m["symbol"]: m["healthy"] for m in body["maintainers"]}
    assert healthies == {"BTCUSDT": True, "ETHUSDT": False}


def test_health_returns_503_when_no_maintainers_registered() -> None:
    with _client({}) as c:
        r = c.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unhealthy"
    assert body["maintainers"] == []


def test_health_includes_resync_and_drop_counters() -> None:
    m = _build_maintainer("binance", "BTCUSDT", healthy=True)
    m._resync_count = 3
    m._drop_count = 17
    with _client({("binance", "BTCUSDT"): m}) as c:
        r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["maintainers"][0]["resync_count"] == 3
    assert body["maintainers"][0]["drop_count"] == 17
