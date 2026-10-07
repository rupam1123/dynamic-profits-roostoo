"""Pure completed-candle features, core selection, sleeve targets and risk rules. No network or orders.

V3 = V2 trend core + entry filters + drawdown ladder + daily activity guard + two small sleeves:
  xs: cross-sectional 14-day momentum (long strongest k, short weakest k), rebalanced every 72h
  ts: time-series 30-day trend on every coin, inverse-volatility weights, rebalanced every 168h
Each coin has at most one owner (core, xs or ts). Mirrors research/v21_lab/sim.py.
"""
from decimal import Decimal, ROUND_DOWN
import math
import statistics

D = Decimal
HOUR_S = 3600


def _pvol(prices):
    """Daily volatility from hourly closes: population stdev of log returns * sqrt(24)."""
    returns = [math.log(b / a) for a, b in zip(prices, prices[1:])]
    return statistics.pstdev(returns) * math.sqrt(24)


def features(rows, regime=False):
    # Each row: open time, open, high, low, close, volume, close time, quote volume.
    if len(rows) < 169:
        raise ValueError('169 completed candles required')
    prices = [float(r[4]) for r in rows]
    if not all(math.isfinite(x) and x > 0 for x in prices): raise ValueError('Invalid feature prices')
    p169 = prices[-169:]
    returns = [math.log(b / a) for a, b in zip(p169, p169[1:])]
    vol = statistics.pstdev(returns[-72:]) * math.sqrt(24)
    m24, m72, m168 = [prices[-1] / prices[-1 - n] - 1 for n in (24, 72, 168)]
    direction = 1 if m168 > .01 and m24 > 0 and m72 > 0 else -1 if m168 < -.01 and m24 < 0 and m72 < 0 else 0
    score = abs(.2 * m24 + .3 * m72 + .5 * m168) / max(vol, .005)
    last24 = prices[-24:]
    sd24 = statistics.pstdev(last24)
    z24 = (prices[-1] - statistics.fmean(last24)) / sd24 if sd24 > 0 else None
    out = dict(close=str(rows[-1][4]), momentum=str(m168), momentum_24h=str(m24),
               momentum_72h=str(m72), daily_vol=str(vol), direction=direction,
               score=str(score), quote_volume_24h=str(sum(D(r[7]) for r in rows[-24:])),
               returns=returns[-72:], z24=None if z24 is None else str(z24),
               r6=str(prices[-1] / prices[-7] - 1), candles=len(rows))
    for n in (336, 720):
        out['momentum_%dh' % n] = str(prices[-1] / prices[-1 - n] - 1) if len(prices) > n else None
    if regime:
        # Median of daily vol sampled every 6h over the prior 720h (decision j uses closes before j).
        n = len(prices)
        if n >= 720 + 73:
            samples = [_pvol(prices[k - 73:k]) for k in range(n - 720, n, 6)]
            out['vol_median_720h'] = str(statistics.median(samples))
        else:
            out['vol_median_720h'] = None
    tr = [max(float(r[2])-float(r[3]), abs(float(r[2])-prices[i-1]), abs(float(r[3])-prices[i-1])) for i,r in enumerate(rows) if i >= len(rows)-14]
    out['atr_abs'] = str(statistics.fmean(tr))
    out['atr_pct'] = str(statistics.fmean(tr)/prices[-1])
    return out


def risk_off(btc, cfg):
    """BTC regime: no new alt longs after a sharp BTC drop or a volatility spike."""
    if btc is None: return True
    f = cfg['filters']
    if D(btc['momentum_24h']) < D(f['btc_m24_off']): return True
    med = btc.get('vol_median_720h')
    return med is not None and D(btc['daily_vol']) > D(f['btc_vol_ratio']) * D(med)


def correlation(left, right):
    if len(left) != len(right) or len(left) < 24:
        return 1.0  # Insufficient overlap cannot certify diversification.
    a, b = statistics.mean(left), statistics.mean(right)
    numerator = sum((x - a) * (y - b) for x, y in zip(left, right))
    denominator = math.sqrt(sum((x - a) ** 2 for x in left) * sum((y - b) ** 2 for y in right))
    return numerator / denominator if denominator else 1.0


def book(pos):
    return pos.get('book', 'core')


def gross_exposure(state, ticks, owner=None):
    # Mark gross risk at ask; for shorts also reserve at least posted collateral. Per book.
    total = D('0')
    for pair, pos in state['positions'].items():
        if owner is not None and book(pos) != owner: continue
        marked = D(pos['quantity']) * D(ticks['Data'][pair]['MinAsk'])
        total += max(marked, D(pos['collateral'])) if pos['direction'] == -1 else marked
    return total


def risk_multiplier(state, cfg, hour):
    lad = cfg['ladder']
    equity, peak, initial = D(state['last_equity']), D(state.get('risk_peak', state['peak_equity'])), D(state['initial_usd'])
    dd = 1 - equity / peak if peak > 0 else D(0)
    m = D(1) if dd < D(lad['soft']) else D('.5')
    if hour < int(state.get('recover_until', -1)): m = min(m, D('.5'))
    if equity < D(lad['floor_fraction']) * initial: m = D('.25')
    return m


def paused(state, hour):
    return hour < int(state.get('pause_until', -1))


def sleeve_target(sleeve, signals, pairs, state, mult):
    """Target {pair: {direction, notional}} or None when any lookback is unavailable."""
    key = 'momentum_%dh' % sleeve['lookback']
    moms = {}
    for pair in pairs:
        sig = signals.get(pair)
        if sig is None or sig.get(key) is None: return None
        moms[pair] = float(sig[key])
    vols = {p: max(float(signals[p]['daily_vol']), .005) for p in pairs}
    raw = {}
    if sleeve['family'] == 'xs':
        order = sorted(pairs, key=lambda p: moms[p] / vols[p])
        k = sleeve['k']
        for p in order[-k:]: raw[p] = 1.0
        for p in order[:k]: raw[p] = -1.0
    else:
        for p in pairs:
            if moms[p] != 0: raw[p] = math.copysign(1 / vols[p], moms[p])
    total = sum(abs(v) for v in raw.values())
    if total <= 0: return {}
    equity = float(min(D(state['initial_usd']), D(state['last_equity'])))
    return {p: dict(direction=1 if v > 0 else -1,
                    notional=str(D(math.floor(equity * float(sleeve['gross']) * float(mult) * abs(v) / total * 100)) / 100))
            for p, v in raw.items()}


def stop_distance(sig, cfg):
    r=cfg['risk']
    if not r['atr_exits']: return D(r['stop_fraction'])
    atr=D(sig['atr_pct'])
    if not atr.is_finite() or atr<=0: raise ValueError('Invalid ATR fraction')
    return min(D(r['max_stop']),max(D(r['min_stop']),D(r['atr_stop'])*atr))


def costs(ticks, pair, cfg):
    q=ticks['Data'][pair]; spread=D(str(q['MinAsk']))/D(str(q['MaxBid']))-1
    ex=cfg['execution']
    return 2*(D(ex['fee'])+D(ex['slippage']))+spread


def opportunity(sig, direction):
    """24-hour trend-move proxy, NOT a calibrated expected return."""
    return max(D(0),direction*(D('.2')*D(sig['momentum_24h'])+
               D('.3')*D(sig['momentum_72h'])/3+D('.5')*D(sig['momentum'])/7))


def open_risk(state, ticks):
    # Use original stop distance even after trailing/partial gains; deliberately conservative.
    return sum((D(v['quantity'])*D(str(ticks['Data'][p]['MinAsk']))*
                D(v.get('stop_fraction','0.08')) for p,v in state['positions'].items()), D(0))


def entry_reason(pair, direction, sig, signals, info, ticks, cfg, state, hour, ignore=None):
    if state.get('stop_reason') or state.get('flatten_pending'): return 'RISK_STOP'
    if paused(state,hour): return 'LADDER_PAUSE'
    if hour-int(state['last_exit'].get(pair,-10000000))<cfg['risk']['cooldown_hours']: return 'COOLDOWN'
    if D(sig['quote_volume_24h'])<D(cfg['min_quote_volume_24h']): return 'LOW_VOLUME'
    if info.get('TradePairs',{}).get(pair,{}).get('CanTrade') is not True: return 'UNAVAILABLE'
    if sig.get('z24') is not None and direction*D(sig['z24'])>D(cfg['filters']['z_max']): return 'OVEREXTENDED'
    if direction*D(sig['r6'])>D(cfg['filters']['r6_max']): return 'OVEREXTENDED'
    if risk_off(signals.get('BTC/USD'),cfg) and direction==1 and pair!='BTC/USD': return 'BTC_RISK_OFF'
    q=ticks.get('Data',{}).get(pair,{})
    try: bid,ask=D(str(q['MaxBid'])),D(str(q['MinAsk']))
    except (KeyError, ValueError, ArithmeticError): return 'BAD_QUOTE'
    if not bid.is_finite() or not ask.is_finite() or bid<=0 or ask<bid: return 'BAD_QUOTE'
    if ask/bid-1>D(cfg['max_spread']): return 'WIDE_SPREAD'
    if opportunity(sig,direction)<costs(ticks,pair,cfg)*D(cfg['execution']['cost_multiple']): return 'COST_HURDLE'
    for held,pos in state['positions'].items():
        if held==ignore: continue
        if held not in signals: return 'HELD_HISTORY_UNAVAILABLE'
        if correlation(sig['returns'],signals[held]['returns'])*direction*pos['direction']>float(cfg['max_correlation']):
            return 'CORRELATED_EXPOSURE'
    return None


def proposals(state, signals, cfg):
    """Core gets first claim to a symbol; sleeve targets use their own stored rebalance clock."""
    pool={p:dict(pair=p,action='BUY' if s['direction']==1 else 'SHORT_OPEN',
           direction=s['direction'],book='core',score=s['score'])
          for p,s in signals.items() if s['direction']}
    for sleeve in cfg['sleeves']:
        cur=state.get('sleeves',{}).get(sleeve['name'],{})
        for p,w in cur.get('target',{}).items():
            if p not in pool and p in signals:
                # Directionally consistent movement, not an absolute score for a contrary trade.
                value=opportunity(signals[p],int(w['direction']))/max(D(signals[p]['daily_vol']),D('.005'))
                pool[p]=dict(pair=p,action='BUY' if int(w['direction'])==1 else 'SHORT_OPEN',
                    direction=int(w['direction']),book=sleeve['name'],score=str(value),budget=w['notional'])
    return sorted(pool.values(),key=lambda x:(x['book']!='core',-D(x['score']),x['pair']))


def budget_for(state, sig, ticks, cfg, owner, mult, desired=None):
    eq=max(D(0),min(D(state['initial_usd']),D(state['last_equity'])))
    total_room=max(D(0),eq*D(cfg['gross_fraction'])-gross_exposure(state,ticks))
    book_cap=D(cfg['risk']['core_gross']) if owner=='core' else next(D(s['gross']) for s in cfg['sleeves'] if s['name']==owner)
    book_room=max(D(0),eq*book_cap-gross_exposure(state,ticks,owner))
    dist=stop_distance(sig,cfg)
    risk_room=max(D(0),eq*D(cfg['risk']['portfolio'])-open_risk(state,ticks))
    size=min(eq*D(cfg['position_fraction'])*min(D(1),D(cfg['daily_vol_target'])/max(D(sig['daily_vol']),D('.005')))*mult,
             eq*D(cfg['risk']['per_position'])*mult/dist,risk_room/dist/D('1.02'),
             total_room/D('1.02'),book_room/D('1.02'),max(D(0),D(state['usd'])-1)/D('1.02'))
    if desired is not None:size=min(size,D(desired))
    return max(D(0),size).quantize(D('.01'),rounding=ROUND_DOWN)


def candidates(state, signals, info, ticks, cfg, hour, mult=D(1)):
    selected=[];why={};shadow=__import__('copy').deepcopy(state)
    for p in proposals(state,signals,cfg):
        pair=p['pair'];sig=signals[pair]
        if pair in shadow['positions']:continue
        reason=entry_reason(pair,p['direction'],sig,signals,info,ticks,cfg,shadow,hour)
        if not reason and len(shadow['positions'])>=cfg['max_positions']:reason='POSITION_LIMIT'
        if reason:why[pair]=reason;continue
        budget=budget_for(shadow,sig,ticks,cfg,p['book'],mult,p.get('budget'))
        if budget<25:why[pair]='EXPOSURE_OR_RISK_LIMIT';continue
        pick=dict(p,budget=str(budget),stop_fraction=str(stop_distance(sig,cfg)),opportunity=str(opportunity(sig,p['direction'])))
        selected.append(pick)
        ask=D(str(ticks['Data'][pair]['MinAsk']))
        shadow['positions'][pair]=dict(direction=p['direction'],quantity=str(budget*D('1.02')/ask),
                         collateral='0',book=p['book'],stop_fraction=pick['stop_fraction'])
        shadow['usd']=str(D(shadow['usd'])-budget*D('1.02'))
    return selected,why


def rotation(state,signals,info,ticks,cfg,hour,mult):
    r=cfg['rotation'];day=(hour+8)//24
    if not r['enabled'] or paused(state,hour) or state.get('stop_reason') or state.get('flatten_pending'):return None
    if hour-int(state.get('last_rotation_hour',-1000000))<r['interval_hours']:return None
    if state.get('rotation_day')==day and state.get('rotation_count',0)>=r['max_per_day']:return None
    if any(p not in signals for p in state['positions']):return None
    # Replace core holdings only; never seize a sleeve's inventory or rotate solely for a fill.
    held=[p for p,v in state['positions'].items() if book(v)=='core' and hour-int(v.get('opened_hour',hour))>=r['min_hold_hours']]
    for pick in proposals(state,signals,cfg):
        p=pick['pair']
        if p in state['positions'] or pick['book']!='core':continue
        for old in sorted(held,key=lambda h:(D(signals[h]['score']),h)):
            oldsig=signals[old];pos=state['positions'][old]
            if D(pick['score'])<max(D(oldsig['score'])*D(r['min_score_ratio']),D(oldsig['score'])+D(r['min_score_gap'])):continue
            reason=entry_reason(p,pick['direction'],signals[p],signals,info,ticks,cfg,state,hour,ignore=old)
            if reason:continue
            hurdle=(costs(ticks,p,cfg)+costs(ticks,old,cfg))*D(r['cost_multiple'])
            if opportunity(signals[p],pick['direction'])-opportunity(oldsig,pos['direction'])<hurdle:continue
            shadow=__import__('copy').deepcopy(state);shadow['positions'].pop(old)
            q=D(pos['quantity']);quote=ticks['Data'][old]
            proceeds=q*D(str(quote['MaxBid']))*D('.999') if pos['direction']==1 else max(D(0),D(pos['collateral'])+q*(D(pos['entry'])-D(str(quote['MinAsk'])))-q*D(str(quote['MinAsk']))*D('.001'))
            shadow['usd']=str(D(shadow['usd'])+proceeds)
            if len(shadow['positions'])>=cfg['max_positions']:continue
            budget=budget_for(shadow,signals[p],ticks,cfg,'core',mult)
            if budget<25:continue
            return dict(sell_pair=old,buy_pair=p,action=pick['action'],budget=str(budget),
                        score_gap=str(D(pick['score'])-D(oldsig['score'])),cost_hurdle=str(hurdle))
    return None
