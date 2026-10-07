"""Offline actual-controller comparison: deployed 12, requested 22 and all mapped crypto assets.

All API clients are fake; no production files, credentials or trading journals are modified.
The imported controller's configuration is patched only inside this research process.
"""
import argparse
import copy
import csv
from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import numpy as np

ROOT=Path(__file__).resolve().parent
PROJECT=ROOT.parent
from . import replay_support as legacy
from competition_v31 import controller as bot, api, strategy
from .replay_support import Exchange
from .eligibility import eligible, filtered_signals, target_pairs

SPEC=json.loads((ROOT/'spec.json').read_text())
BASE=SPEC['baseline'];EXTRA=SPEC['additional'];ASSETS=BASE+EXTRA
BASE_PAIRS=tuple(a+'/USD' for a in BASE)
EXTRA_PAIRS=tuple(a+'/USD' for a in EXTRA)


def load():
    stamps,data,qv=legacy.load(PROJECT/'history12')
    valid={a:np.ones(len(stamps),dtype=bool) for a in BASE}
    hashes={}
    for asset in ASSETS:
        folder=PROJECT/'history12' if asset in BASE else ROOT/'data'
        path=next(folder.glob(asset+'USDT_1h_*.csv'))
        hashes[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        if asset in BASE:continue
        values={k:np.full(len(stamps),np.nan) for k in ('open','high','low','close','volume')}
        volumes=np.zeros(len(stamps));present=np.zeros(len(stamps),dtype=bool)
        with path.open() as f:
            for row in csv.DictReader(f):
                ts=int(datetime.fromisoformat(row['open_time_utc']).timestamp())
                i=int(np.searchsorted(stamps,ts))
                if i>=len(stamps) or int(stamps[i])!=ts:continue
                if present[i]:raise ValueError('Duplicate candle '+asset)
                parsed={k:float(row[k]) for k in values}
                quote=float(row['quote_volume'])
                if not all(np.isfinite(v) for v in parsed.values()) or not np.isfinite(quote):raise ValueError('Nonfinite candle')
                if min(parsed[k] for k in ('open','high','low','close'))<=0 or parsed['volume']<0 or quote<0:raise ValueError('Invalid candle')
                if not parsed['low']<=min(parsed['open'],parsed['close'])<=max(parsed['open'],parsed['close'])<=parsed['high']:raise ValueError('Invalid OHLC')
                for key,value in parsed.items():values[key][i]=value
                volumes[i]=quote;present[i]=True
        data[asset]=values;qv[asset]=volumes;valid[asset]=present
    return stamps,data,qv,valid,hashes


def prepare(stamps,data,qv,valid,indices):
    # Placeholders keep vectorized arithmetic finite; a strict consecutive-history
    # mask prevents any placeholder from entering an admitted feature or order.
    finite={a:{k:np.where(valid[a],v,1.0 if k!='volume' else 0.0) for k,v in values.items()} for a,values in data.items()}
    features=legacy.features(stamps,finite)
    counts={}
    for asset in ASSETS:
        run=0;counts[asset]=np.zeros(len(stamps),dtype=int)
        for i,ok in enumerate(valid[asset]):
            run=run+1 if ok else 0;counts[asset][i]=run
    prepared={};availability={a:0 for a in EXTRA};max_error=0.
    for i in sorted(indices):
        sig={}
        for asset in ASSETS:
            history=int(counts[asset][i-1]);minimum=169 if asset in BASE else 721
            if history<minimum:continue
            f=features[asset]
            s=dict(close=str(f['close'][i]),momentum=str(f['m168'][i]),momentum_24h=str(f['m24'][i]),
                momentum_72h=str(f['m72'][i]),daily_vol=str(f['vol'][i]),direction=int(f['dir'][i]),score=str(f['score'][i]),
                quote_volume_24h=str(float(qv[asset][i-24:i].sum())),returns=f['lr'][i-72:i].tolist(),
                z24=None if not np.isfinite(f['z24'][i]) else str(f['z24'][i]),r6=str(f['r6'][i]),
                momentum_336h=str(f['m336'][i]) if history>=337 else None,
                momentum_720h=str(f['m720'][i]) if history>=721 else None,candles=min(history,1000))
            if asset=='BTC':s['vol_median_720h']=str(float(np.median(f['vol'][i-720:i:6])))
            sig[asset+'/USD']=s
            if asset in EXTRA and eligible(s)[0]:availability[asset]+=1
            if i%337==0:
                rows=[(int(stamps[j]*1000),*[str(data[asset][k][j]) for k in ('open','high','low','close','volume')],int(stamps[j]*1000)+3599999,str(qv[asset][j])) for j in range(i-min(history,1000),i)]
                direct=strategy.features(rows,regime=asset=='BTC')
                for key in ('momentum','daily_vol','score','z24','momentum_720h','vol_median_720h'):
                    if direct.get(key) is not None and s.get(key) is not None:
                        err=abs(float(s[key])-float(direct[key]));max_error=max(max_error,err)
                        if err>1e-6:raise ValueError('Feature parity failed '+asset+' '+key+' '+str(err))
        prepared[i]=sig
    return prepared,availability,max_error


def run(variant,window,stamps,data,prepared,slip,metadata,policy_override=None):
    assets=BASE if variant=='baseline' else BASE+SPEC.get('requested',EXTRA) if variant=='requested' else ASSETS
    expanded=variant!='baseline';pairs=tuple(a+'/USD' for a in assets)
    selected_extras=tuple(a+'/USD' for a in assets if a not in BASE)
    ex=Exchange();ex.precision={p:metadata['TradePairs'][p]['AmountPrecision'] for p in pairs}
    ex.prices={p:D('100') for p in pairs};ex.slip=D(str(slip));ex.clock=int(stamps[window[0]])
    cfg=bot.raw_policy();cfg['universe']=assets
    if policy_override:
        for sleeve in cfg['sleeves']:
            sleeve['rebalance_hours']=policy_override['rebalance_hours'][sleeve['name']]
    policy=dict(cfg,end_epoch=None);marks=[];days=set();last_writes=0;trace=[];extra_fills={a:0 for a in EXTRA};gross=[]
    spacing=SPEC.get('api_spacing_seconds',3.1);clock_sync=[-1e12];calls=[0];cycle_durations=[];blocked=[0];entry_offsets=[]
    target_function=bot.sleeve_target
    def targets(sleeve,sig,unused,state,mult):
        selected=target_pairs(sig,BASE_PAIRS,selected_extras) if expanded else BASE_PAIRS
        return target_function(sleeve,sig,selected,state,mult)
    request=ex.request
    balance=ex.balance;shorts=ex.short_positions
    def delay(signed=False):
        if signed and ex.clock-clock_sync[0]>30:
            ex.clock+=spacing;calls[0]+=1;clock_sync[0]=ex.clock
        ex.clock+=spacing;calls[0]+=1
    def balance_call():
        delay(True);return balance()
    def short_call():
        delay(True);return shorts()
    def traced(endpoint,params=None,**kw):
        delay(kw.get('signed',False))
        if kw.get('deadline') is not None and ex.clock>=kw['deadline']:
            blocked[0]+=1
            raise api.NotSent('Offline modeled API spacing crossed the entry deadline')
        result=request(endpoint,params,**kw)
        if endpoint in ('/v3/place_order','/v6/short_open','/v6/short_close'):
            trace.append(dict(time=ex.clock,endpoint=endpoint,params=params,response=result))
            if endpoint=='/v6/short_open' or (endpoint=='/v3/place_order' and params.get('side')=='BUY'):
                entry_offsets.append(ex.clock%3600)
            asset=params['pair'].split('/')[0]
            if asset in extra_fills:extra_fills[asset]+=1
        return result
    ex.request=traced
    ex.balance=balance_call;ex.short_positions=short_call
    with patch.object(bot,'PAIRS',pairs),patch.object(api,'PAIRS',pairs),patch.object(bot,'raw_policy',return_value=cfg), \
         patch.object(bot,'policy',return_value=policy),patch.object(bot,'sleeve_target',side_effect=targets), \
         patch.object(bot.time,'time',side_effect=lambda:ex.clock),patch.object(bot,'emit'), \
         patch.object(bot,'fingerprint',return_value='OFFLINE_UNIVERSE_STUDY'):
        with tempfile.TemporaryDirectory() as tmp:
            seed=bot.Store(Path(tmp)/'seed.sqlite3');bot.initialize(ex,seed,ex.clock)
            store=legacy.MemoryStore(seed.load())
        for i in window:
            ex.clock=int(stamps[i])+120
            held=store.load()['positions']
            if any(not np.isfinite(data[p.split('/')[0]]['open'][i]) for p in held):
                raise ValueError('Held asset has no executable price; cannot invent a fill')
            ex.prices={a+'/USD':D(str(data[a]['open'][i])) for a in assets if np.isfinite(data[a]['open'][i])}
            sig={p:s for p,s in prepared[i].items() if p in pairs}
            sig=filtered_signals(sig,BASE_PAIRS,held) if expanded else sig
            with patch.object(bot,'signals',return_value=(int(stamps[i])//3600,sig)):
                bot.cycle(ex,store)
            cycle_durations.append(ex.clock-(int(stamps[i])+120))
            if ex.clock>=int(stamps[i])+3600:raise ValueError('Cycle crosses hourly bar; finer-grained replay required')
            if store.pending():raise ValueError('Unresolved simulated intent')
            state=store.load();mark=float(ex.usd);exposure=0.
            for pair,pos in state['positions'].items():
                qty=float(pos['quantity']);px=float(ex.prices[pair]);coll=float(pos['collateral'])
                exposure+=max(qty*px,coll)
                if pos['direction']==1:mark+=qty*px*(1-slip)*.999
                else:
                    fill=px*(1+slip);mark+=coll+max(qty*(float(pos['entry'])-fill),-coll)-qty*fill*.001
            marks.append(mark);gross.append(exposure/mark)
            if ex.writes>last_writes:days.add((int(stamps[i])+8*3600)//86400)
            last_writes=ex.writes
    equity=np.array([100000]+marks);peak=np.maximum.accumulate(equity)
    return dict(return_pct=(marks[-1]/100000-1)*100,max_drawdown_pct=float(np.max(1-equity/peak)*100),
        fills=ex.writes,fees_usd=float(ex.fees),days_with_fills=len(days),unresolved=0,
        mean_gross_pct=float(np.mean(gross)*100),max_gross_pct=max(gross)*100,extra_fills=extra_fills,
        modeled_api_calls=calls[0],maximum_cycle_seconds=max(cycle_durations),
        not_sent_before_transmission=blocked[0],maximum_entry_second=max(entry_offsets,default=0),
        trace_sha256=hashlib.sha256(json.dumps(trace,sort_keys=True).encode()).hexdigest())


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--small',action='store_true');parser.add_argument('--resume',action='store_true');args=parser.parse_args()
    stamps,data,qv,valid,hashes=load()
    starts=SPEC['starts_gmt8'][:1] if args.small else SPEC['starts_gmt8']
    windows=[]
    for start in starts:
        stamp=int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())-8*3600
        first=int(np.searchsorted(stamps,stamp));windows.append(list(range(first,first+SPEC['window_days']*24)))
    prepared,availability,error=prepare(stamps,data,qv,valid,{i for w in windows for i in w})
    metadata=json.loads((ROOT/'evidence/roostoo_exchange.json').read_text())
    result=dict(spec=SPEC,mode='OFFLINE_RESEARCH_ONLY',feature_max_absolute_error=error,eligible_hours_in_windows=availability,
        data_sha256=hashes,comparisons=[],limits=SPEC['limits']+[
            'Hourly-open prices with 0.1% taker fees; no intrahour path, depth, network delay, outages or partial fills.',
            'Every fake API call advances the clock by 3.1 seconds, including modeled signed clock synchronization; entry deadlines remain active.',
            'Current quantity precision for both alternatives; historical precision changes are not reconstructed.',
            'Missing/prelisting data are never admitted; open-price gaps on held assets abort the replay.',
            'Extra assets receive history/liquidity admission checks. Core correlation and BTC filters retain baseline semantics; small portfolios do not gain new correlation filters.',
            'Ending open holdings include estimated liquidation costs. Mean returns are independent-window averages, not compounded annual returns.'])
    output=ROOT/'evidence'/('comparison_smoke.json' if args.small else 'comparison.json')
    if args.resume and output.exists():
        previous=json.loads(output.read_text())
        if previous['spec']!=SPEC or previous['data_sha256']!=hashes:raise ValueError('Resume inputs changed')
        result['comparisons']=previous['comparisons']
    completed={(r['slippage_bps'],r['start_gmt8']) for r in result['comparisons']}
    for bps in SPEC['slippage_bps']:
        for start,window in zip(starts,windows):
            if (bps,start) in completed:continue
            baseline=run('baseline',window,stamps,data,prepared,bps/10000,metadata)
            requested=run('requested',window,stamps,data,prepared,bps/10000,metadata)
            candidate=run('expanded',window,stamps,data,prepared,bps/10000,metadata)
            row=dict(start_gmt8=start,slippage_bps=bps,baseline=baseline,requested=requested,expanded=candidate,return_difference_pp=candidate['return_pct']-baseline['return_pct'])
            result['comparisons'].append(row);output.write_text(json.dumps(result,indent=2)+'\n')
            print(bps,start,'base',round(baseline['return_pct'],4),'requested',round(requested['return_pct'],4),'expanded',round(candidate['return_pct'],4),'fills',candidate['fills'],flush=True)
    summaries={}
    for bps in SPEC['slippage_bps']:
        rows=[r for r in result['comparisons'] if r['slippage_bps']==bps]
        summary={}
        for variant in ('baseline','requested','expanded'):
            values=[r[variant] for r in rows]
            summary[variant]=dict(mean_return_pct=float(np.mean([r['return_pct'] for r in values])),
                median_return_pct=float(np.median([r['return_pct'] for r in values])),
                worst_drawdown_pct=max(r['max_drawdown_pct'] for r in values),
                mean_gross_pct=float(np.mean([r['mean_gross_pct'] for r in values])),
                mean_fills=float(np.mean([r['fills'] for r in values])),
                windows_with_eight_fill_days=sum(r['days_with_fills']>=8 for r in values),
                winning_windows=sum(r[variant]['return_pct']>r['baseline']['return_pct'] for r in rows),
                extra_fills={a:sum(r['extra_fills'][a] for r in values) for a in EXTRA})
        summaries[str(bps)+'bp']=summary
    result['summary']=summaries
    gate=SPEC['research_gate'];stress=summaries[str(gate['cost_bps'])+'bp'];a=stress['baseline']
    result['research_gate']={}
    for variant in ('requested','expanded'):
        b=stress[variant]
        checks=dict(improved_mean=b['mean_return_pct']>a['mean_return_pct'],nonworse_median=b['median_return_pct']>=a['median_return_pct'],
            sufficient_winning_windows=b['winning_windows']>=gate['minimum_winning_windows'],
            drawdown_within_tolerance=b['worst_drawdown_pct']<=a['worst_drawdown_pct']+gate['maximum_extra_drawdown_percentage_points'])
        result['research_gate'][variant]=dict(checks=checks,passed=all(checks.values()) and not args.small,
            decision='RESEARCH_PASS_REQUIRES_FORWARD_VALIDATION' if all(checks.values()) and not args.small else 'DO_NOT_PROMOTE')
    result['source_sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.iterdir() if p.suffix in ('.py','.json')}
    output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result['research_gate']),flush=True)


if __name__=='__main__':main()
