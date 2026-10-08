"""Strategy quality, portfolio caps and completed five-minute data validation."""
import copy,unittest
from decimal import Decimal as D
from unittest.mock import patch
from test_competition_v4a import Base,signal,fast
from competition_v4a import controller as bot,strategy,timing

class SelectionTests(Base):
    def picks(self,state=None,sig=None):
        info,ticks=bot.public_market(self.ex)
        return strategy.opportunity_candidates(state or self.store.load(),sig or self.sig,info,ticks,self.cfg,int(self.ex.clock)//3600)[0]

    def test_both_original_and_extra_assets_qualify(self):
        self.prepare('BTC/USD');self.prepare('PEPE/USD')
        self.assertEqual({p['pair'] for p in self.picks()},{'BTC/USD','PEPE/USD'})

    def test_breakdown_short_requires_volume_and_absolute_downtrend(self):
        self.prepare('ETH/USD',-1);self.assertEqual(self.picks()[0]['action'],'SHORT_OPEN')
        self.sig['ETH/USD']['volume_ratio']='1';self.assertFalse(self.picks())
        self.sig['ETH/USD']['volume_ratio']='2';self.sig['ETH/USD']['direction']=0;self.assertFalse(self.picks())

    def test_history_liquidity_regime_volatility_and_spread_filters(self):
        self.prepare('PEPE/USD');original=copy.deepcopy(self.sig['PEPE/USD'])
        for update in ({'candles':720},{'quote_volume_24h':'9999999'},{'daily_vol':'.11'},{'atr_pct':'0'}):
            self.sig['PEPE/USD']=dict(original,**update);self.assertFalse(self.picks())
        self.sig['PEPE/USD']=original;self.sig['BTC/USD']['momentum_24h']='-.04';self.assertFalse(self.picks())
        self.sig['BTC/USD']['momentum_24h']='.01'
        info,ticks=bot.public_market(self.ex);ticks['Data']['PEPE/USD']['MinAsk']=str(self.ex.prices['PEPE/USD']*D('1.01'))
        self.assertFalse(strategy.opportunity_candidates(self.store.load(),self.sig,info,ticks,self.cfg,int(self.ex.clock)//3600)[0])

    def test_every_inherited_book_counts_toward_position_limit(self):
        self.prepare('PEPE/USD');state=self.store.load()
        for a in ['CRV','FET','POL','FIL','ENA','APT','FLOKI','BIO']:self.prepare(a+'/USD')
        for pair,owner in zip([p for p in self.sig if p!='PEPE/USD'][:20],['core','xs','ts','core','opportunity']*4):
            state['positions'][pair]=dict(quantity='1',entry='100',direction=1,collateral='0',book=owner)
        self.assertFalse(self.picks(state))

    def test_correlation_filter_and_risk_sizing(self):
        self.buy();self.prepare('ETH/USD');self.sig['ETH/USD']['returns']=self.sig['BTC/USD']['returns'][:]
        self.assertFalse(self.picks())
        self.prepare('PEPE/USD');pick=next(p for p in self.picks() if p['pair']=='PEPE/USD')
        self.assertLessEqual(D(pick['budget'])*D(pick['stop_fraction']),D('300'))

    def test_atr_units_and_clamps(self):
        self.prepare();self.sig['BTC/USD']['atr_pct']='.0001';self.assertEqual(D(self.picks()[0]['stop_fraction']),D('.015'))
        self.sig['BTC/USD']['atr_pct']='.1';self.assertEqual(D(self.picks()[0]['stop_fraction']),D('.06'))

    def test_stagnant_exit_not_winning_position_timeout(self):
        pos=dict(direction=1,entry='100',opened_hour=1,book='ts')
        state={'positions':{'BTC/USD':pos}};s=signal(0,1)
        self.assertEqual(bot.choose(state,'BTC/USD',100,s),'SELL')
        s['close']='110';self.assertIsNone(bot.choose(state,'BTC/USD',100,s))

    def test_rotation_requires_cost_edge_and_fresh_confirmation(self):
        # A full portfolio cannot be sold just to manufacture room without confirmation.
        self.prepare('PEPE/USD');state=self.store.load()
        for a in ['CRV','FET','POL','FIL','ENA','APT','FLOKI','BIO']:self.prepare(a+'/USD')
        for pair in [p for p in self.sig if p!='PEPE/USD'][:20]:state['positions'][pair]=dict(direction=1,entry='100',quantity='1',collateral='0',book='core',opened_hour=0)
        self.store.save(state,'TEST',{})
        with patch.object(bot,'execute',side_effect=AssertionError('No confirmation, no close')):bot.run_opportunities(self.ex,self.store,self.sig,int(self.ex.clock)//3600,D(1))


class CandleTests(unittest.TestCase):
    def rows(self):
        return [[i*300000,'100','102','99','101','20',(i+1)*300000-1,'2000'] for i in range(40)]

    def test_open_candle_excluded(self):
        rows=self.rows();self.assertEqual(len(timing.validate(rows,39*300000)),39)

    def test_missing_duplicate_stale_and_nonfinite_rejected(self):
        rows=self.rows()
        for altered in (rows[:-1],rows[:10]+rows[11:],rows[:10]+[rows[9]]+rows[10:]):
            with self.assertRaises(ValueError):timing.validate(altered,40*300000)
        rows[3][4]='NaN'
        with self.assertRaises(ValueError):timing.validate(rows,40*300000)

    def test_confirm_long_short_and_stale(self):
        for d in (1,-1):
            s=signal(0,d);bar=fast(s,3600)
            self.assertTrue(timing.confirms(s,bar,3600000));self.assertFalse(timing.confirms(s,bar,3900000))
            bar['volume_ratio']='.5';self.assertFalse(timing.confirms(s,bar,3600000))

    def test_hourly_atr_uses_price_range_not_percent_twice(self):
        rows=[[i*3600000,'100','102','98','100','20',(i+1)*3600000-1,'2000'] for i in range(1000)]
        s=strategy.features(rows,True)
        self.assertEqual(D(s['atr_pct']),D('.04'));self.assertEqual(D(s['breakout_high']),D('102'))

if __name__=='__main__':unittest.main()
