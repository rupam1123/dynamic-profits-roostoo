"""Read-only producers and a single durable order/journal writer. No timer-forced orders."""
import copy,json,threading,time
from decimal import Decimal as D
from pathlib import Path
from . import controller as bot,feed,timing,fast,universe
from .api import Client
from .market import Market
from .management import addition_candidate,partial_quantity
from .strategy import features,entry_setup,opportunity_candidates,risk_multiplier,paused


def load_context(pairs,path):
    boundary,batches,errors=feed.refresh(path,tuple(p.split('/')[0]+'USDT' for p in pairs))
    result={}
    for symbol,rows in batches.items():
        try:result[symbol[:-4]+'/USD']=features(rows,regime=symbol=='BTCUSDT')
        except (ValueError,ArithmeticError) as e:errors[symbol]=str(e)
    return boundary//feed.HOUR,result,errors


def eligible_context(sig,catalog,cfg):
    out={}
    for pair,s in sig.items():
        if pair not in catalog:continue
        if int(s['candles'])>=721 and D(s['quote_volume_24h'])>=D(cfg['expansion']['min_quote_volume_24h']) and D(s['daily_vol'])<=D(cfg['expansion']['max_daily_vol']):out[pair]=s
    return out


class Runner:
    def __init__(self,client,store):
        self.client=client;self.store=store;self.cfg=bot.raw_policy();self.market=Market();self.stop=threading.Event()
        self.market.hard_fraction=D(self.cfg['ladder']['hard'])
        self.threads=[];self.last_account=-1e9;self.last_risk=-1e9;self.last_signal_key=None;self.last_heartbeat=-1e9
        self.backoff={};self.last_action=None;self.rejections={};self.order_latencies=[]

    def quote_worker(self):
        public=Client();offset=0.;sync=-1e9;meta=-1e9
        while not self.stop.is_set():
            began=time.monotonic()
            try:
                now=time.time()
                if now-sync>=300:
                    offset=int(public.request('/v3/serverTime')['ServerTime'])-time.time()*1000
                    if abs(offset)>60000:raise ValueError('Machine/server clock mismatch')
                    sync=now
                if now-meta>=self.cfg['runtime']['catalog_seconds']:
                    self.market.metadata(public.request('/v3/exchangeInfo'));meta=now
                ticks=public.request('/v3/ticker',{'timestamp':str(int(time.time()*1000+offset))})
                self.market.publish(ticks,self.store.load())
            except Exception as e:
                with self.market.lock:self.market.last_error=type(e).__name__+': '+str(e)
                bot.emit('V4_QUOTE_ERROR',error_type=type(e).__name__,error=str(e))
            self.stop.wait(max(.1,self.cfg['runtime']['quote_seconds']-(time.monotonic()-began)))

    def data_worker(self):
        hour=-1;slot=-1;catalog_at=-1e9;context={};five={};errors={}
        while not self.stop.is_set():
            try:
                now=time.time();current_hour=int(now)//3600
                with self.market.lock:info=copy.deepcopy(self.market.info);info_at=self.market.info_at
                if info is None or now-info_at>self.cfg['runtime']['catalog_max_age_seconds']:
                    self.stop.wait(1);continue
                changed=False
                if now-catalog_at>=self.cfg['runtime']['catalog_seconds']:
                    catalog,skipped=universe.discover(info,feed.get('/api/v3/exchangeInfo'))
                    with self.market.lock:changed=catalog!=self.market.catalog
                    self.market.mapping(catalog,skipped);catalog_at=now
                    bot.emit('V4_UNIVERSE',observed=len(info['TradePairs']),mapped_crypto=len(catalog),excluded=skipped)
                with self.market.lock:catalog=dict(self.market.catalog)
                held=set(self.store.load()['positions'])
                if current_hour!=hour or changed:
                    hour,context,errors=load_context(set(catalog)|held,bot.ROOT/'candles_v4.sqlite3');slot=-1
                current_slot=int(time.time())//300
                eligible=eligible_context(context,catalog,self.cfg)
                if current_slot!=slot:
                    boundary,five,failures=timing.refresh(set(eligible)|held)
                    slot=boundary//300000;errors.update(failures)
                result={p:dict(s,timing=five.get(p)) for p,s in context.items()}
                # Every eligible asset gets 5m data. One-minute work is confined to strong hourly trends.
                strong=[p for p,s in eligible.items() if s['direction'] in (-1,1) and D(s['score'])>=1]
                minute=int(time.time())//60
                if self.cfg['runtime']['fast_breakout'] and strong:
                    boundary,micro,failures=fast.refresh(strong);minute=boundary//60000;errors.update(failures)
                    for p,bar in micro.items():result[p]['fast']=bar
                self.market.publish_signals(hour,slot,result,errors,minute)
                bot.emit('V4_DATA_READY',hour=hour,slot=slot,minute=minute,mapped=len(catalog),hourly_ready=len(result),five_minute_evaluated=len(five),eligible=len(eligible),one_minute_evaluated=len(strong))
            except Exception as e:
                bot.emit('V4_DATA_ERROR',error_type=type(e).__name__,error=str(e))
            self.stop.wait(max(1,60-time.time()%60+2))

    def reject(self,reason,pair=None):
        key=(int(time.time())//60,pair or 'portfolio',reason)
        if key in self.rejections:return
        self.rejections[key]=1
        if len(self.rejections)>5000:self.rejections={k:v for k,v in self.rejections.items() if k[0]>=int(time.time())//60-10}
        state=self.store.load();counts=state.setdefault('rejection_counts',{});counts[reason]=counts.get(reason,0)+1
        self.store.save(state,'SIGNAL_REJECTED',dict(pair=pair,reason=reason,minute=key[0]))

    def execute_one(self,pair,action,hour,reason,context=None,pick=None,position_id=None,trim_qty=None):
        state=self.store.load()
        if position_id is not None and (pair not in state['positions'] or str(state['positions'][pair]['id'])!=str(position_id)):return False
        entry=action in ('BUY','SHORT_OPEN')
        intent=('v4:add:'+str(state['positions'][pair]['id'])+':'+pair+':'+str(int(time.time())//300) if pick and pick.get('adding') else 'v4:entry:'+pair+':'+str(int(time.time())//(60 if pick and pick['setup']=='ONE_MINUTE_BREAKOUT' else 300))) if entry else 'v4:'+reason+':'+pair+':'+str(state['positions'][pair]['id'])
        if self.store.exists(intent):return False
        if time.monotonic()<self.backoff.get(intent,0):return False
        kw=dict(owner='opportunity',budget_override=pick['budget'],signal_context=context) if entry else {}
        if pick and pick.get('adding'):kw['adding']=True
        if trim_qty is not None:kw['trim_qty']=trim_qty
        began=time.monotonic()
        try:
            bot.execute(self.client,self.store,intent,pair,action,hour,**kw)
        except ValueError as e:
            self.backoff[intent]=time.monotonic()+10;self.reject('EXECUTION_DEFERRED:'+str(e),pair);return False
        elapsed=time.monotonic()-began;self.order_latencies.append(elapsed);self.last_action=time.time()
        bot.emit('V4_EXECUTED',pair=pair,action=action,reason=reason,execution_seconds=elapsed)
        return True

    def step(self):
        now=time.time();hour=int(now)//3600;state=self.store.load();bot.identity(self.client,state)
        with self.market.lock:catalog=dict(self.market.catalog)
        bot.adopt_pairs(state,catalog)
        if set(state.get('known_pairs',[]))!=set(bot.PAIRS):
            state['known_pairs']=list(bot.PAIRS);self.store.save(state,'UNIVERSE_ADOPTED',dict(pairs=list(catalog)))
        # Only this thread writes the journal or sends an authenticated order.
        bot.recover_acknowledged(self.client,self.store)
        state=self.store.load()
        if now-self.last_risk>=self.cfg['runtime']['quote_seconds']:
            state,_=bot.risk_mark(self.client,self.store);self.last_risk=now
        key,risk=self.market.next_risk()
        if risk and key[0]=='portfolio':
            if risk['reason']=='COMPETITION_DEADLINE':state['stop_reason']=risk['reason']
            else:state['flatten_pending']=True;state['pause_until']=hour+self.cfg['ladder']['pause_hours']
            self.store.save(state,'RISK_LATCHED',risk);self.market.clear_risk(key)
        state=self.store.load()
        if state['stop_reason'] or state['flatten_pending']:
            if state['positions']:
                pair=next(iter(state['positions']));pos=state['positions'][pair]
                self.execute_one(pair,bot.close_action(pos),hour,'FLATTEN',position_id=pos['id']);return False
            if state['stop_reason']:bot.emit('V4_RUN_COMPLETE',reason=state['stop_reason']);return True
            state['flatten_pending']=False;state['pause_until']=max(state['pause_until'],hour+self.cfg['ladder']['pause_hours'])
            self.store.save(state,'LADDER_FLATTENED',dict(hour=hour));return False
        key,risk=self.market.next_risk()
        if risk:
            pair=risk['pair'];pos=state['positions'].get(pair)
            if not pos or str(pos['id'])!=risk['position_id']:self.market.clear_risk(key);return False
            if self.execute_one(pair,bot.close_action(pos),hour,risk['reason'],position_id=pos['id']):self.market.clear_risk(key)
            return False
        if now-self.last_account>=self.cfg['runtime']['account_check_seconds']:
            bot.match_account(state,bot.account(self.client));self.last_account=time.time()
            if self.market.has_risk():return False
        info,ticks=bot.public_market(self.client,tuple(state['positions']))
        for pair,pos in state['positions'].items():
            quantity=partial_quantity(pos,ticks['Data'][pair],info['TradePairs'][pair],self.cfg)
            if quantity and self.execute_one(pair,'TRIM' if pos['direction']==1 else 'SHORT_TRIM',hour,'PARTIAL_PROFIT',position_id=pos['id'],trim_qty=quantity):return False
        sh,slot,sig=self.market.signals_snapshot()
        if sh!=hour or slot!=int(time.time())//300:
            self.reject('STALE_STRATEGY_DATA');return False
        if state.get('last_slot')!=slot:
            state['last_slot']=slot;state['last_hour']=hour
            self.store.save(state,'CYCLE',dict(hour=hour,slot=slot,signal_assets=len(sig),note='Fresh context evaluated; fills are separate journal events.'))
        missing=[p for p in state['positions'] if p not in sig]
        # Autonomous exits remain available even while entries are paused or some histories are missing.
        for pair,pos in state['positions'].items():
            action=bot.choose(state,pair,hour,sig.get(pair))
            bar=sig.get(pair,{}).get('timing')
            if bar and bar.get('boundary')==slot*300000 and pos.get('setup') in ('BREAKOUT','ONE_MINUTE_BREAKOUT') and hour>pos['opened_hour']:
                if pos['direction']*(D(bar['close'])/D(pos['setup_level'])-1)<D('-.003'):action=bot.close_action(pos)
            if action and self.execute_one(pair,action,hour,'STRATEGY_EXIT',position_id=pos['id']):return False
        if missing:self.reject('HELD_HISTORY_UNAVAILABLE');return False
        if paused(state,hour):self.reject('DRAWDOWN_PAUSE');return False
        info,ticks=bot.public_market(self.client,tuple(state['positions']))
        # Ineligible/removed symbols remain in held context for management, never as fresh candidates.
        context={p:dict(s) for p,s in sig.items()}
        for p in context:
            if not self.market.allowed(p,self.cfg['runtime']['catalog_max_age_seconds']):context[p]['direction']=0
        picks,reasons=opportunity_candidates(state,context,info,ticks,self.cfg,hour,risk_multiplier(state,self.cfg,hour))
        decision_key=(hour,slot,int(time.time())//60)
        if decision_key!=self.last_signal_key:
            self.store.save(state,'SELECTION',dict(hour=hour,slot=slot,candidates=picks,skipped=reasons,eligible_universe=len(catalog)))
            for p,reason in reasons.items():self.reject(reason,p)
            self.last_signal_key=decision_key
        reason=bot.entry_budget_reason(self.store)
        if reason:self.reject(reason);return False
        for pick in picks:
            p=pick['pair'];s=context[p]
            confirmed=fast.breakout(s,s.get('fast'),int(time.time())//60*60000,self.cfg['runtime']) if pick['setup']=='ONE_MINUTE_BREAKOUT' else timing.confirms(s,s.get('timing'),slot*300000)
            if not confirmed:self.reject('ENTRY_CONFIRMATION_MISSING',p);continue
            if self.execute_one(p,pick['action'],hour,'ENTRY',context=context,pick=pick):return False
        for pair,pos in state['positions'].items():
            pick=addition_candidate(state,pair,context,info,ticks,self.cfg,hour)
            if pick and self.execute_one(pair,'BUY',hour,'ADD_BREAKOUT',context=context,pick=pick,position_id=pos['id']):return False
        # One rotation only after a stronger independently confirmed setup passes admission with a freed slot.
        if (len(state['positions'])>=self.cfg['expansion']['max_positions'] or any(r in ('EXPOSURE_OR_CASH_LIMIT','PORTFOLIO_STOP_RISK_LIMIT') for r in reasons.values())) and hour-int(state.get('last_opportunity_rotation',-1000000))>=6:
            mature=[p for p,v in state['positions'].items() if hour-v['opened_hour']>=6]
            if mature:
                weak=min(mature,key=lambda p:(D(sig[p]['score']),p))
                alternatives,_=opportunity_candidates(state,context,info,ticks,self.cfg,hour,risk_multiplier(state,self.cfg,hour),replacing=weak)
                if alternatives:
                    pick=alternatives[0];p=pick['pair'];d=sig[p]['direction'];old_d=state['positions'][weak]['direction']
                    edge=d*D(sig[p]['momentum_24h'])-old_d*D(sig[weak]['momentum_24h'])
                    confirmed=fast.breakout(context[p],context[p].get('fast'),int(time.time())//60*60000,self.cfg['runtime']) if pick['setup']=='ONE_MINUTE_BREAKOUT' else timing.confirms(context[p],context[p].get('timing'),slot*300000)
                    if confirmed and D(pick['score'])>max(D('.1'),D(sig[weak]['score']))*D('1.5') and edge>D(self.cfg['execution']['rotation_cost_hurdle']):
                        pos=state['positions'][weak]
                        if self.execute_one(weak,bot.close_action(pos),hour,'ROTATION',position_id=pos['id']):
                            after=self.store.load();after['last_opportunity_rotation']=hour
                            self.store.save(after,'ROTATION',dict(closed=weak,candidate=p,edge=str(edge)));return False
        return False

    def run(self):
        state=self.store.load();bot.adopt_pairs(state);bot.identity(self.client,state)
        bot.recover_acknowledged(self.client,self.store);bot.match_account(self.store.load(),bot.account(self.client))
        previous=bot.MARKET;bot.MARKET=self.market
        def guard(pair):
            try:
                self.market.snapshot((pair,),self.cfg['runtime']['max_quote_age_seconds'])
                return not self.market.has_risk() and self.market.allowed(pair,self.cfg['runtime']['catalog_max_age_seconds'])
            except ValueError:return False
        self.client.entry_guard=guard
        try:
            for target in (self.quote_worker,self.data_worker):
                thread=threading.Thread(target=target,daemon=True);thread.start();self.threads.append(thread)
            while not self.stop.is_set():
                try:
                    if self.step():return
                except bot.Blocked:raise
                except Exception as e:self.reject('LOOP_DATA_ERROR:'+str(e));bot.emit('V4_RUNTIME_ERROR',error_type=type(e).__name__,error=str(e))
                now=time.time()
                if now-self.last_heartbeat>=self.cfg['runtime']['heartbeat_seconds']:
                    with self.market.lock:
                        bot.emit('V4_HEARTBEAT',quote_age_seconds=None if not self.market.quoted_at else now-self.market.quoted_at,max_observed_quote_gap_seconds=self.market.max_observation_gap,dynamic_assets=len(self.market.catalog),signal_hour=self.market.signal_hour,signal_slot=self.market.signal_slot,urgent_queue=len(self.market.urgent))
                    self.last_heartbeat=now
                self.stop.wait(.5)
        finally:
            self.stop.set()
            for thread in self.threads:thread.join(timeout=1)
            self.client.entry_guard=None
            bot.MARKET=previous


def preview():
    client=Client();info,ticks=bot.public_market(client)
    catalog,skipped=universe.discover(info,feed.get('/api/v3/exchangeInfo'))
    hour,sig,errors=load_context(catalog,bot.ROOT/'candles_v4.sqlite3')
    cfg=bot.raw_policy();eligible=eligible_context(sig,catalog,cfg)
    boundary,five,failures=timing.refresh(eligible);errors.update(failures)
    strong=[p for p,s in eligible.items() if s['direction'] in (-1,1) and D(s['score'])>=1]
    minute,micro,failures=fast.refresh(strong) if strong else (int(time.time())//60*60000,{},{});errors.update(failures)
    for p in sig:sig[p]=dict(sig[p],timing=five.get(p),fast=micro.get(p))
    mock=dict(stop_reason=None,initial_usd='100000',last_equity='100000',peak_equity='100000',usd='100000',positions={},last_exit={},**bot.LADDER_DEFAULTS)
    picks,reasons=opportunity_candidates(mock,sig,info,ticks,cfg,hour)
    bot.emit('V4_PUBLIC_PREVIEW',observed_assets=len(info['TradePairs']),mapped_crypto=len(catalog),hourly_ready=len(sig),eligible_assets=len(eligible),five_minute_evaluated=len(five),one_minute_evaluated=len(micro),candidates=picks,reasons=reasons,mapping_exclusions=skipped,data_errors=errors,note='Public data only. No credentials, account calls or orders. Candidates require fresh confirmation at execution.')
