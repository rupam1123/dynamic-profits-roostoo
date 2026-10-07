"""V3.1 accounting, strategy parity and safety tests. All exchange calls are fake."""
import copy
from contextlib import closing
from decimal import Decimal as D, ROUND_DOWN
import importlib.util
import json
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from competition_v31 import controller as bot, strategy, migrate, report
from competition_v3 import controller as v3
from v3_tests.exchange import Exchange, PAIRS, MIDNIGHT

_baseline_path = Path(__file__).resolve().parent / 'research_v3/reference/competition_v21/strategy.py'
_baseline_spec = importlib.util.spec_from_file_location('frozen_v21_strategy', _baseline_path)
baseline = importlib.util.module_from_spec(_baseline_spec)
_baseline_spec.loader.exec_module(baseline)


def signal(i=0, direction=0):
    rng = random.Random(i)
    return dict(close=str(100 + 10*i), momentum=str(.05*direction), momentum_24h=str(.01*(direction or 1)),
        momentum_72h=str(.02*(direction or 1)), daily_vol='.02', direction=direction, score=str(2+i/10),
        quote_volume_24h='100000000', returns=[rng.uniform(-.01,.01) for _ in range(72)], z24='0', r6='0',
        momentum_336h=str((i-6)/100), momentum_720h='.05' if i%2 else '-.05',
        vol_median_720h='.02', candles=1000)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = bot.Store(Path(self.tmp.name)/'execution.sqlite3')
        self.ex = Exchange()
        for p in (patch.object(bot.time,'time',side_effect=lambda:self.ex.clock), patch.object(bot,'emit'), patch.object(v3,'emit')):
            p.start(); self.addCleanup(p.stop)
        self.sig = {p:signal(i) for i,p in enumerate(PAIRS)}
        self.cfg = bot.raw_policy()
        bot.initialize(self.ex,self.store,MIDNIGHT)

    def cycle(self, sig=None):
        with patch.object(bot,'signals',return_value=(int(self.ex.clock)//3600,self.sig if sig is None else sig)):
            return bot.cycle(self.ex,self.store)

    def buy(self, pair='BTC/USD', short=False, **kw):
        hour = int(self.ex.clock)//3600
        return bot.execute(self.ex,self.store,str(hour)+':'+pair,pair,'SHORT_OPEN' if short else 'BUY',hour,
            budget_override='1000',signal_context=self.sig,**kw)

    def save(self, **kw):
        state=self.store.load();state.update(kw);self.store.save(state,'TEST',{});return state


class BaselineTests(Base):
    def test_policy_is_the_uploaded_v21_policy(self):
        old=Path(baseline.__file__).with_name('policy.json')
        self.assertEqual(bot.raw_policy(),json.loads(old.read_text()))

    def test_core_candidates_match_v21(self):
        s={p:signal(i,1 if i%2 else -1) for i,p in enumerate(PAIRS)}
        state=self.store.load();info,ticks=bot.public_market(self.ex);hour=int(self.ex.clock)//3600
        self.assertEqual(strategy.candidates(state,s,info,ticks,self.cfg,hour),baseline.candidates(state,s,info,ticks,self.cfg,hour))

    def test_sleeve_targets_match_v21(self):
        for sleeve in self.cfg['sleeves']:
            self.assertEqual(strategy.sleeve_target(sleeve,self.sig,PAIRS,self.store.load(),D(1)),
                baseline.sleeve_target(sleeve,self.sig,PAIRS,self.store.load(),D(1)))

    def test_first_cycle_keeps_sleeve_priority_and_twelve_small_positions(self):
        self.cycle();state=self.store.load()
        self.assertEqual(len(state['positions']),12)
        self.assertEqual(sum(p['book']=='xs' for p in state['positions'].values()),4)
        self.assertEqual(sum(p['book']=='ts' for p in state['positions'].values()),8)
        self.assertFalse(any(p['book']=='core' for p in state['positions'].values()))
        bot.match_account(state,bot.account(self.ex))

    def test_guard_preserves_declared_small_probe_and_expiry(self):
        cfg=copy.deepcopy(self.cfg);cfg['sleeves']=[]
        self.ex.clock=MIDNIGHT+12*3600+120
        with patch.object(bot,'raw_policy',return_value=cfg):
            self.cycle();state=self.store.load()
            self.assertEqual(len(state['positions']),1)
            pair=next(iter(state['positions']));pos=state['positions'][pair]
            self.assertLessEqual(D(pos['quantity'])*D(pos['entry']),D('2000'))
            self.assertEqual(pos['probe_until'],int(self.ex.clock)//3600+24)
            self.ex.clock+=24*3600
            self.cycle()
            self.assertNotIn(pair,self.store.load()['positions'])

    def test_restart_same_hour_sends_no_duplicates(self):
        self.cycle();count=self.ex.writes
        self.store=bot.Store(self.store.path)
        self.cycle();self.assertEqual(count,self.ex.writes)

    def test_preview_is_public_and_no_orders(self):
        with patch.object(bot,'Client',return_value=self.ex), patch.object(bot,'credentials',side_effect=AssertionError('No credentials')), \
             patch.object(bot,'signals',return_value=(int(self.ex.clock)//3600,self.sig)), patch('sys.argv',['controller','--preview']):
            bot.main()
        self.assertEqual(self.ex.writes,0)


class SafetyTests(Base):
    def test_every_entry_path_blocks_during_pause(self):
        self.ex.clock=MIDNIGHT+12*3600+120;hour=int(self.ex.clock)//3600
        self.save(pause_until=hour+24)
        self.cycle();self.assertEqual(self.ex.writes,0)
        info,ticks=bot.public_market(self.ex)
        for owner in ('core','xs','ts'):
            with self.assertRaises(bot.Blocked):
                bot.plan_order(self.store.load(),'BTC/USD','BUY',info,ticks,hour,owner=owner)
        with self.assertRaises(bot.Blocked):
            bot.plan_order(self.store.load(),'BTC/USD','BUY',info,ticks,hour,probe_until=hour+24)

    def test_missing_held_history_blocks_all_entries(self):
        self.buy();self.ex.clock+=3600;before=self.ex.writes
        self.cycle({p:s for p,s in self.sig.items() if p!='BTC/USD'})
        self.assertEqual(before,self.ex.writes)

    def test_entry_requires_signal_context(self):
        with self.assertRaises(ValueError):
            bot.execute(self.ex,self.store,'no-context','BTC/USD','BUY',int(self.ex.clock)//3600)
        self.assertFalse(self.store.pending());self.assertEqual(self.ex.writes,0)

    def test_deadline_closes_without_candles(self):
        self.buy();self.save(expires=self.ex.clock-1)
        with patch.object(bot,'signals',side_effect=AssertionError('No candles needed')):
            self.assertTrue(bot.cycle(self.ex,self.store))
        self.assertFalse(self.store.load()['positions'])

    def test_policy_accepts_past_deadline_for_exit_loop(self):
        cfg=copy.deepcopy(self.cfg);cfg['end_utc']='2025-01-01T00:00:00+00:00'
        with patch.object(bot,'raw_policy',return_value=cfg):
            self.assertLess(bot.policy(self.ex.clock)['end_epoch'],self.ex.clock)

    def test_late_entry_never_reserves_or_sends(self):
        self.ex.clock=MIDNIGHT+16*60
        with self.assertRaises(ValueError):self.buy()
        self.assertEqual(self.ex.writes,0);self.assertFalse(self.store.pending())

    def test_deadline_expires_inside_client_is_definitely_unsent(self):
        real=self.ex.request
        def request(endpoint,*args,**kw):
            if endpoint=='/v3/place_order':raise bot.NotSent('Expired after throttling')
            return real(endpoint,*args,**kw)
        with patch.object(self.ex,'request',side_effect=request),self.assertRaises(ValueError):self.buy()
        self.assertEqual(self.ex.writes,0);self.assertFalse(self.store.pending())
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='ORDER_NOT_SENT'").fetchone()[0],1)

    def test_lost_placement_response_blocks_restart_without_resubmission(self):
        real=self.ex.request
        def request(endpoint,*args,**kw):
            result=real(endpoint,*args,**kw)
            if endpoint=='/v3/place_order':raise TimeoutError('Lost acknowledgement')
            return result
        with patch.object(self.ex,'request',side_effect=request),self.assertRaises(bot.Blocked):self.buy()
        count=self.ex.writes
        self.store=bot.Store(self.store.path)
        with self.assertRaises(bot.Blocked):self.cycle()
        self.assertEqual(self.ex.writes,count);self.assertEqual(self.store.pending()[0][1],'UNKNOWN')

    def test_ack_recovery_applies_once(self):
        info,ticks=bot.public_market(self.ex);hour=int(self.ex.clock)//3600
        plan=bot.plan_order(self.store.load(),'BTC/USD','BUY',info,ticks,hour,budget_override='1000')
        self.store.reserve('ack',plan)
        result=self.ex.request(plan['endpoint'],plan['params'])
        self.store.acknowledge('ack','ACK',result)
        bot.recover_acknowledged(self.ex,self.store)
        count=self.ex.writes;bot.recover_acknowledged(self.ex,self.store)
        self.assertEqual(count,self.ex.writes);self.assertFalse(self.store.pending())

    def test_partial_long_trim_and_full_short_close_reconcile(self):
        self.buy();self.buy('ETH/USD',short=True)
        state=self.store.load();half=D(state['positions']['BTC/USD']['quantity'])/2
        bot.execute(self.ex,self.store,'trim','BTC/USD','TRIM',int(self.ex.clock)//3600,trim_qty=str(half))
        bot.execute(self.ex,self.store,'close-short','ETH/USD','SHORT_CLOSE',int(self.ex.clock)//3600)
        self.assertNotIn('ETH/USD',self.store.load()['positions'])
        bot.match_account(self.store.load(),bot.account(self.ex))

    def test_peak_is_not_erased_when_pause_ends(self):
        self.buy();state=self.store.load();peak=state['peak_equity']
        self.save(risk_peak='110000',peak_equity='110000')
        bot.risk_mark(self.ex,self.store);self.assertTrue(self.store.load()['flatten_pending'])
        bot.flatten(self.ex,self.store,int(self.ex.clock)//3600)
        pause=self.store.load()['pause_until'];self.ex.clock=pause*3600+120
        state,_=bot.risk_mark(self.ex,self.store)
        self.assertEqual(state['peak_equity'],'110000')
        self.assertLess(D(state['risk_peak']),D(state['peak_equity']))
        self.assertEqual(state['pause_until'],-1)
        self.assertEqual(strategy.risk_multiplier(state,self.cfg,int(self.ex.clock)//3600),D('.5'))

    def test_pause_starts_from_confirmed_flattening(self):
        self.buy();hour=int(self.ex.clock)//3600
        self.save(flatten_pending=True,pause_until=hour-3)
        bot.flatten(self.ex,self.store,hour)
        self.assertEqual(self.store.load()['pause_until'],hour+24)

    def test_inherited_stop_works_without_candles_outside_window(self):
        self.buy();state=self.store.load();state['positions']['BTC/USD']['inherited_v3_stop']=True
        self.store.save(state,'TEST',{});self.ex.prices['BTC/USD']=D('90');self.ex.clock+=1800
        with patch.object(bot,'signals',side_effect=AssertionError('No candles required')):bot.cycle(self.ex,self.store)
        self.assertFalse(self.store.load()['positions'])

    def test_new_positions_use_baseline_exit_not_inherited_quote_stop(self):
        self.buy();self.ex.prices['BTC/USD']=D('90');self.ex.clock+=1800
        self.cycle();self.assertIn('BTC/USD',self.store.load()['positions'])

    def test_aggregate_cap_includes_sleeves_and_probe(self):
        state=self.store.load();info,ticks=bot.public_market(self.ex);hour=int(self.ex.clock)//3600
        state['positions']={'ETH/USD':dict(direction=1,quantity='460',entry='110',collateral='0',id='x',book='ts')}
        for kw in ({'owner':'xs'},{'owner':'core','probe_until':hour+24}):
            with self.assertRaises(ValueError):bot.plan_order(state,'BTC/USD','BUY',info,ticks,hour,**kw)

    def test_wrong_account_fingerprint_and_version_block(self):
        for key,value in [('account','wrong'),('fingerprint','wrong'),('version','competition-controller-3')]:
            state=self.store.load();state[key]=value
            with self.assertRaises(bot.Blocked):bot.identity(self.ex,state)

    def test_process_lock_prevents_second_writer(self):
        path=Path(self.tmp.name)/'controller.lock'
        with bot.process_lock(path),self.assertRaises(bot.Blocked):
            with bot.process_lock(path):pass

    def test_no_reinitialize(self):
        with self.assertRaises(bot.Blocked):bot.initialize(self.ex,self.store)

    def test_report_does_not_count_unsent_order_as_fill(self):
        self.store.reserve('unsent',dict(hour=1,action='BUY'))
        self.store.save(self.store.load(),'ORDER_NOT_SENT',{},applied='unsent')
        summary=report.build(self.store.path,Path(self.tmp.name)/'report')
        self.assertEqual(summary['reconciled_fills'],0)


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
        v3.execute(self.ex,self.store,'old-long','BTC/USD','BUY',int(self.ex.clock)//3600,budget_override='1000',universe_signals=sig)
        sig['ETH/USD'].update(direction=-1,momentum='-.08',momentum_24h='-.03',momentum_72h='-.05',r6='-.01')
        v3.execute(self.ex,self.store,'old-short','ETH/USD','SHORT_OPEN',int(self.ex.clock)//3600,budget_override='1000',universe_signals=sig)

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
            self.assertTrue(after['positions'][pair]['inherited_v3_stop'])
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
        with patch.object(bot,'signals',return_value=(int(self.ex.clock)//3600,sig)):
            bot.cycle(self.ex,bot.Store(self.path))
            after=self.store.load()
            bot.match_account(after,bot.account(self.ex))
            for pair in previous:
                self.assertEqual(after['positions'][pair]['id'],previous[pair]['id'])
                self.assertEqual(after['positions'][pair]['quantity'],previous[pair]['quantity'])
                self.assertTrue(after['positions'][pair]['inherited_v3_stop'])
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


if __name__=='__main__':
    unittest.main()
