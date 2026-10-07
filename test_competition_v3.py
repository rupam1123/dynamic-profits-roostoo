"""V3 safety, accounting, allocation, lifecycle and migration tests. Offline only."""
import copy,json,math,random,sqlite3,tempfile,unittest,sys
from contextlib import closing
from decimal import Decimal as D, ROUND_DOWN
from pathlib import Path
from unittest.mock import patch
sys.path.append(str(Path(__file__).resolve().parent/'research_v3/reference'))
from competition_v3 import controller as bot,strategy as st,migrate,report
from v3_tests.exchange import Exchange,PAIRS,MIDNIGHT


def sig(i=0,d=1):
    r=random.Random(i)
    return dict(close=str(100+i*10),momentum=str(.08*d),momentum_24h=str(.03*d),momentum_72h=str(.05*d),
        daily_vol='.02',direction=d,score=str(5+i/10),quote_volume_24h='100000000',returns=[r.uniform(-.01,.01) for _ in range(72)],
        z24='0',r6=str(.01*d),momentum_336h=str(.1*d),momentum_720h=str(.2*d),vol_median_720h='.02',
        candles=1000,atr_abs='2',atr_pct='.02')

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=bot.Store(Path(self.tmp.name)/'execution.db');self.ex=Exchange()
        for p in (patch.object(bot.time,'time',side_effect=lambda:self.ex.clock),patch.object(bot,'emit')):
            p.start();self.addCleanup(p.stop)
        self.cfg=bot.raw_policy();self.signals={p:sig(i) for i,p in enumerate(PAIRS)}
        bot.initialize(self.ex,self.store,MIDNIGHT)
    def config(self,mutate):
        c=copy.deepcopy(self.cfg);mutate(c)
        p=patch.object(bot,'raw_policy',return_value=c);p.start();self.addCleanup(p.stop);self.cfg=c;return c
    def run_cycle(self,signals=None):
        with patch.object(bot,'signals',return_value=(int(self.ex.clock)//3600,signals or self.signals)):
            return bot.cycle(self.ex,self.store)
    def buy(self,pair='BTC/USD',short=False):
        bot.execute(self.ex,self.store,str(int(self.ex.clock)//3600)+':'+pair,pair,'SHORT_OPEN' if short else 'BUY',
            int(self.ex.clock)//3600,budget_override='1000',universe_signals=self.signals)
    def save(self,**kw):
        s=self.store.load();s.update(kw);self.store.save(s,'TEST',{});return s

class AllocationTests(Base):
    def test_global_caps_all_books_and_restart(self):
        self.run_cycle();s=self.store.load();_,ticks=bot.public_market(self.ex)
        self.assertTrue(s['positions']);self.assertLessEqual(len(s['positions']),5)
        self.assertLessEqual(st.gross_exposure(s,ticks),D(s['last_equity'])*D('.50'))
        self.assertLessEqual(st.open_risk(s,ticks),D(s['last_equity'])*D('.025')+1)
        count=self.ex.writes;self.store=bot.Store(self.store.path);self.run_cycle();self.assertEqual(count,self.ex.writes)
    def test_no_signal_no_forced_activity(self):
        self.ex.clock=MIDNIGHT+12*3600+120
        quiet={p:dict(s,direction=0,momentum='0',momentum_24h='0',momentum_72h='0',momentum_336h='0',momentum_720h='0') for p,s in self.signals.items()}
        self.run_cycle(quiet);self.assertEqual(self.ex.writes,0)
        self.assertIsNotNone(self.store.load().get('activity_warning_day'))
    def test_pause_blocks_every_book_even_evening(self):
        self.ex.clock=MIDNIGHT+12*3600+120;h=int(self.ex.clock)//3600
        self.save(pause_until=h+24);self.run_cycle();self.assertEqual(self.ex.writes,0)
        info,ticks=bot.public_market(self.ex)
        for owner in ('core','xs','ts'):
            with self.assertRaises(bot.Blocked):bot.plan_order(self.store.load(),'BTC/USD','BUY',info,ticks,h,owner=owner,signal=self.signals['BTC/USD'])
    def test_missing_held_history_blocks_new_entries(self):
        self.buy();self.ex.clock+=3600;before=self.ex.writes
        sigs={p:s for p,s in self.signals.items() if p!='BTC/USD'}
        self.run_cycle(sigs);self.assertEqual(before,self.ex.writes)
    def test_cost_spread_volume_and_correlation_filters_apply_to_sleeves(self):
        info,ticks=bot.public_market(self.ex);s=self.store.load();h=int(self.ex.clock)//3600
        pair='ETH/USD';x=self.signals[pair]
        for changed,want in [(dict(quote_volume_24h='0'),'LOW_VOLUME'),(dict(r6='.1'),'OVEREXTENDED'),
                             (dict(momentum='0',momentum_24h='0',momentum_72h='0'),'COST_HURDLE')]:
            self.assertEqual(st.entry_reason(pair,1,dict(x,**changed),self.signals,info,ticks,self.cfg,s,h),want)
        ticks['Data'][pair]['MinAsk']='120'
        self.assertEqual(st.entry_reason(pair,1,x,self.signals,info,ticks,self.cfg,s,h),'WIDE_SPREAD')
    def test_risk_floor_scales_with_initial_capital(self):
        s=self.save(initial_usd='50000',last_equity='46000',peak_equity='50000',risk_peak='50000')
        self.assertEqual(st.risk_multiplier(s,self.cfg,100),D('.25'))
    def test_global_limit_blocks_sleeve_plan(self):
        self.run_cycle();s=self.store.load()
        if len(s['positions'])<5:self.fail('Fixture must fill five slots')
        pair=next(p for p in PAIRS if p not in s['positions']);info,ticks=bot.public_market(self.ex)
        with self.assertRaises(ValueError):bot.plan_order(s,pair,'BUY',info,ticks,int(self.ex.clock)//3600,owner='xs',signal=self.signals[pair])
    def test_policy_rejects_nonfinite_and_forced_guard(self):
        for mutate in (lambda c:c.update(gross_fraction='.56'),lambda c:c['guard'].update(mode='PROBE'),
                       lambda c:c['risk'].update(per_position='NaN'),lambda c:c.update(max_positions=True)):
            c=copy.deepcopy(self.cfg);mutate(c)
            with patch.object(bot,'raw_policy',return_value=c),self.assertRaises(bot.Blocked):bot.policy(0)
    def test_atr_units_and_bounds(self):
        c=copy.deepcopy(self.cfg);c['risk']['atr_exits']=True
        self.assertEqual(st.stop_distance(dict(atr_abs='2000',atr_pct='.02'),c),D('.05'))
        self.assertEqual(st.stop_distance(dict(atr_abs='200',atr_pct='.0002'),c),D('.01'))

class RiskTests(Base):
    def test_price_stop_runs_outside_hour_and_without_candles(self):
        self.buy();self.ex.prices['BTC/USD']=D(90);self.ex.clock+=30*60
        with patch.object(bot,'signals',side_effect=AssertionError('Must not fetch candles')):bot.cycle(self.ex,self.store)
        self.assertFalse(self.store.load()['positions'])
    def test_flatten_pause_keeps_lifetime_peak(self):
        self.run_cycle();s=self.store.load();peak=s['peak_equity']
        for p in s['positions']:self.ex.prices[p]*=D('.5')
        self.ex.clock+=600;self.run_cycle();s=self.store.load()
        self.assertFalse(s['positions']);self.assertEqual(s['peak_equity'],peak)
        self.ex.clock=s['pause_until']*3600+120;bot.risk_mark(self.ex,self.store);s=self.store.load()
        self.assertEqual(s['peak_equity'],peak);self.assertEqual(s['pause_until'],-1)
        self.assertLess(D(s['risk_peak']),D(peak));self.assertEqual(st.risk_multiplier(s,self.cfg,int(self.ex.clock)//3600),D('.25'))
    def test_overdue_pause_does_not_clear_until_flattened(self):
        self.buy();h=int(self.ex.clock)//3600;self.save(pause_until=h-1,flatten_pending=True)
        s,_=bot.risk_mark(self.ex,self.store);self.assertTrue(s['flatten_pending']);self.assertNotEqual(s['pause_until'],-1)
    def test_deadline_exits_without_signal(self):
        self.buy();self.save(expires=self.ex.clock-1)
        with patch.object(bot,'signals',side_effect=AssertionError('No data needed')):self.assertTrue(bot.cycle(self.ex,self.store))
        self.assertFalse(self.store.load()['positions'])
    def test_partial_spot_and_short_accounting(self):
        for pair,short in [('BTC/USD',False),('ETH/USD',True)]:
            if short:self.signals[pair]=sig(1,-1)
            self.buy(pair,short);pos=self.store.load()['positions'][pair];half=D(pos['quantity'])/2
            bot.execute(self.ex,self.store,'trim:'+pair,pair,'SHORT_TRIM' if short else 'TRIM',int(self.ex.clock)//3600,trim_qty=str(half))
            self.assertEqual(D(self.store.load()['positions'][pair]['quantity']),D(pos['quantity'])-half.quantize(D('1e-8'),rounding=ROUND_DOWN))
            self.assertTrue(self.store.load()['positions'][pair]['partial_taken'])
            bot.match_account(self.store.load(),bot.account(self.ex))
    def test_atr_partial_and_cost_breakeven(self):
        self.config(lambda c:c['risk'].update(atr_exits=True));self.buy();self.ex.prices['BTC/USD']=D(108)
        self.ex.clock+=1200;self.run_cycle();s=self.store.load();self.assertTrue(s['positions']['BTC/USD']['partial_taken'])
        self.ex.prices['BTC/USD']=D('100.1');self.ex.clock+=300;self.run_cycle();self.assertFalse(self.store.load()['positions'])
    def test_unknown_write_blocks_restart(self):
        real=self.ex.request
        def lose(endpoint,*a,**kw):
            r=real(endpoint,*a,**kw)
            if endpoint=='/v3/place_order':raise TimeoutError('lost')
            return r
        with patch.object(self.ex,'request',side_effect=lose),self.assertRaises(bot.Blocked):self.buy()
        n=self.ex.writes
        with self.assertRaises(bot.Blocked):bot.cycle(self.ex,self.store)
        self.assertEqual(self.ex.writes,n);self.assertEqual(self.store.pending()[0][1],'UNKNOWN')
    def test_ack_restart_applied_once(self):
        info,ticks=bot.public_market(self.ex);h=int(self.ex.clock)//3600
        plan=bot.plan_order(self.store.load(),'BTC/USD','BUY',info,ticks,h,budget_override='1000',signal=self.signals['BTC/USD'])
        self.store.reserve('crash',plan);response=self.ex.request(plan['endpoint'],plan['params'])
        self.store.acknowledge('crash','ACK',response);bot.recover_acknowledged(self.ex,self.store)
        n=self.ex.writes;bot.recover_acknowledged(self.ex,self.store)
        self.assertEqual(n,self.ex.writes);self.assertFalse(self.store.pending())
    def test_late_window_prevents_entry_transmission(self):
        self.ex.clock=MIDNIGHT+16*60
        with self.assertRaises(ValueError):self.buy()
        self.assertFalse(self.store.pending());self.assertEqual(self.ex.writes,0)
    def test_wrong_account_and_modified_fingerprint_block(self):
        self.save(fingerprint='wrong')
        with self.assertRaises(bot.Blocked):self.run_cycle()
        self.assertEqual(self.ex.writes,0)

class RotationTests(Base):
    def setup_rotation(self):
        self.buy();s=self.store.load();h=int(self.ex.clock)//3600
        s['positions']['BTC/USD']['opened_hour']=h-48
        # A single-slot portfolio forces comparison instead of filling vacant slots.
        c=copy.deepcopy(self.cfg);c['max_positions']=1
        x=copy.deepcopy(self.signals);x['BTC/USD'].update(score='.1',momentum='.01',momentum_24h='.001',momentum_72h='.001')
        x['ETH/USD'].update(score='10',momentum='.5',momentum_24h='.08',momentum_72h='.2')
        info,ticks=bot.public_market(self.ex)
        return s,x,c,h,info,ticks
    def test_rotation_requires_cost_gap_and_holding_age(self):
        s,x,c,h,info,ticks=self.setup_rotation();r=st.rotation(s,x,info,ticks,c,h,D(1));self.assertIsNotNone(r)
        self.assertEqual(r['sell_pair'],'BTC/USD');self.assertEqual(r['buy_pair'],'ETH/USD')
        s['positions']['BTC/USD']['opened_hour']=h
        self.assertIsNone(st.rotation(s,x,info,ticks,c,h,D(1)))
    def test_rotation_daily_and_interval_budget(self):
        s,x,c,h,info,ticks=self.setup_rotation();s['last_rotation_hour']=h-1
        self.assertIsNone(st.rotation(s,x,info,ticks,c,h,D(1)))
        s['last_rotation_hour']=h-24;s['rotation_day']=(h+8)//24;s['rotation_count']=2
        self.assertIsNone(st.rotation(s,x,info,ticks,c,h,D(1)))
    def test_rotation_cannot_bypass_correlated_remaining_holdings(self):
        s,x,c,h,info,ticks=self.setup_rotation()
        s['positions']['SOL/USD']=dict(direction=1,quantity='1',entry='120',collateral='0',book='ts')
        x['SOL/USD']['returns']=x['ETH/USD']['returns']
        for p in x:
            if p not in ('BTC/USD','ETH/USD','SOL/USD'):x[p]['direction']=0
        self.assertIsNone(st.rotation(s,x,info,ticks,c,h,D(1)))

class MigrationTests(Base):
    def test_upgrade_preserves_positions_history_peak_and_deadline(self):
        self.buy();old=self.store.load();old.update(version='competition-controller-2.1',expires=MIDNIGHT+999999)
        old['positions']['BTC/USD'].update(book='ts',opened_hour=1,partial_taken=True)
        new=migrate.upgrade_state(old,self.ex.clock)
        for k in ('usd','initial_usd','peak_equity','last_exit','last_hour','expires'):self.assertEqual(new[k],old[k])
        for k,v in old['positions']['BTC/USD'].items():self.assertEqual(new['positions']['BTC/USD'][k],v)
    def test_drawdown_conversion_schedules_flatten_and_pause(self):
        self.buy();s=self.store.load();s.update(stop_reason='PORTFOLIO_DRAWDOWN')
        n=migrate.upgrade_state(s,self.ex.clock);self.assertTrue(n['flatten_pending']);self.assertIsNone(n['stop_reason'])
    def test_unresolved_migration_rejected(self):
        self.store.reserve('u',dict(params={}))
        with self.assertRaises(bot.Blocked):migrate.preflight(self.store)
    def test_report_counts_fills_not_canceled_orders(self):
        self.buy();self.store.reserve('cancel',dict(action='BUY'))
        self.store.save(self.store.load(),'ORDER_CANCELED_UNFILLED',{},applied='cancel')
        s=report.build(self.store.path,Path(self.tmp.name)/'report')
        self.assertEqual(s['reconciled_fills'],1);self.assertEqual(s['unresolved'],0)


class UpgradeIntegrationTests(Base):
    def test_real_v2_and_v21_migrations_backup_and_reconcile_without_orders(self):
        from competition_v2 import controller as v2
        from competition_v21 import controller as v21
        for old in (v2,v21):
            path=Path(self.tmp.name)/(old.VERSION+'.db');store=old.Store(path)
            with patch.object(old,'emit'):old.initialize(self.ex,store,self.ex.clock)
            previous=store.load();n=self.ex.writes
            out=migrate.migrate(self.ex,store,Path(self.tmp.name)/'backups',now=self.ex.clock)
            self.assertTrue(out.exists());self.assertEqual(self.ex.writes,n)
            self.assertEqual(store.load()['version'],bot.VERSION)
            for k in ('usd','initial_usd','peak_equity','positions','expires','last_hour'):
                self.assertEqual(store.load()[k],previous[k])
            self.assertIsNone(migrate.migrate(self.ex,store,Path(self.tmp.name)/'backups',now=self.ex.clock))
    def test_expired_deadline_cannot_be_cleared(self):
        from competition_v2 import controller as old
        store=old.Store(Path(self.tmp.name)/'deadline.db')
        with patch.object(old,'emit'):old.initialize(self.ex,store,self.ex.clock)
        s=store.load();s['stop_reason']='COMPETITION_DEADLINE';store.save(s,'TEST',{})
        with self.assertRaises(bot.Blocked):migrate.preflight(store,True)

class DataAndSafetyTests(Base):
    def test_features_and_candles_reject_gap_and_bad_range(self):
        from competition_v3.feed import validate
        rows=[]
        for i in range(1000):
            price=100+math.sin(i/7)+i/500
            rows.append([i*3600000,str(price),str(price+1),str(price-1),str(price),100,i*3600000+3599999,'10000000'])
        good=validate(rows,1000*3600000);self.assertEqual(len(good),1000)
        f=st.features(rows,True);self.assertGreater(D(f['atr_abs']),0);self.assertIsNotNone(f['vol_median_720h'])
        with self.assertRaises(ValueError):validate(rows[:100]+rows[101:],1000*3600000)
        broken=copy.deepcopy(rows);broken[10][2]='0'
        with self.assertRaises(ValueError):validate(broken,1000*3600000)
    def test_hourly_entry_turnover_cap_does_not_block_risk_exit(self):
        self.config(lambda c:c['execution'].update(max_writes_per_hour=1));self.buy()
        with self.assertRaises(ValueError):self.buy('ETH/USD')
        bot.execute(self.ex,self.store,'stop','BTC/USD','SELL',int(self.ex.clock)//3600)
        self.assertFalse(self.store.load()['positions'])
    def test_past_config_deadline_validates_for_liquidation(self):
        self.config(lambda c:c.update(end_utc='2020-01-01T00:00:00Z'))
        self.assertLess(bot.policy(self.ex.clock)['end_epoch'],self.ex.clock)
    def test_testing_initialization_accepts_fifty_thousand_but_not_holdings(self):
        store=bot.Store(Path(self.tmp.name)/'test-account.db');self.ex.usd=D('49999.95')
        bot.initialize(self.ex,store,self.ex.clock,purpose='TESTING');self.assertEqual(store.load()['purpose'],'TESTING')
        self.ex.coins['BTC']=D('.001');other=bot.Store(Path(self.tmp.name)/'nonempty.db')
        with self.assertRaises(bot.Blocked):bot.initialize(self.ex,other,self.ex.clock,purpose='TESTING')

class DeadlineAndProcessTests(Base):
    def test_unsent_deadline_is_terminal_without_fill(self):
        original=self.ex.request
        def request(endpoint,*a,**kw):
            if endpoint=='/v3/place_order':raise bot.NotSent('expired')
            return original(endpoint,*a,**kw)
        with patch.object(self.ex,'request',side_effect=request),self.assertRaises(ValueError):self.buy()
        self.assertFalse(self.store.pending());self.assertEqual(self.ex.writes,0)
        self.assertIsNone(self.store.load()['last_fill_day'])
    def test_exclusive_process_lock(self):
        with bot.process_lock(Path(self.tmp.name)/'controller.lock'):
            with self.assertRaises(bot.Blocked):
                with bot.process_lock(Path(self.tmp.name)/'controller.lock'):pass

if __name__=='__main__':unittest.main()
