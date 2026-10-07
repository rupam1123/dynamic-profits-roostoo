"""Public GET-only eligibility audit. Does not load credentials or touch bot state."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
import sqlite3
from decimal import Decimal
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import urlopen
from competition_v31.strategy import features
from .eligibility import eligible

ROOT=Path(__file__).resolve().parent
SPEC=json.loads((ROOT/'spec.json').read_text())


def get(url):
    with urlopen(url,timeout=25) as response:return json.load(response)


def validate_candles(raw,boundary):
    if not isinstance(raw,list):raise ValueError('Expected candle list')
    rows=[]
    for r in raw:
        if not isinstance(r,list) or len(r)<8:raise ValueError('Invalid candle row')
        start,end=int(r[0]),int(r[6])
        if start>=boundary or end>=boundary:continue
        values=[Decimal(str(v)) for v in r[1:6]];quote=Decimal(str(r[7]))
        if not all(v.is_finite() for v in values+[quote]):raise ValueError('Nonfinite candle')
        op,hi,lo,cl,volume=values
        if min(op,hi,lo,cl)<=0 or volume<0 or quote<0 or not lo<=min(op,cl)<=max(op,cl)<=hi:raise ValueError('Invalid OHLCV')
        if start%3600000 or end!=start+3599999:raise ValueError('Invalid timestamp')
        rows.append((start,*[str(v) for v in values],end,str(quote)))
    if not rows or rows[-1][0]!=boundary-3600000:raise ValueError('Latest completed candle missing')
    if any(b[0]-a[0]!=3600000 for a,b in zip(rows,rows[1:])):raise ValueError('Missing/duplicate/unordered candles')
    return rows


def run(output,database):
    roostoo=get('https://mock-api.roostoo.com/v3/exchangeInfo')
    assets=sorted({r['Coin'] for r in roostoo['TradePairs'].values() if r.get('Unit')=='USD'})
    binance=get('https://data-api.binance.vision/api/v3/exchangeInfo')
    symbols={s['symbol']:s for s in binance['symbols']}
    server=get('https://data-api.binance.vision/api/v3/time')['serverTime']
    boundary=int(server)//3600000*3600000
    def check(asset):
        pair=asset+'/USD';symbol=asset+'USDT';rule=roostoo.get('TradePairs',{}).get(pair,{})
        b=symbols.get(symbol,{})
        row=dict(pair=pair,symbol=symbol,roostoo=rule,binance_status=b.get('status'),base_asset=b.get('baseAsset'),quote_asset=b.get('quoteAsset'),
            asset_type=rule.get('AssetType'),evaluated_universe=asset in SPEC['baseline']+SPEC['additional'])
        if rule.get('Coin')!=asset or rule.get('Unit')!='USD' or b.get('baseAsset')!=asset or b.get('quoteAsset')!='USDT':
            return asset,dict(row,eligible=False,reason='SYMBOL_MAPPING_UNVERIFIED'),[]
        if roostoo.get('IsRunning') is not True or rule.get('CanTrade') is not True or b.get('status')!='TRADING':
            return asset,dict(row,eligible=False,reason='MARKET_UNAVAILABLE'),[]
        try:
            if any(type(rule.get(k)) is not int or not 0<=rule[k]<=12 for k in ('AmountPrecision','PricePrecision')) or not math.isfinite(float(rule['MiniOrder'])) or float(rule['MiniOrder'])<=0:
                return asset,dict(row,eligible=False,reason='INVALID_EXCHANGE_PRECISION_OR_MINIMUM'),[]
        except (KeyError,ValueError,TypeError):
            return asset,dict(row,eligible=False,reason='INVALID_EXCHANGE_RULES'),[]
        rows=[]
        try:
            raw=get('https://data-api.binance.vision/api/v3/klines?'+urlencode(dict(symbol=symbol,interval='1h',limit=1000,endTime=boundary-1)))
            rows=validate_candles(raw,boundary)
            if len(rows)<169:
                row.update(eligible=False,reason='INSUFFICIENT_CONTIGUOUS_HISTORY',closed_hours=len(rows),
                    quote_volume_24h=str(sum(Decimal(r[7]) for r in rows[-24:])),
                    last_close_utc=datetime.fromtimestamp(rows[-1][6]/1000,timezone.utc).isoformat())
                return asset,row,rows
            sig=features(rows,regime=asset=='BTC');ok,why=eligible(sig)
            row.update(eligible=ok,reason=why,closed_hours=len(rows),quote_volume_24h=sig['quote_volume_24h'],
                last_close_utc=datetime.fromtimestamp(rows[-1][6]/1000,timezone.utc).isoformat(),features={k:v for k,v in sig.items() if k!='returns'})
        except Exception as exc:
            rows=[];row.update(eligible=False,reason=type(exc).__name__+': '+str(exc))
        return asset,row,rows
    rows={};candles=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for future in as_completed([pool.submit(check,a) for a in assets]):
            asset,row,batch=future.result();rows[asset]=row
            candles.extend((row['symbol'],)+r for r in batch)
            print(asset,row['reason'],row.get('closed_hours'),flush=True)
    # A fresh public quote snapshot adds an execution-spread check.
    stamp=get('https://mock-api.roostoo.com/v3/serverTime')['ServerTime']
    time.sleep(3.1)
    ticks=get('https://mock-api.roostoo.com/v3/ticker?'+urlencode(dict(timestamp=stamp)))
    for asset,row in rows.items():
        try:
            q=ticks['Data'][row['pair']];bid=float(q['MaxBid']);ask=float(q['MinAsk'])
            spread=ask/bid-1
            row.update(bid=bid,ask=ask,spread_pct=spread*100)
            if abs(int(ticks['ServerTime'])-int(time.time()*1000))>60000:
                row.update(eligible=False,reason='STALE_QUOTE')
            elif ticks.get('Success') is not True or not 0<=spread<=.003 or not bid>0:
                row.update(eligible=False,reason='INVALID_OR_WIDE_QUOTE')
        except Exception:row.update(eligible=False,reason='QUOTE_UNAVAILABLE')
        if row.get('asset_type')!='crypto' or asset in SPEC.get('excluded_from_trading',{}):
            row['technical_checks_passed']=row['eligible']
            row.update(eligible=False,reason=SPEC.get('excluded_from_trading',{}).get(asset,'ASSET_CLASS_REVIEW'))
    result=dict(generated_utc=datetime.now(timezone.utc).isoformat(),mode='PUBLIC_READ_ONLY',
        candle_boundary_utc=datetime.fromtimestamp(boundary/1000,timezone.utc).isoformat(),
        quote_server_time=ticks.get('ServerTime'),assets={a:rows[a] for a in assets},
        note='Current eligibility only. No orders, credentials or service changes; no historical listing or return claim. USDT volume is a USD proxy.')
    database.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(database,timeout=30)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS candles(symbol TEXT,open_time INTEGER,open TEXT,high TEXT,low TEXT,close TEXT,volume TEXT,close_time INTEGER,quote_volume TEXT,PRIMARY KEY(symbol,open_time))')
        db.executemany('INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)',candles)
        db.execute('CREATE TABLE IF NOT EXISTS scans(utc TEXT PRIMARY KEY,body TEXT)')
        db.execute('INSERT OR REPLACE INTO scans VALUES (?,?)',(result['generated_utc'],json.dumps(result)))
    output.parent.mkdir(parents=True,exist_ok=True)
    temporary=output.with_suffix('.tmp');temporary.write_text(json.dumps(result,indent=2)+'\n');temporary.replace(output)
    print('PUBLIC_SCAN_SAVED',str(output),'assets',len(rows),'eligible_crypto',sum(r['eligible'] for r in rows.values()),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch',action='store_true')
    parser.add_argument('--output',type=Path,default=Path('data/universe_watch/latest.json'))
    parser.add_argument('--db',type=Path,default=Path('data/universe_watch/candles.sqlite3'))
    args=parser.parse_args()
    while True:
        try:run(args.output,args.db)
        except Exception as exc:
            print(json.dumps(dict(status='PUBLIC_SCAN_ERROR',error=type(exc).__name__+': '+str(exc))),flush=True)
            if not args.watch:raise
        if not args.watch:return
        time.sleep(max(60,3600-time.time()%3600+120))


if __name__=='__main__':main()
