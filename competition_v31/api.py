"""Order adapter foundation. CLI is public-data preview ONLY; no credentials needed."""
import argparse
from contextlib import closing
import hashlib
import hmac
import json
import sqlite3
import time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = 'https://mock-api.roostoo.com'
PAIRS = tuple(x+'/USD' for x in json.loads(Path(__file__).with_name('policy.json').read_text())['universe'])


def dec(value):
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError('Expected a finite positive number')
    return result


def signature(secret, payload):
    raw = '&'.join(str(k) + '=' + str(payload[k]) for k in sorted(payload))
    return hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


class NotSent(ValueError):
    """The deadline elapsed before the write was transmitted."""


class Client:
    def __init__(self, key='', secret=''):
        self.key, self.secret, self.last_call = key, secret, 0.
        self.clock_offset=0.;self.clock_checked=-1e9
        self.rate_path=Path('data/v3_rate_limit.sqlite3')

    def request(self, endpoint, params=None, method='GET', signed=False, deadline=None):
        values = dict(params or {})
        headers = {}
        if signed:
            if not self.key or not self.secret:
                raise ValueError('Missing credentials')
            if time.monotonic()-self.clock_checked>30:
                self.clock_offset=int(self.request('/v3/serverTime')['ServerTime'])-time.time()*1000
                self.clock_checked=time.monotonic()
            values['timestamp']=str(int(time.time()*1000+self.clock_offset))
            headers = {'RST-API-KEY': self.key, 'MSG-SIGNATURE': signature(self.secret, values)}
        # One client: at most 20 requests/minute, including timestamp requests.
        self.throttle()
        if deadline is not None and time.time()>=deadline:raise NotSent('Decision window ended before network transmission')
        wire = urlencode(values).encode()
        url, body = BASE + endpoint, None
        if method == 'GET':
            if wire:
                url += '?' + wire.decode()
        else:
            body = wire
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        # Never retry a write, and never forward credentials via a redirect.
        from urllib.request import build_opener, HTTPRedirectHandler
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None
        with build_opener(NoRedirect()).open(Request(url, data=body, headers=headers, method=method), timeout=20) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise ValueError('Non-object API response')
        return result

    def throttle(self):
        # Coordinate V3 processes by account without storing credentials. Legacy services do not use this limiter.
        key=hashlib.sha256((self.key or 'public').encode()).hexdigest()
        self.rate_path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.rate_path,timeout=10)) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS rate(account TEXT PRIMARY KEY,next_call REAL)')
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT next_call FROM rate WHERE account=?',(key,)).fetchone()
            now=time.time();slot=max(now,row[0] if row else now)
            if slot-now>30:raise ValueError('Rate limiter clock skew or concurrent overload')
            db.execute('INSERT OR REPLACE INTO rate VALUES (?,?)',(key,slot+3.1))
        time.sleep(max(0,slot-time.time()))

    def balance(self):
        return self.request('/v3/balance', signed=True)

    def short_positions(self):
        return self.request('/v6/short_positions', signed=True)

    def order_history(self, pair):
        return self.request('/v3/query_order', {'pair': pair}, method='POST', signed=True)


def spot_plan(info, ticker, pair, side, amount):
    if pair not in PAIRS or side not in ('BUY', 'SELL'):
        raise ValueError('Unsupported pair or side')
    rules = info['TradePairs'][pair]
    if info.get('IsRunning') is not True or rules.get('CanTrade') is not True:
        raise ValueError('Pair/exchange not tradable')
    precision = rules['AmountPrecision']
    if type(precision) is not int or not 0 <= precision <= 12:
        raise ValueError('Invalid quantity precision')
    bid, ask = dec(ticker['MaxBid']), dec(ticker['MinAsk'])
    if bid > ask:
        raise ValueError('Crossed quote')
    step = Decimal(1).scaleb(-precision)
    # BUY amount is total USD budget with a 1% price allowance + 0.1% fee reserve.
    # SELL amount is owned coin quantity, never a USD budget.
    price = ask if side == 'BUY' else bid
    raw_qty = dec(amount) / (ask * Decimal('1.01') * Decimal('1.001')) if side == 'BUY' else dec(amount)
    qty = raw_qty.quantize(step, rounding=ROUND_DOWN)
    if qty <= 0 or qty * price <= dec(rules['MiniOrder']):
        raise ValueError('Rounded order does not exceed the minimum notional')
    return {'endpoint': '/v3/place_order', 'params': {'pair': pair, 'side': side,
            'type': 'MARKET', 'quantity': format(qty, 'f')},
            'reference_price': str(price), 'estimated_notional': str(qty * price),
            'note': 'Estimate only; market fill price and actual fee can differ.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview', action='store_true', required=True)
    args = parser.parse_args()
    client = Client()
    info = client.request('/v3/exchangeInfo')
    stamp = client.request('/v3/serverTime')['ServerTime']
    ticker = client.request('/v3/ticker', {'timestamp': str(stamp)})
    if ticker.get('Success') is not True:
        raise ValueError('Ticker failed')
    if abs(int(ticker['ServerTime']) - int(time.time()*1000)) > 60000:
        raise ValueError('Stale ticker or incorrect machine clock')
    for pair in PAIRS:
        print(json.dumps(spot_plan(info, ticker['Data'][pair], pair, 'BUY', '10')))
    print('PREVIEW COMPLETE. No orders sent. No credentials used.')


if __name__ == '__main__':
    main()
