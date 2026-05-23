"""Two-moments chart, zoomed: same 5 BTC trade, 14 minutes apart, very
different cost. Cropped to a single trading hour so the two moments dominate
the frame and "14 minutes apart" reads as a real distance on the x-axis.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    times, bps = _load_series()

    # Worst moment
    max_idx = bps.index(max(bps))
    worst_t = times[max_idx]
    worst_b = bps[max_idx]

    # Cheap moment within ±15 minutes of worst
    candidates = [
        (t, b)
        for t, b in zip(times, bps, strict=False)
        if 0 < abs((t - worst_t).total_seconds()) <= 15 * 60
    ]
    candidates.sort(key=lambda x: x[1])
    cheap_t, cheap_b = candidates[0]
    delta_min = (worst_t - cheap_t).total_seconds() / 60

    # Zoom window: 1 hour centred on the worst minute
    window_start = worst_t.replace(minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(hours=1)
    window_pairs = [
        (t, b) for t, b in zip(times, bps, strict=False)
        if window_start <= t <= window_end
    ]
    win_times = [t for t, _ in window_pairs]
    win_bps = [b for _, b in window_pairs]

    print(f"Cheap:  {cheap_t}  →  {cheap_b:.4f} bps")
    print(f"Worst:  {worst_t}  →  {worst_b:.4f} bps")
    print(f"Apart:  {delta_min:.1f} minutes")
    print(f"Window: {window_start} → {window_end}  ({len(win_pairs := window_pairs)} dots)")

    fig, ax = plt.subplots(figsize=(10, 7))
    fig.patch.set_facecolor("#fafafa")

    # Backdrop: all snapshots in this hour. Bigger and more visible than the
    # full-16h version — there's only ~240 of them in this window.
    ax.scatter(
        win_times, win_bps, s=22, alpha=0.32, color="#2E86AB",
        edgecolors="none", zorder=2,
    )

    # Connecting line — vertical-ish jump, dramatic
    ax.plot(
        [cheap_t, worst_t], [cheap_b, worst_b],
        color="#666666", linestyle=":", linewidth=2.0, alpha=0.75, zorder=3,
    )

    # Cheap marker
    ax.scatter(
        [cheap_t], [cheap_b], s=420, color="#2A9D8F",
        edgecolors="white", linewidths=2.2, zorder=6,
    )
    ax.annotate(
        f"{cheap_t:%H:%M UTC}\n0.00 bps",
        xy=(cheap_t, cheap_b),
        xytext=(-25, 55),
        textcoords="offset points",
        fontsize=14, fontweight="bold", color="#1f7a6f",
        ha="right", va="bottom",
        bbox=dict(
            boxstyle="round,pad=0.55", facecolor="white",
            edgecolor="#2A9D8F", linewidth=1.4, alpha=1.0,
        ),
        arrowprops=dict(arrowstyle="-", color="#2A9D8F", lw=1.3, alpha=0.85),
    )

    # Worst marker
    ax.scatter(
        [worst_t], [worst_b], s=420, color="#E63946",
        edgecolors="white", linewidths=2.2, zorder=6,
    )
    ax.annotate(
        f"{worst_t:%H:%M UTC}\n{worst_b:.2f} bps",
        xy=(worst_t, worst_b),
        xytext=(28, -8),
        textcoords="offset points",
        fontsize=14, fontweight="bold", color="#b71c2a",
        ha="left", va="center",
        bbox=dict(
            boxstyle="round,pad=0.55", facecolor="white",
            edgecolor="#E63946", linewidth=1.4, alpha=1.0,
        ),
        arrowprops=dict(arrowstyle="-", color="#E63946", lw=1.3, alpha=0.85),
    )

    # Time-gap callout
    midpoint_t = cheap_t + (worst_t - cheap_t) * 0.5
    midpoint_b = (cheap_b + worst_b) * 0.5
    ax.annotate(
        f"{int(delta_min)} minutes apart\nsame trade, same exchange",
        xy=(midpoint_t, midpoint_b),
        xytext=(-100, 0),
        textcoords="offset points",
        fontsize=13, color="#333333", fontweight="bold",
        ha="center", va="center",
        bbox=dict(
            boxstyle="round,pad=0.6", facecolor="#fff7d6",
            edgecolor="#d4ba66", linewidth=1.0, alpha=1.0,
        ),
    )

    ax.set_ylabel("Slippage (bps)", fontsize=13)
    ax.set_title(
        "Cost of a 5 BTC market buy on Binance, within one hour",
        fontsize=15, pad=14, fontweight="semibold",
    )
    ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0, 15, 30, 45]))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_xlim(window_start, window_end)
    ax.set_ylim(-0.15, worst_b * 1.18)
    ax.tick_params(axis="both", labelsize=11)
    ax.grid(alpha=0.25)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.set_facecolor("#fafafa")

    fig.tight_layout()
    out = OUT_DIR / "chart_5btc_two_moments.png"
    fig.savefig(out, dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
