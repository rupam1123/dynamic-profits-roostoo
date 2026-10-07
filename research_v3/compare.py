"""Matched-window replay of actual V2/V2.1/V3 controllers against a fake exchange.
No network, credentials or actual trades. Hourly opens approximate execution; intrahour stops and outages NOT modelled.
"""
import argparse,copy,csv,hashlib,importlib,json,math,sys,tempfile
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal as D
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent/'reference'))
import baseline_sim as sim
from competition_v3 import strategy
from v3_tests.exchange import Exchange,PAIRS

class MemoryStore:
    def __init__(self,state):self.state=copy.deepcopy(state);self.attempts={};self.events=[]
    def load(self):return copy.deepcopy(self.state)
    def save(self,state,kind,body,applied=None):
        self.state=copy.deepcopy(state);self.events.append((kind,copy.deepcopy(body)))
        if applied:self.attempts[applied][0]='APPLIED'
    def reserve(self,intent,plan):
        if self.pending() or intent in self.attempts:raise RuntimeError('Duplicate or unresolved replay intent')
        self.attempts[intent]=['SENDING',json.dumps(plan),None]
    def acknowledge(self,intent,status,response):self.attempts[intent][0]=status;self.attempts[intent][2]=json.dumps(response)
    def pending(self):return [(k,*v) for k,v in self.attempts.items() if v[0]!='APPLIED']
    def exists(self,intent):return intent in self.attempts
    def already(self,h,p,stopping=False):return self.exists(str(h)+':'+p+(':STOP' if stopping else ''))
    def hour_count(self,h):return sum(json.loads(v[1])['hour']==h for v in self.attempts.values())

def ts(y,m,d):return int(datetime(y,m,d,tzinfo=timezone.utc).timestamp())

def load(folder):
    stamps,data=sim.load(folder)
    qv={}
    for a in sim.ASSETS:
        path=next(folder.glob(a+'USDT_1h_*.csv'))
        with path.open() as f:qv[a]=np.array([float(r['quote_volume']) for r in csv.DictReader(f)])
    return stamps,data,qv

def prepare(stamps,data,qv,indices):
    F=sim.features(stamps,data);out={};max_error=0.
    for i in sorted(indices):
        signals={}
        for a in sim.ASSETS:
            f=F[a]
            s=dict(close=str(f['close'][i]),momentum=str(f['m168'][i]),momentum_24h=str(f['m24'][i]),
                momentum_72h=str(f['m72'][i]),daily_vol=str(f['vol'][i]),direction=int(f['dir'][i]),score=str(f['score'][i]),
                quote_volume_24h=str(float(qv[a][i-24:i].sum())),returns=f['lr'][i-72:i].tolist(),
                z24=None if not np.isfinite(f['z24'][i]) else str(f['z24'][i]),r6=str(f['r6'][i]),
                momentum_336h=str(f['m336'][i]),momentum_720h=str(f['m720'][i]),candles=min(i,1000),
                atr_pct=str(f['atr'][i]),atr_abs=str(f['atr'][i]*f['close'][i]))
            if a=='BTC':s['vol_median_720h']=str(float(np.median(f['vol'][i-720:i:6])))
            signals[a+'/USD']=s
            if i%337==0:
                rows=[(int(stamps[j]*1000),*[str(data[a][k][j]) for k in ('open','high','low','close','volume')],int(stamps[j]*1000)+3599999,str(qv[a][j])) for j in range(i-1000,i)]
                direct=strategy.features(rows,regime=a=='BTC')
                for k in ('momentum','daily_vol','score','z24','atr_pct','vol_median_720h'):
                    if k in direct and direct[k] is not None and k in s:
                        err=abs(float(s[k])-float(direct[k]));max_error=max(max_error,err)
                        if err>1e-6:raise ValueError('Feature parity failed '+k+': '+str(err))
        out[i]=signals
    return out,max_error

def run(module,idx,stamps,data,signals,slip,override=None):
    b=importlib.import_module(module);ex=Exchange(8);ex.slip=D(str(slip));marks=[];days=set();last_fills=0
    base_cfg=b.policy(0);raw={k:v for k,v in base_cfg.items() if k!='end_epoch'}
    if override:
        for name,values in override.items():raw[name].update(values)
        base_cfg=dict(raw,end_epoch=None)
    ex.clock=int(stamps[idx[0]])+120;fprint=b.fingerprint()
    with patch.object(b.time,'time',side_effect=lambda:ex.clock),patch.object(b,'emit'),patch.object(b,'fingerprint',return_value=fprint),patch.object(b,'policy',return_value=base_cfg):
      with tempfile.TemporaryDirectory() as tmp:
        db=b.Store(Path(tmp)/'seed.sqlite3');b.initialize(ex,db,ex.clock)
        store=MemoryStore(db.load())
      raw_patch=patch.object(b,'raw_policy',return_value=raw) if hasattr(b,'raw_policy') else patch.object(b,'policy',return_value=base_cfg)
      with raw_patch:
        for i in idx:
            ex.clock=int(stamps[i])+120;ex.prices={a+'/USD':D(str(data[a]['open'][i])) for a in sim.ASSETS}
            with patch.object(b,'signals',return_value=(int(stamps[i])//3600,signals[i])):
                b.cycle(ex,store)
            if store.pending():raise ValueError('Unresolved replay intent')
            mark=float(b.mark_equity(store.load(),ex.request('/v3/ticker')))
            # Final equity is a liquidation estimate with closing commission AND slippage.
            state=store.load();mark=float(ex.usd)
            for p,pos in state['positions'].items():
                q=float(pos['quantity']);px=float(ex.prices[p]);coll=float(pos['collateral'])
                if pos['direction']==1:mark+=q*px*(1-slip)*.999
                else:
                    fill=px*(1+slip);mark+=coll+max(q*(float(pos['entry'])-fill),-coll)-q*fill*.001
            marks.append(mark)
            if ex.writes>last_fills:days.add((int(stamps[i])+8*3600)//86400)
            last_fills=ex.writes
    equity=np.array([100000]+marks);peaks=np.maximum.accumulate(equity)
    all_days={(int(stamps[i])+8*3600)//86400 for i in idx}
    return dict(start=datetime.fromtimestamp(int(stamps[idx[0]]),timezone.utc).isoformat(),return_pct=(marks[-1]/100000-1)*100,
        max_drawdown_pct=float(np.max(1-equity/peaks)*100),fills=ex.writes,fees_usd=float(ex.fees),days_with_fills=len(days),
        calendar_days=len(all_days),rotation_decisions=sum(k=='ROTATION_DECISION' for k,_ in store.events),
        final_equity=marks[-1],unresolved=len(store.pending()))

def main():
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,default=Path('history12'));p.add_argument('--output',type=Path,default=Path('validation/v3_comparison.json'))
    p.add_argument('--small',action='store_true');a=p.parse_args()
    stamps,data,qv=load(a.data)
    # Predeclared calendar-spaced windows, chosen before inspecting V3 results. They are NOT an untouched holdout.
    starts=[(y,m,1) for y in (2025,2026) for m in (3,5,7,9)]
    if a.small:starts=[(2025,3,1),(2026,9,1)]
    windows=[]
    for start in starts:
        i=int(np.searchsorted(stamps,ts(*start)-8*3600));windows.append(list(range(i,i+14*24)))
    prepared,parity=prepare(stamps,data,qv,{i for w in windows for i in w})
    variants=[('V2','competition_v2.controller',None),('V2.1','competition_v21.controller',None),
              ('V3','competition_v3.controller',None),('V3_ATR','competition_v3.controller',{'risk':{'atr_exits':True}})]
    result=dict(method='Actual controllers; closed hourly signals, next-hour-open fills. Same 12 assets and windows. 0.1% fee per side.',
        limits=['Retrospective research, not a fresh holdout','No intrahour stop prices, order-book depth, latency, outages or partial fills',
                'Days with a fill do not establish organizer qualification','V3 limit entries disabled; candle replay cannot validate maker fills'],
        feature_max_absolute_error=parity,variants={})
    for slip in (.0005,.0015):
      for name,module,over in variants:
        key=name+'_'+str(int(slip*10000))+'bp';rows=[]
        for w in windows:
            row=run(module,w,stamps,data,prepared,slip,over);rows.append(row)
            print(key,row['start'][:10],round(row['return_pct'],3),row['fills'],flush=True)
        result['variants'][key]=dict(windows=rows,mean_return_pct=float(np.mean([r['return_pct'] for r in rows])),
            median_return_pct=float(np.median([r['return_pct'] for r in rows])),worst_drawdown_pct=max(r['max_drawdown_pct'] for r in rows),
            mean_days_with_fills=float(np.mean([r['days_with_fills'] for r in rows])),windows_with_8_fill_days=sum(r['days_with_fills']>=8 for r in rows),
            mean_fills=float(np.mean([r['fills'] for r in rows])))
        a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,indent=2))
    result['data_sha256']={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(a.data.glob('*.csv'))}
    a.output.write_text(json.dumps(result,indent=2));print('SAVED',a.output,flush=True)
if __name__=='__main__':main()
