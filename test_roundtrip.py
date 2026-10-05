"""One-shot BTC spot round trip on a user-confirmed TEST account; preview by default."""
import argparse
from contextlib import closing
from decimal import Decimal
import getpass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import warnings
from roostoo_adapter import Client, TestOrderGate, spot_plan, dec

PAIR = 'BTC/USD'
JOURNAL = Path('data/execution_test/roundtrip.sqlite3')


def emit(status, **values):
    print(json.dumps({'status': status, **values}, default=str), flush=True)


def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError('Invalid nonnegative monetary value')
    return result


def wallet(client):
    response = client.balance()
    if response.get('Success') is not True:
        raise ValueError('Balance request failed')
    spot = response['SpotWallet']
    if response.get('MarginWallet') not in ({}, None):
        raise ValueError('Nonempty margin wallet: test requires an unused account')
    for asset, row in spot.items():
        for field in ('Lock', 'PendingOrders', 'ShortCollateral'):
            if money(row.get(field, 0)) != 0:
                raise ValueError('Locked balance/pending order/collateral present')
        if asset not in ('USD', 'BTC') and money(row['Free']) != 0:
            raise ValueError('Unexpected holdings')
    return {'USD': money(spot['USD']['Free']), 'BTC': money(spot.get('BTC', {}).get('Free', 0))}


def market(client):
    info = client.request('/v3/exchangeInfo')
    stamp = client.request('/v3/serverTime')['ServerTime']
    response = client.request('/v3/ticker', {'timestamp': str(stamp)})
    if response.get('Success') is not True or abs(int(response['ServerTime']) - int(time.time()*1000)) > 60000:
        raise ValueError('Ticker failed, stale, or system clock incorrect')
    return info, response['Data'][PAIR]


def verify_fill(order, plan, before, after):
    params = plan['params']
    if any(order.get(k) != v for k, v in [('Pair', PAIR), ('Side', params['side']), ('Status', 'FILLED'), ('Type', 'MARKET')]):
        raise ValueError('Order identity/status mismatch or incomplete fill')
    qty = dec(order['FilledQuantity'])
    if qty != dec(params['quantity']) or dec(order['Quantity']) != qty:
        raise ValueError('Quantity mismatch/partial fill')
    price = dec(order['FilledAverPrice'])
    fee = money(order['CommissionChargeValue'])
    if order.get('CommissionCoin') != 'USD':
        raise ValueError('Unexpected fee currency; reconcile before continuing')
    sign = 1 if params['side'] == 'BUY' else -1
    expected_usd = before['USD'] - sign * qty * price - fee
    expected_btc = before['BTC'] + sign * qty
    if abs(after['USD'] - expected_usd) > Decimal('.02') or abs(after['BTC'] - expected_btc) > Decimal('.0000000001'):
        raise ValueError('Fill and wallet deltas do not reconcile')
    return {'order_id': order['OrderID'], 'side': params['side'], 'quantity': str(qty),
            'fill_price': str(price), 'fee_usd': str(fee)}


def execute_leg(client, gate, intent, plan, before):
    result = gate.submit(intent, plan)
    if result['status'] != 'ACK_UNRECONCILED':
        raise ValueError('Order not acknowledged: ' + json.dumps(result))
    detail = result['response'].get('OrderDetail', {})
    order_id = detail.get('OrderID')
    if order_id is None:
        raise ValueError('Missing order ID; do not resubmit')
    # Read-only polling, never repeat the place_order request.
    for attempt in range(3):
        response = client.request('/v3/query_order', {'order_id': str(order_id)}, method='POST', signed=True)
        orders = response.get('OrderMatched', []) if response.get('Success') is True else []
        matches = [x for x in orders if str(x.get('OrderID')) == str(order_id)]
        if len(matches) == 1 and matches[0].get('Status') == 'FILLED':
            break
    else:
        raise ValueError('Order fill not confirmed; automatic test halted')
    after = wallet(client)
    record = verify_fill(matches[0], plan, before, after)
    with closing(sqlite3.connect(gate.journal)) as db, db:
        db.execute('INSERT INTO receipts VALUES (?,?,?)', (intent, json.dumps(record), json.dumps(after, default=str)))
        changed = db.execute("UPDATE attempts SET status='RECONCILED' WHERE intent=? AND status='ACK_UNRECONCILED'", (intent,)).rowcount
        if changed != 1:
            raise ValueError('Unexpected journal state')
    emit('FILL_VERIFIED', **record)
    return after


def run(client, journal=JOURNAL):
    gate = TestOrderGate(client, journal, allow_test_orders=True)
    with closing(sqlite3.connect(journal)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS run (id INTEGER PRIMARY KEY, account TEXT, status TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS receipts (intent TEXT PRIMARY KEY, fill TEXT, wallet TEXT)')
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM run').fetchone() or db.execute('SELECT 1 FROM attempts').fetchone():
            raise ValueError('Run already recorded. Do not reset/delete journal or rerun orders.')
        db.execute('INSERT INTO run VALUES (1,?,?)', (hashlib.sha256(client.key.encode()).hexdigest(), 'STARTED'))
    before = wallet(client)
    if before['BTC'] != 0 or not Decimal('49990') <= before['USD'] <= Decimal('50010'):
        raise ValueError('Expected unused testing wallet near USD 50000 and zero BTC')
    positions = client.short_positions()
    if positions.get('Success') is not True or positions.get('Positions') != []:
        raise ValueError('Short positions could not be confirmed empty')
    pending = client.request('/v3/query_order', {'pending_only': 'TRUE'}, method='POST', signed=True)
    empty = pending.get('Success') is False and pending.get('ErrMsg') == 'no order matched'
    empty = empty or (pending.get('Success') is True and pending.get('OrderMatched') == [])
    if not empty:
        raise ValueError('Pending orders could not be confirmed empty')
    info, ticker = market(client)
    buy = spot_plan(info, ticker, PAIR, 'BUY', '10')
    emit('SUBMITTING_TEST_BUY', quantity=buy['params']['quantity'])
    bought = execute_leg(client, gate, 'btc-smoke-buy-v1', buy, before)
    info, ticker = market(client)
    sell = spot_plan(info, ticker, PAIR, 'SELL', bought['BTC'] - before['BTC'])
    emit('SUBMITTING_TEST_SELL', quantity=sell['params']['quantity'])
    after = execute_leg(client, gate, 'btc-smoke-sell-v1', sell, bought)
    with closing(sqlite3.connect(journal)) as db, db:
        db.execute("UPDATE run SET status='COMPLETE' WHERE id=1")
    emit('ROUNDTRIP_COMPLETE', usd_before=before['USD'], usd_after=after['USD'],
         usd_change=after['USD']-before['USD'], remaining_btc=after['BTC'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute-testing', action='store_true')
    mode.add_argument('--report', action='store_true')
    args = parser.parse_args()
    if args.report:
        if not JOURNAL.is_file():
            raise SystemExit('No execution journal exists.')
        with closing(sqlite3.connect(JOURNAL.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            for row in db.execute('SELECT intent,status,request,response FROM attempts'):
                emit('JOURNAL', intent=row[0], order_status=row[1], request=json.loads(row[2]), response=json.loads(row[3]) if row[3] else None)
        return
    if not args.execute_testing:
        info, ticker = market(Client())
        emit('PREVIEW_ONLY', plan=spot_plan(info, ticker, PAIR, 'BUY', '10'))
        return
    print('This places a roughly USD 10 BTC buy and an automatic sell on the supplied account. Fees apply.')
    print('Use only your TESTING keys; the API does not prove account type. An uncertain response halts execution.')
    if input('Type TEST ACCOUNT to continue: ').strip() != 'TEST ACCOUNT':
        raise SystemExit('Cancelled. No orders sent.')
    warnings.simplefilter('error', getpass.GetPassWarning)
    key = getpass.getpass('Testing API key (hidden): ').strip()
    secret = getpass.getpass('Testing API secret (hidden): ').strip()
    if not key or not secret:
        raise SystemExit('Empty credentials. No orders sent.')
    try:
        run(Client(key, secret))
    except Exception as error:
        emit('HALTED_DO_NOT_RESUBMIT', error=str(error), journal=str(JOURNAL))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
