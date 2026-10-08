"""Active management exercised through the real planner/reconciler and fake exchange."""
import copy
from decimal import Decimal as D
from unittest.mock import patch
from test_competition_v4a import Base,fast
from competition_v4a import controller as bot
from competition_v4a.management import addition_candidate,partial_quantity
from competition_v4a.runtime import Runner

class ManagementTests(Base):
    def add_context(self):
        self.ex.clock+=900
        self.ex.prices['BTC/USD']=D('103')
        s=self.sig['BTC/USD'];s.update(close='103',previous_close='102',breakout_high='102')
        s['timing']=dict(fast(s,self.ex.clock),high='102.5',previous='102',close='103')
        info,ticks=bot.public_market(self.ex)
        return addition_candidate(self.store.load(),'BTC/USD',self.sig,info,ticks,self.cfg,int(self.ex.clock)//3600)

    def add(self,pick):
        bot.execute(self.ex,self.store,'addition-test','BTC/USD','BUY',int(self.ex.clock)//3600,
                    owner='opportunity',adding=True,budget_override=pick['budget'],signal_context=self.sig)

    def test_addition_merges_quantity_weighted_cost_and_preserves_identity(self):
        self.buy();old=copy.deepcopy(self.store.load()['positions']['BTC/USD']);pick=self.add_context();self.assertIsNotNone(pick)
        self.add(pick);pos=self.store.load()['positions']['BTC/USD']
        self.assertEqual(pos['id'],old['id']);self.assertEqual(pos['addition_count'],1)
        self.assertGreater(D(pos['quantity']),D(old['quantity']))
        added=D(pos['quantity'])-D(old['quantity'])
        self.assertEqual(D(pos['entry']),(D(old['quantity'])*D(old['entry'])+added*D('103'))/D(pos['quantity']))
        self.assertGreaterEqual(D(pos['entry'])*(1-D(pos['stop_fraction'])),D(old['entry'])*(1-D(old['stop_fraction'])))
        bot.match_account(self.store.load(),bot.account(self.ex))
        info,ticks=bot.public_market(self.ex)
        self.assertIsNone(addition_candidate(self.store.load(),'BTC/USD',self.sig,info,ticks,self.cfg,int(self.ex.clock)//3600))

    def test_no_averaging_down_no_short_add_and_no_add_after_partial(self):
        self.buy();pick=self.add_context();self.assertIsNotNone(pick)
        info,ticks=bot.public_market(self.ex);state=self.store.load()
        for change in ({'partial_taken':True},{'direction':-1},{'book':'core'}):
            altered=copy.deepcopy(state);altered['positions']['BTC/USD'].update(change)
            self.assertIsNone(addition_candidate(altered,'BTC/USD',self.sig,info,ticks,self.cfg,int(self.ex.clock)//3600))
        ticks['Data']['BTC/USD'].update(MaxBid='99',MinAsk='99')
        self.assertIsNone(addition_candidate(state,'BTC/USD',self.sig,info,ticks,self.cfg,int(self.ex.clock)//3600))

    def test_cost_hurdle_blocks_addition(self):
        self.buy();self.add_context();self.sig['BTC/USD']['atr_pct']='.001'
        info,ticks=bot.public_market(self.ex)
        self.assertIsNone(addition_candidate(self.store.load(),'BTC/USD',self.sig,info,ticks,self.cfg,int(self.ex.clock)//3600))

    def test_addition_respects_combined_position_and_portfolio_risk_caps(self):
        self.buy();self.add_context();info,ticks=bot.public_market(self.ex);state=self.store.load()
        for quantity,stop in [('49','.025'),('19','.99')]:
            view=copy.deepcopy(state);view['positions']['BTC/USD'].update(quantity=quantity,stop_fraction=stop)
            self.assertIsNone(addition_candidate(view,'BTC/USD',self.sig,info,ticks,self.cfg,int(self.ex.clock)//3600))

    def test_partial_long_and_short_reconcile_once_and_preserve_residual(self):
        for pair,short,price in [('BTC/USD',False,'104'),('ETH/USD',True,'105')]:
            self.ex.clock+=60;self.buy(pair,short);self.ex.prices[pair]=D(price)
            info,ticks=bot.public_market(self.ex);pos=self.store.load()['positions'][pair]
            qty=partial_quantity(pos,ticks['Data'][pair],info['TradePairs'][pair],self.cfg);self.assertIsNotNone(qty)
            bot.execute(self.ex,self.store,'trim:'+pair,pair,'SHORT_TRIM' if short else 'TRIM',int(self.ex.clock)//3600,trim_qty=qty)
            after=self.store.load()['positions'][pair];self.assertEqual(D(after['quantity']),D(pos['quantity'])-D(qty));self.assertTrue(after['partial_taken'])
            self.assertIsNone(partial_quantity(after,ticks['Data'][pair],info['TradePairs'][pair],self.cfg))
            bot.match_account(self.store.load(),bot.account(self.ex))

    def test_runtime_partial_does_not_need_fresh_signal_history(self):
        self.buy();self.ex.prices['BTC/USD']=D('104');runner=Runner(self.ex,self.store)
        info,ticks=bot.public_market(self.ex);runner.market.metadata(info);runner.market.publish(ticks,self.store.load())
        with patch.object(bot,'MARKET',runner.market):runner.step()
        self.assertTrue(self.store.load()['positions']['BTC/USD']['partial_taken'])

    def test_acknowledged_addition_recovers_once_after_restart(self):
        self.buy();pick=self.add_context();real=bot.recover_acknowledged
        with patch.object(bot,'recover_acknowledged'):
            self.add(pick)
        self.assertEqual(self.store.pending()[0][1],'ACK')
        before=self.ex.writes;real(self.ex,self.store);real(self.ex,self.store)
        self.assertEqual(before,self.ex.writes);self.assertEqual(self.store.load()['positions']['BTC/USD']['addition_count'],1)
        bot.match_account(self.store.load(),bot.account(self.ex))

    def test_lost_addition_response_blocks_resubmission(self):
        self.buy();pick=self.add_context();original=self.ex.request
        def lost(endpoint,*a,**kw):
            result=original(endpoint,*a,**kw)
            if endpoint=='/v3/place_order':raise TimeoutError('Lost response')
            return result
        with patch.object(self.ex,'request',side_effect=lost):
            with self.assertRaises(bot.Blocked):self.add(pick)
        count=self.ex.writes
        with self.assertRaises(bot.Blocked):bot.recover_acknowledged(self.ex,self.store)
        self.assertEqual(self.ex.writes,count)

    def test_reentry_requires_cooldown_and_fresh_qualified_bar(self):
        self.buy();hour=int(self.ex.clock)//3600
        bot.execute(self.ex,self.store,'close','BTC/USD','SELL',hour)
        info,ticks=bot.public_market(self.ex)
        picks,_=bot.opportunity_candidates(self.store.load(),self.sig,info,ticks,self.cfg,hour)
        self.assertFalse(picks)
        self.ex.clock+=3600;self.prepare();self.buy()
        self.assertIn('BTC/USD',self.store.load()['positions'])
