"""LinkedIn-ready time-series chart: cost of a 5 BTC market buy across the
16-hour BTC recording window. Produces both line and scatter variants so the
user can compare which reads cleaner.

Output: data/analysis/chart_5btc_timeseries_{line,scatter}.png
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from vortexec.core.book import OrderBook
from vortexec.core.simulator import simulate_market_order
from vortexec.core.types import Level, Side, Snapshot

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = Path("/Users/anshshetty/vortexec_recordings/btcusdt_depth.jsonl")
OUT_DIR = ROOT / "data" / "analysis"
TRADE_SIZE = 5.0


def _load_series() -> tuple[list[datetime], list[float]]:
    times: list[datetime] = []
    bps: list[float] = []
    book = OrderBook()
    with DATA_FILE.open() as f:
        for line in f:
            d: dict[str, Any] = json.loads(line)
            ts = datetime.fromtimestamp(d["timestamp"] / 1000.0, tz=timezone.utc)
            snap = Snapshot(
                bids=[Level(price=p, quantity=q) for p, q in d["bids"]],
                asks=[Level(price=p, quantity=q) for p, q in d["asks"]],
                timestamp=ts,
            )
            book.apply_snapshot(snap)
            result = simulate_market_order(book, Side.BUY, TRADE_SIZE)
            if result.slippage_bps is not None and result.unfilled_qty == 0.0:
                times.append(ts)
                bps.append(result.slippage_bps)
    return times, bps


def _render(
    times: list[datetime],
    bps: list[float],
    style: str,
    out_path: Path,
) -> None:
    bps_sorted = sorted(bps)
    median = statistics.median(bps_sorted)
    p95 = bps_sorted[int(0.95 * (len(bps_sorted) - 1))]
    max_idx = bps.index(max(bps))
    max_time = times[max_idx]
    max_val = bps[max_idx]

    fig, ax = plt.subplots(figsize=(11, 5.5))

    if style == "line":
        ax.plot(times, bps, color="#2E86AB", linewidth=0.7, alpha=0.85)
    else:
        ax.scatter(times, bps, s=5, alpha=0.25, color="#2E86AB", edgecolors="none")

    # Three reference lines: median, P95, and the max marker further down.
    # Each label is on a white badge so it reads cleanly against the dot cloud.
    def _badge_label(text: str, y: float, color: str = "#222222") -> None:
        ax.annotate(
            text,
            xy=(0.01, y),
            xycoords=("axes fraction", "data"),
            xytext=(0, 8),
            textcoords="offset points",
            fontsize=10,
            color=color,
            va="bottom",
            ha="left",
            bbox=dict(
                boxstyle="round,pad=0.35",
                facecolor="white",
                edgecolor="#bbbbbb",
                linewidth=0.6,
                alpha=0.95,
            ),
            zorder=6,
        )

    ax.axhline(median, color="#222222", linestyle="--", linewidth=1.1, alpha=0.85)
    _badge_label(f"median: {median:.2f} bps", median)

    ax.axhline(p95, color="#222222", linestyle="--", linewidth=1.1, alpha=0.85)
    _badge_label(f"P95: {p95:.2f} bps", p95)

    # Max: red marker + offset text label.
    ax.scatter(
        [max_time], [max_val], s=80, color="#E63946", zorder=5,
        edgecolors="white", linewidths=1.2,
    )
    # Place label to the left of the marker if it's near the right edge,
    # otherwise to the right. Keeps it inside the axes.
    frac_x = (max_time - times[0]) / (times[-1] - times[0])
    if frac_x > 0.7:
        offset_x, ha = -12, "right"
    else:
        offset_x, ha = 12, "left"
    ax.annotate(
        f"worst minute: {max_val:.2f} bps",
        xy=(max_time, max_val),
        xytext=(offset_x, -2),
        textcoords="offset points",
        fontsize=10,
        color="#E63946",
        fontweight="bold",
        va="center",
        ha=ha,
        bbox=dict(
            boxstyle="round,pad=0.35",
            facecolor="white",
            edgecolor="#f0c7cb",
            linewidth=0.6,
            alpha=0.95,
        ),
        zorder=6,
    )

    # Axes / ticks
    ax.set_ylabel("Slippage (bps)")
    ax.set_title("Cost of a 5 BTC market buy on Binance, across 16 hours")
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_xlim(times[0], times[-1])
    ax.margins(y=0.08)
    ax.grid(alpha=0.2)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    times, bps = _load_series()
    span = times[-1] - times[0]
    print(
        f"Loaded {len(times)} valid points, "
        f"span {span.total_seconds() / 3600:.2f}h "
        f"({times[0]:%H:%M} UTC → {times[-1]:%H:%M} UTC)"
    )
    print(
        f"  median = {statistics.median(bps):.2f} bps, "
        f"max = {max(bps):.2f} bps"
    )
    for style in ("line", "scatter"):
        out_path = OUT_DIR / f"chart_5btc_timeseries_{style}.png"
        _render(times, bps, style, out_path)
        print(f"  wrote {out_path}")


if __name__ == "__main__":
    main()
