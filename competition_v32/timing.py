"""Closed five-minute confirmation. Public data only; no orders or credentials."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal as D
import time
from .feed import get

INTERVAL=300000


def validate(rows,boundary):
    out=[]
    for row in rows:
        if not isinstance(row,(list,tuple)) or len(row)<8:raise ValueError('Invalid five-minute candle')
        start,end=int(row[0]),int(row[6])
        if start>=boundary or end>=boundary:continue
        if start%INTERVAL or end!=start+INTERVAL-1:raise ValueError('Invalid five-minute timestamp')
        op,hi,lo,cl,vol,quote=[D(str(row[k])) for k in (1,2,3,4,5,7)]
        if not all(x.is_finite() for x in (op,hi,lo,cl,vol,quote)) or lo<=0 or min(vol,quote)<0 or not lo<=min(op,cl)<=max(op,cl)<=hi:raise ValueError('Invalid five-minute OHLCV')
        out.append(row)
    if len(out)<25 or any(int(b[0])-int(a[0])!=INTERVAL for a,b in zip(out,out[1:])) or int(out[-1][0])!=boundary-INTERVAL:raise ValueError('Missing/stale five-minute candles')
    return out


def feature(rows,boundary):
    rows=validate(rows,boundary)
    closes=[D(str(r[4])) for r in rows]
    mean=sum(closes[-13:-1])/12
    prior_mean=sum(closes[-14:-2])/12
    avg=sum(D(str(r[7])) for r in rows[-13:-1])/12
    volume=D(str(rows[-1][7]))
    return dict(boundary=boundary,close=str(closes[-1]),previous=str(closes[-2]),mean=str(mean),prior_mean=str(prior_mean),high=str(max(D(str(r[2])) for r in rows[-13:-1])),low=str(min(D(str(r[3])) for r in rows[-13:-1])),volume_ratio=str(volume/avg if avg>0 else 0))


def confirms(sig, timing, boundary):
    if not timing or timing.get('boundary')!=boundary:return False
    d=int(sig['direction']);px=D(timing['close']);prev=D(timing['previous'])
    if d not in (-1,1) or D(timing['volume_ratio'])<D('1.1'):return False
    breakout=d*(px-D(timing['high'] if d==1 else timing['low']))>0
    reclaim=d*(prev-D(timing['prior_mean']))<=0 and d*(px-D(timing['mean']))>0
    return (breakout or reclaim) and d*(px-prev)>0


def refresh(pairs):
    server=int(get('/api/v3/time')['serverTime'])
    if abs(server-int(time.time()*1000))>60000:raise ValueError('Five-minute feed clock mismatch')
    boundary=server//INTERVAL*INTERVAL
    def fetch(pair):
        symbol=pair.split('/')[0]+'USDT'
        rows=get('/api/v3/klines',dict(symbol=symbol,interval='5m',limit=40,endTime=boundary-1))
        return feature(rows,boundary)
    data={};failures={}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures={pool.submit(fetch,p):p for p in sorted(set(pairs))}
        for future in as_completed(futures):
            p=futures[future]
            try:data[p]=future.result()
            except Exception as exc:failures[p]=type(exc).__name__+': '+str(exc)
    return boundary,data,failures
