"""Top-level orchestration. ``python -m vortexec`` runs this.

Spins up one (connector, maintainer, recorder) trio per symbol — all in one
process, one event loop — and runs until SIGINT/SIGTERM. Periodic stats are
logged so the operator can see each maintained book is live. Optional
Parquet recording (one file tree per symbol) is enabled with --record-to.

Recorder (Phase 3) is wired in. Model (Phase 5) and HTTP API (Phase 4) are
not yet implemented; when they land they're added here below the maintainer.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import aiohttp
import uvicorn

from vortexec.api.deps import MaintainersMap
from vortexec.api.server import create_app
from vortexec.maintainer.book_maintainer import BookMaintainer
from vortexec.recorder.parquet_recorder import ParquetRecorder
from vortexec.venues.binance import BinanceConnector

log = logging.getLogger("vortexec.service")
STATS_INTERVAL_SECONDS = 10.0
HEALTHCHECKS_PING_INTERVAL_SECONDS = 60.0
HEALTHCHECKS_PING_TIMEOUT_SECONDS = 10.0


@dataclass
class _Trio:
    connector: BinanceConnector
    maintainer: BookMaintainer
    recorder: ParquetRecorder | None


def _fmt(v: float | None, places: int = 2) -> str:
    return f"{v:.{places}f}" if v is not None else "—"


def _on_signal(stop: asyncio.Event, sig_name: str) -> None:
    log.info("received %s, shutting down", sig_name)
    stop.set()


async def _healthchecks_ping_loop(
    url: str, trios: list[_Trio], stop: asyncio.Event
) -> None:
    """Periodically GET the Healthchecks.io URL so external monitoring fires
    an alert if this service silently dies. Skips the ping if any maintainer
    reports unhealthy — let the alert fire rather than mask a broken book.
    """
    timeout = aiohttp.ClientTimeout(total=HEALTHCHECKS_PING_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=HEALTHCHECKS_PING_INTERVAL_SECONDS
                )
                return
            except asyncio.TimeoutError:
                pass
            healthy = all(t.maintainer.is_healthy() for t in trios) if trios else False
            if not healthy:
                log.warning(
                    "skipping healthchecks ping: one or more maintainers unhealthy"
                )
                continue
            try:
                async with session.get(url) as resp:
                    if resp.status >= 400:
                        log.warning(
                            "healthchecks ping returned HTTP %d", resp.status
                        )
            except Exception as e:
                log.warning("healthchecks ping failed: %r", e)


async def _log_stats(trios: list[_Trio], stop: asyncio.Event) -> None:
    """Emit one line per symbol every STATS_INTERVAL_SECONDS until stop."""
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=STATS_INTERVAL_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        for trio in trios:
            m = trio.maintainer
            book = m.get_book()
            log.info(
                "%-8s bid=%s ask=%s spread=%s mid=%s  healthy=%s  resync=%d  drop=%d",
                m.symbol,
                _fmt(book.best_bid()),
                _fmt(book.best_ask()),
                _fmt(book.spread(), 4),
                _fmt(book.mid()),
                m.is_healthy(),
                m.resync_count,
                m.drop_count,
            )


async def run(
    symbols: list[str],
    venue: str,
    verify_ssl: bool,
    record_to: Path | None,
    snapshot_interval_seconds: float,
    api_host: str,
    api_port: int,
    healthchecks_url: str | None,
) -> None:
    trios: list[_Trio] = []
    for symbol in symbols:
        connector = BinanceConnector(verify_ssl=verify_ssl)
        maintainer = BookMaintainer(connector, venue, symbol)
        recorder = (
            ParquetRecorder(record_to, snapshot_interval_seconds=snapshot_interval_seconds)
            if record_to is not None
            else None
        )
        trios.append(_Trio(connector, maintainer, recorder))

    log.info(
        "starting %d symbol(s) on venue=%s: %s (verify_ssl=%s, record_to=%s, "
        "snapshot_every=%.0fs, api=%s:%d)",
        len(symbols),
        venue,
        ",".join(symbols),
        verify_ssl,
        record_to,
        snapshot_interval_seconds,
        api_host,
        api_port,
    )

    # Subscribers (recorders) must register before maintainers start producing.
    for trio in trios:
        if trio.recorder is not None:
            await trio.recorder.start(trio.maintainer)
    for trio in trios:
        await trio.maintainer.start()

    # FastAPI app borrows references to the maintainers; lifecycle stays here.
    maintainers_map: MaintainersMap = {
        (t.maintainer.venue, t.maintainer.symbol): t.maintainer for t in trios
    }
    app = create_app(maintainers_map)
    config = uvicorn.Config(
        app, host=api_host, port=api_port, log_level="info", access_log=False
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, partial(_on_signal, stop, sig.name))

    stats_task = asyncio.create_task(_log_stats(trios, stop))
    hc_task: asyncio.Task[None] | None = None
    if healthchecks_url:
        hc_task = asyncio.create_task(
            _healthchecks_ping_loop(healthchecks_url, trios, stop)
        )
        log.info("healthchecks ping enabled (every %.0fs)", HEALTHCHECKS_PING_INTERVAL_SECONDS)

    try:
        await stop.wait()
    finally:
        log.info("stopping %d symbol(s) and HTTP server", len(trios))
        for t in (stats_task, hc_task):
            if t is None:
                continue
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        # Tell uvicorn to exit, then await its serve() task.
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=10.0)
        except asyncio.TimeoutError:
            log.warning("uvicorn did not exit cleanly within 10s, cancelling")
            server_task.cancel()
        # Stop all trios in parallel. Each maintainer.stop takes ~7s (WS close);
        # serialising N of them would multiply that, so gather.
        coros = []
        for trio in trios:
            coros.append(trio.maintainer.stop())
            if trio.recorder is not None:
                coros.append(trio.recorder.stop())
        await asyncio.gather(*coros, return_exceptions=True)
        log.info("clean shutdown complete")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--symbols",
        nargs="+",
        default=["BTCUSDT"],
        help="One or more symbols to maintain (default: BTCUSDT).",
    )
    p.add_argument("--venue", default="binance", choices=["binance"])
    p.add_argument(
        "--no-verify-ssl",
        dest="verify_ssl",
        action="store_false",
        default=True,
        help="Disable SSL verification (needed on some local machines whose "
        "Python doesn't see corporate keychain roots).",
    )
    p.add_argument(
        "--record-to",
        type=Path,
        default=None,
        help="If set, persist diff events to Parquet under this directory. "
        "Layout: {dir}/{venue}/{symbol}/{YYYY-MM-DD}/{HH}.parquet, and "
        "{dir}/.../snapshots/{HH-MM-SS}.parquet for periodic full snapshots.",
    )
    p.add_argument(
        "--snapshot-interval",
        type=float,
        default=600.0,
        help="Seconds between full-book Parquet snapshots (default: 600s = 10 min). "
        "Set to 0 to disable. Only takes effect when --record-to is set.",
    )
    p.add_argument("--api-host", default="127.0.0.1", help="HTTP API bind address.")
    p.add_argument("--api-port", type=int, default=8000, help="HTTP API port.")
    p.add_argument(
        "--healthchecks-url",
        default=None,
        help="If set, GET this URL every 60s as a liveness ping (e.g. "
        "https://hc-ping.com/UUID from healthchecks.io). External monitoring "
        "alerts when pings stop.",
    )
    args = p.parse_args()
    try:
        asyncio.run(
            run(
                args.symbols,
                args.venue,
                args.verify_ssl,
                args.record_to,
                args.snapshot_interval,
                args.api_host,
                args.api_port,
                args.healthchecks_url,
            )
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
