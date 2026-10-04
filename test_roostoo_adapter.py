import tempfile
import sqlite3
from unittest.mock import patch
import unittest
from pathlib import Path
from decimal import Decimal
from roostoo_adapter import spot_plan, signature, TestOrderGate


class Tests(unittest.TestCase):
    def test_precision_budget_minimum(self):
        for pair, places in [('BTC/USD',5),('ETH/USD',4),('SOL/USD',3)]:
            info={'IsRunning':True,'TradePairs':{pair:{'CanTrade':True,'AmountPrecision':places,'MiniOrder':1}}}
            plan=spot_plan(info,{'MaxBid':99,'MinAsk':100},pair,'BUY','10')
            qty=Decimal(plan['params']['quantity'])
            self.assertEqual(-qty.as_tuple().exponent, places)
            self.assertLessEqual(qty*Decimal('100')*Decimal('1.01')*Decimal('1.001'),10)
            with self.assertRaises(ValueError):
                spot_plan(info,{'MaxBid':99,'MinAsk':100},pair,'BUY','1')

    def test_signature_order(self):
        self.assertEqual(signature('test',{'b':'2','a':'1'}), signature('test',{'a':'1','b':'2'}))

    def test_connections_closed_on_success_and_error(self):
        original = sqlite3.connect
        connections = []
        def tracked(*args, **kwargs):
            db = original(*args, **kwargs)
            connections.append(db)
            return db
        with patch('roostoo_adapter.sqlite3.connect', side_effect=tracked):
            self.test_timeout_persists_and_blocks()
        self.assertGreaterEqual(len(connections), 6)
        for db in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')

    def test_timeout_persists_and_blocks(self):
        class Fake:
            key='fake'; secret='fake'; calls=0
            def request(self,*args,**kwargs):
                self.calls+=1
                raise TimeoutError()
        with tempfile.TemporaryDirectory() as folder:
            client=Fake(); path=Path(folder)/'test.db'
            plan={'endpoint':'/v3/place_order','params':{'pair':'BTC/USD','type':'MARKET','side':'BUY','quantity':'0.001'}}
            gate=TestOrderGate(client,path)
            with self.assertRaises(ValueError): gate.submit('first',plan)
            self.assertEqual(client.calls,0)
            gate=TestOrderGate(client,path,allow_test_orders=True)
            self.assertEqual(gate.submit('first',plan)['status'],'UNKNOWN')
            gate=TestOrderGate(client,path,allow_test_orders=True)
            for intent in ('first','second'):
                with self.assertRaises(ValueError): gate.submit(intent,plan)
            self.assertEqual(client.calls,1)


if __name__=='__main__': unittest.main()
