"""Offline tests for universe eligibility and absence of future/prelisting leakage."""
import copy
from decimal import Decimal as D
import unittest
import numpy as np
from universe_research.eligibility import eligible,filtered_signals,target_pairs
from universe_research import compare
from competition_v31.strategy import sleeve_target


def signal():
    return dict(candles=1000,quote_volume_24h='10000000',close='1',daily_vol='.02',
        momentum_336h='.1',momentum_720h='.2',score='1')


class EligibilityTests(unittest.TestCase):
    def test_history_threshold(self):
        self.assertFalse(eligible(dict(signal(),candles=720))[0])
        self.assertTrue(eligible(dict(signal(),candles=721))[0])

    def test_volume_threshold(self):
        self.assertFalse(eligible(dict(signal(),quote_volume_24h='4999999.99'))[0])
        self.assertTrue(eligible(dict(signal(),quote_volume_24h='5000000'))[0])

    def test_nonfinite_and_missing_features_are_rejected(self):
        self.assertFalse(eligible(None)[0])
        for key in ('quote_volume_24h','close','daily_vol','momentum_720h','score'):
            for value in ('NaN','Infinity',None):
                self.assertFalse(eligible(dict(signal(),**{key:value}))[0],(key,value))

    def test_missing_new_asset_does_not_block_legacy_targets(self):
        sig={p:signal() for p in compare.BASE_PAIRS}
        pairs=target_pairs(sig,compare.BASE_PAIRS,compare.EXTRA_PAIRS)
        self.assertEqual(pairs,list(compare.BASE_PAIRS))
        cfg=compare.bot.raw_policy()['sleeves'][0]
        target=sleeve_target(cfg,sig,pairs,dict(initial_usd='100000',last_equity='100000'),D(1))
        self.assertEqual(len(target),4)

    def test_low_liquidity_held_asset_retains_exit_signal(self):
        low=dict(signal(),quote_volume_24h='1')
        sig={'BTC/USD':signal(),'BIO/USD':low,'EDEN/USD':low}
        filtered=filtered_signals(sig,compare.BASE_PAIRS,{'BIO/USD'})
        self.assertIn('BIO/USD',filtered)
        self.assertNotIn('EDEN/USD',filtered)
        self.assertNotIn('BIO/USD',target_pairs(filtered,compare.BASE_PAIRS,compare.EXTRA_PAIRS))


class FeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        n=1080;cls.index=1000;cls.stamps=np.arange(n)*3600
        cls.data={};cls.qv={};cls.valid={}
        for k,a in enumerate(compare.ASSETS):
            prices=(1+k)*np.exp(np.sin(np.arange(n)/11)*.01+np.arange(n)*.00001)
            cls.data[a]=dict(open=prices.copy(),high=prices*1.01,low=prices*.99,close=prices.copy(),volume=np.ones(n)*1e6)
            cls.qv[a]=np.ones(n)*1e6;cls.valid[a]=np.ones(n,dtype=bool)
        cls.valid['PUMP'][:400]=False
        cls.valid['EDEN'][950]=False
        cls.valid['FET'][:279]=False # exactly 721 closed candles at decision 1000

    def test_no_prelisting_or_gapped_history_admitted(self):
        prepared,_,_=compare.prepare(self.stamps,self.data,self.qv,self.valid,{self.index})
        sig=prepared[self.index]
        self.assertNotIn('PUMP/USD',sig);self.assertNotIn('EDEN/USD',sig)
        self.assertIn('FET/USD',sig)
        self.assertEqual(sig['FET/USD']['candles'],721)
        actual=self.data['FET']['close'][999]/self.data['FET']['close'][279]-1
        self.assertAlmostEqual(float(sig['FET/USD']['momentum_720h']),actual)

    def test_expanded_controller_respects_clock_and_reconciles(self):
        prepared,_,_=compare.prepare(self.stamps,self.data,self.qv,self.valid,{self.index})
        metadata={'TradePairs':{a+'/USD':{'AmountPrecision':8} for a in compare.ASSETS}}
        result=compare.run('expanded',[self.index],self.stamps,self.data,prepared,.0015,metadata)
        self.assertGreater(result['fills'],0)
        self.assertEqual(result['unresolved'],0)
        self.assertLess(result['maximum_entry_second'],900)
        self.assertLess(result['maximum_cycle_seconds'],3480)
        self.assertGreater(result['modeled_api_calls'],result['fills'])

    def test_tiny_price_features_match_production_after_listing(self):
        altered=copy.deepcopy(self.data);valid=copy.deepcopy(self.valid)
        asset='1000CHEEMS'
        for field in ('open','high','low','close'):altered[asset][field]*=1e-10
        valid[asset][:279]=False
        prepared,_,_=compare.prepare(self.stamps,altered,self.qv,valid,{self.index})
        rows=[(int(self.stamps[j]*1000),*[str(altered[asset][k][j]) for k in ('open','high','low','close','volume')],int(self.stamps[j]*1000)+3599999,str(self.qv[asset][j])) for j in range(279,self.index)]
        direct=compare.strategy.features(rows)
        for key in ('z24','daily_vol','score'):
            self.assertAlmostEqual(float(prepared[self.index][asset+'/USD'][key]),float(direct[key]),places=9)

    def test_future_price_changes_cannot_change_current_features(self):
        a,_,_=compare.prepare(self.stamps,self.data,self.qv,self.valid,{self.index})
        altered=copy.deepcopy(self.data)
        for asset in compare.ASSETS:
            for field in ('open','high','low','close'):altered[asset][field][self.index:]*=100
        b,_,_=compare.prepare(self.stamps,altered,self.qv,self.valid,{self.index})
        self.assertEqual(a,b)


if __name__=='__main__':unittest.main()
