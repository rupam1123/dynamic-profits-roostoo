"""Closed one-minute breakout entry signal with hourly trend context."""
from concurrent.futures import ThreadPoolExecutor,as_completed
from decimal import Decimal as D
import time
from .feed import get


def feature(rows,boundary):
    clean=[]
    for r in rows:
        if len(r)<8:raise ValueError('Invalid 1m candle')
        start,end=int(r[0]),int(r[6])
        if start>=boundary or end>=boundary:continue
        if start%60000 or end!=start+59999:raise ValueError('Invalid 1m timestamp')
        op,hi,lo,cl,vol,qv=[D(str(r[k])) for k in (1,2,3,4,5,7)]
        if not all(x.is_finite() for x in (op,hi,lo,cl,vol,qv)) or lo<=0 or min(vol,qv)<0 or not lo<=min(op,cl)<=max(op,cl)<=hi:raise ValueError('Invalid 1m OHLCV')
        clean.append(r)
    if len(clean)<21 or int(clean[-1][6])!=boundary-1 or any(int(b[0])-int(a[0])!=60000 for a,b in zip(clean,clean[1:])):raise ValueError('Missing/stale 1m candles')
    closes=[D(str(r[4])) for r in clean];avg=sum(D(str(r[7])) for r in clean[-21:-1])/20
    return dict(boundary=boundary,close=str(closes[-1]),previous=str(closes[-2]),mean=str(sum(closes[-6:-1])/5),volume_ratio=str(D(str(clean[-1][7]))/avg if avg>0 else 0),high=str(max(D(str(r[2])) for r in clean[-21:-1])),low=str(min(D(str(r[3])) for r in clean[-21:-1])))


def confirms(signal,bar,boundary):
    if not bar or bar.get('boundary')!=boundary:return False
    d=int(signal['direction']);px=D(bar['close'])
    return d in (-1,1) and d*(px-D(bar['previous']))>0 and d*(px-D(bar['mean']))>0 and D(bar['volume_ratio'])>=1


def refresh(pairs):
    server=int(get('/api/v3/time')['serverTime'])
    if abs(server-int(time.time()*1000))>60000:raise ValueError('One-minute clock mismatch')
    boundary=server//60000*60000;out={};failed={}
    def fetch(p):return feature(get('/api/v3/klines',dict(symbol=p.split('/')[0]+'USDT',interval='1m',limit=25,endTime=boundary-1)),boundary)
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs={pool.submit(fetch,p):p for p in set(pairs)}
        for job in as_completed(jobs):
            p=jobs[job]
            try:out[p]=job.result()
            except Exception as e:failed[p]=type(e).__name__+': '+str(e)
    return boundary,out,failed


def breakout(signal,bar,boundary,cfg):
    if not confirms(signal,bar,boundary):return False
    d=int(signal['direction']);px=D(bar['close']);hi=D(bar['high']);lo=D(bar['low'])
    return D(signal['score'])>=1 and D(bar['volume_ratio'])>=D(cfg['fast_minimum_volume_ratio']) and (hi-lo)/px>=D(cfg['fast_minimum_range']) and d*(px-(hi if d==1 else lo))>0
