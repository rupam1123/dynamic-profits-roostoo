"""Offline V3.2 end-to-end accounting, timing and migration checks. Fake exchange only."""
import copy,json,random,sqlite3,tempfile,unittest
from contextlib import closing
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
from competition_v4a import controller as bot,strategy,migrate,report,timing
from competition_v31 import controller as v3
from v3_tests.exchange import Exchange as OriginalExchange,PAIRS,MIDNIGHT

class Exchange(OriginalExchange):
    def request(self,endpoint,params=None,**kw):
        result=super().request(endpoint,params,**kw)
        if endpoint=='/v3/place_order' and params.get('type')=='LIMIT':
            result['OrderDetail']['Type']='LIMIT'
        return result



def signal(i=0,direction=0):
    rng=random.Random(i);px=D(100+10*i);d=direction
    return dict(close=str(px),momentum=str(.05*d),momentum_24h=str(.01*(d or 1)),momentum_72h=str(.02*(d or 1)),daily_vol='.02',direction=d,score=str(2+i/10),quote_volume_24h='100000000',returns=[rng.uniform(-.01,.01) for _ in range(72)],z24='0',r6='0',momentum_336h='.02',momentum_720h='.03',vol_median_720h='.02',candles=1000,atr_pct='.01',breakout_high=str(px-D(1)),breakout_low=str(px+D(1)),sma20=str(px-D(d)),sma50=str(px-D(2*d)),previous_close=str(px-D(d)),volume_ratio='2')


def fast(sig,clock):
    px=D(sig['close']);d=sig['direction'] or 1
    return dict(boundary=int(clock)//300*300000,close=str(px),previous=str(px-D(d)),mean=str(px-D(d)),prior_mean=str(px-D(d)),high=str(px-D(1)),low=str(px+D(1)),volume_ratio='2')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=bot.Store(Path(self.tmp.name)/'execution.sqlite3');self.ex=Exchange()
        for p in (patch.object(bot.time,'time',side_effect=lambda:self.ex.clock),patch.object(bot,'emit'),patch.object(v3,'emit')):
            p.start();self.addCleanup(p.stop)
        self.sig={p:signal(i) for i,p in enumerate(PAIRS)};self.cfg=bot.raw_policy()
        bot.initialize(self.ex,self.store,MIDNIGHT)

    def prepare(self,pair='BTC/USD',d=1):
        i=list(PAIRS).index(pair) if pair in PAIRS else 71
        self.sig[pair]=signal(i,d)
        self.sig[pair]['timing']=fast(self.sig[pair],self.ex.clock)
        if pair not in self.ex.prices:self.ex.prices[pair]=D(self.sig[pair]['close']);self.ex.precision[pair]=8

    def buy(self,pair='BTC/USD',short=False,**kw):
        self.prepare(pair,-1 if short else 1);hour=int(self.ex.clock)//3600
        bot.execute(self.ex,self.store,str(hour)+':'+pair,pair,'SHORT_OPEN' if short else 'BUY',hour,owner='opportunity',budget_override='1000',signal_context=self.sig,**kw)

    def cycle(self,sig=None):
        signals=self.sig if sig is None else sig
        data={p:fast(s,self.ex.clock) for p,s in signals.items()}
        with patch.object(bot,'signals',return_value=(int(self.ex.clock)//3600,signals)),patch.object(timing,'refresh',return_value=(int(self.ex.clock)//300*300000,data,{})):
            return bot.cycle(self.ex,self.store)

    def save(self,**kw):
        state=self.store.load();state.update(kw);self.store.save(state,'TEST',{});return state


class ControllerTests(Base):
    def test_long_short_reconcile_and_close(self):
        self.buy();self.ex.clock+=60;self.buy('ETH/USD',True)
        self.assertEqual(len(self.store.load()['positions']),2)
        for pair,pos in list(self.store.load()['positions'].items()):bot.execute(self.ex,self.store,'close:'+pair,pair,bot.close_action(pos),int(self.ex.clock)//3600)
        bot.match_account(self.store.load(),bot.account(self.ex));self.assertFalse(self.store.load()['positions'])

    def test_extra_asset_and_atr_risk_budget(self):
        self.buy('PEPE/USD');pos=self.store.load()['positions']['PEPE/USD']
        self.assertEqual(D(pos['stop_fraction']),D('.025'));self.assertEqual(pos['setup'],'BREAKOUT')
        self.assertLessEqual(D(pos['quantity'])*D(pos['entry'])*D(pos['stop_fraction']),D('300'))

    def test_five_minute_reentry_checks_do_not_duplicate(self):
        self.prepare();self.cycle();count=self.ex.writes;self.assertGreater(count,0)
        self.store=bot.Store(self.store.path);self.cycle();self.assertEqual(count,self.ex.writes)
        self.ex.clock+=300;self.cycle();self.assertEqual(count,self.ex.writes)
        self.assertEqual(self.store.load()['last_slot'],int(self.ex.clock)//300)

    def test_new_signal_can_enter_mid_hour(self):
        self.cycle();self.assertEqual(self.ex.writes,0)
        self.ex.clock+=1500;self.prepare();self.cycle();self.assertEqual(self.ex.writes,1)

    def test_stale_timing_never_sends(self):
        self.prepare();self.sig['BTC/USD']['timing']['boundary']-=300000
        with self.assertRaises(ValueError):bot.execute(self.ex,self.store,'bad','BTC/USD','BUY',int(self.ex.clock)//3600,owner='opportunity',signal_context=self.sig)
        self.assertEqual(self.ex.writes,0);self.assertFalse(self.store.pending())

    def test_stale_hour_never_enters(self):
        self.prepare()
        with patch.object(bot,'signals',return_value=(int(self.ex.clock)//3600-1,self.sig)),self.assertRaises(ValueError):bot.cycle(self.ex,self.store)
        self.assertEqual(self.ex.writes,0)

    def test_pause_blocks_entry(self):
        self.save(pause_until=int(self.ex.clock)//3600+24)
        with self.assertRaises(bot.Blocked):self.buy()
        self.assertEqual(self.ex.writes,0)

    def test_no_signal_and_guard_never_force_trades(self):
        self.ex.clock=MIDNIGHT+13*3600+120;self.cycle();self.assertEqual(self.ex.writes,0)
        self.assertEqual(bot.guard(self.ex,self.store,self.cfg,self.sig,int(self.ex.clock)//3600,D(1)),[])

    def test_old_entry_paths_blocked(self):
        self.prepare()
        with self.assertRaises(bot.Blocked):bot.execute(self.ex,self.store,'old','BTC/USD','BUY',int(self.ex.clock)//3600,signal_context=self.sig)

    def test_lost_response_blocks_restart_without_resend(self):
        real=self.ex.request
        def request(endpoint,*a,**kw):
            result=real(endpoint,*a,**kw)
            if endpoint=='/v3/place_order':raise TimeoutError('lost')
            return result
        with patch.object(self.ex,'request',side_effect=request),self.assertRaises(bot.Blocked):self.buy()
        count=self.ex.writes
        with self.assertRaises(bot.Blocked):self.cycle()
        self.assertEqual(count,self.ex.writes);self.assertEqual(self.store.pending()[0][1],'UNKNOWN')

    def test_throttle_expiry_is_recorded_unsent(self):
        real=self.ex.request
        def request(endpoint,*a,**kw):
            if endpoint=='/v3/place_order':raise bot.NotSent('late')
            return real(endpoint,*a,**kw)
        with patch.object(self.ex,'request',side_effect=request),self.assertRaises(ValueError):self.buy()
        self.assertEqual(self.ex.writes,0);self.assertFalse(self.store.pending())

    def test_all_books_quote_stops_without_candles(self):
        self.buy();self.ex.prices['BTC/USD']=D('95')
        state,ticks=bot.risk_mark(self.ex,self.store)
        bot.protect_inherited(self.ex,self.store,int(self.ex.clock)//3600,ticks)
        self.assertFalse(self.store.load()['positions']);bot.match_account(self.store.load(),bot.account(self.ex))

    def test_short_quote_stop(self):
        self.buy('ETH/USD',True);self.ex.prices['ETH/USD']=D('115')
        state,ticks=bot.risk_mark(self.ex,self.store);bot.protect_inherited(self.ex,self.store,int(self.ex.clock)//3600,ticks)
        self.assertFalse(self.store.load()['positions'])

    def test_trailing_stop(self):
        self.buy();self.ex.prices['BTC/USD']=D('106');bot.risk_mark(self.ex,self.store)
        self.ex.prices['BTC/USD']=D('102');state,ticks=bot.risk_mark(self.ex,self.store)
        bot.protect_inherited(self.ex,self.store,int(self.ex.clock)//3600,ticks);self.assertFalse(self.store.load()['positions'])

    def test_round_deadline_closes_without_data(self):
        self.buy();self.save(expires=self.ex.clock-1)
        with patch.object(bot,'signals',side_effect=AssertionError('no candles')):self.assertTrue(bot.cycle(self.ex,self.store))
        self.assertFalse(self.store.load()['positions'])

    def test_missing_held_history_blocks_additions(self):
        self.buy();self.ex.clock+=300;self.prepare('ETH/USD');count=self.ex.writes
        self.cycle({p:s for p,s in self.sig.items() if p!='BTC/USD'});self.assertEqual(count,self.ex.writes)

    def test_daily_total_does_not_block_new_entries(self):
        with patch.object(bot,'daily_entries',return_value=1000):self.buy()
        self.assertEqual(self.ex.writes,1)

    def test_original_peak_preserved_after_drawdown_pause(self):
        self.buy();self.save(risk_peak='110000',peak_equity='110000');bot.risk_mark(self.ex,self.store)
        self.assertTrue(self.store.load()['flatten_pending']);bot.flatten(self.ex,self.store,int(self.ex.clock)//3600)
        self.ex.clock=self.store.load()['pause_until']*3600+120
        state,_=bot.risk_mark(self.ex,self.store);self.assertEqual(state['peak_equity'],'110000');self.assertEqual(state['pause_until'],-1)

    def test_identity_and_single_writer(self):
        state=self.store.load();state['fingerprint']='bad'
        with self.assertRaises(bot.Blocked):bot.identity(self.ex,state)
        with bot.process_lock(Path(self.tmp.name)/'lock'),self.assertRaises(bot.Blocked):
            with bot.process_lock(Path(self.tmp.name)/'lock'):pass


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ex=Exchange();self.path=Path(self.tmp.name)/'execution.sqlite3'
        self.store=v3.Store(self.path)
        for p in (patch.object(v3.time,'time',side_effect=lambda:self.ex.clock),patch.object(v3,'emit'),patch.object(bot,'emit')):
            p.start();self.addCleanup(p.stop)
        v3.initialize(self.ex,self.store)
        sig={p:dict(signal(i,1),atr_pct='.02',atr_abs='2') for i,p in enumerate(PAIRS)}
        # Use direct strategy execution with its required full context; only fake API writes occur.
        sig['BTC/USD'].update(momentum='.08',momentum_24h='.03',momentum_72h='.05')
        v3.execute(self.ex,self.store,'old-long','BTC/USD','BUY',int(self.ex.clock)//3600,budget_override='1000',signal_context=sig)
        sig['ETH/USD'].update(direction=-1,momentum='-.08',momentum_24h='-.03',momentum_72h='-.05',r6='-.01')
        v3.execute(self.ex,self.store,'old-short','ETH/USD','SHORT_OPEN',int(self.ex.clock)//3600,budget_override='1000',signal_context=sig)

    def test_migration_preserves_all_account_and_risk_state_and_history(self):
        before=self.store.load();before.update(pause_until=123, recover_until=456,last_rotation_hour=789,expires=self.ex.clock+86400)
        self.store.save(before,'TEST',{})
        count=self.ex.writes
        with closing(sqlite3.connect(self.path)) as db:
            attempts=db.execute('SELECT * FROM attempts').fetchall();events=db.execute('SELECT * FROM events').fetchall()
        backup=migrate.migrate(self.ex,bot.Store(self.path),Path(self.tmp.name)/'backups')
        self.assertTrue(backup.is_file());after=bot.Store(self.path).load()
        for field in ('usd','initial_usd','peak_equity','risk_peak','pause_until','recover_until','flatten_pending','expires','last_hour','last_exit','sleeves','last_fill_day','account'):
            self.assertEqual(after[field],before[field],field)
        for pair,pos in before['positions'].items():
            self.assertEqual({k:after['positions'][pair][k] for k in pos},pos)
            self.assertEqual(after['positions'][pair].get('inherited_v3_stop'),before['positions'][pair].get('inherited_v3_stop'))
        self.assertEqual(after['budget'],'10000.00');self.assertEqual(self.ex.writes,count)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT * FROM attempts').fetchall(),attempts)
            self.assertEqual(db.execute('SELECT * FROM events ORDER BY id').fetchall()[:len(events)],events)
        self.assertEqual(migrate.ReadOnlyStore(backup).load(),before)
        self.assertIsNone(migrate.migrate(self.ex,bot.Store(self.path),Path(self.tmp.name)/'backups'))
        bot.match_account(after,bot.account(self.ex))

    def test_pending_intent_prevents_migration(self):
        self.store.reserve('unknown',{'hour':1})
        with self.assertRaises(bot.Blocked):migrate.preflight(migrate.ReadOnlyStore(self.path))
        self.assertEqual(self.store.load()['version'],v3.VERSION)

    def test_mismatched_account_prevents_migration(self):
        self.ex.key='different'
        with self.assertRaises(v3.Blocked):migrate.migrate(self.ex,bot.Store(self.path),Path(self.tmp.name)/'backups')
        self.assertEqual(self.store.load()['version'],v3.VERSION)

    def test_expiry_is_never_removed_or_extended(self):
        before=self.store.load();before['expires']=self.ex.clock-1;before['stop_reason']='COMPETITION_DEADLINE'
        after=migrate.upgrade_state(before,self.ex.clock)
        self.assertEqual(after['expires'],before['expires']);self.assertEqual(after['stop_reason'],before['stop_reason'])
        cfg=bot.raw_policy();cfg['end_utc']='2030-01-01T00:00:00+00:00'
        with patch.object(bot,'raw_policy',return_value=cfg),self.assertRaises(bot.Blocked):migrate.upgrade_state(before,self.ex.clock)

    def test_preflight_is_read_only_and_missing_path_is_not_created(self):
        before=self.path.read_bytes();migrate.preflight(migrate.ReadOnlyStore(self.path))
        self.assertEqual(before,self.path.read_bytes())
        absent=Path(self.tmp.name)/'absent.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):migrate.ReadOnlyStore(absent).load()
        self.assertFalse(absent.exists())

    def test_legacy_controller_cannot_operate_migrated_state(self):
        migrate.migrate(self.ex,bot.Store(self.path),Path(self.tmp.name)/'backups')
        with self.assertRaises(v3.Blocked):v3.identity(self.ex,self.store.load())

    def test_migrated_long_and_short_continue_with_one_owner_and_no_duplicate_hour(self):
        migrate.migrate(self.ex,bot.Store(self.path),Path(self.tmp.name)/'backups')
        sig={p:signal(i,1) for i,p in enumerate(PAIRS)}
        sig['ETH/USD'].update(direction=-1,momentum='-.05',momentum_24h='-.01',momentum_72h='-.02')
        previous=self.store.load()['positions']
        with patch.object(bot,'signals',return_value=(int(self.ex.clock)//3600,sig)), patch.object(bot.timing,'refresh',return_value=(int(self.ex.clock)//300*300000,{},{})):
            bot.cycle(self.ex,bot.Store(self.path))
            after=self.store.load()
            bot.match_account(after,bot.account(self.ex))
            for pair in previous:
                self.assertEqual(after['positions'][pair]['id'],previous[pair]['id'])
                self.assertEqual(after['positions'][pair]['quantity'],previous[pair]['quantity'])
                self.assertEqual(after['positions'][pair].get('inherited_v3_stop'),previous[pair].get('inherited_v3_stop'))
            writes=self.ex.writes
            bot.cycle(self.ex,bot.Store(self.path))
            self.assertEqual(self.ex.writes,writes)
            self.assertFalse(self.store.pending())

    def test_migration_refuses_wrong_purpose_or_source_fingerprint(self):
        state=self.store.load()
        for change in ({'purpose':'TESTING'},{'fingerprint':'wrong'}):
            self.store.save(dict(state,**change),'TEST',{})
            with self.assertRaises(bot.Blocked):migrate.preflight(migrate.ReadOnlyStore(self.path))
        self.store.save(state,'TEST',{})



if __name__=='__main__':unittest.main()
