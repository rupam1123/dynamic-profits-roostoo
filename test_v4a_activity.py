import copy,json,tempfile,unittest
from pathlib import Path
from decimal import Decimal as D
from unittest.mock import patch
from test_competition_v4a import Base,fast,signal,Exchange
from competition_v4a import controller as bot,migrate
from competition_v4a.management import addition_candidate
from competition_v4a.runtime import Runner

class ActivityTests(Base):
    def test_pacing_is_durable_and_exits_remain_allowed(self):
        self.buy();self.prepare('ETH/USD');hour=int(self.ex.clock)//3600
        restarted=bot.Store(self.store.path)
        self.assertEqual(bot.entry_budget_reason(restarted),'ENTRY_PACING_60_SECONDS')
        with self.assertRaises(ValueError):bot.execute(self.ex,restarted,'next','ETH/USD','BUY',hour,owner='opportunity',budget_override='100',signal_context=self.sig)
        bot.execute(self.ex,restarted,'exit','BTC/USD','SELL',hour)
        self.ex.clock+=60
        self.assertIsNone(bot.entry_budget_reason(restarted))
        bot.execute(self.ex,restarted,'next','ETH/USD','BUY',hour,owner='opportunity',budget_override='100',signal_context=self.sig)
        bot.match_account(restarted.load(),bot.account(self.ex))

    def test_sixty_second_reentry_requires_fresh_closed_signal(self):
        self.buy();hour=int(self.ex.clock)//3600;bot.execute(self.ex,self.store,'exit','BTC/USD','SELL',hour)
        self.ex.clock+=59;info,ticks=bot.public_market(self.ex)
        self.assertFalse(bot.opportunity_candidates(self.store.load(),self.sig,info,ticks,self.cfg,hour)[0])
        self.ex.clock+=1;self.prepare()
        self.sig['BTC/USD']['direction']=0
        self.assertFalse(bot.opportunity_candidates(self.store.load(),self.sig,info,ticks,self.cfg,hour)[0])
        self.prepare()
        self.assertFalse(bot.opportunity_candidates(self.store.load(),self.sig,info,ticks,self.cfg,hour)[0])
        self.ex.clock=(int(self.ex.clock)//300+1)*300+2;self.prepare()
        bot.execute(self.ex,self.store,'fresh','BTC/USD','BUY',hour,owner='opportunity',budget_override='100',signal_context=self.sig)
        self.assertIn('BTC/USD',self.store.load()['positions'])

    def test_third_addition_allowed_only_with_capacity_and_new_breakout(self):
        self.buy();self.ex.clock+=300;state=self.store.load();state['positions']['BTC/USD']['addition_count']=2
        self.ex.prices['BTC/USD']=D(103);s=self.sig['BTC/USD'];s.update(close='103',breakout_high='102')
        s['timing']=dict(fast(s,self.ex.clock),previous='102',high='102.5',close='103')
        info,ticks=bot.public_market(self.ex)
        self.assertIsNotNone(addition_candidate(state,'BTC/USD',self.sig,info,ticks,self.cfg,int(self.ex.clock)//3600))
        state['positions']['BTC/USD']['quantity']='50'
        self.assertIsNone(addition_candidate(state,'BTC/USD',self.sig,info,ticks,self.cfg,int(self.ex.clock)//3600))

    def test_low_maker_fee_adjusts_target_but_retains_exit_cost_buffer(self):
        self.prepare();s=self.sig['BTC/USD'];s.update(volume_ratio='.5',breakout_high='110')
        s['fast']=dict(boundary=int(self.ex.clock)//60*60000,close='100',previous='99',mean='99.5',volume_ratio='3',high='99.8',low='98')
        original=self.ex.request
        def fee(endpoint,params=None,**kw):
            out=original(endpoint,params,**kw)
            if endpoint=='/v3/place_order':
                receipt=out['OrderDetail'];amount=D(receipt['CommissionChargeValue']);receipt['CommissionChargeValue']=str(amount/2)
                self.ex.usd+=amount/2
            return out
        with patch.object(self.ex,'request',side_effect=fee):
            bot.execute(self.ex,self.store,'maker','BTC/USD','BUY',int(self.ex.clock)//3600,owner='opportunity',budget_override='1000',signal_context=self.sig)
        p=self.store.load()['positions']['BTC/USD'];self.assertEqual(D(p['entry_fee_rate']),D('.0005'))
        self.assertEqual(D(p['take_profit_fraction']),D('.0035'))

class MigrationTests(unittest.TestCase):
    def test_original_v4_holdings_and_fingerprint_preserved_in_backup(self):
        from competition_v4 import controller as old
        ex=Exchange()
        with tempfile.TemporaryDirectory() as folder,patch.object(bot.time,'time',side_effect=lambda:ex.clock),patch.object(old,'emit'),patch.object(bot,'emit'):
            store=old.Store(Path(folder)/'execution.sqlite3');old.initialize(ex,store)
            sig={p:signal(i) for i,p in enumerate(ex.prices)};sig['BTC/USD']=signal(0,1);sig['BTC/USD']['timing']=fast(sig['BTC/USD'],ex.clock)
            old.execute(ex,store,'buy','BTC/USD','BUY',int(ex.clock)//3600,owner='opportunity',budget_override='1000',signal_context=sig)
            before=store.load();writes=ex.writes;backup=migrate.migrate(ex,bot.Store(store.path),Path(folder)/'backups')
            self.assertEqual(store.load()['positions'],before['positions']);self.assertEqual(ex.writes,writes)
            self.assertEqual(migrate.ReadOnlyStore(backup).load(),before)
            self.assertEqual(store.load()['version'],'competition-controller-4-active-1')

class ScheduleTests(unittest.TestCase):
    def test_explicit_ist_schedule_keeps_timezone_and_offsets(self):
        from competition_v4a.checkpoint import schedule
        rows=schedule('2026-10-08T15:48:00+05:30')
        self.assertEqual(rows[0]['utc'],'2026-10-08T10:18:00+00:00')
        self.assertEqual(rows[3]['ist'],'2026-10-08T16:03:00+05:30')
        self.assertEqual(rows[-1]['ist'],'2026-10-09T15:48:00+05:30')
