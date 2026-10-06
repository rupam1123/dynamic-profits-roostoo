from contextlib import closing
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from competition_v2 import controller as bot, strategy, feed, migrate
from competition_bot import controller as old
from test_competition_v2 import Exchange, MIDNIGHT

class ArchitectureChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=bot.Store(Path(self.tmp.name)/'db.sqlite3'); self.c=Exchange()
        self.time=patch.object(bot.time,'time',side_effect=lambda:self.c.clock)
        self.time.start(); self.addCleanup(self.time.stop)
        self.quiet=patch.object(bot,'emit'); self.quiet.start(); self.addCleanup(self.quiet.stop)
        bot.initialize(self.c,self.store,MIDNIGHT)
        self.cfg=bot.policy(MIDNIGHT)

    def sig(self,m='.03',vol='.02',trace=None):
        return dict(close='100',momentum=m,direction=1 if D(m)>0 else -1,score=str(abs(D(m))),
                    daily_vol=vol,quote_volume_24h='10000000',returns=trace or [1,-1,1,-1]*18)

    def test_ranked_selection_not_alphabetical(self):
        self.cfg['max_positions']=1
        info,ticks=bot.public_market(self.c)
        result,_=strategy.candidates(self.store.load(),{'BTC/USD':self.sig('.02'),'SOL/USD':self.sig('.08')},info,ticks,self.cfg,1)
        self.assertEqual(result[0]['pair'],'SOL/USD')

    def test_more_coins_does_not_raise_total_exposure(self):
        self.cfg['max_correlation']='.99'
        info,ticks=bot.public_market(self.c)
        traces=([1,-1,1,-1]*18,[1,1,-1,-1]*18,[1,-1,-1,1]*18)
        sig={p:self.sig(trace=traces[i]) for i,p in enumerate(self.c.prices)}
        picks,_=strategy.candidates(self.store.load(),sig,info,ticks,self.cfg,1)
        self.assertEqual(len(picks),3)
        self.assertLessEqual(sum(D(p['budget'])*D('1.02') for p in picks),D('30000'))

    def test_volatile_asset_gets_smaller_size(self):
        info,ticks=bot.public_market(self.c)
        picks,_=strategy.candidates(self.store.load(),{'BTC/USD':self.sig(vol='.08')},info,ticks,self.cfg,1)
        self.assertEqual(D(picks[0]['budget']),D('2500'))

    def test_correlated_entries_filtered(self):
        info,ticks=bot.public_market(self.c)
        picks,why=strategy.candidates(self.store.load(),{p:self.sig() for p in self.c.prices},info,ticks,self.cfg,1)
        self.assertEqual(len(picks),1)
        self.assertIn('CORRELATED_EXPOSURE',why.values())

    def test_bad_unused_pair_does_not_break_held_risk_check(self):
        self.c.prices['SOL/USD']=D('-1')
        info,ticks=bot.public_market(self.c,('BTC/USD',))
        picks,why=strategy.candidates(self.store.load(),{'SOL/USD':self.sig()},info,ticks,self.cfg,1)
        self.assertEqual(picks,[]); self.assertEqual(why['SOL/USD'],'BAD_QUOTE')

    def test_low_volume_and_wide_spread_skip_entries(self):
        info,ticks=bot.public_market(self.c)
        sig=self.sig(); sig['quote_volume_24h']='100'
        picks,why=strategy.candidates(self.store.load(),{'BTC/USD':sig},info,ticks,self.cfg,1)
        self.assertEqual(why['BTC/USD'],'LOW_VOLUME')
        ticks['Data']['BTC/USD']['MinAsk']=str(self.c.prices['BTC/USD']*D('1.01'))
        picks,why=strategy.candidates(self.store.load(),{'BTC/USD':self.sig()},info,ticks,self.cfg,1)
        self.assertEqual(why['BTC/USD'],'WIDE_SPREAD')

    def test_features_reject_flat_or_conflicting_trend(self):
        rows=[(i*feed.HOUR,'100','101','99','100','100',i*feed.HOUR+feed.HOUR-1,'1000000') for i in range(200)]
        self.assertEqual(strategy.features(rows)['direction'],0)
        rows=[(i*feed.HOUR,str(100+i),str(101+i),str(99+i),str(100+i),'100',i*feed.HOUR+feed.HOUR-1,'1000000') for i in range(200)]
        self.assertEqual(strategy.features(rows)['direction'],1)

    def test_feed_rejects_gap_future_or_nan(self):
        boundary=200*feed.HOUR
        raw=[[i*feed.HOUR,'100','101','99','100','1',i*feed.HOUR+feed.HOUR-1,'100'] for i in range(200)]
        bad=[r for i,r in enumerate(raw) if i!=190]
        with self.assertRaises(ValueError): feed.validate(bad,boundary)
        bad=[list(r) for r in raw]; bad[-1][4]='NaN'
        with self.assertRaises(ValueError): feed.validate(bad,boundary)
        self.assertEqual(len(feed.validate(raw,boundary)),200)

    def test_feed_failure_isolated_and_unclosed_candle_excluded(self):
        boundary=200*feed.HOUR
        rows=[[i*feed.HOUR,'100','101','99','100','1',i*feed.HOUR+feed.HOUR-1,'100'] for i in range(201)]
        def get(path,params=None):
            if path.endswith('time'): return {'serverTime':boundary+1000}
            if params['symbol']=='BADUSDT': raise ConnectionError('outage')
            return rows
        with patch.object(feed,'get',side_effect=get),patch.object(feed.time,'time',return_value=(boundary+1000)/1000):
            stamp,batches,failures=feed.refresh(Path(self.tmp.name)/'candles.sqlite3',('BTCUSDT','BADUSDT'))
        self.assertEqual(stamp,boundary); self.assertEqual(len(batches['BTCUSDT']),200)
        self.assertEqual(batches['BTCUSDT'][-1][0],boundary-feed.HOUR)
        self.assertIn('BADUSDT',failures)

    def open(self,pair='BTC/USD'):
        state=self.store.load(); info,ticks=bot.public_market(self.c)
        bot.submit(self.c,self.store,'initial:'+pair,bot.plan_order(state,pair,'BUY',info,ticks,MIDNIGHT//3600))

    def test_missing_one_held_history_does_not_block_other_exit(self):
        self.open('BTC/USD'); self.open('ETH/USD'); self.c.clock+=3600
        with patch.object(bot,'signals',return_value=(int(self.c.clock)//3600,{'BTC/USD':self.sig('-.03')})):
            bot.cycle(self.c,self.store)
        self.assertNotIn('BTC/USD',self.store.load()['positions'])
        self.assertIn('ETH/USD',self.store.load()['positions'])
        self.assertEqual(self.c.writes,3)

    def test_stop_latch_blocks_new_candidates(self):
        state=self.store.load(); state['stop_reason']='PORTFOLIO_DRAWDOWN'
        info,ticks=bot.public_market(self.c)
        picks,_=strategy.candidates(state,{'BTC/USD':self.sig()},info,ticks,self.cfg,1)
        self.assertEqual(picks,[])

    def test_equity_record_updated_after_entry(self):
        with patch.object(bot,'signals',return_value=(MIDNIGHT//3600,{'BTC/USD':self.sig()})):
            bot.cycle(self.c,self.store)
        self.assertLess(D(self.store.load()['last_equity']),D('100000'))

    def test_new_asset_can_execute_and_reconcile(self):
        self.c.prices['LINK/USD']=D('12'); self.c.precision['LINK/USD']=3
        with patch.object(bot,'signals',return_value=(MIDNIGHT//3600,{'LINK/USD':self.sig()})):
            bot.cycle(self.c,self.store)
        self.assertIn('LINK/USD',self.store.load()['positions'])
        self.assertFalse(self.store.pending())

    def test_migration_preserves_open_position_cash_history_and_high_water(self):
        self.open()
        state=self.store.load(); state['version']='competition-controller-1'; state['fingerprint']=old.fingerprint()
        state['peak_equity']='100123'; state['last_exit']={'SOL/USD':99}; state['last_hour']=77
        self.store.save(state,'LEGACY_TEST',{})
        writes=self.c.writes
        backup=migrate.migrate(self.c,self.store,Path(self.tmp.name)/'backups')
        migrated=self.store.load()
        for field in ('positions','usd','initial_usd','peak_equity','drawdown_usd','last_exit','last_hour','expires'):
            self.assertEqual(migrated[field],state[field])
        self.assertTrue(backup.exists()); self.assertEqual(self.c.writes,writes)
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(json.loads(db.execute('SELECT body FROM state').fetchone()[0])['version'],'competition-controller-1')
        self.assertIsNone(migrate.migrate(self.c,self.store,Path(self.tmp.name)/'backups'))

    def test_migration_refuses_unknown_and_different_account(self):
        state=self.store.load(); state['version']='competition-controller-1'; state['fingerprint']=old.fingerprint()
        self.store.save(state,'LEGACY_TEST',{})
        self.c.key='wrong'
        with self.assertRaises(old.Blocked): migrate.migrate(self.c,self.store,Path(self.tmp.name)/'backups')
        self.c.key=Exchange.key; self.store.reserve('unknown',{})
        with self.assertRaises(bot.Blocked): migrate.migrate(self.c,self.store,Path(self.tmp.name)/'backups')
        self.assertEqual(self.store.load()['version'],'competition-controller-1')
        self.assertEqual(self.c.writes,0)

if __name__=='__main__': unittest.main()
