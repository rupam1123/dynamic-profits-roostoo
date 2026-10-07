"""Standard-library tests for the public data collector; no network or orders."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from universe_research import audit


def rows(count=10):
    return [[i*3600000,'1','2','.5','1.5','10',i*3600000+3599999,'15'] for i in range(count)]


class CandleTests(unittest.TestCase):
    def test_new_listing_data_can_be_collected_before_strategy_warmup(self):
        self.assertEqual(len(audit.validate_candles(rows(),10*3600000)),10)

    def test_open_candle_is_not_used(self):
        self.assertEqual(len(audit.validate_candles(rows(11),10*3600000)),10)

    def test_gap_and_duplicate_are_rejected(self):
        r=rows()
        for damaged in (r[:3]+r[4:],r[:3]+[r[2]]+r[3:]):
            with self.assertRaises(ValueError):audit.validate_candles(damaged,10*3600000)

    def test_stale_tail_is_rejected(self):
        with self.assertRaises(ValueError):audit.validate_candles(rows(),11*3600000)

    def test_bad_prices_and_nonfinite_volume_are_rejected(self):
        for column,value in ((1,'0'),(2,'.1'),(4,'NaN'),(5,'-1'),(7,'Infinity')):
            r=rows();r[0][column]=value
            with self.assertRaises(ValueError):audit.validate_candles(r,10*3600000)

    def test_scan_only_calls_public_get_endpoints_and_preserves_short_history(self):
        calls=[]
        rule=dict(Coin='NEW',Unit='USD',CanTrade=True,AmountPrecision=2,PricePrecision=2,MiniOrder=1,AssetType='crypto')
        def get(url):
            calls.append(url)
            if url.endswith('mock-api.roostoo.com/v3/exchangeInfo'):return dict(IsRunning=True,TradePairs={'NEW/USD':rule})
            if url.endswith('/api/v3/exchangeInfo'):return dict(symbols=[dict(symbol='NEWUSDT',baseAsset='NEW',quoteAsset='USDT',status='TRADING')])
            if url.endswith('/api/v3/time'):return dict(serverTime=10*3600000)
            if '/api/v3/klines?' in url:return rows()
            if url.endswith('/v3/serverTime'):return dict(ServerTime=10*3600000)
            if '/v3/ticker?' in url:return dict(Success=True,ServerTime=10*3600000,Data={'NEW/USD':dict(MaxBid='1',MinAsk='1.001')})
            raise AssertionError('Unexpected endpoint: '+url)
        with tempfile.TemporaryDirectory() as folder,patch.object(audit,'get',side_effect=get),patch.object(audit.time,'sleep'), \
                patch.object(audit.time,'time',return_value=36000),patch('builtins.print'):
            output=Path(folder)/'latest.json';db=Path(folder)/'candles.sqlite3'
            audit.run(output,db)
            import json,sqlite3
            result=json.loads(output.read_text())
            self.assertFalse(result['assets']['NEW']['eligible'])
            self.assertEqual(result['assets']['NEW']['closed_hours'],10)
            self.assertFalse(result['assets']['NEW']['evaluated_universe'])
            from contextlib import closing
            with closing(sqlite3.connect(db)) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM candles').fetchone()[0],10)
        from urllib.parse import urlsplit
        allowed={'mock-api.roostoo.com':{'/v3/exchangeInfo','/v3/serverTime','/v3/ticker'},
            'data-api.binance.vision':{'/api/v3/exchangeInfo','/api/v3/time','/api/v3/klines'}}
        self.assertTrue(all(urlsplit(u).path in allowed[urlsplit(u).hostname] for u in calls))


class ActivityReportTests(unittest.TestCase):
    def test_activity_report_does_not_modify_journal(self):
        import json,sqlite3
        from contextlib import closing
        from datetime import datetime,timezone
        from universe_research.activity_report import report
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'execution.sqlite3'
            with closing(sqlite3.connect(path)) as db,db:
                db.execute('CREATE TABLE state(id INTEGER, body TEXT)')
                db.execute('CREATE TABLE attempts(id TEXT, status TEXT)')
                db.execute('CREATE TABLE events(id INTEGER, utc TEXT, kind TEXT, body TEXT)')
                db.execute('INSERT INTO state VALUES (1,?)',(json.dumps({'positions':{'BTC/USD':{}},'stop_reason':None}),))
                db.execute('INSERT INTO events VALUES (1,?,?,?)',(datetime.now(timezone.utc).isoformat(),'SELECTION',json.dumps({'skipped':{'ETH/USD':'NO_ALIGNED_TREND'}})))
            before=path.read_bytes();result=report(path,7)
            self.assertEqual(result['selection_or_deferral_reasons'],{'NO_ALIGNED_TREND':1})
            self.assertEqual(result['open_positions'],1)
            self.assertEqual(path.read_bytes(),before)


if __name__=='__main__':unittest.main()
