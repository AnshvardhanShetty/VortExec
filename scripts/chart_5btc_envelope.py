"""Rolling 15-min cheap/worst envelope across the 16-hour BTC recording.

For every timestamp, plot the cheapest and worst execution within a ±7.5-min
window. The shaded gap between the two lines IS the cost variance the trader
faces by picking one minute vs another — and it shows that gap exists across
the whole day, not just in one cherry-picked window.
"""

from __future__ import annotations

import json
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
WINDOW_SECS = 15 * 60          # rolling window total width
HALF_WINDOW = WINDOW_SECS / 2  # ±7.5 minutes around each point


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


def _rolling_envelope(
    times: list[datetime], bps: list[float]
) -> tuple[list[float], list[float]]:
    """For each point, compute min and max of bps within ±HALF_WINDOW seconds.
    Two-pointer sweep over time-sorted data. O(N·W) where W is avg window size.
    """
    n = len(times)
    min_env: list[float] = [0.0] * n
    max_env: list[float] = [0.0] * n
    left = 0
    right = 0
    for i in range(n):
        while right < n and (times[right] - times[i]).total_seconds() <= HALF_WINDOW:
            right += 1
        while left < n and (times[i] - times[left]).total_seconds() > HALF_WINDOW:
            left += 1
        window = bps[left:right]
        min_env[i] = min(window)
        max_env[i] = max(window)
    return min_env, max_env


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    times, bps, mids = _load_series()
    min_env, max_env = _rolling_envelope(times, bps)

    avg_mid = sum(mids) / len(mids)
    trade_value_k = TRADE_SIZE * avg_mid / 1000
    median_gap_bps = sorted(
        [mx - mn for mx, mn in zip(max_env, min_env, strict=False)]
    )[len(min_env) // 2]
    median_gap_usd = median_gap_bps / 10_000.0 * TRADE_SIZE * avg_mid

    print(f"trade value ≈ ${trade_value_k:,.0f}k")
    print(f"median 15-min gap ≈ {median_gap_bps:.2f} bps  (~${median_gap_usd:,.0f})")
    print(f"max envelope range: {min(max_env):.2f} → {max(max_env):.2f} bps")
    print(f"min envelope range: {min(min_env):.2f} → {max(min_env):.2f} bps")

    fig, ax = plt.subplots(figsize=(11, 6))
    fig.patch.set_facecolor("#fafafa")
    ax.set_facecolor("#fafafa")

    # The "you could have paid anywhere in this band" region
    ax.fill_between(
        times, min_env, max_env,
        color="#888888", alpha=0.16, linewidth=0, zorder=2,
        label=f"cost range available within any 15 minutes",
    )

    ax.plot(
        times, max_env,
        color="#E63946", linewidth=1.5, zorder=4,
        label="worst execution in the surrounding 15 min",
    )
    ax.plot(
        times, min_env,
        color="#2A9D8F", linewidth=1.5, zorder=4,
        label="cheapest execution in the surrounding 15 min",
    )

    # Anchor with a single concrete example callout so the chart isn't pure
    # abstraction — the reader can read off one specific gap.
    max_idx = bps.index(max(bps))
    example_t = times[max_idx]
    example_max = max_env[max_idx]
    example_min = min_env[max_idx]
    example_gap_usd = (
        (example_max - example_min) / 10_000.0 * TRADE_SIZE * mids[max_idx]
    )
    ax.annotate(
        f"e.g. at {example_t:%H:%M UTC}\n"
        f"cheap: {example_min:.2f} bps\n"
        f"worst: {example_max:.2f} bps\n"
        f"= ~${example_gap_usd:,.0f} difference\n"
        f"on the same trade",
        xy=(example_t, (example_min + example_max) / 2),
        xytext=(-180, 30),
        textcoords="offset points",
        fontsize=11, color="#222222",
        ha="left", va="center",
        bbox=dict(
            boxstyle="round,pad=0.55", facecolor="white",
            edgecolor="#d4ba66", linewidth=1.3, alpha=1.0,
        ),
        arrowprops=dict(
            arrowstyle="->", color="#d4ba66", lw=1.4, alpha=0.85,
            connectionstyle="arc3,rad=-0.15",
        ),
        zorder=7,
    )

    ax.set_title(
        f"Cost of a 5 BTC market buy (~${trade_value_k:,.0f}k) on Binance — "
        f"cheap vs worst minute, every 15 minutes",
        fontsize=14, pad=14, fontweight="semibold",
    )
    ax.set_ylabel("Slippage (bps)", fontsize=12)
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_xlim(times[0], times[-1])
    ax.set_ylim(-0.1, max(max_env) * 1.20)
    ax.tick_params(axis="both", labelsize=10)
    ax.legend(loc="upper left", framealpha=0.95, fontsize=10)
    ax.grid(alpha=0.25)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    fig.tight_layout()
    out = OUT_DIR / "chart_5btc_envelope.png"
    fig.savefig(out, dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
