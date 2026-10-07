"""Pure completed-candle features and bounded portfolio selection. No network or orders."""
from decimal import Decimal
import math
import statistics

D = Decimal

def features(rows):
    # Each row: open time, open, high, low, close, volume, close time, quote volume.
    if len(rows) < 169:
        raise ValueError('169 completed candles required')
    prices = [float(r[4]) for r in rows[-169:]]
    if not all(math.isfinite(x) and x>0 for x in prices): raise ValueError('Invalid feature prices')
    returns = [math.log(b/a) for a,b in zip(prices, prices[1:])]
    vol = statistics.pstdev(returns[-72:]) * math.sqrt(24)
    m24, m72, m168 = [prices[-1]/prices[-1-n]-1 for n in (24,72,168)]
    direction = 1 if m168 > .01 and m24 > 0 and m72 > 0 else -1 if m168 < -.01 and m24 < 0 and m72 < 0 else 0
    score = abs(.2*m24 + .3*m72 + .5*m168) / max(vol,.005)
    return dict(close=str(rows[-1][4]), momentum=str(m168), momentum_24h=str(m24),
                momentum_72h=str(m72), daily_vol=str(vol), direction=direction,
                score=str(score), quote_volume_24h=str(sum(D(r[7]) for r in rows[-24:])),
                returns=returns[-72:])

def correlation(left, right):
    if len(left) != len(right) or len(left) < 24:
        return 1.0  # Insufficient overlap cannot certify diversification.
    a,b = statistics.mean(left),statistics.mean(right)
    numerator=sum((x-a)*(y-b) for x,y in zip(left,right))
    denominator=math.sqrt(sum((x-a)**2 for x in left)*sum((y-b)**2 for y in right))
    return numerator/denominator if denominator else 1.0

def gross_exposure(state, ticks):
    # Mark gross risk at ask; for shorts also reserve at least posted collateral.
    total=D('0')
    for pair,pos in state['positions'].items():
        marked=D(pos['quantity'])*D(ticks['Data'][pair]['MinAsk'])
        total += max(marked,D(pos['collateral'])) if pos['direction']==-1 else marked
    return total

def candidates(state, signals, info, ticks, cfg, hour):
    selected=[]; reasons={}; occupied=dict(state['positions'])
    if state.get('stop_reason'):
        return [], {'portfolio':'STOP_LATCHED'}
    equity=min(D(state['initial_usd']),D(state['last_equity']))
    cap=max(D('0'),equity*D(cfg['gross_fraction']))
    room=max(D('0'),cap-gross_exposure(state,ticks))
    cash=max(D('0'),D(state['usd'])-D('1'))
    ranked=sorted(signals, key=lambda p:(-D(signals[p]['score']),p))
    for pair in ranked:
        sig=signals[pair]; reason=None
        if pair in occupied: continue
        if not sig['direction']: reason='NO_ALIGNED_TREND'
        elif hour-int(state['last_exit'].get(pair,-10000000))<12: reason='COOLDOWN'
        elif len(occupied)>=cfg['max_positions']: reason='POSITION_LIMIT'
        elif D(sig['quote_volume_24h'])<D(cfg['min_quote_volume_24h']): reason='LOW_VOLUME'
        elif info.get('TradePairs',{}).get(pair,{}).get('CanTrade') is not True: reason='UNAVAILABLE'
        else:
            quote=ticks.get('Data',{}).get(pair,{})
            try:
                bid=D(str(quote.get('MaxBid',0))); ask=D(str(quote.get('MinAsk',0)))
            except Exception:
                reasons[pair]='BAD_QUOTE'; continue
            if not bid.is_finite() or not ask.is_finite() or bid<=0 or ask<bid: reason='BAD_QUOTE'
            elif ask/bid-1>D(cfg['max_spread']): reason='WIDE_SPREAD'
            else:
                for held,pos in occupied.items():
                    if held not in signals:
                        reason='HELD_HISTORY_UNAVAILABLE'; break
                    corr=correlation(sig['returns'],signals[held]['returns'])*sig['direction']*pos['direction']
                    if corr>float(cfg['max_correlation']): reason='CORRELATED_EXPOSURE'; break
        if reason:
            reasons[pair]=reason; continue
        vol=max(D(sig['daily_vol']),D('.005'))
        budget=min(equity*D(cfg['position_fraction'])*min(D(1),D(cfg['daily_vol_target'])/vol),
                   room/D('1.02'),cash/D('1.02'))
        budget=budget.quantize(D('.01'),rounding='ROUND_DOWN')
        if budget<D('25'):
            reasons[pair]='EXPOSURE_OR_CASH_LIMIT'; continue
        selected.append(dict(pair=pair,action='BUY' if sig['direction']==1 else 'SHORT_OPEN',budget=str(budget),score=sig['score']))
        occupied[pair]={'direction':sig['direction']}
        room-=budget*D('1.02'); cash-=budget*D('1.02')
    return selected,reasons
