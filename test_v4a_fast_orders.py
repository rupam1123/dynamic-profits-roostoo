"""Limit cancellation uncertainty and fast target lifecycle tests; fake exchange only."""
import copy,json,unittest
from decimal import Decimal as D
from unittest.mock import patch
from test_competition_v4a import Base,fast
from competition_v4a import controller as bot
from competition_v4a.runtime import Runner
from competition_v4a.limit_orders import settle

class FastExecutionTests(Base):
    def runner(self):
        self.prepare();s=self.sig['BTC/USD'];s.update(breakout_high='110',breakout_low='90',volume_ratio='.5')
        s['fast']=dict(boundary=int(self.ex.clock)//60*60000,close='100',previous='99',mean='99.5',volume_ratio='3',high='99.8',low='98')
        r=Runner(self.ex,self.store);info,ticks=bot.public_market(self.ex)
        r.market.metadata(info);r.market.mapping({p:p.split('/')[0]+'USDT' for p in self.sig},{})
        r.market.publish(ticks,self.store.load());r.market.publish_signals(int(self.ex.clock)//3600,int(self.ex.clock)//300,self.sig,{},int(self.ex.clock)//60)
        market_patch=patch.object(bot,'MARKET',r.market)
        market_patch.start();self.addCleanup(market_patch.stop)
        return r

    def test_fast_limit_entry_and_quote_target_exit(self):
        r=self.runner();r.step();pos=self.store.load()['positions']['BTC/USD']
        self.assertEqual(pos['stop_fraction'],'0.006');self.assertEqual(D(pos['take_profit_fraction']),D('.004'))
        self.assertEqual(next(iter(self.ex.orders.values()))['Type'],'LIMIT')
        self.ex.prices['BTC/USD']=D('100.7');r.market.publish(self.ex.request('/v3/ticker'),self.store.load())
        self.assertEqual(r.market.next_risk()[1]['reason'],'FAST_PROFIT_TARGET');r.step()
        self.assertNotIn('BTC/USD',self.store.load()['positions'])
        bot.match_account(self.store.load(),bot.account(self.ex))

    def test_fast_timeout_is_autonomous_without_new_signal(self):
        r=self.runner();r.step();self.ex.clock+=901;r.market.publish(self.ex.request('/v3/ticker'),self.store.load())
        self.assertEqual(r.market.next_risk()[1]['reason'],'FAST_TIME_EXIT');r.step()
        self.assertFalse(self.store.load()['positions'])

    def test_fast_partial_limit_fill_reconciles_residual_once(self):
        r=self.runner();original=self.ex.request
        def partial(endpoint,params=None,**kw):
            if endpoint=='/v3/place_order':
                partial_params=dict(params,quantity=str(D(params['quantity'])/2))
                result=original(endpoint,partial_params,**kw);order=result['OrderDetail']
                order.update(Quantity=params['quantity'],Status='CANCELED')
                return result
            return original(endpoint,params,**kw)
        with patch.object(self.ex,'request',side_effect=partial):r.step()
        self.assertTrue(self.store.load()['positions']);self.assertFalse(self.store.pending())
        count=self.ex.writes;r.step();self.assertEqual(count,self.ex.writes)
        bot.match_account(self.store.load(),bot.account(self.ex))

    def test_fast_cost_hurdle_rejects_tiny_range(self):
        r=self.runner()
        with r.market.lock:r.market.signals['BTC/USD']['fast'].update(high='99.9',low='99.6')
        r.step();self.assertEqual(self.ex.writes,0)

class CancelTests(unittest.TestCase):
    def test_lost_cancel_resolves_by_read_never_resubmits(self):
        records=[];calls=[]
        class Store:
            def acknowledge(self,*args):records.append(args[1])
        class Client:
            def request(self,endpoint,params,**kwargs):
                calls.append(endpoint)
                if endpoint=='/v3/cancel_order':raise TimeoutError('lost ACK')
                status='CANCELED' if '/v3/cancel_order' in calls else 'NEW'
                return dict(Success=True,OrderMatched=[dict(OrderID=7,Pair='BTC/USD',Side='BUY',Type='LIMIT',Status=status)])
        plan=dict(created_utc=0,limit_timeout_seconds=12,pair='BTC/USD')
        result=settle(Client(),Store(),'intent',plan,{'OrderDetail':{'OrderID':7}},'ACK',bot.Blocked)
        self.assertEqual(result['Status'],'CANCELED');self.assertEqual(calls.count('/v3/cancel_order'),1)
        self.assertEqual(records,['CANCEL_SENDING','CANCEL_UNKNOWN'])

    def test_unknown_cancel_still_open_blocks_without_second_cancel(self):
        class Client:
            def request(self,endpoint,params,**kwargs):
                self.assertion=endpoint
                if endpoint!='/v3/query_order':raise AssertionError('No second cancellation permitted')
                return dict(Success=True,OrderMatched=[dict(OrderID=7,Pair='BTC/USD',Side='BUY',Type='LIMIT',Status='NEW')])
        with self.assertRaises(bot.Blocked):settle(Client(),None,'intent',dict(created_utc=0,limit_timeout_seconds=12,pair='BTC/USD'),{'OrderDetail':{'OrderID':7}},'CANCEL_UNKNOWN',bot.Blocked)
