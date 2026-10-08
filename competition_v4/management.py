"""Cost-aware, bounded long additions and partial exits; no network or journal writes.
Cost/ATR hurdle is a heuristic, not a prediction of profitable fills.
"""
import copy,time
from decimal import Decimal as D, ROUND_DOWN
from .strategy import opportunity_candidates,risk_multiplier
from . import timing


def addition_candidate(state,pair,signals,info,ticks,cfg,hour):
    pos=state['positions'].get(pair);m=cfg['management'];sig=signals.get(pair)
    # Roostoo short-add aggregation is not assumed. Inherited books are managed but never pyramided.
    if not pos or pos['direction']!=1 or pos.get('book')!='opportunity' or pos.get('partial_taken') or not sig or sig['direction']!=1:return None
    if int(pos.get('addition_count',0))>=m['max_additions']:return None
    boundary=int(time.time())//300*300000;bar=sig.get('timing')
    last=int(pos.get('last_addition_bar',pos.get('decision_bar',boundary)))
    if boundary-last<m['addition_interval_seconds']*1000 or not timing.confirms(sig,bar,boundary):return None
    level=D(bar['high']);px=D(bar['close']);quote=ticks['Data'][pair];bid=D(quote['MaxBid']);ask=D(quote['MinAsk'])
    # A distinct closed-bar cross at a higher breakout level, only after >=1R profit. No averaging down.
    if not D(bar['previous'])<=level<px or level<=D(pos.get('last_addition_level',pos.get('setup_level',pos['entry']))):return None
    stop=D(pos['stop_fraction'])
    if bid/D(pos['entry'])-1<stop:return None
    costs=2*(D(m['fee_per_side'])+D(m['slippage_per_side']))+ask/bid-1
    if 2*D(sig['atr_pct'])<=D(m['cost_multiple'])*costs:return None
    # Reuse all normal quality, correlation, volatility and regime gates without self-correlation.
    view=copy.deepcopy(state);view['positions'].pop(pair)
    picks,_=opportunity_candidates(view,signals,info,ticks,cfg,hour,risk_multiplier(state,cfg,hour))
    pick=next((p for p in picks if p['pair']==pair and p['action']=='BUY'),None)
    if not pick:return None
    equity=min(D(state['initial_usd']),D(state['last_equity']))
    gross=sum((D(p['quantity'])*D(ticks['Data'][symbol]['MinAsk']) for symbol,p in state['positions'].items()),D(0))
    used=sum((D(p['quantity'])*D(ticks['Data'][symbol]['MinAsk'])*D(p['stop_fraction']) for symbol,p in state['positions'].items()),D(0))
    effective_stop=min(stop,D(pick['stop_fraction']))
    budget=min(D(pick['budget']),D(pos['quantity'])*ask*D(m['addition_fraction']),
               max(D(0),equity*D(cfg['expansion']['position_fraction'])-D(pos['quantity'])*ask)/D('1.02'),
               max(D(0),equity*D(cfg['expansion']['gross_fraction'])-gross)/D('1.02'),
               max(D(0),equity*D(cfg['runtime']['portfolio_stop_risk_fraction'])-used)/effective_stop/D('1.02'))
    if budget<25:return None
    return dict(pick,budget=str(budget.quantize(D('.01'),rounding=ROUND_DOWN)),adding=True,
                stop_fraction=str(effective_stop),setup='ADD_BREAKOUT',level=str(level))


def partial_quantity(pos,quote,rules,cfg):
    if pos.get('partial_taken'):return None
    m=cfg['management'];d=pos['direction'];price=D(quote['MaxBid'] if d==1 else quote['MinAsk'])
    costs=2*(D(m['fee_per_side'])+D(m['slippage_per_side']))+D(quote['MinAsk'])/D(quote['MaxBid'])-1
    if d*(price/D(pos['entry'])-1)<max(D(pos['stop_fraction'])*D(m['partial_at_r']),costs*D(m['cost_multiple'])):return None
    qty=(D(pos['quantity'])*D(m['partial_fraction'])).quantize(D(1).scaleb(-rules['AmountPrecision']),rounding=ROUND_DOWN)
    if qty<=0 or qty*price<=D(rules['MiniOrder']) or (D(pos['quantity'])-qty)*price<=D(rules['MiniOrder']):return None
    return str(qty)
