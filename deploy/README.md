# Deploying VortExec to a Hetzner VPS

End-to-end walkthrough. Should take ~30 min of active time plus a 24-hour
soak before declaring the deployment production-ready.

## Prerequisites (one-time, from previous sign-ups)

You should already have:

- **Hetzner Cloud** account, verified, with at least one SSH public key uploaded
  in *Security → SSH Keys*.
- **Cloudflare** account with an **R2 bucket** created (e.g. `vortexec-backup`).
  Note the bucket's *account ID*. Generate an *R2 API token* with read/write on
  this bucket: *R2 → Manage R2 API Tokens → Create API token*. Save the access
  key and secret somewhere safe.
- **Healthchecks.io** account with **two checks**:
  - One named "vortexec-live", period 1 minute, grace 5 minutes — the live
    service pings this every 60s.
  - One named "vortexec-diffcount", period 1 day, grace 1 day — the daily
    cron pings this after verifying yesterday's data.

  Note the ping URLs (`https://hc-ping.com/<UUID>`) for both.

Have these five strings handy before you start:

```
HETZNER_SSH_KEY_NAME=...           # name you gave the key in Hetzner
R2_ACCOUNT_ID=...
R2_ACCESS_KEY=...
R2_SECRET=...
HC_LIVE_URL=https://hc-ping.com/...
HC_DIFFCOUNT_URL=https://hc-ping.com/...
```

---

## 1. Provision the server (Hetzner web UI)

1. *Cloud Console → New project → "vortexec"*.
2. *Servers → Add server*:
   - **Location**: Ashburn (`ash`) or Falkenstein (`fsn1`). Ashburn is marginally
     closer to Binance's edge; either works.
   - **Image**: Ubuntu 24.04
   - **Type**: Shared vCPU → CX22 (€5.83/mo)
   - **SSH keys**: select the one you uploaded
   - **Name**: `vortexec-1`
3. *Volumes → Add volume*: 100 GB, same location as the server. Attach to
   `vortexec-1`. **Format**: ext4. **Mount**: `/mnt/HC_Volume_<ID>` (Hetzner
   shows the exact mountpoint).
4. Note the public IPv4 of the server (in the server dashboard).

Expected total cost: **~€10.20/month**.

---

## 2. Initial VPS prep (one ssh session)

From your laptop:

```bash
# Replace with your IP
export VPS=root@<IP>

# Verify ssh works
ssh "$VPS" "uname -a && cat /etc/os-release | grep PRETTY_NAME"
# expect: Linux ... Ubuntu 24.04 LTS

# Bind-mount the volume to where the service expects data.
# Hetzner auto-mounts volumes at /mnt/HC_Volume_<ID>; find yours:
ssh "$VPS" "df -h | grep HC_Volume"

# Then bind-mount it to /var/lib/vortexec/data (the data dir the service uses)
ssh "$VPS" '
mkdir -p /var/lib/vortexec
ln -s /mnt/HC_Volume_*/  /var/lib/vortexec/data
ls -la /var/lib/vortexec/
'
```

(Using a symlink is simpler than a bind mount in fstab and survives reboots
because the volume auto-mounts.)

---

## 3. Rsync the repo from your laptop

```bash
# From your laptop, in the VortExec/ project directory
cd "/Users/anshshetty/Library/Mobile Documents/com~apple~CloudDocs/VortExec"

# Push code (excludes venv and data — those get rebuilt on the VPS)
rsync -avz \
    --exclude .venv \
    --exclude data \
    --exclude __pycache__ \
    --exclude '*.pyc' \
    --exclude .pytest_cache \
    --exclude .mypy_cache \
    ./ "$VPS:/opt/vortexec/"
```

---

## 4. Run setup.sh

```bash
ssh "$VPS" "bash /opt/vortexec/deploy/setup.sh"
```

This installs Python 3.12, creates the `vortexec` service user, builds the
venv with all deps (pyarrow included), installs the systemd unit, sets up
the cron jobs, and verifies NTP is on.

Expected output ends with:

```
Next steps:
  1. EDIT /etc/vortexec/env  — set symbols, Healthchecks URLs, R2 bucket
  2. Configure rclone for R2:  sudo -u vortexec rclone config
  3. Start the service:  systemctl enable --now vortexec
  ...
```

---

## 5. Edit `/etc/vortexec/env`

```bash
ssh "$VPS"
sudo nano /etc/vortexec/env
```

Fill in:

```
VORTEXEC_SYMBOLS="BTCUSDT ETHUSDT SOLUSDT"
VORTEXEC_DATA_DIR=/var/lib/vortexec/data
VORTEXEC_SNAPSHOT_INTERVAL=600
VORTEXEC_HEALTHCHECKS_URL=<HC_LIVE_URL>
VORTEXEC_DIFFCOUNT_HC_URL=<HC_DIFFCOUNT_URL>
VORTEXEC_MIN_DIFFS_PER_SYMBOL_DAY=100000
R2_BUCKET=vortexec-backup
R2_PREFIX=production
```

Save and exit.

---

## 6. Configure rclone for R2 (interactive, one-time)

```bash
sudo -u vortexec rclone config
```

- `n` (new remote)
- name: `r2`
- type: `s3`
- provider: `Cloudflare`
- env_auth: `false`
- access_key_id: `<R2_ACCESS_KEY>`
- secret_access_key: `<R2_SECRET>`
- region: leave blank
- endpoint: `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`
- location_constraint: leave blank
- ACL: leave blank
- Edit advanced config: `n`
- Keep this remote: `y`
- Quit: `q`

Test:

```bash
sudo -u vortexec rclone lsd r2:
# should list your buckets (vortexec-backup)
```

---

## 7. Manual smoke test (5 min — do NOT enable systemd yet)

Verify the python invocation works before wrapping it in systemd, so a
later failure narrows the search to one of "config | systemd | code"
instead of "all three".

```bash
sudo -u vortexec bash -c '
source /etc/vortexec/env
cd /opt/vortexec
.venv/bin/python -m vortexec \
    --symbols $VORTEXEC_SYMBOLS \
    --record-to $VORTEXEC_DATA_DIR \
    --snapshot-interval 60 \
    --healthchecks-url $VORTEXEC_HEALTHCHECKS_URL
'
```

(Using `--snapshot-interval 60` for the smoke test only — so we see a
snapshot get written within a minute instead of waiting 10.)

Expected in the logs:

- `starting 3 symbol(s) on venue=binance: BTCUSDT,ETHUSDT,SOLUSDT ...`
- `healthchecks ping enabled (every 60s)`
- Within ~10s: `book bid=... ask=... healthy=True resync=0 drop=0` lines
- Within ~60s: `recorder opened /var/lib/vortexec/data/binance/.../.parquet`
- Within ~60s: `recorder wrote snapshot: ...`
- On your Healthchecks.io dashboard: the live check switches to green within
  a minute.

**Press Ctrl+C** to stop. Should shut down cleanly in 5-10s.

If anything goes wrong here, **don't proceed to step 8**. Fix it first.

---

## 8. Enable the systemd service for real

```bash
sudo systemctl enable --now vortexec
sudo systemctl status vortexec      # expect: active (running)
sudo journalctl -u vortexec -f      # watch a few minutes of log
```

Same log lines as step 7. Hit `Ctrl+C` to detach from `journalctl` (service
keeps running).

---

## 9. The 24-hour soak

Walk away. Come back 24+ hours later and verify:

```bash
# Service still up?
sudo systemctl status vortexec
# Disk growing?
du -sh /var/lib/vortexec/data
# Today's files present?
ls -la /var/lib/vortexec/data/binance/BTCUSDT/$(date -u +%Y-%m-%d)/
ls /var/lib/vortexec/data/binance/BTCUSDT/$(date -u +%Y-%m-%d)/snapshots/
# Daily backup landed?
sudo -u vortexec rclone lsd r2:vortexec-backup/production/binance/
# Daily diff-count ran?
tail /var/log/vortexec/diff_count.log
# Healthchecks dashboard — both checks should be green.
```

If all of these are healthy, you're done. The recorder will keep accumulating
data without further intervention.

---

## Day-to-day operations

```bash
# Check status
ssh "$VPS" "sudo systemctl status vortexec"

# Tail live logs
ssh "$VPS" "sudo journalctl -u vortexec -f"

# Disk usage
ssh "$VPS" "du -sh /var/lib/vortexec/data"

# Pull recent data back to your laptop for analysis
rsync -avz "$VPS:/var/lib/vortexec/data/" ~/vortexec_data_pulled/

# Apply a code update from your laptop
rsync -avz --exclude .venv --exclude data ./ "$VPS:/opt/vortexec/"
ssh "$VPS" "
    cd /opt/vortexec
    .venv/bin/pip install -e .  # only if dependencies changed
    sudo systemctl restart vortexec
"

# Stop recording (e.g. moving to a new VPS)
ssh "$VPS" "sudo systemctl disable --now vortexec"
```

---

## Failure-mode quick reference

| Symptom | First check |
|---|---|
| Healthchecks alert "vortexec-live down" | `journalctl -u vortexec -n 100` |
| Service constantly restarting | Same — look for the exception before each restart |
| Disk fills up (alert at 80% via `df`) | `du -sh /var/lib/vortexec/data/binance/*/*` to find the biggest |
| Backup not landing in R2 | `tail /var/log/vortexec/backup.log` |
| Diff-count cron silent | `tail /var/log/vortexec/diff_count.log` |
| SSH lost (key rotation) | Use Hetzner's web console (in the server dashboard → Console) |

Five-minute operations. That's the whole surface area.
