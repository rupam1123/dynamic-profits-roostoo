V4 guardian: deployment and operations
======================================
Purpose: independent health checks, guarded restart for stalls, and Telegram alerts.
It never submits, cancels or retries trading orders. No source file in competition_v4a
is changed, so its source fingerprint and portfolio parameters are untouched.

Installation on the AWS host (ssm-user with sudo):
  bash install.sh

Check the monitor:
  sudo journalctl -u roostoo-v4-guardian -n 30 --no-pager
  systemctl list-timers roostoo-v4-guardian.timer --no-pager

Telegram remote notification (OPTIONAL, but required for phone alerts):
1. Create a Telegram bot via BotFather and obtain its bot token.
2. Message your bot; use Telegram's getUpdates endpoint to find your chat ID.
3. Edit /etc/roostoo-v4-guardian.env as root (mode 0600):
   V4_TG_TOKEN=your_token
   V4_TG_CHAT_ID=your_id
4. Run:
   sudo systemctl start roostoo-v4-guardian.service
   sudo bash -c 'set -a; . /etc/roostoo-v4-guardian.env; set +a; \
     /usr/bin/python3 /opt/roostoo-v4-guardian/guardian.py --test-alert'
The guardian writes all alerts locally even without Telegram configuration:
  sudo tail -n 20 /var/lib/roostoo-v4-guardian/alerts.jsonl

Behavior:
- 60-second watchdog service timer, first check ~3 minutes after startup.
- Service startup grace: 4 minutes.
- Checks current PID's V4_HEARTBEAT, V4_DATA_READY and quote age.
- Allows 2 automatic restarts per 60 minutes, spaced at least 30 minutes.
- Checks execution.sqlite3 for un-reconciled attempts both before and AFTER
  stopping a stale process, preventing restart into uncertain order state.
- Skips restart after account/integrity blocks in latest logs.
- Respects a manual 'systemctl stop roostoo-competition'; it does not start it.
- Caps systemd automatic restart storms at four starts per 10 minutes.
- Alerts about an ongoing outage. Existing exchange positions remain live,
  but trading risk protection may be unavailable during downtime.

Pause watchdog when doing maintenance:
  sudo systemctl stop roostoo-v4-guardian.timer
  sudo systemctl stop roostoo-v4-guardian.service
Resume:
  sudo systemctl start roostoo-v4-guardian.timer

Uninstall without touching bot:
  sudo systemctl disable --now roostoo-v4-guardian.timer
  sudo rm -f /etc/systemd/system/roostoo-v4-guardian.service \
    /etc/systemd/system/roostoo-v4-guardian.timer \
    /etc/systemd/system/roostoo-competition.service.d/30-guarded-restarts.conf
  sudo systemctl daemon-reload

Limitations:
- Telegram will not deliver until configured, tested and reachable from AWS.
- An outage due to account mismatch, open orders, source integrity, external
  market-data unavailability or platform outage requires investigation.
- Read-only quote/data retry loops already exist in the published V4 runtime;
  this independent monitor does not change their internal retry rules.
- This system is not a guarantee of live risk management or uptime.
