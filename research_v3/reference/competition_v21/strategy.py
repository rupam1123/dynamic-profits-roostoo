"""Pure completed-candle features, core selection, sleeve targets and risk rules. No network or orders.

V2.1 = V2 trend core + entry filters + drawdown ladder + daily activity guard + two small sleeves:
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


def gross_exposure(state, ticks, owner='core'):
    # Mark gross risk at ask; for shorts also reserve at least posted collateral. Per book.
    total = D('0')
    for pair, pos in state['positions'].items():
        if book(pos) != owner: continue
        marked = D(pos['quantity']) * D(ticks['Data'][pair]['MinAsk'])
        total += max(marked, D(pos['collateral'])) if pos['direction'] == -1 else marked
    return total


def risk_multiplier(state, cfg, hour):
    lad = cfg['ladder']
    equity, peak, initial = D(state['last_equity']), D(state['peak_equity']), D(state['initial_usd'])
    dd = 1 - equity / peak if peak > 0 else D(0)
    m = D(1) if dd < D(lad['soft']) else D('.5')
    if hour < int(state.get('recover_until', -1)): m = min(m, D('.5'))
    if equity < D(lad['floor_fraction']) * initial: m = D('.25')
    return m


def paused(state, hour):
    return hour < int(state.get('pause_until', -1))


def candidates(state, signals, info, ticks, cfg, hour, mult=D(1)):
    selected = []; reasons = {}; occupied = {p: v['direction'] for p, v in state['positions'].items()}
    n_core = sum(1 for v in state['positions'].values() if book(v) == 'core')
    if state.get('stop_reason'):
        return [], {'portfolio': 'STOP_LATCHED'}
    if paused(state, hour):
        return [], {'portfolio': 'LADDER_PAUSE'}
    equity = min(D(state['initial_usd']), D(state['last_equity']))
    cap = max(D('0'), equity * D(cfg['gross_fraction']))
    room = max(D('0'), cap - gross_exposure(state, ticks))
    cash = max(D('0'), D(state['usd']) - D('1'))
    off = risk_off(signals.get('BTC/USD'), cfg)
    flt = cfg['filters']
    ranked = sorted(signals, key=lambda p: (-D(signals[p]['score']), p))
    for pair in ranked:
        sig = signals[pair]; reason = None
        if pair in occupied: continue
        d = sig['direction']
        if not d: reason = 'NO_ALIGNED_TREND'
        elif hour - int(state['last_exit'].get(pair, -10000000)) < 12: reason = 'COOLDOWN'
        elif n_core >= cfg['max_positions']: reason = 'POSITION_LIMIT'
        elif D(sig['quote_volume_24h']) < D(cfg['min_quote_volume_24h']): reason = 'LOW_VOLUME'
        elif info.get('TradePairs', {}).get(pair, {}).get('CanTrade') is not True: reason = 'UNAVAILABLE'
        elif sig.get('z24') is not None and d * D(sig['z24']) > D(flt['z_max']): reason = 'OVEREXTENDED'
        elif d * D(sig['r6']) > D(flt['r6_max']): reason = 'OVEREXTENDED'
        elif d == 1 and off and pair != 'BTC/USD': reason = 'BTC_RISK_OFF'
        else:
            quote = ticks.get('Data', {}).get(pair, {})
            try:
                bid = D(str(quote.get('MaxBid', 0))); ask = D(str(quote.get('MinAsk', 0)))
            except Exception:
                reasons[pair] = 'BAD_QUOTE'; continue
            if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid: reason = 'BAD_QUOTE'
            elif ask / bid - 1 > D(cfg['max_spread']): reason = 'WIDE_SPREAD'
            else:
                for held, hd in occupied.items():
                    if held not in signals:
                        reason = 'HELD_HISTORY_UNAVAILABLE'; break
                    corr = correlation(sig['returns'], signals[held]['returns']) * d * hd
                    if corr > float(cfg['max_correlation']): reason = 'CORRELATED_EXPOSURE'; break
        if reason:
            reasons[pair] = reason; continue
        vol = max(D(sig['daily_vol']), D('.005'))
        budget = min(equity * D(cfg['position_fraction']) * min(D(1), D(cfg['daily_vol_target']) / vol) * mult,
                     room / D('1.02'), cash / D('1.02'))
        budget = budget.quantize(D('.01'), rounding=ROUND_DOWN)
        if budget < D('25'):
            reasons[pair] = 'EXPOSURE_OR_CASH_LIMIT'; continue
        selected.append(dict(pair=pair, action='BUY' if d == 1 else 'SHORT_OPEN', budget=str(budget), score=sig['score']))
        occupied[pair] = d; n_core += 1
        room -= budget * D('1.02'); cash -= budget * D('1.02')
    return selected, reasons


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
