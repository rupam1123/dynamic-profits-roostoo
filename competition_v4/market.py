"""Thread-safe immutable market snapshots and deduplicated risk-priority queue."""
import copy,threading,time
from decimal import Decimal as D


class Market:
    def __init__(self):
        self.lock=threading.RLock();self.info=None;self.ticks=None;self.quoted_at=0.;self.info_at=0.
        self.catalog={};self.catalog_at=0.;self.catalog_errors={};self.signals={};self.signal_hour=-1
        self.signal_slot=-1;self.fast_minute=-1;self.failures={};self.extremes={};self.urgent={};self.last_error=None
        self.last_observation=None;self.max_observation_gap=0.;self.hard_fraction=D('.03');self.observed_peak=None;self.episode=None

    def metadata(self,info):
        with self.lock:self.info=copy.deepcopy(info);self.info_at=time.time()

    def mapping(self,catalog,skipped):
        with self.lock:self.catalog=dict(catalog);self.catalog_errors=dict(skipped);self.catalog_at=time.time()

    def publish(self,ticks,state):
        now=time.time()
        if ticks.get('Success') is not True or abs(int(ticks['ServerTime'])-int(now*1000))>60000:raise ValueError('Invalid/stale quote response')
        for quote in ticks.get('Data',{}).values():
            bid,ask=D(str(quote['MaxBid'])),D(str(quote['MinAsk']))
            if not bid.is_finite() or not ask.is_finite() or bid<=0 or ask<bid:raise ValueError('Invalid market quote')
        with self.lock:
            self.ticks=copy.deepcopy(ticks);self.quoted_at=now
            if self.last_observation is not None:self.max_observation_gap=max(self.max_observation_gap,now-self.last_observation)
            self.last_observation=now
            for pair,pos in state['positions'].items():
                quote=ticks.get('Data',{}).get(pair)
                if not quote:continue
                d=pos['direction'];px=D(str(quote['MaxBid'] if d==1 else quote['MinAsk']));entry=D(pos['entry'])
                key=(pair,str(pos['id']));old=self.extremes.get(key,D(pos.get('extreme_price',pos['entry'])))
                extreme=max(old,px) if d==1 else min(old,px);self.extremes[key]=extreme
                stop=D(pos.get('stop_fraction','0.08'));loss=d*(px/entry-1);profit=d*(extreme/entry-1)
                if pos.get('setup')=='ONE_MINUTE_BREAKOUT':
                    if loss>=D(pos['take_profit_fraction']):self.urgent[key]=dict(pair=pair,position_id=str(pos['id']),reason='FAST_PROFIT_TARGET',trigger_time=now)
                    elif now>=pos['opened_at']+pos['max_hold_seconds']:self.urgent[key]=dict(pair=pair,position_id=str(pos['id']),reason='FAST_TIME_EXIT',trigger_time=now)
                if loss<=-stop or (profit>=stop and loss<=D('.003')) or (profit>=stop*D('1.5') and d*(px/extreme-1)<=-stop):
                    self.urgent[key]=dict(pair=pair,position_id=str(pos['id']),reason='QUOTE_STOP',trigger_time=now)
            if all(p in ticks.get('Data',{}) for p in state['positions']):
                equity=D(state['usd'])
                for pair,pos in state['positions'].items():
                    q=ticks['Data'][pair];qty=D(pos['quantity'])
                    if pos['direction']==1:equity+=qty*D(str(q['MaxBid']))*D('.999')
                    else:
                        collateral=D(pos['collateral']);px=D(str(q['MinAsk']))
                        equity+=collateral+max(qty*(D(pos['entry'])-px),-collateral)-qty*px*D('.001')
                episode=state.get('recover_until',-1)
                if self.episode!=episode:self.observed_peak=None;self.episode=episode
                self.observed_peak=max(equity,D(state.get('risk_peak',state['peak_equity'])),self.observed_peak or D(0))
                if state.get('pause_until',-1)<0 and not state.get('flatten_pending') and equity<=self.observed_peak*(1-self.hard_fraction):
                    self.urgent[('portfolio','drawdown')]=dict(reason='HARD_DRAWDOWN',trigger_time=now,observed_equity=str(equity))
            if state.get('expires') is not None and now>=state['expires']:
                self.urgent[('portfolio','deadline')]=dict(reason='COMPETITION_DEADLINE',trigger_time=now)

    def snapshot(self,required=(),max_age=25):
        with self.lock:
            if self.info is None or self.ticks is None or time.time()-self.quoted_at>max_age:raise ValueError('Fresh quote snapshot unavailable')
            if self.info.get('IsRunning') is not True:raise ValueError('Exchange stopped')
            for p in required:
                if p not in self.ticks.get('Data',{}):raise ValueError('Held quote missing: '+p)
            return copy.deepcopy(self.info),copy.deepcopy(self.ticks)

    def signals_snapshot(self):
        with self.lock:return self.signal_hour,self.signal_slot,copy.deepcopy(self.signals)

    def publish_signals(self,hour,slot,signals,failures,minute):
        with self.lock:
            self.signal_hour=hour;self.signal_slot=slot;self.signals=copy.deepcopy(signals);self.failures=dict(failures);self.fast_minute=minute

    def next_risk(self):
        with self.lock:
            for key in self.urgent:
                if key[0]=='portfolio':return key,dict(self.urgent[key])
            return next(((k,dict(v)) for k,v in self.urgent.items()),(None,None))

    def clear_risk(self,key):
        with self.lock:self.urgent.pop(key,None)

    def has_risk(self):
        with self.lock:return bool(self.urgent)

    def allowed(self,pair,max_age=900):
        with self.lock:return time.time()-self.catalog_at<=max_age and pair in self.catalog
