"""Retrospective monthly comparison using closed candles and next-hour opens.
No network or orders. This is a simplified research replay, NOT exchange execution.
"""
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
from . import strategy
from .controller import choose, policy


def load(folder):
    markets={}; provenance={}
    for asset in ('BTC','ETH','SOL'):
        matches=list(folder.glob(asset+'USDT_1h_*.csv'))
        if len(matches)!=1: raise ValueError('Supply a folder with exactly one history CSV per BTC/ETH/SOL')
        path=matches[0]; rows=[]
        with path.open(newline='') as handle:
            for row in csv.DictReader(handle):
                stamp=datetime.fromisoformat(row['open_time_utc']).timestamp()
                op,hi,lo,cl,volume=[D(row[k]) for k in ('open','high','low','close','volume')]
                if not all(v.is_finite() for v in (op,hi,lo,cl,volume)) or min(op,hi,lo,cl)<=0 or volume<0 or not lo<=min(op,cl)<=max(op,cl)<=hi:
                    raise ValueError('Invalid OHLCV')
                if stamp%3600 or (rows and stamp-rows[-1][0]/1000!=3600): raise ValueError('Noncontiguous hourly data')
                rows.append((int(stamp*1000),str(op),str(hi),str(lo),str(cl),str(volume),int(stamp*1000)+3599999,str(volume*cl)))
        markets[asset+'/USD']=rows
        provenance[asset]=dict(file=path.name,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),rows=len(rows))
    if any([r[0] for r in rows]!=[r[0] for r in markets['BTC/USD']] for rows in markets.values()): raise ValueError('Histories not aligned')
    return markets,provenance


def simulate(markets,prepared,indices,mode,slip):
    cash=D('100000'); state=dict(initial_usd='100000',usd=str(cash),last_equity=str(cash),positions={},last_exit={},stop_reason=None)
    cfg=policy(0); peak=cash; maxdd=D(0); fills=0; fees=D(0); days=set()
    def equity(prices):
        value=cash
        for p,pos in state['positions'].items():
            q=D(pos['quantity']); px=prices[p]
            if pos['direction']==1: value+=q*px*(1-slip)*D('.999')
            else: value+=D(pos['collateral'])+max(q*(D(pos['entry'])-px*(1+slip)),-D(pos['collateral']))-q*px*(1+slip)*D('.001')
        return value
    for index in indices:
        sig={p:prepared[p][index] for p in markets}
        prices={p:D(rows[index][1]) for p,rows in markets.items()}
        hour=markets['BTC/USD'][index][0]//3600000
        mark=equity(prices); peak=max(peak,mark); maxdd=max(maxdd,1-mark/peak)
        if peak-mark>=3000: state['stop_reason']='PORTFOLIO_DRAWDOWN'
        closed=set()
        for p,pos in list(state['positions'].items()):
            if choose(state,p,hour,sig[p],bool(state['stop_reason'])):
                q=D(pos['quantity']); px=prices[p]*(1-slip if pos['direction']==1 else 1+slip); fee=q*px*D('.001');fees+=fee
                cash+=q*px-fee if pos['direction']==1 else D(pos['collateral'])+max(q*(D(pos['entry'])-px),-D(pos['collateral']))-fee
                del state['positions'][p];state['last_exit'][p]=hour;closed.add(p);fills+=1;days.add(hour//24)
        state['usd']=str(cash);state['last_equity']=str(equity(prices))
        if not state['stop_reason']:
            if mode=='v2':
                info={'TradePairs':{p:{'CanTrade':True} for p in markets}}
                ticks={'Data':{p:{'MaxBid':str(px),'MinAsk':str(px)} for p,px in prices.items()}}
                picks,_=strategy.candidates(state,sig,info,ticks,cfg,hour)
            else:
                picks=[dict(pair=p,action=choose(state,p,hour,sig[p]),budget='10000') for p in markets if p not in state['positions'] and p not in closed and choose(state,p,hour,sig[p])]
            for pick in picks:
                p=pick['pair']
                if p in state['positions'] or p in closed or len(state['positions'])>=3: continue
                direction=1 if pick['action']=='BUY' else -1
                budget=D(pick['budget']);px=prices[p]*(1+slip if direction==1 else 1-slip)
                q=budget/(px*D('1.01101')) if direction==1 else budget/px
                collateral=D(0) if direction==1 else budget;fee=q*px*D('.001')
                cost=q*px+fee if direction==1 else collateral+fee
                if cost>cash: continue
                cash-=cost;fees+=fee;fills+=1;days.add(hour//24)
                state['positions'][p]=dict(direction=direction,quantity=str(q),entry=str(px),collateral=str(collateral))
        # Hour-end mark; this month's final value includes estimated liquidation costs.
        mark=equity({p:D(rows[index][4]) for p,rows in markets.items()})
        peak=max(peak,mark);maxdd=max(maxdd,1-mark/peak)
    return dict(return_pct=float((mark/D('100000')-1)*100),max_drawdown_pct=float(maxdd*100),
                fills=fills,active_days=len(days),fees_usd=float(fees),stop_reason=state['stop_reason'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('data/research/v2_replay.json'))
    args=parser.parse_args(); markets,provenance=load(args.data)
    prepared={p:{i:strategy.features(rows[i-169:i]) for i in range(169,len(rows))} for p,rows in markets.items()}
    months={}
    for i in range(169,len(markets['BTC/USD'])):
        month=datetime.fromtimestamp(markets['BTC/USD'][i][0]/1000,timezone.utc).strftime('%Y-%m')
        months.setdefault(month,[]).append(i)
    results=[]
    for slip in (D('.0005'),D('.0015')):
        for month,indices in months.items():
            for mode in ('baseline','v2'):
                results.append(dict(month=month,mode=mode,slippage_bps=float(slip*10000),**simulate(markets,prepared,indices,mode,slip)))
    summaries=[]
    for cost in (5.,15.):
        for mode in ('baseline','v2'):
            sample=[r for r in results if r['mode']==mode and r['slippage_bps']==cost]
            summaries.append(dict(mode=mode,slippage_bps=cost,months=len(sample),
                mean_month_return_pct=sum(r['return_pct'] for r in sample)/len(sample),
                worst_month_return_pct=min(r['return_pct'] for r in sample),
                worst_month_drawdown_pct=max(r['max_drawdown_pct'] for r in sample),
                mean_month_fills=sum(r['fills'] for r in sample)/len(sample)))
    out=dict(note='RETROSPECTIVE, not unseen validation. Monthly wallets reset to USD100000; first month has 169h warmup. Only BTC/ETH/SOL. Volume filter uses close*base_volume proxy. Next-hour opens approximate minute-2 execution; no intrahour risk polls, spread, exchange precision, outage or limit fill simulation. No evidence for new-universe performance.',
             provenance=provenance,summary=summaries,monthly=results)
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(out,indent=2)+'\n')
    print(json.dumps(summaries,indent=2))

if __name__=='__main__': main()
