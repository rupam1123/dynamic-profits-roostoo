"""Offline-only support copied from the existing verified research harness.
Feature calculations: research_v3/baseline_sim.py.
In-memory journal: research_v3/compare.py. Fake exchange: v3_tests/exchange.py.
No network operations; the production V3.1 controller supplies all decisions.
"""
import copy,csv,json,math
from pathlib import Path
from datetime import datetime
from decimal import Decimal as D, ROUND_DOWN
import numpy as np
ASSETS='BTC ETH SOL BNB XRP ADA DOGE AVAX LINK DOT LTC NEAR'.split()

def load(folder):
    stamps=None;data={};qv={}
    for asset in ASSETS:
        path=next(Path(folder).glob(asset+'USDT_1h_*.csv'))
        with path.open() as f:rows=list(csv.DictReader(f))
        ts=np.array([datetime.fromisoformat(r['open_time_utc']).timestamp() for r in rows],dtype=np.int64)
        if stamps is None:stamps=ts
        if not np.array_equal(ts,stamps):raise ValueError('Unaligned baseline history '+asset)
        data[asset]={k:np.array([float(r[k]) for r in rows]) for k in ('open','high','low','close','volume')}
        qv[asset]=np.array([float(r['quote_volume']) for r in rows])
    return stamps,data,qv

def _roll_mean(x,w):
    out=np.full(len(x),np.nan)
    if len(x)>=w:out[w-1:]=np.lib.stride_tricks.sliding_window_view(x,w).mean(axis=1)
    return out


def _roll_std(x,w):
    # Direct centered variance avoids cancellation for tiny token prices after
    # prelisting placeholders. Only fully observed windows are ever admitted.
    out=np.full(len(x),np.nan)
    if len(x)>=w:out[w-1:]=np.lib.stride_tricks.sliding_window_view(x,w).std(axis=1)
    return out


def features(stamps, data):
    """f[a][key][i] = value known at decision index i (uses candles < i)."""
    F = {}
    for a, d in data.items():
        o, h, l, c, v = d["open"], d["high"], d["low"], d["close"], d["volume"]
        n = len(c)
        lr = np.zeros(n); lr[1:] = np.log(c[1:] / c[:-1])
        vol_end = _roll_std(lr, 72) * math.sqrt(24)          # through bar j
        prevc = np.roll(c, 1); prevc[0] = c[0]
        tr = np.maximum(h - l, np.maximum(abs(h - prevc), abs(l - prevc)))
        atr_end = _roll_mean(tr, 14) / c
        qv_end = _roll_mean(c * v, 24) * 24
        sma24 = _roll_mean(c, 24); sd24 = _roll_std(c, 24)
        z_end = (c - sma24) / np.where(sd24 > 0, sd24, np.nan)
        def lag(x):  # value at decision i = value at end of bar i-1
            y = np.full(n, np.nan); y[1:] = x[:-1]; return y
        def mom(k):
            y = np.full(n, np.nan); y[k + 1:] = c[k:-1] / c[:-k - 1] - 1; return y
        F[a] = dict(close=lag(c), m24=mom(24), m72=mom(72), m168=mom(168), m336=mom(336), m720=mom(720), r6=mom(6),
                    vol=lag(vol_end), atr=lag(atr_end), qv=lag(qv_end), z24=lag(z_end), lr=lr)
        m24, m72, m168 = F[a]["m24"], F[a]["m72"], F[a]["m168"]
        F[a]["dir"] = np.where((m168 > .01) & (m24 > 0) & (m72 > 0), 1,
                               np.where((m168 < -.01) & (m24 < 0) & (m72 < 0), -1, 0))
        def prior_ext(n, fn):
            y = np.full(n_, np.nan)
            for i in range(n + 1, n_):
                y[i] = fn(c[i - 1 - n:i - 1])
            return y
        n_ = n
        F[a]["hh72"] = prior_ext(72, np.max); F[a]["ll72"] = prior_ext(72, np.min)
        F[a]["hh24"] = prior_ext(24, np.max); F[a]["ll24"] = prior_ext(24, np.min)
        cl = F[a]["close"]
        F[a]["dir_breakout"] = np.where((cl > F[a]["hh72"]) & (m168 > 0), 1, np.where((cl < F[a]["ll72"]) & (m168 < 0), -1, 0))
        z = F[a]["z24"]
        F[a]["dir_meanrev"] = np.where((z < -2.5) & (m168 > 0), 1, np.where((z > 2.5) & (m168 < 0), -1, 0))
        F[a]["score"] = abs(.2 * m24 + .3 * m72 + .5 * m168) / np.maximum(F[a]["vol"], .005)
    b = F["BTC"]
    med = np.full(len(stamps), np.nan)
    for i in range(720, len(stamps)):
        med[i] = np.median(b["vol"][i - 720:i:6])
    F["_risk_off"] = (b["m24"] < -0.03) | (b["vol"] > 2 * med)
    return F


class MemoryStore:
    def __init__(self,state):self.state=copy.deepcopy(state);self.attempts={};self.events=[]
    def load(self):return copy.deepcopy(self.state)
    def save(self,state,kind,body,applied=None):
        self.state=copy.deepcopy(state);self.events.append((kind,copy.deepcopy(body)))
        if applied:self.attempts[applied][0]='APPLIED'
    def reserve(self,intent,plan):
        if self.pending() or intent in self.attempts:raise RuntimeError('Duplicate or unresolved replay intent')
        self.attempts[intent]=['SENDING',json.dumps(plan),None]
    def acknowledge(self,intent,status,response):self.attempts[intent][0]=status;self.attempts[intent][2]=json.dumps(response)
    def pending(self):return [(k,*v) for k,v in self.attempts.items() if v[0]!='APPLIED']
    def exists(self,intent):return intent in self.attempts
    def already(self,h,p,stopping=False):return self.exists(str(h)+':'+p+(':STOP' if stopping else ''))
    def hour_count(self,h):return sum(json.loads(v[1])['hour']==h for v in self.attempts.values())

PAIRS=[a+'/USD' for a in 'BTC ETH SOL BNB XRP ADA DOGE AVAX LINK DOT LTC NEAR'.split()]
MIDNIGHT=20732*86400
class Exchange:
    """Fake Roostoo: market fills at the quoted price, 0.1% fee, shorts by collateral."""
    key = 'offline-test-key'

    def __init__(self, precision=8):
        self.usd = D('100000'); self.coins = {}; self.shorts = {}; self.orders = {}; self.writes = 0
        self.prices = {p: D(str(100 + 10 * i)) for i, p in enumerate(PAIRS)}
        self.precision = {p: precision for p in PAIRS}
        self.clock = MIDNIGHT + 120
        self.slip=D(0);self.fees=D(0)

    def balance(self):
        locked = sum((D(p['Collateral']) for p in self.shorts.values()), D('0'))
        spot = {'USD': {'Free': str(self.usd), 'Lock': str(locked), 'ShortCollateral': str(locked), 'PendingOrders': 0}}
        for asset, qty in self.coins.items():
            spot[asset] = {'Free': str(qty), 'Lock': 0, 'ShortCollateral': 0, 'PendingOrders': 0}
        return {'Success': True, 'SpotWallet': spot, 'MarginWallet': {}}

    def short_positions(self): return {'Success': True, 'Positions': list(self.shorts.values())}

    def request(self, endpoint, params=None, **kw):
        if endpoint == '/v3/serverTime': return {'ServerTime': int(self.clock * 1000)}
        if endpoint == '/v3/exchangeInfo':
            return {'IsRunning': True, 'TradePairs': {p: dict(CanTrade=True, AmountPrecision=self.precision[p], PricePrecision=8, MiniOrder=1) for p in self.prices}}
        if endpoint == '/v3/ticker':
            return {'Success': True, 'ServerTime': int(self.clock * 1000),
                    'Data': {p: dict(MaxBid=str(v), MinAsk=str(v)) for p, v in self.prices.items()}}
        if endpoint == '/v3/query_order':
            if 'pending_only' in params: return {'Success': False, 'ErrMsg': 'no order matched'}
            return {'Success': True, 'OrderMatched': [self.orders[params['order_id']]]}
        self.writes += 1
        pair = params['pair']; price = self.prices[pair]
        buys=endpoint=='/v6/short_close' or (endpoint=='/v3/place_order' and params['side']=='BUY')
        price*=1+self.slip if buys else 1-self.slip
        if endpoint == '/v3/place_order':
            qty = D(params['quantity']); fee = qty * price * D('.001'); buy = params['side'] == 'BUY'
            asset = pair.split('/')[0]
            self.coins[asset] = self.coins.get(asset, D('0')) + (qty if buy else -qty)
            if self.coins[asset] == 0: del self.coins[asset]
            self.usd += -qty * price - fee if buy else qty * price - fee
            order = dict(OrderID=self.writes, Pair=pair, Side=params['side'], Type='MARKET', Status='FILLED',
                         Quantity=str(qty), FilledQuantity=str(qty), FilledAverPrice=str(price),
                         CommissionCoin='USD', CommissionChargeValue=str(fee))
            self.orders[str(self.writes)] = order
            result = {'Success': True, 'OrderDetail': order}
        elif endpoint == '/v6/short_open':
            assert pair not in self.shorts
            qty = (D(params['collateral']) / price).quantize(D(1).scaleb(-self.precision[pair]), rounding=ROUND_DOWN)
            collateral = (qty * price).quantize(D('.01')); fee = qty * price * D('.001')
            self.usd -= collateral + fee
            result = dict(Success=True, ID=self.writes, Pair=pair, Status='OPEN', ShortQty=str(qty),
                          EntryPrice=str(price), Collateral=str(collateral), OpenFee=str(fee))
            self.shorts[pair] = result.copy()
        elif endpoint == '/v6/short_close':
            pos = self.shorts.pop(pair); qty = D(pos['ShortQty']); collateral = D(pos['Collateral'])
            close=D(params['close_qty']);full=close>=qty
            if not full:
                self.shorts[pair]=dict(pos,ShortQty=str(qty-close),Collateral=str(collateral*(qty-close)/qty))
                collateral=collateral*close/qty;qty=close
            pnl = max(qty * (D(pos['EntryPrice']) - price), -collateral); fee = qty * price * D('.001')
            returned = collateral + pnl - fee; self.usd += returned
            result = dict(Success=True, FullyClosed=full, RemainingQty='0' if full else self.shorts[pair]['ShortQty'], RemainingCollateral='0' if full else self.shorts[pair]['Collateral'], ClosedQty=str(qty), ClosePrice=str(price),
                          CloseFee=str(fee), RealizedPNL=str(pnl), ReturnAmount=str(returned))
        else: raise AssertionError(endpoint)
        self.fees+=fee
        self.usd = self.usd.quantize(D('.01'))
        return result

