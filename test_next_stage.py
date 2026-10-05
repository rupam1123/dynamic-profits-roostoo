from contextlib import closing
from decimal import Decimal,ROUND_DOWN
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import candle_feed as feed
import candle_paper as paper
import short_roundtrip as short
from test_roundtrip_checks import Exchange


class ShortExchange(Exchange):
    def __init__(self,failure=None):
        super().__init__(failure); self.usd=Decimal('49999.97'); self.pos=[]
    def short_positions(self): return {'Success':True,'Positions':self.pos.copy()}
    def request(self,endpoint,params=None,**kwargs):
        if endpoint not in ('/v6/short_open','/v6/short_close'): return super().request(endpoint,params,**kwargs)
        self.writes+=1
        if endpoint.endswith('short_open'):
            if self.failure=='permission': return {'Success':False,'ErrMsg':'your do not have permission to trade'}
            self.qty=(Decimal('10')/Decimal('85000')).quantize(Decimal('.00001'),rounding=ROUND_DOWN)
            self.fee=self.qty*85000*Decimal('.001'); self.usd-=10+self.fee
            p={'ID':1,'Pair':'BTC/USD','EntryPrice':85000,'ShortQty':str(self.qty),'Collateral':10}
            self.pos=[p]
            if self.failure=='timeout': raise TimeoutError()
            return dict(p,Success=True,Status='OPEN',OpenFee=str(self.fee))
        pnl=-self.qty; fee=self.qty*85001*Decimal('.001'); returned=10+pnl-fee
        self.usd+=returned; self.pos=[]
        return {'Success':True,'FullyClosed':True,'ClosedQty':str(self.qty),'ClosePrice':85001,
                'CloseFee':str(fee),'RealizedPNL':str(pnl),'ReturnAmount':str(returned)}


class Checks(unittest.TestCase):
    def bars(self):
        return [[h*feed.HOUR,'100','102','99','101','2',(h+1)*feed.HOUR-1] for h in range(200)]
    def test_closed_candles_and_causal_signal(self):
        rows=self.bars(); boundary=200*feed.HOUR
        self.assertEqual(len(feed.validate(rows,boundary)),200)
        future=[boundary,'1','999','1','999','1',boundary+feed.HOUR-1]
        self.assertEqual(feed.validate(rows+[future],boundary),feed.validate(rows,boundary))
        for bad in (rows[:-1],rows[:50]+rows[51:],rows[:50]+[rows[49]]+rows[50:]):
            with self.assertRaises(ValueError): feed.validate(bad,boundary)
    def test_short_roundtrip_and_repetition(self):
        with tempfile.TemporaryDirectory() as d,patch('short_roundtrip.emit'):
            path=Path(d)/'short.db'; c=ShortExchange()
            short.run(c,path)
            self.assertEqual(c.writes,2); self.assertEqual(c.pos,[])
            self.assertLess(c.usd,Decimal('49999.97'))
            with self.assertRaises(ValueError): short.run(c,path)
            self.assertEqual(c.writes,2)
    def test_short_rejection_and_ambiguous_open_never_retry(self):
        for failure in ('permission','timeout'):
            with tempfile.TemporaryDirectory() as d,patch('short_roundtrip.emit'):
                path=Path(d)/'short.db'; c=ShortExchange(failure)
                with self.assertRaises(ValueError): short.run(c,path)
                self.assertEqual(c.writes,1)
                with self.assertRaises(ValueError): short.run(c,path)
                self.assertEqual(c.writes,1)
    def test_candle_paper_bootstrap_and_restart(self):
        boundary=20700*24*feed.HOUR; now=boundary+120000
        class Public:
            def request(self,endpoint,params=None):
                if endpoint=='/v3/exchangeInfo': return {'IsRunning':True,'TradePairs':{p:{'CanTrade':True} for p in ('BTC/USD','ETH/USD','SOL/USD')}}
                if endpoint=='/v3/serverTime': return {'ServerTime':now}
                return {'Success':True,'ServerTime':now,'Data':{p:{'MaxBid':100,'MinAsk':100} for p in ('BTC/USD','ETH/USD','SOL/USD')}}
        with tempfile.TemporaryDirectory() as d:
            candles=Path(d)/'candles.db'; state=Path(d)/'state.db'
            with closing(sqlite3.connect(candles)) as db,db:
                db.execute('CREATE TABLE candles(symbol TEXT,open_time INTEGER,close TEXT)')
                for symbol in feed.SYMBOLS:
                    db.executemany('INSERT INTO candles VALUES (?,?,?)',[(symbol,boundary-h*feed.HOUR,'98' if h==169 else '100') for h in range(1,170)])
            with patch.object(paper,'CANDLES',candles),patch.object(paper,'STATE',state),patch.object(paper,'refresh',return_value=boundary),patch.object(paper,'Client',Public),patch('candle_paper.time.time',return_value=now/1000):
                result=paper.step()
                self.assertTrue(all(x['direction']==1 for x in result['pairs'].values()))
                self.assertEqual(paper.step()['status'],'ALREADY_PROCESSED_HOUR')
            with closing(sqlite3.connect(state)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM trades').fetchone()[0],3)


if __name__=='__main__': unittest.main()
