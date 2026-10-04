"""Local snapshot-based paper test. Standard library only; no network or credentials."""
import argparse
import hashlib
import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

HOUR = 3600000
PAIRS = ('BTC/USD', 'ETH/USD', 'SOL/USD')
FEE, SLIP = .001, .0005
VERSION = 'snapshot-tsmom-v1'


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def number(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('Invalid price')
    return value


def read_source(path, now):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10) as db:
        rows = db.execute('SELECT server_time, received_utc, exchange_json, ticker_json '
                          'FROM snapshots WHERE server_time >= ? AND server_time <= ? '
                          'ORDER BY server_time', (now - 171 * HOUR, now)).fetchall()
    closes = {p: {} for p in PAIRS}
    latest = None
    for stamp, received, exchange, ticker in rows:
        received_ms = int(datetime.fromisoformat(received).timestamp() * 1000)
        if abs(received_ms - stamp) > 120000 or received_ms > now:
            continue
        info, tick = json.loads(exchange), json.loads(ticker)
        if info.get('IsRunning') is not True or tick.get('Success') is False:
            continue
        data = tick.get('Data', {})
        hour = stamp // HOUR
        for pair in PAIRS:
            try:
                price = number(data[pair]['LastPrice'])
            except (KeyError, TypeError, ValueError):
                continue
            # Last available observation within final 15 minutes of the hour.
            if stamp % HOUR >= 45 * 60000:
                closes[pair][hour] = price
        latest = (stamp, info, data)
    return latest, closes


def initialize(db, source, now):
    db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time_utc TEXT, '
               'snapshot INTEGER, pair TEXT, kind TEXT, detail TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS equity (snapshot INTEGER PRIMARY KEY, time_utc TEXT, value REAL)')
    fingerprint = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
    if row:
        state = json.loads(row[0])
        if state['source'] != str(source.resolve()) or state['code_sha256'] != fingerprint:
            raise ValueError('Source/code changed: preserve this run; use a new --state path for a new experiment.')
        return state
    state = dict(version=VERSION, source=str(source.resolve()), code_sha256=fingerprint,
                 started_ms=now, cursor=now - 1,
                 sleeves={p: dict(cash=50000/3, direction=0, quantity=0., entry=0.,
                                 collateral=0., exit_hour=-1000000, decision_hour=-1) for p in PAIRS})
    db.execute('INSERT INTO state VALUES (1,?)', (json.dumps(state),))
    return state


def event(db, stamp, pair, kind, detail):
    db.execute('INSERT INTO events(time_utc,snapshot,pair,kind,detail) VALUES (?,?,?,?,?)',
               (iso(stamp), stamp, pair, kind, json.dumps(detail)))


def decide(s, hour, close, momentum, bid, ask, allowed):
    direction = s['direction']
    reason = None
    if direction:
        risk = close is not None and direction * (close / s['entry'] - 1) <= -.08
        reverse = momentum is not None and direction * momentum <= 0
        if risk or reverse:
            fill = bid * (1 - SLIP) if direction == 1 else ask * (1 + SLIP)
            qty = s['quantity']; fee = qty * fill * FEE
            if direction == 1:
                s['cash'] += qty * fill - fee
            else:
                s['cash'] += s['collateral'] + max(qty * (s['entry'] - fill), -s['collateral']) - fee
            reason = dict(side='SELL' if direction == 1 else 'SHORT_CLOSE', price=fill,
                          quantity=qty, fee=fee, reason='risk_exit' if risk else 'signal_exit')
            s.update(direction=0, quantity=0., entry=0., collateral=0., exit_hour=hour)
    elif allowed and hour % 24 == 0 and hour - s['exit_hour'] >= 12 and s['cash'] > 0:
        target = 1 if momentum > .01 else -1 if momentum < -.01 else 0
        if target:
            fill = ask * (1 + SLIP) if target == 1 else bid * (1 - SLIP)
            notional = .5 * s['cash'] / (1 + FEE)
            qty = notional / fill; fee = notional * FEE
            s['cash'] -= notional + fee
            s.update(direction=target, quantity=qty, entry=fill,
                     collateral=notional if target == -1 else 0.)
            reason = dict(side='BUY' if target == 1 else 'SHORT_OPEN', price=fill,
                          quantity=qty, fee=fee, reason='signal_entry')
    return reason


def marked(s, bid, ask):
    if s['direction'] == 1:
        return s['cash'] + s['quantity'] * bid * (1 - SLIP)
    if s['direction'] == -1:
        return s['cash'] + s['collateral'] + max(
            s['quantity'] * (s['entry'] - ask * (1 + SLIP)), -s['collateral'])
    return s['cash']


def step(source, target, now=None):
    now = int(time.time() * 1000) if now is None else now
    if source.resolve() == target.resolve():
        raise ValueError('Source and state databases must differ')
    latest, closes = read_source(source, now)
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target, timeout=10) as db:
        db.execute('BEGIN IMMEDIATE')
        state = initialize(db, source, now)
        if latest is None:
            return {'status': 'WAITING_FOR_VALID_DATA'}
        stamp, info, data = latest
        if now - stamp > 10 * 60000:
            return {'status': 'STALE_DATA', 'latest_utc': iso(stamp), 'no_execution': True}
        if stamp <= state['cursor']:
            return {'status': 'WAITING_FOR_NEW_SNAPSHOT', 'started_utc': iso(state['started_ms'])}
        hour = stamp // HOUR
        summary = {}; total = 0.; complete = True
        for pair, s in state['sleeves'].items():
            try:
                bid, ask = number(data[pair]['MaxBid']), number(data[pair]['MinAsk'])
                if bid > ask:
                    raise ValueError('Crossed quote')
            except (KeyError, TypeError, ValueError):
                complete = False
                summary[pair] = {'status': 'INVALID_QUOTE'}
                event(db, stamp, pair, 'INVALID_QUOTE', {})
                continue
            series = closes[pair]
            count = 0
            for h in range(hour - 1, hour - 170, -1):
                if h not in series:
                    break
                count += 1
            close = series.get(hour - 1)
            momentum = close / series[hour - 169] - 1 if count == 169 else None
            status = 'READY' if count == 169 else 'WARMUP_OR_GAP'
            if s['decision_hour'] != hour:
                # Never recreate missed historical fills. Skip a late hourly decision.
                if stamp % HOUR <= 10 * 60000 and now - hour * HOUR <= 10 * 60000:
                    allowed = count == 169 and info.get('TradePairs', {}).get(pair, {}).get('CanTrade') is True
                    trade = decide(s, hour, close, momentum, bid, ask, allowed)
                    if trade:
                        event(db, stamp, pair, 'PAPER_TRADE', trade)
                else:
                    status = 'LATE_HOUR_SKIPPED'
                s['decision_hour'] = hour
                event(db, stamp, pair, 'DECISION', dict(status=status, usable_hours=count, momentum=momentum))
            total += marked(s, bid, ask)
            summary[pair] = dict(status=status, usable_hours=count, required_hours=169,
                                 direction=s['direction'], momentum=momentum)
        state['cursor'] = stamp
        db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(state),))
        if complete:
            db.execute('INSERT INTO equity VALUES (?,?,?)', (stamp, iso(stamp), total))
        return dict(status='PAPER_ONLY', snapshot_utc=iso(stamp),
                    equity=round(total, 2) if complete else None, pairs=summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path('data/market.sqlite3'))
    parser.add_argument('--state', type=Path, default=Path('data/paper/forward.sqlite3'))
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error('Collector database missing: run market_data.py or supply its path with --db')
    while True:
        try:
            print(json.dumps(step(args.db, args.state)), flush=True)
        except (ValueError, KeyError, TypeError, sqlite3.Error, OSError) as exc:
            print(json.dumps({'status': 'ERROR', 'message': str(exc)}), flush=True)
            raise SystemExit(1)
        if not args.watch:
            break
        time.sleep(60)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Paper test stopped. Saved positions resume with the same command.')
