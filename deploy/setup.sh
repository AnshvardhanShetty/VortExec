#!/bin/bash
# VPS bootstrap for VortExec. Idempotent — safe to re-run after code update.
#
# Prerequisites on the VPS:
#   - Ubuntu 24.04 LTS (ships Python 3.12, has pyarrow wheels)
#   - The repo has already been copied to /opt/vortexec, e.g.:
#       rsync -avz --exclude .venv --exclude data \
#           /path/to/VortExec/ root@VPS:/opt/vortexec/
#   - A block-storage volume mounted at /mnt/vortexec-data (or DATA_DIR)
#
# Run as root:
#   sudo bash /opt/vortexec/deploy/setup.sh

set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
    echo "ERROR: must run as root (sudo bash ...)" >&2
    exit 1
fi

INSTALL_DIR="${INSTALL_DIR:-/opt/vortexec}"
DATA_DIR="${DATA_DIR:-/var/lib/vortexec/data}"
LOG_DIR="${LOG_DIR:-/var/log/vortexec}"
SERVICE_USER="${SERVICE_USER:-vortexec}"
PY=python3.12

echo "─── 1/8  Installing OS packages ───"
apt-get update -y
apt-get install -y \
    "${PY}" "${PY}-venv" "${PY}-dev" \
    build-essential git rsync curl ca-certificates \
    systemd-timesyncd cron rclone

echo "─── 2/8  Ensuring NTP sync is active ───"
timedatectl set-ntp true
timedatectl status | grep -E "(NTP service|System clock synchronized)"

echo "─── 3/8  Creating service user ───"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/bash "${SERVICE_USER}"
fi

echo "─── 4/8  Creating data + log directories ───"
mkdir -p "${DATA_DIR}" "${LOG_DIR}" /etc/vortexec
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}" "${LOG_DIR}"

echo "─── 5/8  Installing Python deps in venv ───"
if [ ! -d "${INSTALL_DIR}/.venv" ]; then
    sudo -u "${SERVICE_USER}" "${PY}" -m venv "${INSTALL_DIR}/.venv"
fi
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -e "${INSTALL_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

echo "─── 6/8  Installing config + systemd unit ───"
chmod +x "${INSTALL_DIR}/deploy/run.sh" \
         "${INSTALL_DIR}/deploy/backup.sh" \
         "${INSTALL_DIR}/deploy/diff_count_check.sh"
if [ ! -f /etc/vortexec/env ]; then
    cp "${INSTALL_DIR}/deploy/vortexec.env.example" /etc/vortexec/env
    chmod 640 /etc/vortexec/env
    chown root:"${SERVICE_USER}" /etc/vortexec/env
    echo "  → wrote /etc/vortexec/env (template — EDIT before starting service)"
else
    echo "  → /etc/vortexec/env already exists; leaving alone"
fi
cp "${INSTALL_DIR}/deploy/vortexec.service" /etc/systemd/system/
systemctl daemon-reload

echo "─── 7/8  Installing cron jobs (backup + daily check) ───"
cat >/etc/cron.d/vortexec <<'EOF'
# Daily R2 backup at 04:00 UTC
0 4 * * * vortexec /opt/vortexec/deploy/backup.sh >>/var/log/vortexec/backup.log 2>&1
# Daily diff-count health check at 05:00 UTC
0 5 * * * vortexec /opt/vortexec/deploy/diff_count_check.sh >>/var/log/vortexec/diff_count.log 2>&1
EOF
chmod 644 /etc/cron.d/vortexec

echo "─── 8/8  Done ───"
echo ""
echo "Next steps:"
echo "  1. EDIT /etc/vortexec/env  — set symbols, Healthchecks URLs, R2 bucket"
echo "  2. Configure rclone for R2:  sudo -u ${SERVICE_USER} rclone config"
echo "     (create a remote named 'r2', type 's3', provider 'Cloudflare')"
echo "  3. Start the service:  systemctl enable --now vortexec"
echo "  4. Watch logs:  journalctl -u vortexec -f"
echo "  5. Verify Parquet writes:  ls -la ${DATA_DIR}/"
