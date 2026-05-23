#!/bin/bash
# Daily backup of VortExec data to Cloudflare R2.
# Installed by setup.sh as a cron job at 04:00 UTC.
#
# Configure rclone once on the VPS:
#   sudo -u vortexec rclone config
# Create a remote named "r2", type "s3", provider "Cloudflare", with the
# account ID, access key, and secret from the Cloudflare dashboard.

set -euo pipefail

# shellcheck disable=SC1091
source /etc/vortexec/env

: "${R2_BUCKET:?R2_BUCKET must be set in /etc/vortexec/env}"
: "${VORTEXEC_DATA_DIR:?VORTEXEC_DATA_DIR must be set in /etc/vortexec/env}"
R2_PREFIX="${R2_PREFIX:-vortexec}"

echo "[$(date -u +%FT%TZ)] starting backup → r2:${R2_BUCKET}/${R2_PREFIX}/"

# 'sync' deletes remote files no longer present locally — fine here since we
# never prune locally during the recording window. Use 'copy' instead if you
# start rotating local files.
rclone sync \
    "${VORTEXEC_DATA_DIR}/" \
    "r2:${R2_BUCKET}/${R2_PREFIX}/" \
    --exclude '*.tmp' \
    --transfers 4 \
    --checkers 8 \
    --stats-one-line --stats 30s

echo "[$(date -u +%FT%TZ)] backup complete"
