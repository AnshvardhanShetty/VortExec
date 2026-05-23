#!/bin/bash
# Wrapper that systemd ExecStart points at. Reads /etc/vortexec/env and
# builds the python -m vortexec invocation. Keeps the systemd unit free of
# argument-parsing concerns.
set -euo pipefail

# shellcheck disable=SC1091
source /etc/vortexec/env

cd /opt/vortexec

# Symbols expand unquoted so each becomes its own argv item.
args=(
    --symbols ${VORTEXEC_SYMBOLS}
    --record-to "${VORTEXEC_DATA_DIR}"
    --snapshot-interval "${VORTEXEC_SNAPSHOT_INTERVAL:-600}"
)

if [ -n "${VORTEXEC_HEALTHCHECKS_URL:-}" ]; then
    args+=(--healthchecks-url "${VORTEXEC_HEALTHCHECKS_URL}")
fi

exec .venv/bin/python -m vortexec "${args[@]}"
