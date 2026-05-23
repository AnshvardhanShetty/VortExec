"""Daily sanity check: count yesterday's recorded diffs per (venue, symbol)
and ping Healthchecks.io if everything looks healthy. Silence (no ping) means
the alert fires.

Installed by setup.sh as a cron job at 05:00 UTC. The cron entry must source
/etc/vortexec/env first so HEALTHCHECKS_URL and friends are visible — see
deploy/diff_count_check.sh wrapper.

Required env (sourced from /etc/vortexec/env):
    VORTEXEC_DATA_DIR
    VORTEXEC_DIFFCOUNT_HC_URL     (optional — if unset, no ping is sent)
    VORTEXEC_MIN_DIFFS_PER_SYMBOL_DAY  (defaults to 100_000)
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq

DATA_DIR = Path(os.environ.get("VORTEXEC_DATA_DIR", "/var/lib/vortexec/data"))
HEALTHCHECKS_URL = os.environ.get("VORTEXEC_DIFFCOUNT_HC_URL", "").strip()
MIN_DIFFS_PER_SYMBOL_DAY = int(
    os.environ.get("VORTEXEC_MIN_DIFFS_PER_SYMBOL_DAY", "100000")
)


def yesterday_str() -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%d")


def main() -> int:
    yday = yesterday_str()
    # Layout: DATA_DIR/{venue}/{symbol}/{date}/{HH}.parquet
    files = list(DATA_DIR.glob(f"*/*/{yday}/*.parquet"))
    if not files:
        print(f"FAIL: no diff files found under {DATA_DIR} for {yday}")
        return 1

    totals: dict[tuple[str, str], int] = defaultdict(int)
    unreadable: list[Path] = []
    for f in files:
        try:
            n = pq.ParquetFile(f).metadata.num_rows
        except Exception as e:
            # Don't fail the whole check on one corrupt file — an ungraceful
            # kill (unattended-upgrades, OOM, etc.) can leave a single file
            # without a Parquet footer. Log it, skip it, and let the other
            # files speak for the day. Persistent corruption shows up as a
            # growing list of warnings, not as silent ping suppression.
            print(f"WARN: unreadable parquet file {f}: {e}")
            unreadable.append(f)
            continue
        symbol = f.parent.parent.name
        venue = f.parent.parent.parent.name
        totals[(venue, symbol)] += n

    print(f"Diff counts for {yday} (threshold: {MIN_DIFFS_PER_SYMBOL_DAY:,}/symbol):")
    failures: list[str] = []
    for (venue, symbol), n in sorted(totals.items()):
        marker = "OK " if n >= MIN_DIFFS_PER_SYMBOL_DAY else "LOW"
        print(f"  [{marker}] {venue}/{symbol}: {n:,} diffs")
        if n < MIN_DIFFS_PER_SYMBOL_DAY:
            failures.append(f"{venue}/{symbol}")

    if unreadable:
        print(f"(skipped {len(unreadable)} unreadable file(s))")

    if failures:
        print(f"FAIL: low diff counts: {', '.join(failures)}")
        return 1

    if HEALTHCHECKS_URL:
        try:
            with urllib.request.urlopen(HEALTHCHECKS_URL, timeout=10) as resp:
                if resp.status >= 400:
                    print(f"WARN: healthchecks ping returned HTTP {resp.status}")
        except Exception as e:
            print(f"WARN: healthchecks ping failed: {e!r}")
    else:
        print("(no VORTEXEC_DIFFCOUNT_HC_URL set; skipping ping)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
