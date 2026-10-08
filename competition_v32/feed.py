"""Fetch validated, completed Binance hourly candles (1000 per symbol for 30-day sleeves). No credentials or orders."""
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
CACHE_PATH = Path('data/universe_watch/candles.sqlite3')
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


def refresh(path, symbols=SYMBOLS):
    """Return coherent successes and per-symbol failures; one failure cannot block other exits."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    server = int(get('/api/v3/time')['serverTime'])
    if abs(server-int(time.time()*1000)) > 60000:
        raise ValueError('Machine and exchange clocks differ by over one minute')
    boundary=server//HOUR*HOUR
    metadata={s['symbol']:s for s in get('/api/v3/exchangeInfo')['symbols']}
    cache={};cache_path=CACHE_PATH
    if cache_path.exists():
        try:
            with closing(sqlite3.connect(cache_path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
                db.execute('BEGIN')
                for symbol in symbols:
                    raw=db.execute('SELECT open_time,open,high,low,close,volume,close_time,quote_volume FROM candles WHERE symbol=? AND open_time<? ORDER BY open_time DESC LIMIT 1000',(symbol,boundary)).fetchall()
                    cache[symbol]=[list(r) for r in reversed(raw)]
        except sqlite3.Error:cache={}
    def fetch(symbol):
        rule=metadata.get(symbol,{})
        if rule.get('baseAsset')!=symbol[:-4] or rule.get('quoteAsset')!='USDT' or rule.get('status')!='TRADING':raise ValueError('Exact active Binance mapping unavailable')
        cached=cache.get(symbol,[])
        if cached:
            try:
                clean=validate(cached,boundary)
                volume=[Decimal(str(r[7])) for r in cached]
                if any(not x.is_finite() or x<0 for x in volume):raise ValueError('Invalid cached quote volume')
                return [r+(str(v),) for r,v in zip(clean,volume)]
            except (ValueError,ArithmeticError,IndexError):pass
        raw=get('/api/v3/klines',dict(symbol=symbol,interval='1h',limit=1000,endTime=boundary-1))
        clean=validate(raw,boundary)
        volumes={int(r[0]):Decimal(str(r[7])) for r in raw if int(r[0])<boundary}
        if any(not x.is_finite() or x<0 for x in volumes.values()):
            raise ValueError('Invalid quote volume')
        return [r+(str(volumes[r[0]]),) for r in clean]
    batches={}; failures={}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures={pool.submit(fetch,s):s for s in symbols}
        for future in as_completed(futures):
            symbol=futures[future]
            try: batches[symbol]=future.result()
            except Exception as exc: failures[symbol]=type(exc).__name__+': '+str(exc)
    path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS candles (symbol TEXT, open_time INTEGER, open TEXT, high TEXT, low TEXT, close TEXT, volume TEXT, close_time INTEGER, quote_volume TEXT, PRIMARY KEY(symbol,open_time))')
        for symbol,rows in batches.items():
            db.executemany('INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(symbol,)+r for r in rows])
    return boundary,batches,failures
