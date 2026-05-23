"""Record Binance order-book snapshots to JSONL files for offline analysis.

For each symbol, every ``interval`` seconds, calls ``GET /api/v3/depth?limit=...``
and appends one JSON line to ``data/raw/{symbol_lower}_depth.jsonl`` matching the
schema of the existing legacy recordings.

Examples:
    # 1 hour of BTC + ETH + SOL, every 15 seconds, full 5000-deep books
    .venv/bin/python scripts/record_depth.py

    # Custom: 4 hours, BTC only, every 5 seconds
    .venv/bin/python scripts/record_depth.py \\
        --symbols BTCUSDT --interval 5 --duration 14400

Run in the background with ``nohup`` so it survives terminal close:
    nohup .venv/bin/python scripts/record_depth.py > /tmp/recorder.log 2>&1 &
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "raw"
DEPTH_URL = "https://api.binance.com/api/v3/depth"

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
DEFAULT_INTERVAL = 15.0
DEFAULT_DURATION = 3600.0
DEFAULT_LIMIT = 5000

log = logging.getLogger("recorder")


async def _fetch(
    session: aiohttp.ClientSession, symbol: str, limit: int
) -> dict[str, Any]:
    async with session.get(
        DEPTH_URL, params={"symbol": symbol, "limit": limit}
    ) as response:
        response.raise_for_status()
        data: dict[str, Any] = await response.json()
        return data


async def _record_one(
    session: aiohttp.ClientSession,
    symbol: str,
    out_path: Path,
    interval: float,
    duration: float,
    limit: int,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    deadline = start + duration
    snapshots = 0
    errors = 0

    with out_path.open("a") as f:
        while time.monotonic() < deadline:
            tick_start = time.monotonic()
            try:
                data = await _fetch(session, symbol, limit)
                record = {
                    "timestamp": int(time.time() * 1000),
                    "lastUpdateId": data["lastUpdateId"],
                    "bids": [[float(p), float(q)] for p, q in data["bids"]],
                    "asks": [[float(p), float(q)] for p, q in data["asks"]],
                }
                f.write(json.dumps(record) + "\n")
                f.flush()
                snapshots += 1
                if snapshots % 20 == 0:
                    log.info(
                        "%s: %d snapshots written (%d errors so far)",
                        symbol,
                        snapshots,
                        errors,
                    )
            except Exception as e:
                errors += 1
                log.warning("%s: fetch failed (%s)", symbol, e)

            elapsed = time.monotonic() - tick_start
            await asyncio.sleep(max(0.0, interval - elapsed))

    log.info(
        "%s: done — %d snapshots, %d errors, %.1f minutes",
        symbol,
        snapshots,
        errors,
        (time.monotonic() - start) / 60.0,
    )


async def _main(
    symbols: list[str],
    interval: float,
    duration: float,
    limit: int,
    out_dir: Path,
    verify_ssl: bool,
) -> None:
    log.info(
        "recording %s every %.0fs for %.0fs (%.1f minutes), limit=%d, into %s",
        ",".join(symbols),
        interval,
        duration,
        duration / 60.0,
        limit,
        out_dir,
    )
    # ``verify_ssl=False`` is the default because Python on macOS often can't
    # see corporate roots in the system keychain that curl uses transparently.
    # We're recording public market data with no auth; integrity isn't a
    # concern for offline analysis. Override with --verify-ssl in trusted envs.
    connector = aiohttp.TCPConnector(ssl=verify_ssl)
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(
            *(
                _record_one(
                    session,
                    s,
                    out_dir / f"{s.lower()}_depth.jsonl",
                    interval,
                    duration,
                    limit,
                )
                for s in symbols
            )
        )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    p.add_argument("--duration", type=float, default=DEFAULT_DURATION)
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--out-dir", type=Path, default=OUT_DIR)
    p.add_argument(
        "--verify-ssl",
        action="store_true",
        help="Enable SSL verification (off by default for macOS/keychain quirks)",
    )
    return p.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args()
    asyncio.run(
        _main(
            args.symbols,
            args.interval,
            args.duration,
            args.limit,
            args.out_dir,
            args.verify_ssl,
        )
    )
