"""Testing-only limit lifecycle fault injection: no public/private network access."""
import copy,json
from decimal import Decimal as D
from unittest.mock import patch
from test_competition_v3 import Base
from competition_v3 import controller as bot

class LimitTests(Base):
    def configure(self,fraction,*,lost_cancel=False,fill_race=False,unknown_terminal=False):
        self.config(lambda c:c['execution'].update(entry_type='LIMIT_TEST_ONLY'))
        self.save(purpose='TESTING')
        real=self.ex.request;orders={};self.cancel_calls=0
        def request(endpoint,params=None,**kw):
            if endpoint=='/v3/place_order' and params.get('type')=='LIMIT':
                self.ex.writes+=1;oid=str(self.ex.writes);qty=D(params['quantity']);price=D(params['price']);filled=qty*fraction
                fee=filled*price*D('.0005')
                self.ex.usd-=filled*price+fee;asset=params['pair'].split('/')[0]
                self.ex.coins[asset]=self.ex.coins.get(asset,D(0))+filled
                self.ex.usd=self.ex.usd.quantize(D('.01'))
                r=dict(OrderID=oid,Pair=params['pair'],Side='BUY',Type='LIMIT',Status='FILLED' if fraction==1 else 'PENDING',
                    Quantity=str(qty),FilledQuantity=str(filled),FilledAverPrice=str(price) if filled else '0',
                    CommissionCoin='USD',CommissionChargeValue=str(fee))
                orders[oid]=r
                return dict(Success=True,OrderDetail=copy.deepcopy(r))
            if endpoint=='/v3/query_order' and 'order_id' in params and params['order_id'] in orders:
                return dict(Success=True,OrderMatched=[copy.deepcopy(orders[params['order_id']])])
            if endpoint=='/v3/cancel_order':
                self.cancel_calls+=1;r=orders[params['order_id']]
                if fill_race:
                    extra=D(r['Quantity'])-D(r['FilledQuantity']);px=D('100');fee=extra*px*D('.0005')
                    self.ex.coins['BTC']+=extra;self.ex.usd-=extra*px+fee;self.ex.usd=self.ex.usd.quantize(D('.01'))
                    r.update(Status='FILLED',FilledQuantity=r['Quantity'],FilledAverPrice=str(px),CommissionChargeValue=str(D(r['CommissionChargeValue'])+fee))
                elif not unknown_terminal:r['Status']='CANCELED'
                if lost_cancel:raise TimeoutError('Lost cancel acknowledgement')
                return dict(Success=True,CanceledList=[params['order_id']])
            return real(endpoint,params,**kw)
        p=patch.object(self.ex,'request',side_effect=request);p.start();self.addCleanup(p.stop)
        # Model elapsed time without a wall-clock sleep.
        p=patch.object(bot.time,'sleep',side_effect=lambda x:setattr(self.ex,'clock',self.ex.clock+x));p.start();self.addCleanup(p.stop)
    def test_full_limit_fill(self):
        self.configure(D(1));self.buy();self.assertTrue(self.store.load()['positions']);self.assertEqual(self.cancel_calls,0)
        bot.match_account(self.store.load(),bot.account(self.ex))
    def test_partial_then_cancel_reconciles_only_actual_fill(self):
        self.configure(D('.5'));self.buy();self.assertEqual(self.cancel_calls,1)
        p=self.store.load()['positions']['BTC/USD'];self.assertLess(D(p['quantity'])*D(p['entry']),D(501))
        self.assertFalse(self.store.pending());bot.match_account(self.store.load(),bot.account(self.ex))
    def test_unfilled_cancel_creates_no_position(self):
        self.configure(D(0));self.buy();self.assertEqual(self.cancel_calls,1)
        self.assertFalse(self.store.load()['positions']);self.assertIsNone(self.store.load()['last_fill_day'])
    def test_lost_cancel_resolved_by_terminal_query_without_retry(self):
        self.configure(D('.5'),lost_cancel=True);self.buy();bot.recover_acknowledged(self.ex,self.store)
        self.assertEqual(self.cancel_calls,1);self.assertFalse(self.store.pending())
    def test_cancel_fill_race_adopts_full_fill(self):
        self.configure(D('.5'),fill_race=True);self.buy();self.assertEqual(self.cancel_calls,1)
        p=self.store.load()['positions']['BTC/USD'];self.assertGreater(D(p['quantity'])*D(p['entry']),D(990))
    def test_unknown_cancel_does_not_repeat(self):
        self.configure(D('.5'),lost_cancel=True,unknown_terminal=True)
        with self.assertRaises(bot.Blocked):self.buy()
        with self.assertRaises(bot.Blocked):bot.recover_acknowledged(self.ex,self.store)
        self.assertEqual(self.cancel_calls,1)
    def test_competition_limit_mode_refused(self):
        self.config(lambda c:c['execution'].update(entry_type='LIMIT_TEST_ONLY'))
        with self.assertRaises(bot.Blocked):self.buy()
        self.assertEqual(self.ex.writes,0)
