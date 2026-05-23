"""Three alternative distribution charts for the 5 BTC slippage story.

Produces, from the same 16-hour BTC recording:
  data/analysis/chart_5btc_loghist.png   — log-scale histogram, distribution shape
  data/analysis/chart_5btc_quantile.png  — sorted-value curve, hockey stick shape
  data/analysis/chart_5btc_threebar.png  — median / P95 / max as three bars
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib.axes import Axes

from vortexec.core.book import OrderBook
from vortexec.core.simulator import simulate_market_order
from vortexec.core.types import Level, Side, Snapshot

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = Path("/Users/anshshetty/vortexec_recordings/btcusdt_depth.jsonl")
OUT_DIR = ROOT / "data" / "analysis"
TRADE_SIZE = 5.0
TITLE = "Cost of a 5 BTC market buy on Binance, across 16 hours"


def _load_bps() -> list[float]:
    bps: list[float] = []
    book = OrderBook()
    with DATA_FILE.open() as f:
        for line in f:
            d: dict[str, Any] = json.loads(line)
            snap = Snapshot(
                bids=[Level(price=p, quantity=q) for p, q in d["bids"]],
                asks=[Level(price=p, quantity=q) for p, q in d["asks"]],
                timestamp=datetime.fromtimestamp(
                    d["timestamp"] / 1000.0, tz=timezone.utc
                ),
            )
            book.apply_snapshot(snap)
            result = simulate_market_order(book, Side.BUY, TRADE_SIZE)
            if result.slippage_bps is not None and result.unfilled_qty == 0.0:
                bps.append(result.slippage_bps)
    return bps


def _stats(bps: list[float]) -> tuple[float, float, float]:
    bps_sorted = sorted(bps)
    median = statistics.median(bps_sorted)
    p95 = bps_sorted[int(0.95 * (len(bps_sorted) - 1))]
    return median, p95, bps_sorted[-1]


def _clean_spines(ax: Axes) -> None:
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)


def _badge(
    ax: Axes,
    text: str,
    x: float,
    y: float,
    *,
    xycoords: Any = "data",
    offset: tuple[int, int] = (5, 0),
    ha: str = "left",
    va: str = "center",
    color: str = "#222222",
    bold: bool = False,
    edge: str = "#bbbbbb",
) -> None:
    ax.annotate(
        text,
        xy=(x, y),
        xycoords=xycoords,
        xytext=offset,
        textcoords="offset points",
        fontsize=10,
        color=color,
        fontweight="bold" if bold else "normal",
        va=va,
        ha=ha,
        bbox=dict(
            boxstyle="round,pad=0.35",
            facecolor="white",
            edgecolor=edge,
            linewidth=0.6,
            alpha=0.95,
        ),
        zorder=6,
    )


def plot_loghist(
    bps: list[float], median: float, p95: float, mx: float, out: Path
) -> None:
    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.hist(bps, bins=60, color="#2E86AB", edgecolor="white", linewidth=0.3)
    ax.set_yscale("log")

    for x, label in [
        (median, f"median: {median:.2f} bps"),
        (p95, f"P95: {p95:.2f} bps"),
    ]:
        ax.axvline(x, color="#222222", linestyle="--", linewidth=1.1, alpha=0.85)
        _badge(ax, label, x, 0.95, xycoords=("data", "axes fraction"), va="top")

    ax.axvline(mx, color="#E63946", linestyle="--", linewidth=1.1)
    _badge(
        ax,
        f"worst minute: {mx:.2f} bps",
        mx,
        0.95,
        xycoords=("data", "axes fraction"),
        offset=(-5, 0),
        ha="right",
        va="top",
        color="#E63946",
        bold=True,
        edge="#f0c7cb",
    )

    ax.set_xlabel("Slippage (bps)")
    ax.set_ylabel("Number of snapshots (log scale)")
    ax.set_title(TITLE)
    ax.grid(axis="y", which="major", alpha=0.25)
    _clean_spines(ax)
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def plot_quantile(
    bps: list[float], median: float, p95: float, mx: float, out: Path
) -> None:
    bps_sorted = sorted(bps)
    n = len(bps_sorted)
    pct = [100.0 * i / (n - 1) for i in range(n)]

    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.fill_between(pct, 0, bps_sorted, color="#2E86AB", alpha=0.18, linewidth=0)
    ax.plot(pct, bps_sorted, color="#2E86AB", linewidth=1.8)

    # 50% marker
    ax.scatter(
        [50], [median], s=70, color="#222222", zorder=5,
        edgecolors="white", linewidths=1.2,
    )
    _badge(ax, f"median: {median:.2f} bps", 50, median, offset=(10, 0), ha="left")

    # 95% marker
    ax.scatter(
        [95], [p95], s=70, color="#222222", zorder=5,
        edgecolors="white", linewidths=1.2,
    )
    _badge(ax, f"P95: {p95:.2f} bps", 95, p95, offset=(-10, 0), ha="right")

    # Max marker (100%)
    ax.scatter(
        [100], [mx], s=90, color="#E63946", zorder=5,
        edgecolors="white", linewidths=1.4,
    )
    _badge(
        ax,
        f"worst minute: {mx:.2f} bps",
        100,
        mx,
        offset=(-10, 0),
        ha="right",
        color="#E63946",
        bold=True,
        edge="#f0c7cb",
    )

    ax.set_xlabel("Percentile of snapshots (sorted from cheapest to costliest)")
    ax.set_ylabel("Slippage (bps)")
    ax.set_title(TITLE)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, mx * 1.1)
    ax.set_xticks([0, 25, 50, 75, 95, 100])
    ax.grid(alpha=0.25)
    _clean_spines(ax)
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def plot_threebar(
    median: float, p95: float, mx: float, out: Path
) -> None:
    fig, ax = plt.subplots(figsize=(9, 5.5))
    labels = ["median\n(50% of moments)", "P95\n(top 5% worst)", "worst minute"]
    vals = [median, p95, mx]
    colors = ["#2E86AB", "#2E86AB", "#E63946"]

    bars = ax.bar(
        labels, vals, color=colors, edgecolor="white", linewidth=1.5, width=0.55
    )
    for bar, val in zip(bars, vals, strict=False):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            val + mx * 0.02,
            f"{val:.2f} bps",
            ha="center",
            va="bottom",
            fontsize=14,
            fontweight="bold",
            color="#222222",
        )

    ax.set_ylabel("Slippage (bps)")
    ax.set_title(TITLE)
    ax.set_ylim(0, mx * 1.18)
    ax.grid(axis="y", alpha=0.25)
    _clean_spines(ax)
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    bps = _load_bps()
    median, p95, mx = _stats(bps)
    print(
        f"N={len(bps)}  median={median:.2f}  P95={p95:.2f}  max={mx:.2f}"
    )

    plot_loghist(bps, median, p95, mx, OUT_DIR / "chart_5btc_loghist.png")
    plot_quantile(bps, median, p95, mx, OUT_DIR / "chart_5btc_quantile.png")
    plot_threebar(median, p95, mx, OUT_DIR / "chart_5btc_threebar.png")
    print(f"wrote 3 charts to {OUT_DIR}/")


if __name__ == "__main__":
    main()
