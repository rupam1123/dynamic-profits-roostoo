"""Bounded TEST-account controller. Default mode previews public signals; no orders."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from candle_feed import refresh, HOUR, SYMBOLS
from roostoo_adapter import Client, dec, spot_plan

PAIRS = ('BTC/USD', 'ETH/USD', 'SOL/USD')
ROOT = Path('data/test_controller')
BUDGET = Decimal('10')
VERSION = 'test-controller-1'


class Blocked(Exception):
    """Stop sending orders; preserve evidence and reconcile before resuming."""


def emit(status, **fields):
    print(json.dumps(dict(status=status, **fields), default=str), flush=True)


def number(value):
    result = Decimal(str(value))
    if not result.is_finite(): raise Blocked('Nonfinite exchange value')
    return result


def near(a, b, tolerance=Decimal('.03')):
    if abs(number(a)-number(b)) > tolerance:
        raise Blocked('Exchange account and execution record do not reconcile')


def fingerprint():
    return hashlib.sha256(b''.join(Path(__file__).with_name(p).read_bytes() for p in
                         ('test_controller.py', 'roostoo_adapter.py', 'candle_feed.py'))).hexdigest()


@contextmanager
def process_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
            try: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError: raise Blocked('Another controller holds the process lock')
        else:
            import fcntl
            try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError: raise Blocked('Another controller holds the process lock')
        try: yield
        finally:
            if os.name == 'nt':
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(handle, fcntl.LOCK_UN)


class Store:
    def __init__(self, path=ROOT/'execution.sqlite3'):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, status TEXT, plan TEXT, response TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, utc TEXT, kind TEXT, body TEXT)')

    def load(self):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
        if not row: raise Blocked('Initialize the testing controller first')
        return json.loads(row[0])

    def save(self, state, kind, body, applied=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO state VALUES (1,?)', (json.dumps(state),))
            if applied: db.execute("UPDATE attempts SET status='APPLIED' WHERE id=?", (applied,))
            db.execute('INSERT INTO events(utc,kind,body) VALUES (?,?,?)',
                       (datetime.now(timezone.utc).isoformat(), kind, json.dumps(body, default=str)))

    def reserve(self, intent, plan):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM attempts WHERE status!='APPLIED'").fetchone():
                raise Blocked('An earlier execution needs reconciliation')
            if db.execute('SELECT 1 FROM attempts WHERE id=?', (intent,)).fetchone():
                raise Blocked('Duplicate intent prevented')
            db.execute('INSERT INTO attempts VALUES (?,?,?,NULL)', (intent, 'SENDING', json.dumps(plan)))

    def acknowledge(self, intent, status, response):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE attempts SET status=?,response=? WHERE id=?',
                       (status, json.dumps(response), intent))

    def pending(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT id,status,plan,response FROM attempts WHERE status!='APPLIED'").fetchall()

    def already(self, hour, pair, stopping=False):
        with closing(sqlite3.connect(self.path)) as db:
            return bool(db.execute('SELECT 1 FROM attempts WHERE id=?', (str(hour)+':'+pair+(':STOP' if stopping else ''),)).fetchone())


def account(client):
    balance = client.balance()
    shorts = client.short_positions()
    pending = client.request('/v3/query_order', {'pending_only':'TRUE'}, method='POST', signed=True)
    if balance.get('Success') is not True or shorts.get('Success') is not True or not isinstance(shorts.get('Positions'), list):
        raise Blocked('Cannot verify account state')
    empty = ((pending.get('Success') is False and pending.get('ErrMsg') == 'no order matched') or
             (pending.get('Success') is True and pending.get('OrderMatched') == []))
    if not empty or balance.get('MarginWallet') not in ({}, None):
        raise Blocked('Pending orders or margin wallet not empty')
    spot = balance['SpotWallet']
    for asset, row in spot.items():
        if number(row.get('PendingOrders', 0)) != 0: raise Blocked('Pending order count is nonzero')
        if asset not in ('USD', 'BTC', 'ETH', 'SOL') and any(number(row.get(k, 0)) != 0 for k in ('Free', 'Lock', 'ShortCollateral')):
            raise Blocked('Unexpected asset in testing account')
    return {'spot':spot, 'shorts':shorts['Positions'], 'usd':str(number(spot['USD']['Free']))}


def match_account(state, observed):
    near(observed['usd'], state['usd'])
    shorts = observed['shorts']
    expected_short = [p for p, v in state['positions'].items() if v['direction'] == -1]
    if len(shorts) != len(expected_short): raise Blocked('Unexpected short position count')
    collateral = Decimal('0')
    for pair in PAIRS:
        pos = state['positions'].get(pair)
        row = observed['spot'].get(pair.split('/')[0], {})
        expected_qty = pos['quantity'] if pos and pos['direction'] == 1 else '0'
        near(row.get('Free', 0), expected_qty, Decimal('.0000000001'))
        for field in ('Lock', 'ShortCollateral'):
            near(row.get(field, 0), 0, Decimal('.0000000001'))
        if pos and pos['direction'] == -1:
            found = [x for x in shorts if x.get('Pair') == pair and str(x.get('ID')) == str(pos['id'])]
            if len(found) != 1: raise Blocked('Short identity mismatch')
            for remote, local in [('ShortQty','quantity'), ('EntryPrice','entry'), ('Collateral','collateral')]:
                near(found[0][remote], pos[local], Decimal('.0000000001'))
            collateral += number(pos['collateral'])
    usd = observed['spot']['USD']
    near(usd.get('Lock', 0), collateral)
    near(usd.get('ShortCollateral', 0), collateral)


def identity(client, state):
    if state['account'] != hashlib.sha256(client.key.encode()).hexdigest(): raise Blocked('Different account credentials')
    if state['fingerprint'] != fingerprint(): raise Blocked('Code changed; preserve journal and review migration')


def initialize(client, store, now=None):
    now = time.time() if now is None else now
    with closing(sqlite3.connect(store.path)) as db:
        if db.execute('SELECT 1 FROM state').fetchone() or db.execute('SELECT 1 FROM attempts').fetchone():
            raise Blocked('Already initialized; do not reset the execution database')
    observed = account(client)
    if not Decimal('49900') <= number(observed['usd']) <= Decimal('50100'):
        raise Blocked('Expected testing balance near USD 50000; competition credentials must not be used')
    state = dict(account=hashlib.sha256(client.key.encode()).hexdigest(), fingerprint=fingerprint(),
                 version=VERSION, usd=observed['usd'], initial_usd=observed['usd'], positions={},
                 last_exit={}, last_hour=-1, started=now, expires=now+26*3600,
                 peak_equity=observed['usd'], last_equity=observed['usd'], stop_reason=None)
    match_account(state, observed)
    store.save(state, 'INITIALIZED', {'usd':state['usd'], 'expires':state['expires']})
    emit('TEST_CONTROLLER_INITIALIZED', usd=state['usd'], test_duration_hours=26, target_usd_per_pair=10,
         note='No orders placed. Account purpose is user-confirmed, not identified by the API.')


def public_market(client):
    info = client.request('/v3/exchangeInfo')
    stamp = client.request('/v3/serverTime')['ServerTime']
    ticks = client.request('/v3/ticker', {'timestamp':str(stamp)})
    if info.get('IsRunning') is not True or ticks.get('Success') is not True:
        raise ValueError('Public market unavailable')
    if abs(int(ticks['ServerTime'])-int(time.time()*1000)) > 60000:
        raise ValueError('Stale quotes or incorrect machine clock')
    for pair in PAIRS:
        quote = ticks['Data'][pair]
        if dec(quote['MaxBid']) > dec(quote['MinAsk']): raise ValueError('Crossed quote')
        if info['TradePairs'][pair].get('CanTrade') is not True: raise ValueError('Pair not tradable')
    return info, ticks


def signals(path=ROOT/'candles.sqlite3'):
    boundary = refresh(path)
    result = {}
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        for symbol in SYMBOLS:
            rows = db.execute('SELECT open_time,close FROM candles WHERE symbol=? AND open_time<? ORDER BY open_time DESC LIMIT 169', (symbol,boundary)).fetchall()
            if len(rows) != 169 or rows[0][0] != boundary-HOUR or any(a[0]-b[0] != HOUR for a,b in zip(rows,rows[1:])):
                raise ValueError('Signal history missing an hour')
            latest, oldest = dec(rows[0][1]), dec(rows[-1][1])
            result[symbol[:-4]+'/USD'] = {'close':str(latest), 'momentum':str(latest/oldest-1)}
    return boundary//HOUR, result


def choose(state, pair, hour, signal, expired=False):
    pos = state['positions'].get(pair)
    if pos:
        if expired: return 'SELL' if pos['direction'] == 1 else 'SHORT_CLOSE'
        mom = number(signal['momentum']); close = dec(signal['close'])
        if pos['direction']*mom <= 0 or pos['direction']*(close/dec(pos['entry'])-1) <= Decimal('-.08'):
            return 'SELL' if pos['direction'] == 1 else 'SHORT_CLOSE'
    elif not expired and hour % 24 == 0 and hour-int(state['last_exit'].get(pair, -10000000)) >= 12:
        mom = number(signal['momentum'])
        if mom > Decimal('.01'): return 'BUY'
        if mom < Decimal('-.01'): return 'SHORT_OPEN'
    return None


def plan_order(state, pair, action, info, ticks, hour):
    quote = ticks['Data'][pair]
    if action in ('BUY', 'SHORT_OPEN'):
        if pair in state['positions'] or len(state['positions']) >= 3: raise Blocked('Exposure slot unavailable')
        if number(state['usd']) < BUDGET+1: raise Blocked('Insufficient free testing USD')
        if (dec(quote['MinAsk'])/dec(quote['MaxBid'])-1) > Decimal('.005'):
            raise ValueError('Spread above 0.5%; entry deferred')
    if action in ('BUY', 'SELL'):
        amount = str(BUDGET) if action == 'BUY' else state['positions'][pair]['quantity']
        plan = spot_plan(info, quote, pair, action, amount)
    elif action == 'SHORT_OPEN':
        precision = info['TradePairs'][pair]['AmountPrecision']
        if type(precision) is not int or not 0 <= precision <= 12: raise Blocked('Invalid amount precision')
        step = Decimal(1).scaleb(-precision)
        qty = (BUDGET/dec(quote['MaxBid'])).quantize(step, rounding=ROUND_DOWN)
        if qty <= 0 or qty*dec(quote['MaxBid']) <= dec(info['TradePairs'][pair]['MiniOrder']):
            raise ValueError('Short below exchange minimum')
        plan = {'endpoint':'/v6/short_open', 'params':{'pair':pair, 'collateral':str(BUDGET)}}
    else:
        plan = {'endpoint':'/v6/short_close', 'params':{'pair':pair, 'close_qty':state['positions'][pair]['quantity']}}
    plan.update(pair=pair, action=action, hour=hour, precision=info['TradePairs'][pair]['AmountPrecision'])
    return plan


def reconcile(client, store, intent, plan, response):
    state = store.load(); identity(client, state)
    after = json.loads(json.dumps(state))
    pair, action = plan['pair'], plan['action']
    usd = number(state['usd']); receipt = response
    if action in ('BUY', 'SELL'):
        order_id = response.get('OrderDetail', {}).get('OrderID')
        if order_id is None: raise Blocked('Acknowledged spot order missing its ID')
        queried = client.request('/v3/query_order', {'order_id':str(order_id)}, method='POST', signed=True)
        found = [x for x in queried.get('OrderMatched', []) if str(x.get('OrderID')) == str(order_id)] if queried.get('Success') is True else []
        if len(found) != 1: raise Blocked('Spot order cannot be found; no resubmission')
        receipt = found[0]
        if any(receipt.get(k) != v for k,v in [('Pair',pair), ('Side',action), ('Status','FILLED'), ('Type','MARKET'), ('CommissionCoin','USD')]):
            raise Blocked('Spot fill identity/status mismatch')
        qty = dec(receipt['FilledQuantity']); price = dec(receipt['FilledAverPrice']); fee = number(receipt['CommissionChargeValue'])
        if qty != dec(plan['params']['quantity']) or dec(receipt['Quantity']) != qty or fee < 0:
            raise Blocked('Partial/unexpected spot fill')
        if action == 'BUY':
            if qty*price+fee > BUDGET*Decimal('1.02'): raise Blocked('Actual buy cost exceeded testing allowance; reconcile exposure')
            usd -= qty*price+fee
            after['positions'][pair] = dict(direction=1, quantity=str(qty), entry=str(price), collateral='0', id=str(order_id))
        else:
            if qty != dec(state['positions'][pair]['quantity']): raise Blocked('Sell quantity mismatch')
            usd += qty*price-fee
            del after['positions'][pair]; after['last_exit'][pair] = plan['hour']
    elif action == 'SHORT_OPEN':
        if response.get('Status') != 'OPEN' or response.get('Pair') != pair or response.get('ID') is None:
            raise Blocked('Short opening identity/status mismatch')
        qty = dec(response['ShortQty']); price = dec(response['EntryPrice']); collateral = dec(response['Collateral']); fee = number(response['OpenFee'])
        step = Decimal(1).scaleb(-plan['precision'])
        if qty != (BUDGET/price).quantize(step, rounding=ROUND_DOWN) or collateral > BUDGET or fee < 0:
            raise Blocked('Unexpected short size')
        if collateral != BUDGET and abs(collateral-qty*price) > Decimal('.01'):
            raise Blocked('Short collateral differs from notional')
        near(fee, qty*price*Decimal('.001'), Decimal('.000002'))
        usd -= collateral+fee
        after['positions'][pair] = dict(direction=-1, quantity=str(qty), entry=str(price), collateral=str(collateral), id=str(response['ID']))
    else:
        pos = state['positions'][pair]
        qty = dec(pos['quantity']); collateral = dec(pos['collateral'])
        if response.get('FullyClosed') is not True or dec(response['ClosedQty']) != qty: raise Blocked('Short not fully closed')
        price = dec(response['ClosePrice']); fee = number(response['CloseFee']); pnl = number(response['RealizedPNL']); returned = number(response['ReturnAmount'])
        if fee < 0: raise Blocked('Invalid close fee')
        near(pnl, max(qty*(dec(pos['entry'])-price), -collateral))
        near(returned, collateral+pnl-fee)
        usd += returned
        del after['positions'][pair]; after['last_exit'][pair] = plan['hour']
    after['usd'] = str(usd)
    observed = account(client)
    match_account(after, observed)
    # Anchor subsequent cash calculations to exchange precision after bounded reconciliation.
    after['usd'] = observed['usd']
    store.save(after, 'FILL_RECONCILED', {'intent':intent, 'fill':receipt, 'usd':after['usd']}, applied=intent)
    emit('TEST_FILL_RECONCILED', intent=intent, action=action, pair=pair, usd_free=after['usd'])


def recover_acknowledged(client, store):
    pending = store.pending()
    if len(pending) > 1: raise Blocked('Multiple unresolved intents')
    for intent, status, plan, response in pending:
        if status != 'ACK': raise Blocked('Unresolved '+status+' intent '+intent+'; no automatic write retry')
        reconcile(client, store, intent, json.loads(plan), json.loads(response))


def submit(client, store, intent, plan):
    store.reserve(intent, plan)
    try:
        response = client.request(plan['endpoint'], plan['params'], method='POST', signed=True)
    except Exception as exc:
        store.acknowledge(intent, 'UNKNOWN', {'error_type':type(exc).__name__})
        raise Blocked('Write response lost; no resubmission')
    status = 'ACK' if response.get('Success') is True else 'REJECTED' if response.get('Success') is False else 'UNKNOWN'
    store.acknowledge(intent, status, response)
    if status != 'ACK': raise Blocked('Write '+status+'; inspect journal before continuing')
    reconcile(client, store, intent, plan, response)


def mark_equity(state, ticks):
    equity = number(state['usd'])
    for pair, pos in state['positions'].items():
        qty = dec(pos['quantity']); quote = ticks['Data'][pair]
        if pos['direction'] == 1:
            equity += qty*dec(quote['MaxBid'])*Decimal('.999')
        else:
            price = dec(quote['MinAsk']); collateral = dec(pos['collateral'])
            equity += collateral+max(qty*(dec(pos['entry'])-price), -collateral)-qty*price*Decimal('.001')
    return equity


def cycle(client, store):
    state = store.load(); identity(client, state)
    recover_acknowledged(client, store)
    state = store.load()
    match_account(state, account(client))
    _, risk_ticks = public_market(client)
    equity = mark_equity(state, risk_ticks)
    state['last_equity'] = str(equity)
    peak = max(equity, number(state['peak_equity']))
    state['peak_equity'] = str(peak)
    if peak-equity >= Decimal('3') and not state['stop_reason']:
        state['stop_reason'] = 'USD_3_DRAWDOWN'
    if time.time() >= state['expires'] and not state['stop_reason']:
        state['stop_reason'] = 'TEST_DEADLINE'
    store.save(state, 'EQUITY', {'equity':str(equity), 'peak':str(peak), 'stop_reason':state['stop_reason']})
    expired = bool(state['stop_reason'])
    if expired and not state['positions']:
        emit('TEST_RUN_COMPLETE', reason=state['stop_reason'], usd=state['usd'], realized_usd_change=number(state['usd'])-number(state['initial_usd']))
        return True
    if expired:
        hour = int(time.time()*1000)//HOUR; sig = {}
    else:
        hour, sig = signals()
        if state['last_hour'] >= hour:
            emit('TEST_ALREADY_PROCESSED_HOUR', hour=hour); return False
        if int(time.time()*1000)//HOUR != hour: raise ValueError('Hour changed while loading signals')
        if time.time()*1000-hour*HOUR > 10*60000:
            state['last_hour'] = hour; store.save(state, 'LATE_HOUR_SKIPPED', {'hour':hour})
            emit('TEST_LATE_HOUR_SKIPPED', hour=hour); return False
    actions = []
    # Existing positions are evaluated before any entries, so an entry failure cannot delay an exit.
    ordered = sorted(PAIRS, key=lambda p: p not in state['positions'])
    for pair in ordered:
        state = store.load()
        if store.already(hour, pair, expired): continue
        action = choose(state, pair, hour, sig.get(pair), expired)
        if not action: continue
        match_account(state, account(client))
        info, ticks = public_market(client)
        if not expired and (time.time()*1000-hour*HOUR > 10*60000 or time.time() >= state['expires']):
            raise ValueError('Decision window ended; retry next cycle')
        plan = plan_order(state, pair, action, info, ticks, hour)
        intent = str(hour)+':'+pair+(':STOP' if expired else '')
        submit(client, store, intent, plan)
        actions.append({'pair':pair,'action':action})
    state = store.load(); state['last_hour'] = hour
    store.save(state, 'CYCLE', {'hour':hour,'actions':actions,'signals':sig})
    emit('TEST_CYCLE_COMPLETE', hour=hour, actions=actions, usd_free=state['usd'], open_positions=state['positions'])
    complete = expired and not state['positions']
    if complete:
        emit('TEST_RUN_COMPLETE', reason=state['stop_reason'], usd=state['usd'],
             realized_usd_change=number(state['usd'])-number(state['initial_usd']))
    return complete


def credentials(path):
    if os.name != 'nt' and path.stat().st_mode & 0o077: raise Blocked('Credentials file must have mode 600')
    cfg = json.loads(path.read_text())
    if cfg.get('purpose') != 'TESTING' or not cfg.get('key') or not cfg.get('secret'):
        raise Blocked('Expected explicitly labeled TESTING credentials')
    return Client(cfg['key'], cfg['secret'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('preview','initialize-testing','check-testing','execute-testing','report'):
        modes.add_argument('--'+name, action='store_true')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--credentials', type=Path, default=Path.home()/'.config/dynamic-profits/testing.json')
    args = parser.parse_args()
    if args.watch and not args.execute_testing: parser.error('--watch requires --execute-testing')
    try:
        if args.preview:
            hour, sig = signals(); public_market(Client())
            emit('PUBLIC_PREVIEW_ONLY', hour=hour, signals=sig, target_usd_per_pair=10, entry_hour_utc=0)
            return
        if args.report:
            with closing(sqlite3.connect((ROOT/'execution.sqlite3').resolve().as_uri()+'?mode=ro', uri=True)) as db:
                for row in db.execute('SELECT utc,kind,body FROM events ORDER BY id DESC LIMIT 20'):
                    emit('TEST_EVENT', utc=row[0], kind=row[1], detail=json.loads(row[2]))
                for row in db.execute("SELECT id,status,response FROM attempts WHERE status!='APPLIED'"):
                    emit('UNRESOLVED', intent=row[0], state=row[1], response=json.loads(row[2]) if row[2] else None)
            return
        client = credentials(args.credentials)
        with process_lock(ROOT/'controller.lock'):
            store = Store()
            if args.initialize_testing: initialize(client, store); return
            if args.check_testing:
                state = store.load(); identity(client, state)
                if store.pending(): raise Blocked('Unresolved intent; inspect --report')
                match_account(state, account(client))
                emit('TEST_ACCOUNT_RECONCILED', usd=state['usd'], positions=state['positions'], expires_utc=datetime.fromtimestamp(state['expires'], timezone.utc).isoformat())
                return
            while True:
                try:
                    if cycle(client, store): return
                except Blocked: raise
                except Exception as exc:
                    emit('TEST_DATA_ERROR', error_type=type(exc).__name__, error=str(exc))
                    if not args.watch: raise SystemExit(1)
                    time.sleep(60); continue
                if not args.watch: break
                time.sleep(max(1, min(3600-time.time()%3600+120, store.load()['expires']-time.time())))
    except Blocked as exc:
        emit('TEST_HALTED_RECONCILE_REQUIRED', error=str(exc)); raise SystemExit(78)


if __name__ == '__main__': main()
