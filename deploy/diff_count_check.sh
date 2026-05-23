#!/bin/bash
# Wrapper for diff_count_check.py that sources /etc/vortexec/env first so the
# Python script sees VORTEXEC_DATA_DIR, VORTEXEC_DIFFCOUNT_HC_URL, etc.
# Cron runs with a near-empty environment by default, so without this wrapper
# the Healthchecks ping was silently skipped.
# Installed by setup.sh as a cron job at 05:00 UTC.

set -euo pipefail

# Auto-export every var sourced from the env file so the Python child sees
# them via os.environ. The env file uses bare KEY=value lines (no `export`)
# to stay compatible with systemd's EnvironmentFile= directive; without
# `set -a` they'd be shell-local and invisible to subprocesses.
set -a
# shellcheck disable=SC1091
source /etc/vortexec/env
set +a

exec /opt/vortexec/.venv/bin/python /opt/vortexec/deploy/diff_count_check.py
