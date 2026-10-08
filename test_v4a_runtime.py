"""Coordinator, dynamic mapping and real-adapter admission checks. No real orders."""
import copy,json,threading,unittest
from decimal import Decimal as D
from unittest.mock import patch
from test_competition_v4a import Base,signal,fast as five
from competition_v4a import controller as bot,universe,fast,api
from competition_v4a.market import Market
from competition_v4a.runtime import Runner


def metadata(pairs):
    return {'IsRunning':True,'TradePairs':{p:dict(AssetType='crypto',CanTrade=True) for p in pairs}}


def binance(pairs):
    return {'symbols':[dict(symbol=p.split('/')[0]+'USDT',baseAsset=p.split('/')[0],quoteAsset='USDT',status='TRADING',isSpotTradingAllowed=True) for p in pairs]}


class MappingTests(unittest.TestCase):
    def test_new_asset_discovered_without_source_change(self):
        pairs=['BTC/USD','NEWCOIN/USD'];found,skip=universe.discover(metadata(pairs),binance(pairs))
        self.assertIn('NEWCOIN/USD',found);self.assertFalse(skip)

    def test_stock_delisted_mismatch_rejected(self):
        pairs=['BTC/USD','A/USD','B/USD','C/USD'];info=metadata(pairs);meta=binance(pairs)
        info['TradePairs']['A/USD']['AssetType']='stock';info['TradePairs']['B/USD']['CanTrade']=False
        meta['symbols'][3]['baseAsset']='NOT_C'
        found,skip=universe.discover(info,meta);self.assertEqual(set(found),{'BTC/USD'});self.assertEqual(len(skip),3)


class RuntimeTests(Base):
    def setup_runner(self):
        runner=Runner(self.ex,self.store);info,ticks=bot.public_market(self.ex)
        runner.market.metadata(info);runner.market.mapping({p:p.split('/')[0]+'USDT' for p in self.sig},{})
        runner.market.publish(ticks,self.store.load())
        s={p:dict(v,timing=five(v,self.ex.clock)) for p,v in self.sig.items()}
        runner.market.publish_signals(int(self.ex.clock)//3600,int(self.ex.clock)//300,s,{},int(self.ex.clock)//60)
        market_patch=patch.object(bot,'MARKET',runner.market)
        market_patch.start();self.addCleanup(market_patch.stop)
        return runner

    def tick(self,runner):
        ticks=self.ex.request('/v3/ticker');runner.market.publish(ticks,self.store.load())

    def test_idle_timer_never_manufactures_order(self):
        runner=self.setup_runner()
        for _ in range(5):self.ex.clock+=10;self.tick(runner);runner.step()
        self.assertEqual(self.ex.writes,0)

    def test_valid_entry_through_single_writer(self):
        self.prepare();runner=self.setup_runner();runner.step()
        self.assertIn('BTC/USD',self.store.load()['positions']);count=self.ex.writes
        runner.step();self.assertEqual(count,self.ex.writes)
        bot.match_account(self.store.load(),bot.account(self.ex))

    def test_latched_stop_precedes_new_entry_even_after_rebound(self):
        self.buy();self.prepare('ETH/USD');runner=self.setup_runner()
        self.ex.prices['BTC/USD']=D('96');self.tick(runner)
        self.ex.prices['BTC/USD']=D('100');self.tick(runner)
        self.assertTrue(runner.market.has_risk());runner.step()
        self.assertNotIn('BTC/USD',self.store.load()['positions']);self.assertNotIn('ETH/USD',self.store.load()['positions'])

    def test_quote_peak_persisted_even_if_executor_was_busy(self):
        self.buy();runner=self.setup_runner();self.ex.prices['BTC/USD']=D('110');self.tick(runner)
        self.ex.prices['BTC/USD']=D('109');self.tick(runner)
        state,_=bot.risk_mark(self.ex,self.store)
        self.assertEqual(D(state['positions']['BTC/USD']['extreme_price']),D('110'))

    def test_old_position_risk_job_never_closes_replacement(self):
        self.buy();runner=self.setup_runner()
        runner.market.urgent[('BTC/USD','old')]=dict(pair='BTC/USD',position_id='old',reason='QUOTE_STOP')
        before=self.ex.writes;runner.step();self.assertEqual(before,self.ex.writes);self.assertFalse(runner.market.has_risk())

    def test_stale_quotes_block_new_entry(self):
        self.prepare();runner=self.setup_runner();self.ex.clock+=26
        with self.assertRaises(ValueError):runner.step()
        self.assertEqual(self.ex.writes,0)

    def test_stale_catalog_prevents_entries_but_preserves_held_management(self):
        self.prepare();runner=self.setup_runner();runner.market.catalog_at=self.ex.clock-1000
        runner.step();self.assertEqual(self.ex.writes,0)

    def test_drawdown_observer_enqueues_portfolio_exit(self):
        runner=self.setup_runner();state=self.store.load();state['risk_peak']='110000'
        runner.market.publish(self.ex.request('/v3/ticker'),state)
        key,risk=runner.market.next_risk();self.assertEqual(key[0],'portfolio');self.assertEqual(risk['reason'],'HARD_DRAWDOWN')

    def test_rejection_counts_survive_restart_and_deduplicate_same_minute(self):
        runner=self.setup_runner();runner.reject('POSITION_LIMIT','BTC/USD');runner.reject('POSITION_LIMIT','BTC/USD')
        self.assertEqual(self.store.load()['rejection_counts']['POSITION_LIMIT'],1)
        runner=Runner(self.ex,bot.Store(self.store.path));self.assertEqual(runner.store.load()['rejection_counts']['POSITION_LIMIT'],1)

    def test_dynamic_symbol_can_trade_and_remains_known_after_removal(self):
        self.prepare('NEWCOIN/USD');runner=self.setup_runner();runner.step()
        self.assertIn('NEWCOIN/USD',self.store.load()['positions'])
        runner.market.mapping({'BTC/USD':'BTCUSDT'},{});runner.step()
        self.assertIn('NEWCOIN/USD',self.store.load()['known_pairs'])

    def test_fast_breakout_independent_of_five_minute_confirmation(self):
        self.prepare();s=self.sig['BTC/USD'];s.update(breakout_high='110',breakout_low='90',volume_ratio='.5')
        s['fast']=dict(boundary=int(self.ex.clock)//60*60000,close='100',previous='99',mean='99.5',volume_ratio='3',high='99.8',low='98')
        runner=self.setup_runner()
        with runner.market.lock:runner.market.signals['BTC/USD']['timing']['volume_ratio']='.1'
        runner.step();self.assertEqual(self.store.load()['positions']['BTC/USD']['setup'],'ONE_MINUTE_BREAKOUT')

    def test_minute_stale_cannot_create_fast_breakout(self):
        self.prepare();s=self.sig['BTC/USD'];s.update(breakout_high='110',breakout_low='90',volume_ratio='.5')
        s['fast']=dict(boundary=(int(self.ex.clock)//60-1)*60000,close='100',previous='99',mean='99.5',volume_ratio='3',high='99.8',low='98')
        runner=self.setup_runner();runner.step();self.assertEqual(self.ex.writes,0)

    def test_catalog_worker_evaluates_every_eligible_asset(self):
        runner=Runner(self.ex,self.store);pairs=list(self.sig)
        runner.market.metadata(metadata(pairs));captured=[]
        def five_refresh(requested):captured.extend(requested);return int(self.ex.clock)//300*300000,{},{}
        original=runner.market.publish_signals
        def publish(*args):original(*args);runner.stop.set()
        with patch('competition_v4a.runtime.feed.get',return_value=binance(pairs)),patch('competition_v4a.runtime.load_context',return_value=(int(self.ex.clock)//3600,self.sig,{})),patch('competition_v4a.runtime.timing.refresh',side_effect=five_refresh),patch.object(runner.market,'publish_signals',side_effect=publish):runner.data_worker()
        self.assertEqual(set(captured),set(pairs))

    def test_price_observer_continues_while_executor_is_blocked(self):
        self.buy();runner=self.setup_runner();began=threading.Event();release=threading.Event()
        def busy():began.set();release.wait(2)
        worker=threading.Thread(target=busy);worker.start();began.wait(1)
        self.ex.clock+=10;self.ex.prices['BTC/USD']=D('95');self.tick(runner)
        self.assertTrue(runner.market.has_risk());self.assertTrue(worker.is_alive());release.set();worker.join()


class TransmissionTests(unittest.TestCase):
    def test_pending_risk_cancels_entry_before_network(self):
        client=api.Client('fake','fake');client.clock_checked=api.time.monotonic();client.entry_guard=lambda pair:False
        with patch.object(client,'throttle'),patch('urllib.request.build_opener') as network,self.assertRaises(api.NotSent):client.request('/v3/place_order',{'pair':'BTC/USD','side':'BUY'},method='POST',signed=True)
        network.assert_not_called()

    def test_optional_fast_rejects_incomplete_and_nan_candles(self):
        rows=[[i*60000,'100','102','99','101','2',(i+1)*60000-1,'200'] for i in range(25)]
        self.assertEqual(fast.feature(rows,24*60000)['boundary'],24*60000)
        rows[5][4]='NaN'
        with self.assertRaises(ValueError):fast.feature(rows,25*60000)


class RateTests(unittest.TestCase):
    def test_public_and_private_share_twenty_per_minute_budget(self):
        import tempfile
        from pathlib import Path
        clock=[1000.];sent=[]
        with tempfile.TemporaryDirectory() as folder:
            clients=[api.Client(),api.Client('account','secret')]
            for client in clients:client.rate_path=Path(folder)/'rate.sqlite3'
            def sleep(delay):clock[0]+=delay
            with patch.object(api.time,'time',side_effect=lambda:clock[0]),patch.object(api.time,'sleep',side_effect=sleep):
                for i in range(50):clients[i%2].throttle();sent.append(clock[0])
            self.assertTrue(all(b-a>=3.0999 for a,b in zip(sent,sent[1:])))
            self.assertTrue(all(sum(t<=x<t+60 for x in sent)<=20 for t in sent))

class V32MigrationTests(unittest.TestCase):
    def test_delivered_v32_journal_migrates_without_orders(self):
        import tempfile
        from pathlib import Path
        from v3_tests.exchange import Exchange
        from competition_v32 import controller as old
        from competition_v4a import migrate
        ex=Exchange()
        with tempfile.TemporaryDirectory() as folder,patch.object(bot.time,'time',side_effect=lambda:ex.clock),patch.object(old,'emit'),patch.object(bot,'emit'):
            store=old.Store(Path(folder)/'execution.sqlite3');old.initialize(ex,store)
            before=store.load();backup=migrate.migrate(ex,bot.Store(store.path),Path(folder)/'backups')
            after=store.load();self.assertEqual(after['version'],bot.VERSION);self.assertEqual(ex.writes,0);self.assertTrue(backup.exists())
            for k in ('positions','usd','risk_peak','expires','last_exit','last_hour'):self.assertEqual(before[k],after[k])
            with self.assertRaises(old.Blocked):old.identity(ex,after)

if __name__=='__main__':unittest.main()
