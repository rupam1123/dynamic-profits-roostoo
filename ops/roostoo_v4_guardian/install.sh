#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m py_compile guardian.py
sudo install -d -m 0700 /opt/roostoo-v4-guardian /var/lib/roostoo-v4-guardian
sudo install -m 0700 guardian.py /opt/roostoo-v4-guardian/guardian.py
sudo tee /etc/systemd/system/roostoo-v4-guardian.service >/dev/null <<'UNIT'
[Unit]
Description=Independent Roostoo V4 liveness and safe-recovery watchdog
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
User=root
EnvironmentFile=-/etc/roostoo-v4-guardian.env
ExecStart=/usr/bin/python3 /opt/roostoo-v4-guardian/guardian.py --apply
UMask=0077
NoNewPrivileges=true
TimeoutStartSec=150
UNIT
sudo tee /etc/systemd/system/roostoo-v4-guardian.timer >/dev/null <<'UNIT'
[Unit]
Description=Check Roostoo V4 every minute
[Timer]
OnBootSec=3min
OnUnitInactiveSec=60s
AccuracySec=15s
Unit=roostoo-v4-guardian.service
[Install]
WantedBy=timers.target
UNIT
# Prevent another indefinitely looping systemd restart storm. Does not restart the running bot.
sudo install -d -m 0755 /etc/systemd/system/roostoo-competition.service.d
sudo tee /etc/systemd/system/roostoo-competition.service.d/30-guarded-restarts.conf >/dev/null <<'UNIT'
[Unit]
StartLimitIntervalSec=600
StartLimitBurst=4
UNIT
if ! sudo test -f /etc/roostoo-v4-guardian.env; then
  sudo sh -c "umask 077; printf 'V4_TG_TOKEN=\nV4_TG_CHAT_ID=\n' > /etc/roostoo-v4-guardian.env"
fi
sudo chmod 600 /etc/roostoo-v4-guardian.env
sudo systemctl daemon-reload
sudo systemctl enable --now roostoo-v4-guardian.timer
sudo systemctl start roostoo-v4-guardian.service
echo 'GUARDIAN_INSTALLED: Strategy and trading journal unchanged.'
echo 'Check: sudo journalctl -u roostoo-v4-guardian -n 15 --no-pager'
echo 'Remote alerts require Telegram token/chat ID in /etc/roostoo-v4-guardian.env'
