"""Fetch validated, completed Binance hourly candles. No credentials or orders."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlencode
from urllib.request import urlopen

BASE = 'https://data-api.binance.vision'
HOUR = 3600000
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')


def get(path, params=None):
    url = BASE + path + ('?' + urlencode(params) if params else '')
    with urlopen(url, timeout=20) as response:
        return json.load(response)


def validate(rows, boundary):
    if not isinstance(rows, list):
        raise ValueError('Expected candle array')
    clean = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            raise ValueError('Invalid candle format')
        start, end = int(row[0]), int(row[6])
        if start >= boundary or end >= boundary:
            continue
        if start % HOUR or end != start + HOUR - 1:
            raise ValueError('Invalid hourly timestamp')
        values = [Decimal(str(x)) for x in row[1:6]]
        if not all(x.is_finite() for x in values) or min(values[:4]) <= 0 or values[4] < 0:
            raise ValueError('Invalid OHLCV values')
        op, hi, lo, cl, volume = values
        if not lo <= min(op, cl) <= max(op, cl) <= hi:
            raise ValueError('Invalid OHLC range')
        clean.append((start, str(op), str(hi), str(lo), str(cl), str(volume), end))
    if len(clean) < 169:
        raise ValueError('Fewer than 169 completed hourly candles')
    if any(b[0]-a[0] != HOUR for a,b in zip(clean,clean[1:])):
        raise ValueError('Missing, duplicate or unordered candles')
    if clean[-1][0] != boundary-HOUR:
        raise ValueError('Latest completed hour missing')
    return clean


def refresh(path):
    server = int(get('/api/v3/time')['serverTime'])
    if abs(server - int(time.time()*1000)) > 60000:
        raise ValueError('Machine and exchange clocks differ by over one minute')
    boundary = server // HOUR * HOUR
    batches = {}
    for symbol in SYMBOLS:
        rows = get('/api/v3/klines', {'symbol':symbol,'interval':'1h','limit':200,'endTime':boundary-1})
        batches[symbol] = validate(rows,boundary)
    # All pairs validated before committing a coherent snapshot.
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS candles (symbol TEXT, open_time INTEGER, open TEXT, high TEXT, '
                   'low TEXT, close TEXT, volume TEXT, close_time INTEGER, PRIMARY KEY(symbol,open_time))')
        db.execute('CREATE TABLE IF NOT EXISTS refreshes (fetched_ms INTEGER, boundary INTEGER, source TEXT)')
        for symbol, rows in batches.items():
            db.executemany('INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)', [(symbol,)+r for r in rows])
        db.execute('INSERT INTO refreshes VALUES (?,?,?)',(server,boundary,BASE))
    for symbol, rows in batches.items():
        momentum = Decimal(rows[-1][4]) / Decimal(rows[-169][4]) - 1
        print(json.dumps({'status':'CANDLES_READY','symbol':symbol,'closed_hours':len(rows),
                         'last_close_utc':datetime.fromtimestamp((boundary-1)/1000,timezone.utc).isoformat(),
                         'momentum_168h_pct':round(float(momentum*100),4),
                         'entry_signal': 'LONG' if momentum>Decimal('.01') else 'SHORT' if momentum<Decimal('-.01') else 'FLAT',
                         'note':'Readiness/signal only; no orders and no forward-test PnL.'}),flush=True)
    return boundary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=Path('data/candles/recent.sqlite3'))
    parser.add_argument('--watch',action='store_true')
    args=parser.parse_args()
    while True:
        try:
            refresh(args.db)
        except Exception as exc:
            print(json.dumps({'status':'CANDLE_FEED_ERROR','error':str(exc)}),flush=True)
            if not args.watch: raise SystemExit(1)
        if not args.watch: break
        # One refresh per completed hour, at approximately minute 2 UTC.
        delay = 3600 - time.time()%3600 + 120
        time.sleep(delay)


if __name__=='__main__': main()
