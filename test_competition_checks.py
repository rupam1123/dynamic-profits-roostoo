from contextlib import closing
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from competition_bot import controller as bot
from competition_bot import report as controller_report

D = Decimal
MIDNIGHT = 20732*86400


class Exchange:
    key = 'offline-test-key'
    def __init__(self):
        self.usd = D('100000'); self.coins = {}; self.shorts = {}; self.orders = {}
        self.writes = 0; self.failure = None; self.read_failure = False
        self.prices = {'BTC/USD':D('85649.14'), 'ETH/USD':D('2500'), 'SOL/USD':D('120')}
        self.precision = {'BTC/USD':5, 'ETH/USD':4, 'SOL/USD':3}
        self.clock = MIDNIGHT+120

    def balance(self):
        if self.read_failure: raise ConnectionError('simulated read outage')
        locked = sum((D(p['Collateral']) for p in self.shorts.values()), D('0'))
        spot = {'USD':{'Free':str(self.usd), 'Lock':str(locked), 'ShortCollateral':str(locked), 'PendingOrders':0}}
        for asset, qty in self.coins.items(): spot[asset] = {'Free':str(qty), 'Lock':0, 'ShortCollateral':0, 'PendingOrders':0}
        return {'Success':True, 'SpotWallet':spot, 'MarginWallet':{}}

    def short_positions(self): return {'Success':True, 'Positions':list(self.shorts.values())}

    def request(self, endpoint, params=None, **kwargs):
        if endpoint == '/v3/serverTime': return {'ServerTime':int(self.clock*1000)}
        if endpoint == '/v3/exchangeInfo':
            return {'IsRunning':True, 'TradePairs':{p:dict(CanTrade=True, AmountPrecision=self.precision[p], MiniOrder=1) for p in bot.PAIRS}}
        if endpoint == '/v3/ticker':
            return {'Success':True, 'ServerTime':int(self.clock*1000), 'Data':{p:dict(MaxBid=str(v), MinAsk=str(v)) for p,v in self.prices.items()}}
        if endpoint == '/v3/query_order':
            if 'pending_only' in params: return {'Success':False, 'ErrMsg':'no order matched'}
            return {'Success':True, 'OrderMatched':[self.orders[params['order_id']]]}
        self.writes += 1
        if self.failure == 'reject': return {'Success':False, 'ErrMsg':'no permission'}
        pair = params['pair']; price = self.prices[pair]
        if endpoint == '/v3/place_order':
            qty = D(params['quantity']); fee = qty*price*D('.001'); buy = params['side']=='BUY'
            asset = pair.split('/')[0]
            self.coins[asset] = self.coins.get(asset,D('0'))+(qty if buy else -qty)
            self.usd += -qty*price-fee if buy else qty*price-fee
            order = dict(OrderID=self.writes, Pair=pair, Side=params['side'], Type='MARKET', Status='FILLED',
                         Quantity=str(qty), FilledQuantity=str(qty), FilledAverPrice=str(price),
                         CommissionCoin='USD', CommissionChargeValue=str(fee))
            self.orders[str(self.writes)] = order
            result = {'Success':True, 'OrderDetail':order}
        elif endpoint == '/v6/short_open':
            qty = (D(params['collateral'])/price).quantize(D(1).scaleb(-self.precision[pair]), rounding=ROUND_DOWN)
            collateral = (qty*price).quantize(D('.01')); fee = qty*price*D('.001')
            self.usd -= collateral+fee
            result = dict(Success=True, ID=self.writes, Pair=pair, Status='OPEN', ShortQty=str(qty),
                          EntryPrice=str(price), Collateral=str(collateral), OpenFee=str(fee))
            self.shorts[pair] = result.copy()
        elif endpoint == '/v6/short_close':
            pos = self.shorts.pop(pair); qty = D(pos['ShortQty']); collateral = D(pos['Collateral'])
            pnl = max(qty*(D(pos['EntryPrice'])-price), -collateral); fee = qty*price*D('.001')
            returned = collateral+pnl-fee; self.usd += returned
            result = dict(Success=True, FullyClosed=True, ClosedQty=str(qty), ClosePrice=str(price),
                          CloseFee=str(fee), RealizedPNL=str(pnl), ReturnAmount=str(returned))
        else: raise AssertionError(endpoint)
        self.usd = self.usd.quantize(D('.01'))
        if self.failure == 'timeout': raise TimeoutError('response lost after fill')
        if self.failure == 'post_fill_read': self.read_failure = True
        return result


class ControllerChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = bot.Store(Path(self.tmp.name)/'execution.db')
        self.c = Exchange()
        self.clock = patch('competition_bot.controller.time.time', side_effect=lambda:self.c.clock)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.quiet = patch('competition_bot.controller.emit'); self.quiet.start(); self.addCleanup(self.quiet.stop)
        bot.initialize(self.c, self.store, MIDNIGHT)

    def signal(self, values):
        return {p:{'close':str(self.c.prices[p]), 'momentum':str(values[i])} for i,p in enumerate(bot.PAIRS)}

    def cycle(self, values):
        with patch.object(bot, 'signals', return_value=(int(self.c.clock)//3600,self.signal(values))):
            return bot.cycle(self.c, self.store)

    def test_long_and_short_autonomous_open_close(self):
        self.cycle([.02,-.02,0])
        self.assertEqual(self.c.writes,2)
        self.assertEqual(len(self.store.load()['positions']),2)
        self.c.clock += 3600
        self.cycle([-.01,.01,0])
        self.assertEqual(self.c.writes,4)
        self.assertEqual(self.store.load()['positions'],{})
        self.assertLess(self.c.usd,D('100000'))

    def test_repeated_hour_and_restart_do_not_duplicate(self):
        self.cycle([.02,.02,.02]); self.assertEqual(self.c.writes,3)
        self.store = bot.Store(self.store.path)
        self.cycle([.02,.02,.02]); self.assertEqual(self.c.writes,3)
        self.assertLessEqual(sum(D(p['quantity'])*D(p['entry']) for p in self.store.load()['positions'].values()),30000)

    def test_acknowledged_fill_recovers_after_read_outage(self):
        self.c.failure = 'post_fill_read'
        with self.assertRaises(ConnectionError): self.cycle([.02,0,0])
        self.assertEqual(self.store.pending()[0][1],'ACK')
        self.c.failure = None; self.c.read_failure = False
        self.cycle([.02,0,0])
        self.assertEqual(self.c.writes,1)
        self.assertEqual(len(self.store.load()['positions']),1)
        self.assertFalse(self.store.pending())

    def test_short_open_and_close_acknowledgements_recover(self):
        self.c.failure = 'post_fill_read'
        with self.assertRaises(ConnectionError): self.cycle([-.02,0,0])
        self.c.failure = None; self.c.read_failure = False
        self.cycle([-.02,0,0]); self.assertEqual(self.c.writes,1)
        self.c.clock += 3600; self.c.failure = 'post_fill_read'
        with self.assertRaises(ConnectionError): self.cycle([.02,0,0])
        self.c.failure = None; self.c.read_failure = False
        self.cycle([.02,0,0])
        self.assertEqual(self.c.writes,2)
        self.assertEqual(self.store.load()['positions'],{})

    def test_incomplete_spot_fill_is_not_applied_or_resent(self):
        self.c.failure = 'post_fill_read'
        with self.assertRaises(ConnectionError): self.cycle([.02,0,0])
        self.c.failure = None; self.c.read_failure = False
        self.c.orders['1']['Status'] = 'PARTIALLY_FILLED'
        with self.assertRaises(bot.Blocked): self.cycle([.02,0,0])
        self.assertEqual(self.c.writes,1)
        self.assertFalse(self.store.load()['positions'])

    def test_audit_reports_persisted_fills_without_credentials(self):
        self.cycle([.02,-.02,0])
        output = Path(self.tmp.name)/'report'
        summary = controller_report.build(self.store.path, output)
        self.assertEqual(summary['reconciled_fills'],2)
        self.assertEqual(summary['unresolved'],0)
        audit = (output/'controller_audit.json').read_text()
        self.assertNotIn(self.c.key, audit)
        self.assertTrue((output/'controller_audit.html').exists())

    def test_unknown_and_rejected_orders_block_restart(self):
        for failure in ('timeout','reject'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                store = bot.Store(Path(folder)/'x.db'); c = Exchange()
                bot.initialize(c,store,MIDNIGHT); c.failure = failure
                with patch.object(bot,'signals',return_value=(MIDNIGHT//3600,self.signal([.02,0,0]))):
                    with self.assertRaises(bot.Blocked): bot.cycle(c,store)
                    c.failure = None
                    with self.assertRaises(bot.Blocked): bot.cycle(c,store)
                self.assertEqual(c.writes,1)

    def test_crash_before_ack_blocks_instead_of_repeating(self):
        state = self.store.load(); info,ticks = bot.public_market(self.c)
        plan = bot.plan_order(state,'BTC/USD','BUY',info,ticks,MIDNIGHT//3600)
        self.store.reserve(str(MIDNIGHT//3600)+':BTC/USD',plan)
        self.c.request(plan['endpoint'],plan['params'])
        with self.assertRaises(bot.Blocked): self.cycle([.02,0,0])
        self.assertEqual(self.c.writes,1)

    def test_changed_account_code_or_holdings_block(self):
        self.c.key = 'wrong'
        with self.assertRaises(bot.Blocked): self.cycle([.02,0,0])
        self.c.key = Exchange.key
        with patch.object(bot,'fingerprint',return_value='changed'):
            with self.assertRaises(bot.Blocked): self.cycle([.02,0,0])
        self.c.coins['BTC'] = D('0.01')
        with self.assertRaises(bot.Blocked): self.cycle([.02,0,0])
        self.assertEqual(self.c.writes,0)

    def test_expiry_closes_without_candle_feed_and_never_reopens(self):
        self.cycle([.02,-.02,0])
        state = self.store.load(); state['expires'] = MIDNIGHT+26*3600
        self.store.save(state,'TEST_DEADLINE',{})
        self.c.clock = MIDNIGHT+26*3600+120
        with patch.object(bot,'signals',side_effect=AssertionError('expiry must not need candles')):
            self.assertTrue(bot.cycle(self.c,self.store))
            self.assertTrue(bot.cycle(self.c,self.store))
        self.assertEqual(self.c.writes,4)
        self.assertEqual(self.store.load()['positions'],{})

    def test_portfolio_drawdown_latches_and_closes(self):
        self.cycle([.02,0,0]); self.c.clock += 3600
        self.c.prices['BTC/USD'] *= D('.5')
        self.assertTrue(self.cycle([.02,0,0]))
        self.assertEqual(self.store.load()['stop_reason'],'PORTFOLIO_DRAWDOWN')
        self.assertEqual(self.store.load()['positions'],{})
        self.assertEqual(self.c.writes,2)

    def test_drawdown_can_close_in_same_hour_as_entry(self):
        self.cycle([.02,0,0])
        self.c.clock += 60
        self.c.prices['BTC/USD'] *= D('.5')
        self.assertTrue(self.cycle([.02,0,0]))
        self.assertEqual(self.c.writes,2)
        self.assertEqual(self.store.load()['positions'],{})

    def test_hourly_entry_and_late_hour_skip(self):
        self.c.clock += 3600; self.cycle([.02,-.02,0])
        self.assertEqual(self.c.writes,2)
        self.c.clock = MIDNIGHT+86400+11*60; self.cycle([.02,-.02,.02])
        self.assertEqual(self.c.writes,2)

    def test_competition_has_no_test_deadline(self):
        self.assertIsNone(self.store.load()['expires'])
        self.assertEqual(D(self.store.load()['budget']),D('10000'))
        self.assertEqual(D(self.store.load()['drawdown_usd']),D('3000'))

    def test_testing_balance_rejected_before_any_order(self):
        with tempfile.TemporaryDirectory() as folder:
            c=Exchange(); c.usd=D('49999.95')
            with self.assertRaises(bot.Blocked): bot.initialize(c,bot.Store(Path(folder)/'x.db'),MIDNIGHT)
            self.assertEqual(c.writes,0)

    def test_signal_exit_and_cooldown(self):
        self.cycle([.02,0,0]); self.c.clock += 3600
        self.cycle([-.02,0,0]); self.assertEqual(self.c.writes,2)
        state = self.store.load()
        self.assertIsNone(bot.choose(state,'BTC/USD',MIDNIGHT//3600+2,{'momentum':'.02','close':'85000'}))

    def test_no_reinitialization_or_pending_intent_bypass(self):
        with self.assertRaises(bot.Blocked): bot.initialize(self.c,self.store,MIDNIGHT)
        self.store.reserve('a',{'example':'intent'})
        with self.assertRaises(bot.Blocked): self.store.reserve('b',{'example':'intent'})
        self.assertEqual(self.c.writes,0)

    def test_exclusive_process_lock(self):
        path = Path(self.tmp.name)/'process.lock'
        with bot.process_lock(path):
            with self.assertRaises(bot.Blocked):
                with bot.process_lock(path): pass


if __name__ == '__main__': unittest.main()
