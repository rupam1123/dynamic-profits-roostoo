#!/usr/bin/env bash
set -euo pipefail
cd /home/ssm-user/dynamic-profits-roostoo
python3 -m unittest test_universe_watch -q
mkdir -p data/universe_watch
sudo tee /etc/systemd/system/roostoo-universe-watch.service > /dev/null <<'EOF'
[Unit]
Description=Dynamic Profits public universe data collection (no trading)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=ssm-user
WorkingDirectory=/home/ssm-user/dynamic-profits-roostoo
ExecStart=/usr/bin/python3 -u -m universe_research.audit --watch
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=60
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now roostoo-universe-watch
systemctl show roostoo-universe-watch -p ExecStart -p ActiveState --no-pager
echo 'Public collector started. Competition service and trading policy were not changed.'
