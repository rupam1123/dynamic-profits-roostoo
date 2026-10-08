#!/usr/bin/env python3
"""Independent V4 watchdog. Never submits, cancels, or retries trade orders."""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SERVICE = 'roostoo-competition'
ROOT = Path('/var/lib/roostoo-v4-guardian')
DB = Path('/home/ssm-user/dynamic-profits-roostoo/data/competition/execution.sqlite3')
STATE = ROOT / 'state.json'
ALERTS = ROOT / 'alerts.jsonl'
HEARTBEAT_AGE = 135
DATA_AGE = 330
QUOTE_AGE = 90
STARTUP_GRACE = 240
RESTART_COOLDOWN = 1800
MAX_RESTARTS_60M = 2


def command(*args, timeout=15):
    p = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=timeout, check=False)
    if p.returncode != 0:
        raise RuntimeError('command failed: ' + ' '.join(args) + ': ' + p.stderr[:250])
    return p.stdout


def service_info():
    values = {}
    for line in command('systemctl', 'show', SERVICE, '-p', 'ActiveState', '-p', 'SubState',
                        '-p', 'MainPID', '-p', 'ActiveEnterTimestampMonotonic', '-p', 'Result').splitlines():
        key, _, value = line.partition('=')
        values[key] = value
    return values


def read_events():
    output = command('journalctl', '-u', SERVICE, '-b', '--since', '13 minutes ago',
                     '--no-pager', '-o', 'json', timeout=20)
    result = []
    for line in output.splitlines():
        try:
            row = json.loads(line)
            stamp = float(row.get('__REALTIME_TIMESTAMP', 0)) / 1_000_000
            message = str(row.get('MESSAGE', ''))
            data = json.loads(message) if message.startswith('{') else {}
            result.append((stamp, str(row.get('_PID', '')), message, data))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return result


def unresolved_count():
    if not DB.is_file():
        raise RuntimeError('execution database missing')
    con = sqlite3.connect(DB.resolve().as_uri() + '?mode=ro', uri=True, timeout=4)
    try:
        row = con.execute("SELECT count(*) FROM attempts WHERE status != 'APPLIED'").fetchone()
        return int(row[0])
    finally:
        con.close()


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(value):
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, sort_keys=True))
    tmp.replace(STATE)


def alert(kind, info, state, now, force=False):
    prior = state.setdefault('alert_times', {}).get(kind, 0)
    if not force and now - prior < 900:
        return
    state['alert_times'][kind] = now
    message = '[Roostoo V4] ' + kind + ': ' + info
    record = {'utc_epoch': now, 'kind': kind, 'message': info}
    with ALERTS.open('a') as f:
        f.write(json.dumps(record) + '\n')
    print('V4_GUARDIAN_ALERT ' + message, flush=True)
    token = os.environ.get('V4_TG_TOKEN', '').strip()
    chat = os.environ.get('V4_TG_CHAT_ID', '').strip()
    if not (token and chat):
        print('V4_GUARDIAN_REMOTE_ALERT_UNCONFIGURED', flush=True)
        return
    try:
        body = urlencode({'chat_id': chat, 'text': message}).encode()
        request = Request('https://api.telegram.org/bot' + token + '/sendMessage', data=body)
        with urlopen(request, timeout=8) as result:
            answer = json.load(result)
        if answer.get('ok') is not True:
            raise ValueError('Telegram delivery not accepted')
        print('V4_GUARDIAN_NOTIFICATION_SENT', flush=True)
    except Exception as exc:
        # Don't log the URL or token; keep the failure local for diagnosis.
        print('V4_GUARDIAN_NOTIFICATION_FAILED ' + type(exc).__name__, flush=True)


def service_age(info):
    try:
        seconds = int(info.get('ActiveEnterTimestampMonotonic') or 0) / 1_000_000
        uptime = float(Path('/proc/uptime').read_text().split()[0])
        return max(0, uptime - seconds) if seconds else 0
    except (OSError, ValueError):
        return 0


def evaluate(info, events, now):
    active = info.get('ActiveState')
    if active == 'inactive':
        return 'MANUALLY_STOPPED', 'service is inactive; manual stop is respected', False
    if active == 'activating':
        return 'STARTING', 'systemd is starting or auto-restarting the service', False
    if active != 'active' or info.get('SubState') != 'running':
        return 'SERVICE_DOWN', 'service state=' + str(active) + '/' + str(info.get('SubState')), True
    if service_age(info) < STARTUP_GRACE:
        return 'STARTING', 'startup grace period', False

    pid = str(info.get('MainPID'))
    live = [(t, d) for t, p, _, d in events if p == pid]
    heartbeats = [(t, d) for t, d in live if d.get('status') == 'V4_HEARTBEAT']
    data = [(t, d) for t, d in live if d.get('status') == 'V4_DATA_READY']
    if not heartbeats or now - heartbeats[-1][0] > HEARTBEAT_AGE:
        return 'STALE_HEARTBEAT', 'no current V4 heartbeat in >135 seconds', True
    if not data or now - data[-1][0] > DATA_AGE:
        return 'STALE_DATA', 'no V4_DATA_READY event in >330 seconds', True
    latest = heartbeats[-1][1]
    age = latest.get('quote_age_seconds')
    try:
        age = float(age)
    except (ValueError, TypeError):
        age = float('inf')
    if age > QUOTE_AGE or int(latest.get('dynamic_assets') or 0) == 0:
        return 'STALE_QUOTES', 'quote age=' + str(latest.get('quote_age_seconds')) + 's, dynamic_assets=' + str(latest.get('dynamic_assets')), True
    return 'HEALTHY', 'quotes, market data and heartbeat fresh', False


def run(mode):
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = time.time()
    state = load_state()
    if mode == '--test-alert':
        alert('TEST', 'delivery test; no trading action', state, now, force=True)
        save_state(state)
        return

    info = service_info()
    events = read_events()
    kind, detail, unsafe = evaluate(info, events, now)
    previous = state.get('kind')
    count = (state.get('bad_checks', 0) + 1 if kind == previous and unsafe else 1 if unsafe else 0)
    state.update(kind=kind, bad_checks=count, last_check=now, detail=detail)
    print('V4_GUARDIAN_CHECK ' + json.dumps(dict(kind=kind, detail=detail, bad_checks=count)), flush=True)

    if kind == 'HEALTHY' and previous not in ('HEALTHY', None):
        alert('RECOVERED', 'service and quote feed healthy again', state, now)
    if unsafe and (count >= 2 or kind == 'SERVICE_DOWN'):
        alert(kind, detail, state, now)

    # Respect user-initiated stop and ongoing systemd restarts.
    if not unsafe or mode != '--apply' or count < 4 or info.get('ActiveState') not in ('active', 'failed'):
        save_state(state)
        return

    # Never automatically restart if the previous process reported an account/integrity block.
    blocked = any('controller.Blocked:' in msg or 'Code changed; preserve journal' in msg
                  or 'Pending order count is nonzero' in msg for t, _, msg, _ in events if now-t < 780)
    if blocked:
        alert('RESTART_BLOCKED', 'fatal account/integrity block; manual review required', state, now)
        save_state(state)
        return
    try:
        unfinished = unresolved_count()
    except (OSError, sqlite3.Error, RuntimeError) as e:
        alert('RESTART_BLOCKED', 'cannot verify execution journal: ' + type(e).__name__, state, now)
        save_state(state)
        return
    if unfinished:
        alert('RESTART_BLOCKED', str(unfinished) + ' unresolved execution attempt(s); no automatic restart', state, now)
        save_state(state)
        return

    recent = [float(t) for t in state.get('restart_times', []) if now - float(t) < 3600]
    if len(recent) >= MAX_RESTARTS_60M or (recent and now - recent[-1] < RESTART_COOLDOWN):
        alert('RESTART_LIMIT', 'watchdog recovery limited to 2 restarts/hour, 30-minute spacing', state, now)
        save_state(state)
        return

    # Quiesce the writer first, then recheck its durable journal. A concurrent
    # order can be reserved between the first check and systemctl stop.
    # Never restart until *after* the stopped process has left a clean journal.
    state['restart_times'] = recent + [now]
    save_state(state)
    try:
        if info.get('ActiveState') == 'active':
            command('systemctl', 'stop', SERVICE, timeout=90)
        # Re-read only after the previous writer has fully stopped.
        leftovers = unresolved_count()
        if leftovers:
            alert('RESTART_BLOCKED', str(leftovers) + ' execution attempt(s) unresolved after stop; service left stopped for review', state, now, force=True)
            save_state(state)
            return
        command('systemctl', 'start', SERVICE, timeout=30)
        print('V4_GUARDIAN_SAFE_RESTART_REQUESTED', flush=True)
        alert('RESTARTED', 'stopped, verified clean journal and started service', state, now, force=True)
    except Exception as exc:
        alert('RESTART_FAILED', 'safe recovery failed; check service manually: ' + type(exc).__name__, state, now, force=True)
    save_state(state)


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else '--check'
    if mode not in ('--check', '--apply', '--test-alert'):
        raise SystemExit('usage: guardian.py [--check|--apply|--test-alert]')
    run(mode)
