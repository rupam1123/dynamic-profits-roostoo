import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import test_roundtrip as runner


class Exchange:
    key='testing-fixture'; secret='fixture'
    def __init__(self, failure=None):
        self.usd=Decimal('50000'); self.btc=Decimal('0')
        self.orders=[]; self.writes=0; self.failure=failure
    def balance(self):
        usd=self.usd
        if self.failure=='balance' and self.orders: usd+=Decimal('1')
        return {'Success':True,'SpotWallet':{'USD':{'Free':str(usd)},'BTC':{'Free':str(self.btc)}},'MarginWallet':{}}
    def short_positions(self): return {'Success':True,'Positions':[]}
    def request(self, endpoint, params=None, **kwargs):
        if endpoint=='/v3/exchangeInfo':
            return {'IsRunning':True,'TradePairs':{'BTC/USD':{'CanTrade':True,'AmountPrecision':5,'MiniOrder':1}}}
        if endpoint=='/v3/serverTime': return {'ServerTime':int(time.time()*1000)}
        if endpoint=='/v3/ticker':
            return {'Success':True,'ServerTime':int(time.time()*1000),'Data':{'BTC/USD':{'MaxBid':85000,'MinAsk':85001}}}
        if endpoint=='/v3/query_order':
            if 'pending_only' in params: return {'Success':False,'ErrMsg':'no order matched'}
            return {'Success':True,'OrderMatched':[x for x in self.orders if str(x['OrderID'])==params['order_id']]}
        assert endpoint=='/v3/place_order'
        self.writes+=1
        qty=Decimal(params['quantity']); sign=1 if params['side']=='BUY' else -1
        price=Decimal('85001') if sign==1 else Decimal('85000')
        fee=qty*price*Decimal('.001')
        self.usd-=sign*qty*price+fee; self.btc+=sign*qty
        order={'OrderID':len(self.orders)+1,'Pair':'BTC/USD','Side':params['side'],
               'Type':'MARKET','Status':'FILLED','Quantity':str(qty),'FilledQuantity':str(qty),
               'FilledAverPrice':str(price),'CommissionCoin':'USD','CommissionChargeValue':str(fee)}
        if self.failure=='partial': order['Status']='PARTIALLY_FILLED'
        self.orders.append(order)
        if self.failure=='timeout': raise TimeoutError('simulated response lost after fill')
        return {'Success':True,'OrderDetail':order}


class RoundtripChecks(unittest.TestCase):
    def test_roundtrip_and_repeat_blocked(self):
        with tempfile.TemporaryDirectory() as folder, patch('test_roundtrip.emit'):
            path=Path(folder)/'journal.db'; client=Exchange()
            runner.run(client,path)
            self.assertEqual(client.writes,2)
            self.assertEqual(client.btc,0)
            self.assertLess(client.usd,50000)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT status FROM run').fetchone()[0],'COMPLETE')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0],2)
            with self.assertRaises(ValueError): runner.run(client,path)
            self.assertEqual(client.writes,2)
    def test_ambiguous_fill_partial_and_balance_mismatch_halt(self):
        for failure in ('timeout','partial','balance'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder, patch('test_roundtrip.emit'):
                path=Path(folder)/'journal.db'; client=Exchange(failure)
                with self.assertRaises(ValueError): runner.run(client,path)
                self.assertEqual(client.writes,1)
                with self.assertRaises(ValueError): runner.run(client,path)
                self.assertEqual(client.writes,1)
    def test_existing_btc_blocks_before_order(self):
        with tempfile.TemporaryDirectory() as folder, patch('test_roundtrip.emit'):
            client=Exchange(); client.btc=Decimal('.1')
            with self.assertRaises(ValueError): runner.run(client,Path(folder)/'journal.db')
            self.assertEqual(client.writes,0)


if __name__=='__main__': unittest.main()
