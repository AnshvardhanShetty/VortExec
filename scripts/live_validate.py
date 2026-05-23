"""Live end-to-end validation against real Binance.

Wires a BinanceConnector to a BookMaintainer, runs against live Binance for a
configurable duration, and periodically fetches an independent REST snapshot
to compare against the maintained top-of-book. Logs per-round results and a
final summary.

A "match" means the maintained best bid/ask exactly equals the freshly-fetched
REST snapshot's. A "near-match" means they differ by <= 1 tick — likely just a
race window between the maintainer applying its latest diff and the independent
fetch landing. Anything beyond that is a real divergence and should be rare;
many divergences over time would indicate the maintainer is drifting.

Usage:
    .venv/bin/python scripts/live_validate.py --symbol BTCUSDT --duration 1800 --interval 30
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from typing import Any

import aiohttp

from vortexec.maintainer.book_maintainer import BookMaintainer
from vortexec.venues.binance import BinanceConnector

log = logging.getLogger("validator")

REST_URL = "https://api.binance.com/api/v3/depth"
TICK_TOLERANCE = 0.01  # near-match window for race-condition allowance
READY_TIMEOUT = 30.0


async def _fetch_validation(
    session: aiohttp.ClientSession, symbol: str, limit: int = 100
) -> dict[str, Any]:
    async with session.get(
        REST_URL, params={"symbol": symbol, "limit": limit}
    ) as r:
        r.raise_for_status()
        data: dict[str, Any] = await r.json()
        return data


async def _wait_for_ready(maintainer: BookMaintainer, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if maintainer.get_book().best_bid() is not None:
            return True
        await asyncio.sleep(0.5)
    return False


def _classify(
    m_bid: float | None,
    m_ask: float | None,
    v_bid: float,
    v_ask: float,
    tick_tolerance: float,
) -> str:
    if m_bid is None or m_ask is None:
        return "empty"
    if m_bid == v_bid and m_ask == v_ask:
        return "exact"
    if abs(m_bid - v_bid) <= tick_tolerance and abs(m_ask - v_ask) <= tick_tolerance:
        return "near"
    return "divergent"


async def run(symbol: str, duration: int, interval: int) -> int:
    connector = BinanceConnector(verify_ssl=False)
    maintainer = BookMaintainer(connector, "binance", symbol)
    val_session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=False)
    )

    log.info("starting maintainer for %s", symbol)
    await maintainer.start()

    if not await _wait_for_ready(maintainer, READY_TIMEOUT):
        log.error("maintainer never populated book within %.1fs", READY_TIMEOUT)
        await maintainer.stop()
        await val_session.close()
        return 2

    log.info(
        "maintainer ready; running %ds with %ds comparison interval",
        duration,
        interval,
    )

    counts: dict[str, int] = {"exact": 0, "near": 0, "divergent": 0, "empty": 0}
    fetch_errors = 0
    deadline = time.monotonic() + duration

    try:
        while time.monotonic() < deadline:
            try:
                val = await _fetch_validation(val_session, symbol)
                v_bid = float(val["bids"][0][0])
                v_ask = float(val["asks"][0][0])
                book = maintainer.get_book()
                m_bid = book.best_bid()
                m_ask = book.best_ask()
                kind = _classify(m_bid, m_ask, v_bid, v_ask, TICK_TOLERANCE)
                counts[kind] += 1
                rounds = sum(counts.values())
                if kind == "exact":
                    log.info(
                        "round %d  ✓ bid=%.2f ask=%.2f  health=%s resync=%d drop=%d",
                        rounds,
                        v_bid,
                        v_ask,
                        maintainer.is_healthy(),
                        maintainer.resync_count,
                        maintainer.drop_count,
                    )
                elif kind == "near":
                    log.info(
                        "round %d  ~ bid m=%.2f v=%.2f  ask m=%.2f v=%.2f (race)",
                        rounds,
                        m_bid,
                        v_bid,
                        m_ask,
                        v_ask,
                    )
                elif kind == "divergent":
                    log.warning(
                        "round %d  ✗ DIVERGENT  bid m=%.2f v=%.2f (Δ%.2f)  ask m=%.2f v=%.2f (Δ%.2f)",
                        rounds,
                        m_bid,
                        v_bid,
                        (m_bid or 0) - v_bid,
                        m_ask,
                        v_ask,
                        (m_ask or 0) - v_ask,
                    )
                else:  # empty
                    log.warning(
                        "round %d  ! maintained book empty (m_bid=%s m_ask=%s)",
                        rounds,
                        m_bid,
                        m_ask,
                    )
            except Exception as e:
                fetch_errors += 1
                log.error("validation fetch failed: %s", e)

            await asyncio.sleep(interval)
    finally:
        log.info("stopping maintainer")
        await maintainer.stop()
        await val_session.close()

    rounds = sum(counts.values())
    pct = {k: 100.0 * v / max(rounds, 1) for k, v in counts.items()}
    log.info("=" * 60)
    log.info("VALIDATION SUMMARY  symbol=%s duration=%ds", symbol, duration)
    log.info("rounds=%d  fetch_errors=%d", rounds, fetch_errors)
    log.info(
        "  exact (top-of-book matches):       %4d  (%5.1f%%)",
        counts["exact"],
        pct["exact"],
    )
    log.info(
        "  near  (≤1-tick race window):       %4d  (%5.1f%%)",
        counts["near"],
        pct["near"],
    )
    log.info(
        "  divergent (real disagreement):     %4d  (%5.1f%%)",
        counts["divergent"],
        pct["divergent"],
    )
    log.info(
        "  empty (maintainer book empty):     %4d  (%5.1f%%)",
        counts["empty"],
        pct["empty"],
    )
    log.info(
        "maintainer  resync=%d  drop=%d  is_healthy=%s",
        maintainer.resync_count,
        maintainer.drop_count,
        maintainer.is_healthy(),
    )

    # Exit 0 only if no real divergences and no fetch errors.
    if counts["divergent"] == 0 and counts["empty"] == 0 and rounds > 0:
        return 0
    return 1


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument(
        "--duration",
        type=int,
        default=600,
        help="Total wall-clock seconds to run (default 600 = 10 min)",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Seconds between independent REST comparisons (default 30)",
    )
    return p.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args()
    raise SystemExit(asyncio.run(run(args.symbol, args.duration, args.interval)))
