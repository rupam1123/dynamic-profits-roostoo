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


class Client:
    def __init__(self, key='', secret=''):
        self.key, self.secret, self.last_call = key, secret, 0.

    def request(self, endpoint, params=None, method='GET', signed=False):
        values = dict(params or {})
        headers = {}
        if signed:
            if not self.key or not self.secret:
                raise ValueError('Missing credentials')
            values['timestamp'] = str(self.request('/v3/serverTime')['ServerTime'])
            headers = {'RST-API-KEY': self.key, 'MSG-SIGNATURE': signature(self.secret, values)}
        # One client: at most 20 requests/minute, including timestamp requests.
        time.sleep(max(0, 3.1 - (time.monotonic() - self.last_call)))
        self.last_call = time.monotonic()
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


class TestOrderGate:
    """Import-only foundation. Not wired to CLI or strategy; requires external reconciliation."""
    def __init__(self, client, journal, *, allow_test_orders=False):
        self.client, self.enabled = client, allow_test_orders
        journal = Path(journal)
        journal.parent.mkdir(parents=True, exist_ok=True)
        self.journal = journal
        with closing(sqlite3.connect(journal)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS attempts (intent TEXT PRIMARY KEY, '
                       'account TEXT, status TEXT, request TEXT, response TEXT)')

    def submit(self, intent, plan):
        if not self.enabled:
            raise ValueError('Order transmission disabled')
        if not intent or not self.client.key or not self.client.secret:
            raise ValueError('Intent and credentials required')
        params = plan['params']
        if plan['endpoint'] != '/v3/place_order' or set(params) != {'pair','side','type','quantity'}:
            raise ValueError('Only spot market payloads supported by this gate')
        if params['pair'] not in PAIRS or params['side'] not in ('BUY','SELL') or params['type'] != 'MARKET':
            raise ValueError('Invalid market order')
        dec(params['quantity'])
        account = hashlib.sha256(self.client.key.encode()).hexdigest()
        with closing(sqlite3.connect(self.journal, timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM attempts WHERE intent=?', (intent,)).fetchone():
                raise ValueError('Intent already recorded; never resubmit it')
            if db.execute("SELECT 1 FROM attempts WHERE status IN ('SENDING','UNKNOWN','ACK_UNRECONCILED')").fetchone():
                raise ValueError('Previous attempt needs reconciliation before another order')
            db.execute('INSERT INTO attempts VALUES (?,?,?,?,?)',
                       (intent, account, 'SENDING', json.dumps(plan), None))
        # Durable SENDING is recorded before any request. Crash => blocked on restart.
        try:
            result = self.client.request(plan['endpoint'], params, method='POST', signed=True)
            status = 'REJECTED' if result.get('Success') is False else 'ACK_UNRECONCILED' if result.get('Success') is True else 'UNKNOWN'
        except Exception as exc:
            status, result = 'UNKNOWN', {'error_type': type(exc).__name__}
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute('UPDATE attempts SET status=?,response=? WHERE intent=?',
                       (status, json.dumps(result), intent))
        return {'status': status, 'response': result}


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
