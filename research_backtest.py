"""Fixed retrospective research candidates, January 2025–June 2026.

Keep backtest.py beside this script (its metrics function is reused).
Run: python research_backtest.py
No API access, orders, optimization, or automatic strategy selection.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from backtest import metrics

SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
STRATEGIES = ['cash', 'buy_hold', 'momentum', 'mean_reversion', 'slow_trend', 'trend_dip']
PERIODS = [
    ('2025_Q1_partial', '2025-01-15', '2025-04-01'),
    ('2025_Q2', '2025-04-01', '2025-07-01'),
    ('2025_Q3', '2025-07-01', '2025-10-01'),
    ('2025_Q4', '2025-10-01', '2026-01-01'),
    ('2026_Q1', '2026-01-01', '2026-04-01'),
    ('2026_Q2', '2026-04-01', '2026-07-01'),
    ('continuous', '2025-01-15', '2026-07-01'),
]
CONFIG = {
    'initial': 50000, 'fee_bps': 10, 'slippage_cases_bps': {'base': 5, 'stress': 15},
    'slow_trend': {'fast_sma_hours': 48, 'slow_sma_hours': 168, 'entry_ratio': 1.005,
                   'exit_ratio': 0.995, 'entry_check_hours': 6, 'cooldown_hours': 12},
    'trend_dip': {'z_hours': 48, 'entry_z': -2, 'exit_z': 0, 'trend_sma_hours': 168,
                  'sma_slope_hours': 24, 'entry_check_hours': 6, 'cooldown_hours': 12,
                  'max_hold_hours': 72, 'trend_exit_ratio': 0.98},
}


def prepare(frame):
    c = frame.close
    sma24, sma48, sma72, sma168 = [c.rolling(n, min_periods=n).mean() for n in (24,48,72,168)]
    z24 = (c-sma24)/c.rolling(24).std(ddof=0).replace(0,np.nan)
    z48 = (c-sma48)/c.rolling(48).std(ddof=0).replace(0,np.nan)
    signal = pd.DataFrame({
        'momentum': (sma24>sma72)&sma72.notna(), 'z24': z24, 'z48': z48,
        'slow_enter': (sma48>1.005*sma168)&sma168.notna(),
        'slow_exit': sma48<0.995*sma168,
        'dip_enter': (z48 < -2)&(c>sma168)&(sma168>sma168.shift(24)),
        'dip_exit': (z48>=0)|(c<0.98*sma168),
    },index=frame.index).shift(1)
    return signal


def load(folder):
    expected = pd.date_range('2025-01-01','2026-07-01',freq='h',inclusive='left',tz='UTC')
    frames, provenance = {}, {}
    for symbol in SYMBOLS:
        path = folder/f'{symbol}_1h_2025-01-01_2026-06-30.csv'
        frame = pd.read_csv(path)
        frame['open_time_utc'] = pd.to_datetime(frame.open_time_utc,utc=True)
        frame = frame.set_index('open_time_utc').sort_index()
        if not frame.index.equals(expected):
            raise ValueError(f'{symbol}: missing, duplicate, or unexpected hourly timestamps')
        cols=['open','high','low','close','volume']
        frame[cols]=frame[cols].apply(pd.to_numeric,errors='raise')
        if not np.isfinite(frame[cols].to_numpy()).all() or (frame[cols[:4]]<=0).any().any() or (frame.volume<0).any():
            raise ValueError(f'{symbol}: invalid prices or volume')
        if (frame.high<frame[['open','low','close']].max(axis=1)).any() or (frame.low>frame[['open','high','close']].min(axis=1)).any():
            raise ValueError(f'{symbol}: inconsistent OHLC')
        frames[symbol] = (frame,prepare(frame))
        provenance[symbol] = {'file': path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'rows':len(frame)}
    return frames, provenance


def simulate(frames,strategy,start,end,slip,initial=50000,fee=.001):
    start,end = pd.Timestamp(start,tz='UTC'),pd.Timestamp(end,tz='UTC')
    curves,orders=[],[]
    for symbol,(frame,signals) in frames.items():
        mask=(frame.index>=start)&(frame.index<end)
        sample=frame.loc[mask]; sig=signals.loc[mask]
        if sample.empty: raise ValueError('No data in requested period')
        opens,closes=sample.open.to_numpy(),sample.close.to_numpy()
        flags={col:sig[col].eq(True).to_numpy(dtype=bool) for col in ['momentum','slow_enter','slow_exit','dip_enter','dip_exit']}
        zs=sig.z24.to_numpy()
        cash,units=initial/len(frames),0.
        entered=-1; exited=-100000; values=[]
        for j,stamp in enumerate(sample.index):
            held=units>0; want=held
            if strategy=='cash': want=False
            elif strategy=='buy_hold': want=True
            elif strategy=='momentum': want=bool(flags['momentum'][j])
            elif strategy=='mean_reversion':
                if np.isfinite(zs[j]):
                    if not held and zs[j]<-1.5: want=True
                    elif held and zs[j]>=0: want=False
            elif strategy=='slow_trend':
                if held and flags['slow_exit'][j]: want=False
                elif not held and stamp.hour%6==0 and j-exited>=12 and flags['slow_enter'][j]: want=True
            elif strategy=='trend_dip':
                if held and (flags['dip_exit'][j] or j-entered>=72): want=False
                elif not held and stamp.hour%6==0 and j-exited>=12 and flags['dip_enter'][j]: want=True
            else: raise ValueError('Unknown strategy')
            if want and not held:
                price=opens[j]*(1+slip); units=cash/(price*(1+fee)); charge=units*price*fee; cash=0.; entered=j
                orders.append([stamp,symbol,'BUY',price,units,charge,'signal'])
            elif held and not want:
                price=opens[j]*(1-slip); charge=units*price*fee; cash+=units*price-charge
                orders.append([stamp,symbol,'SELL',price,units,charge,'signal']); units=0.; exited=j
            values.append(cash+units*closes[j])
        if units>0:
            price=closes[-1]*(1-slip);charge=units*price*fee
            values[-1]=cash+units*price-charge
            orders.append([end,symbol,'SELL',price,units,charge,'period_end'])
        curves.append(pd.Series(values,index=sample.index+pd.Timedelta(hours=1)-pd.Timedelta(microseconds=1)))
    curve=pd.concat(curves,axis=1).sum(axis=1)
    trades=pd.DataFrame(orders,columns=['time_utc','symbol','side','price','quantity','fee','reason'])
    return curve,trades


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,default=Path('data/history'))
    parser.add_argument('--output',type=Path,default=Path('data/research'))
    args=parser.parse_args()
    frames,provenance=load(args.data)
    args.output.mkdir(parents=True,exist_ok=True)
    summaries=[]; detail=[]; equity=[]
    for case,bps in CONFIG['slippage_cases_bps'].items():
        for period,start,end in PERIODS:
            print(f'{case}: {period}',flush=True)
            for strategy in STRATEGIES:
                curve,trades=simulate(frames,strategy,start,end,bps/10000)
                summaries.append({'cost_case':case,'period':period,'strategy':strategy,**metrics(curve,trades,50000)})
                if period=='continuous':
                    detail.append(trades.assign(cost_case=case,strategy=strategy))
                    equity.append(pd.DataFrame({'time_utc':curve.index,'equity':curve.values,'cost_case':case,'strategy':strategy}))
    summary=pd.DataFrame(summaries)
    summary[summary.period!='continuous'].to_csv(args.output/'quarterly_summary.csv',index=False)
    continuous=summary[summary.period=='continuous']
    continuous.to_csv(args.output/'continuous_summary.csv',index=False)
    pd.concat([x for x in detail if not x.empty],ignore_index=True).to_csv(args.output/'continuous_trades.csv',index=False)
    pd.concat(equity,ignore_index=True).to_csv(args.output/'continuous_equity.csv',index=False)
    (args.output/'run_config.json').write_text(json.dumps({'config':CONFIG,'source_files':provenance,'periods':PERIODS},indent=2),encoding='utf-8')
    (args.output/'README.txt').write_text('''RETROSPECTIVE RESEARCH, NOT A DEPLOYABLE BOT
These fixed candidates were proposed after reviewing July–September 2026 failures.
Earlier history is useful for robustness checks but is not a pristine prospective
holdout or an unbiased estimate of an adaptive walk-forward process. No parameter
search, model fitting, automatic selection, or official competition score occurs.
No trades or network calls occur. Only January 2025–June 2026 files are loaded.
The first 14 days provide indicator warm-up. Every decision uses prior closed bars.
Each coin starts with one third of $50,000 in an isolated sleeve; no rebalancing.
All strategies are long/cash. New candidates limit entry checks to 00/06/12/18 UTC
and wait 12 hours after exits before re-entry. Exits are checked every hour.
Slow trend: SMA48 above 1.005*SMA168 enters; below 0.995*SMA168 exits.
Trend dip: z48<-2, price>SMA168, and SMA168 rising over 24h enters; z48>=0,
price<0.98*SMA168, or 72h holding time exits. These are hypotheses, not proven fixes.
Orders execute at next hourly opens. Costs: 10 bps fee per side; 5 bps adverse
slippage base and 15 bps stress, per side. No guaranteed maker fills are assumed.
Quarterly runs independently reset to cash. Continuous runs carry capital and
positions across quarters, liquidating only at the final close. Do not compound
the independent quarter results and call them continuous strategy performance.
End liquidations incur both costs. Daily-return metrics use 365-day annualization,
zero risk-free/target rate, downside deviation over all days, hourly drawdowns.
Undefined ratios are NaN. Activity counts are not organizer-certified compliance.
No precision, minimum-size, latency, liquidity, partial-fill, or outage model yet.
USDT candles proxy Roostoo USD pairs. Three selected surviving coins limit coverage.
Slippage stress tests are assumptions. Do not choose a candidate solely by its
highest full-period return; inspect quarterly consistency, exposure, costs and risk.
After candidate selection, use fresh forward testing and testing-account execution.
''',encoding='utf-8')
    print('\n'+continuous[['cost_case','strategy','return_pct','max_drawdown_pct','sharpe','orders']].round(3).to_string(index=False))
    print(f'\nResearch backtest complete: {args.output.resolve()}',flush=True)


if __name__=='__main__':
    main()
