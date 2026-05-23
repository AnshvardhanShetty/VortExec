"""Spike-forest line plot, fully self-labelled.

Every element on the chart explains itself: the title carries the punch line,
each highlighted moment is labelled at the dot, the gradient line has a
colour scale to its right. No callout box, no orphan annotations.

The "typical" endpoint is the moment within ±15 min of the worst whose
slippage is closest to the global median (0.26 bps) — an honest, achievable
comparison rather than a zero-cost fantasy.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import matplotlib.cm as cm
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import LineCollection
from matplotlib.colors import Normalize

from vortexec.core.book import OrderBook
from vortexec.core.simulator import simulate_market_order
from vortexec.core.types import Level, Side, Snapshot

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = Path("/Users/anshshetty/vortexec_recordings/btcusdt_depth.jsonl")
OUT_DIR = ROOT / "data" / "analysis"
TRADE_SIZE = 5.0


def _load_series() -> tuple[list[datetime], list[float], list[float]]:
    times: list[datetime] = []
    bps: list[float] = []
    mids: list[float] = []
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
            mid = book.mid()
            result = simulate_market_order(book, Side.BUY, TRADE_SIZE)
            if (
                result.slippage_bps is not None
                and result.unfilled_qty == 0.0
                and mid is not None
            ):
                times.append(ts)
                bps.append(result.slippage_bps)
                mids.append(mid)
    return times, bps, mids


def _bps_to_usd(bps: float, mid: float) -> float:
    return bps / 10_000.0 * TRADE_SIZE * mid


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    times, bps, mids = _load_series()
    overall_median = statistics.median(bps)

    max_idx = bps.index(max(bps))
    worst_t = times[max_idx]
    worst_b = bps[max_idx]
    worst_usd = _bps_to_usd(worst_b, mids[max_idx])

    nearby = [
        (t, b, m)
        for t, b, m in zip(times, bps, mids, strict=False)
        if 0 < abs((t - worst_t).total_seconds()) <= 15 * 60
    ]
    nearby.sort(key=lambda x: abs(x[1] - overall_median))
    typ_t, typ_b, typ_mid = nearby[0]
    typ_usd = _bps_to_usd(typ_b, typ_mid)
    delta_min = abs((worst_t - typ_t).total_seconds()) / 60
    ratio = worst_b / typ_b if typ_b > 0 else float("inf")

    avg_mid = sum(mids) / len(mids)
    trade_value_k = TRADE_SIZE * avg_mid / 1000

    print(f"trade value ≈ ${trade_value_k:,.0f}k")
    print(f"typical: {typ_t}  {typ_b:.2f} bps  (~${typ_usd:.0f})")
    print(f"worst:   {worst_t}  {worst_b:.2f} bps  (~${worst_usd:.0f})")
    print(f"apart:   {delta_min:.1f} min   ratio: {ratio:.1f}×")

    fig, ax = plt.subplots(figsize=(11, 6))
    fig.patch.set_facecolor("#fafafa")
    ax.set_facecolor("#fafafa")

    # Subtle band over the example window — explained by the two labelled dots
    # sitting inside it, not by a separate caption.
    pad = timedelta(seconds=45)
    band_left = min(typ_t, worst_t) - pad
    band_right = max(typ_t, worst_t) + pad
    ax.axvspan(
        band_left, band_right,
        color="#fff2c7", alpha=0.7, zorder=1, linewidth=0,
    )

    # Colour-graded line: green at low cost, red at high cost.
    x_num = np.array(mdates.date2num(times))
    y_arr = np.array(bps)
    points = np.column_stack([x_num, y_arr]).reshape(-1, 1, 2)
    segments = np.concatenate([points[:-1], points[1:]], axis=1)
    norm = Normalize(vmin=0.0, vmax=worst_b)
    lc = LineCollection(
        segments, cmap="RdYlGn_r", norm=norm, linewidth=0.9, alpha=0.95, zorder=2,
    )
    lc.set_array((y_arr[:-1] + y_arr[1:]) / 2)
    ax.add_collection(lc)

    # Endpoint markers with directly-attached labels — no callout box.
    ax.scatter(
        [typ_t], [typ_b], s=240, color="#2A9D8F",
        edgecolors="white", linewidths=2.0, zorder=6,
    )
    ax.annotate(
        f"typical minute\n{typ_b:.2f} bps  (~${typ_usd:.0f})",
        xy=(typ_t, typ_b),
        xytext=(-22, 60),
        textcoords="offset points",
        fontsize=11, fontweight="bold", color="#1f7a6f",
        ha="right", va="bottom",
        bbox=dict(
            boxstyle="round,pad=0.5", facecolor="white",
            edgecolor="#2A9D8F", linewidth=1.4, alpha=1.0,
        ),
        arrowprops=dict(arrowstyle="-", color="#2A9D8F", lw=1.3, alpha=0.85),
        zorder=7,
    )

    ax.scatter(
        [worst_t], [worst_b], s=240, color="#E63946",
        edgecolors="white", linewidths=2.0, zorder=6,
    )
    ax.annotate(
        f"worst minute\n{worst_b:.2f} bps  (~${worst_usd:.0f})",
        xy=(worst_t, worst_b),
        xytext=(28, 0),
        textcoords="offset points",
        fontsize=11, fontweight="bold", color="#b71c2a",
        ha="left", va="center",
        bbox=dict(
            boxstyle="round,pad=0.5", facecolor="white",
            edgecolor="#E63946", linewidth=1.4, alpha=1.0,
        ),
        arrowprops=dict(arrowstyle="-", color="#E63946", lw=1.3, alpha=0.85),
        zorder=7,
    )

    # Thin colour scale on the right edge — explains the gradient.
    sm = cm.ScalarMappable(cmap="RdYlGn_r", norm=norm)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, fraction=0.022, pad=0.018)
    cbar.set_label("Cost level (bps)", fontsize=10)
    cbar.ax.tick_params(labelsize=9)

    # Title carries the punch line — short.
    ax.set_title(
        f"5 BTC market buy on Binance — "
        f"{int(delta_min)} min apart, ~10× cost",
        fontsize=15, pad=14, fontweight="semibold",
    )
    ax.set_ylabel("Slippage (bps)", fontsize=12)
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_xlim(times[0], times[-1])
    ax.set_ylim(-0.15, worst_b * 1.22)
    ax.tick_params(axis="both", labelsize=10)
    ax.grid(alpha=0.25)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    fig.tight_layout()
    out = OUT_DIR / "chart_5btc_line_highlight.png"
    fig.savefig(out, dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
