"""Walk recorded depth snapshots and plot the resulting slippage distributions.

Reads a JSONL file of snapshots (one per line, format:
``{"timestamp": ms, "lastUpdateId": int, "bids": [[p,q]...], "asks": [[p,q]...]}``),
reconstructs each into an OrderBook, runs simulate_market_order at a few fixed
sizes, and writes matplotlib histograms to data/analysis/.

Usage:
    # Default: legacy BTC recording
    .venv/bin/python scripts/slippage_histogram.py

    # New recording, custom symbol label
    .venv/bin/python scripts/slippage_histogram.py \\
        --data-file data/raw/ethusdt_depth.jsonl --label ETH --sizes 10 50 200
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt

from vortexec.core.book import OrderBook
from vortexec.core.simulator import simulate_market_order
from vortexec.core.types import Level, Side, Snapshot

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_FILE = ROOT / "data" / "legacy" / "btcusdt_depth.jsonl"
DEFAULT_OUT_DIR = ROOT / "data" / "analysis"
DEFAULT_SIZES: list[float] = [1.0, 5.0, 25.0, 100.0]
DEFAULT_HEADLINE_SIZE = 5.0
DEFAULT_LABEL = "BTC"


def load_snapshots(path: Path) -> list[Snapshot]:
    snapshots: list[Snapshot] = []
    with path.open() as f:
        for line in f:
            d: dict[str, Any] = json.loads(line)
            ts = datetime.fromtimestamp(d["timestamp"] / 1000.0, tz=timezone.utc)
            snapshots.append(
                Snapshot(
                    bids=[Level(price=p, quantity=q) for p, q in d["bids"]],
                    asks=[Level(price=p, quantity=q) for p, q in d["asks"]],
                    timestamp=ts,
                )
            )
    return snapshots


def compute_results(
    snapshots: list[Snapshot], sizes: list[float]
) -> dict[float, dict[str, list[float]]]:
    results: dict[float, dict[str, list[float]]] = {
        s: {"buy": [], "sell": []} for s in sizes
    }
    book = OrderBook()
    for snap in snapshots:
        book.apply_snapshot(snap)
        for size in sizes:
            buy = simulate_market_order(book, Side.BUY, size)
            sell = simulate_market_order(book, Side.SELL, size)
            if buy.slippage_bps is not None and buy.unfilled_qty == 0.0:
                results[size]["buy"].append(buy.slippage_bps)
            if sell.slippage_bps is not None and sell.unfilled_qty == 0.0:
                results[size]["sell"].append(sell.slippage_bps)
    return results


def compute_time_series(
    snapshots: list[Snapshot], size: float
) -> tuple[list[datetime], list[float]]:
    """Per-snapshot slippage_bps timeline for a single trade size (BUY)."""
    times: list[datetime] = []
    bps: list[float] = []
    book = OrderBook()
    for snap in snapshots:
        book.apply_snapshot(snap)
        result = simulate_market_order(book, Side.BUY, size)
        if result.slippage_bps is not None and result.unfilled_qty == 0.0:
            times.append(snap.timestamp)
            bps.append(result.slippage_bps)
    return times, bps


def print_summary(
    results: dict[float, dict[str, list[float]]], n_snapshots: int
) -> None:
    print(f"\n{n_snapshots} snapshots analysed\n")
    print(
        f"{'Size (BTC)':>10} | {'Side':>4} | {'N':>5} | "
        f"{'min bps':>8} | {'p50 bps':>8} | {'p95 bps':>8} | "
        f"{'max bps':>8} | {'max/p50':>8}"
    )
    print("-" * 86)
    for size in sorted(results):
        for side in ("buy", "sell"):
            vals = results[size][side]
            if not vals:
                print(
                    f"{size:>10.1f} | {side:>4} | {0:>5} | "
                    f"{'-':>8} | {'-':>8} | {'-':>8} | {'-':>8} | {'-':>8}"
                )
                continue
            vals_sorted = sorted(vals)
            mn = vals_sorted[0]
            p50 = statistics.median(vals_sorted)
            p95 = vals_sorted[int(0.95 * (len(vals_sorted) - 1))]
            mx = vals_sorted[-1]
            ratio = mx / p50 if p50 > 0 else float("inf")
            print(
                f"{size:>10.1f} | {side:>4} | {len(vals):>5} | "
                f"{mn:>8.2f} | {p50:>8.2f} | {p95:>8.2f} | "
                f"{mx:>8.2f} | {ratio:>7.1f}x"
            )


def plot_headline(
    vals: list[float],
    n_snapshots: int,
    span_hours: float,
    out_path: Path,
    headline_size: float,
    label: str,
) -> None:
    vals_sorted = sorted(vals)
    mn = vals_sorted[0]
    p50 = statistics.median(vals_sorted)
    p95 = vals_sorted[int(0.95 * (len(vals_sorted) - 1))]
    mx = vals_sorted[-1]

    fig, ax = plt.subplots(figsize=(10, 5.5))
    ax.hist(vals, bins=50, color="#2E86AB", edgecolor="white", linewidth=0.4)
    for x, label, color, style in [
        (mn, f"min  {mn:.2f}", "#2A9D8F", "--"),
        (p50, f"p50  {p50:.2f}", "black", "-"),
        (p95, f"p95  {p95:.2f}", "#F4A261", ":"),
        (mx, f"max  {mx:.2f}", "#E63946", "--"),
    ]:
        ax.axvline(x, color=color, linestyle=style, linewidth=1.4, label=label)
    ax.set_xlabel("Slippage vs. mid (bps)")
    ax.set_ylabel("Number of snapshots")
    ax.set_title(
        f"What it costs to market-buy {headline_size:g} {label} on Binance\n"
        f"{n_snapshots} snapshots over ~{span_hours:.1f} hours of recorded book"
    )
    ax.legend(title="bps", loc="upper right", framealpha=0.9)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def plot_headline_log(
    vals: list[float],
    n_snapshots: int,
    span_hours: float,
    out_path: Path,
    headline_size: float,
    label: str,
) -> None:
    """Same headline histogram, log y-axis to let the tail breathe."""
    vals_sorted = sorted(vals)
    mn = vals_sorted[0]
    p50 = statistics.median(vals_sorted)
    p95 = vals_sorted[int(0.95 * (len(vals_sorted) - 1))]
    mx = vals_sorted[-1]

    fig, ax = plt.subplots(figsize=(10, 5.5))
    ax.hist(vals, bins=50, color="#2E86AB", edgecolor="white", linewidth=0.4)
    for x, label, color, style in [
        (p50, f"p50  {p50:.2f}", "black", "-"),
        (p95, f"p95  {p95:.2f}", "#F4A261", ":"),
        (mx, f"max  {mx:.2f}", "#E63946", "--"),
    ]:
        ax.axvline(x, color=color, linestyle=style, linewidth=1.4, label=label)
    ax.set_yscale("log")
    ax.set_xlabel("Slippage vs. mid (bps)")
    ax.set_ylabel("Number of snapshots (log scale)")
    ax.set_title(
        f"Tail of slippage cost — {headline_size:g} {label} market buy on Binance\n"
        f"{n_snapshots} snapshots over ~{span_hours:.1f} hours"
    )
    ax.legend(title="bps", loc="upper right", framealpha=0.9)
    ax.grid(axis="y", which="both", alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def plot_time_series(
    times: list[datetime],
    bps: list[float],
    out_path: Path,
    headline_size: float,
    label: str,
) -> None:
    """Slippage over wall-clock time across the recording window."""
    if not times:
        return

    p95 = sorted(bps)[int(0.95 * (len(bps) - 1))]
    max_idx = bps.index(max(bps))
    max_time = times[max_idx]
    max_val = bps[max_idx]

    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.scatter(
        times, bps, s=8, alpha=0.4, color="#2E86AB", edgecolors="none",
        label="per-snapshot slippage",
    )
    ax.axhline(p95, color="#F4A261", linestyle=":", linewidth=1.4,
               label=f"p95  {p95:.2f} bps")
    ax.scatter(
        [max_time], [max_val], s=70, color="#E63946", zorder=5,
        label=f"max  {max_val:.2f} bps  ({max_time:%b %d %H:%M} UTC)",
    )
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel("Slippage vs. mid (bps)")
    ax.set_title(
        f"Cost of a {headline_size:g} {label} market buy on Binance, minute-by-minute"
    )
    ax.legend(loc="upper left", framealpha=0.9)
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def plot_size_comparison(
    results: dict[float, dict[str, list[float]]],
    out_path: Path,
    label: str,
) -> None:
    fig, ax = plt.subplots(figsize=(10, 5.5))
    palette = ["#2E86AB", "#A23B72", "#F18F01", "#E63946"]
    sizes_in_order = sorted(results)
    for size, color in zip(sizes_in_order, palette, strict=False):
        vals = results[size]["buy"]
        if not vals:
            continue
        ax.hist(
            vals,
            bins=50,
            alpha=0.55,
            color=color,
            label=f"{size:g} {label}  (median {statistics.median(vals):.1f} bps, "
            f"max {max(vals):.1f} bps)",
        )
    ax.set_xlabel("Slippage vs. mid (bps)")
    ax.set_ylabel("Number of snapshots")
    ax.set_title(f"Slippage distribution by trade size — {label} market buy on Binance")
    ax.legend(loc="upper right", framealpha=0.9)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def main(
    data_file: Path,
    out_dir: Path,
    sizes: list[float],
    headline_size: float,
    label: str,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshots = load_snapshots(data_file)
    span = snapshots[-1].timestamp - snapshots[0].timestamp
    span_hours = span.total_seconds() / 3600.0
    print(f"Source:  {data_file}")
    print(f"Loaded:  {len(snapshots)} snapshots")
    print(f"Span:    {snapshots[0].timestamp} -> {snapshots[-1].timestamp}")
    print(f"         {span_hours:.2f} hours")

    results = compute_results(snapshots, sizes)
    print_summary(results, len(snapshots))

    slug = label.lower()
    headline_path = out_dir / f"slippage_{headline_size:g}{slug}_buy.png"
    plot_headline(
        results[headline_size]["buy"],
        len(snapshots),
        span_hours,
        headline_path,
        headline_size,
        label,
    )

    log_path = out_dir / f"slippage_{headline_size:g}{slug}_buy_log.png"
    plot_headline_log(
        results[headline_size]["buy"],
        len(snapshots),
        span_hours,
        log_path,
        headline_size,
        label,
    )

    times, bps = compute_time_series(snapshots, headline_size)
    timeseries_path = out_dir / f"slippage_{headline_size:g}{slug}_buy_timeseries.png"
    plot_time_series(times, bps, timeseries_path, headline_size, label)

    comparison_path = out_dir / f"slippage_by_size_{slug}_buy.png"
    plot_size_comparison(results, comparison_path, label)

    print(f"\nWrote: {headline_path}")
    print(f"       {log_path}")
    print(f"       {timeseries_path}")
    print(f"       {comparison_path}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-file", type=Path, default=DEFAULT_DATA_FILE)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument(
        "--sizes",
        type=float,
        nargs="+",
        default=DEFAULT_SIZES,
        help="Trade sizes (in base units) to evaluate",
    )
    p.add_argument(
        "--headline-size",
        type=float,
        default=DEFAULT_HEADLINE_SIZE,
        help="Size used for the headline / log / time-series charts",
    )
    p.add_argument(
        "--label",
        default=DEFAULT_LABEL,
        help="Asset label for chart titles (e.g. BTC, ETH, SOL)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.headline_size not in args.sizes:
        args.sizes.append(args.headline_size)
    main(args.data_file, args.out_dir, args.sizes, args.headline_size, args.label)
