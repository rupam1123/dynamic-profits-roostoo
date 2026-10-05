"""Candle-seeded forward paper runner. No signed calls or actual orders."""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from candle_feed import refresh, HOUR, SYMBOLS
from paper_forward import decide, marked
from roostoo_adapter import Client, dec

STATE=Path('data/paper_candles/forward.sqlite3')
CANDLES=Path('data/candles/recent.sqlite3')


def step():
    boundary=refresh(CANDLES)
    hour=boundary//HOUR
    client=Client()
    info=client.request('/v3/exchangeInfo')
    stamp=int(client.request('/v3/serverTime')['ServerTime'])
    quote=client.request('/v3/ticker',{'timestamp':str(stamp)})
    now=int(time.time()*1000)
    if quote.get('Success') is not True or info.get('IsRunning') is not True:
        raise ValueError('Roostoo data unavailable')
    stamp=int(quote['ServerTime'])
    if abs(now-stamp)>60000 or stamp//HOUR!=hour or now//HOUR!=hour:
        raise ValueError('Stale data or hour changed during refresh; retry next cycle')
    signal={}
    with closing(sqlite3.connect(CANDLES.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        for symbol in SYMBOLS:
            rows=db.execute('SELECT close FROM candles WHERE symbol=? AND open_time<? ORDER BY open_time DESC LIMIT 169',(symbol,boundary)).fetchall()
            pair=symbol[:-4]+'/USD'
            latest,older=float(rows[0][0]),float(rows[-1][0])
            if info['TradePairs'][pair].get('CanTrade') is not True: raise ValueError('Pair unavailable')
            bid=float(dec(quote['Data'][pair]['MaxBid'])); ask=float(dec(quote['Data'][pair]['MinAsk']))
            if bid>ask: raise ValueError('Crossed quote')
            signal[pair]=(latest,latest/older-1,bid,ask)
    fingerprint=hashlib.sha256(b''.join(Path(__file__).with_name(name).read_bytes() for name in
                       ('candle_paper.py','candle_feed.py','paper_forward.py','roostoo_adapter.py'))).hexdigest()
    STATE.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(STATE)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY,body TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS observations (hour INTEGER PRIMARY KEY,time_utc TEXT,equity REAL,detail TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY,time_utc TEXT,pair TEXT,detail TEXT)')
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT body FROM state WHERE id=1').fetchone()
        if row:
            state=json.loads(row[0])
            if state['fingerprint']!=fingerprint: raise ValueError('Code changed: preserve experiment and review migration before restarting')
        else:
            state={'fingerprint':fingerprint,'started_utc':datetime.now(timezone.utc).isoformat(),'last_hour':-1,
                   'sleeves':{p:dict(cash=50000/3,direction=0,quantity=0.,entry=0.,collateral=0.,exit_hour=-1000000) for p in signal}}
        if state['last_hour']>=hour:
            return {'status':'ALREADY_PROCESSED_HOUR'}
        timely=max(now,stamp)-boundary<=10*60000
        detail={}; total=0.
        for pair,values in signal.items():
            close,momentum,bid,ask=values; sleeve=state['sleeves'][pair]
            trade=decide(sleeve,hour,close,momentum,bid,ask,True) if timely else None
            utc=datetime.fromtimestamp(stamp/1000,timezone.utc).isoformat()
            if trade: db.execute('INSERT INTO trades(time_utc,pair,detail) VALUES (?,?,?)',(utc,pair,json.dumps(trade)))
            total+=marked(sleeve,bid,ask)
            detail[pair]={'momentum_168h_pct':round(momentum*100,4),'direction':sleeve['direction'],'trade':trade}
        state['last_hour']=hour
        db.execute('INSERT OR REPLACE INTO state VALUES (1,?)',(json.dumps(state),))
        db.execute('INSERT INTO observations VALUES (?,?,?,?)',(hour,utc,total,json.dumps(detail)))
        return {'status':'CANDLE_PAPER_ONLY','equity':round(total,2),
                'decision':'HOURLY_EVALUATED' if timely else 'LATE_HOUR_SKIPPED','pairs':detail}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch',action='store_true')
    args=parser.parse_args()
    while True:
        try:
            print(json.dumps(step()),flush=True)
            success=True
        except Exception as exc:
            print(json.dumps({'status':'CANDLE_PAPER_ERROR','error':str(exc)}),flush=True)
            success=False
            if not args.watch: raise SystemExit(1)
        if not args.watch: break
        time.sleep(3600-time.time()%3600+120 if success else 60)


if __name__=='__main__': main()
