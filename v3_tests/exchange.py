from decimal import Decimal as D, ROUND_DOWN
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

