"""V3 autonomous controller: unified allocation, guarded rotation, drawdown recovery.
Preview is public-only. Competition market execution and testing execution are separate modes.
"""
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
from .feed import refresh, HOUR, SYMBOLS
from .api import Client, dec, spot_plan, NotSent
from .strategy import (features, candidates, gross_exposure, risk_multiplier, paused, sleeve_target, book,
                       stop_distance, open_risk, rotation, budget_for, entry_reason)
from .config import validate
from .limit_orders import settle

PAIRS = tuple(x+'/USD' for x in json.loads(Path(__file__).with_name('policy.json').read_text())['universe'])
ROOT = Path('data/competition')
CONFIG = Path(__file__).with_name('policy.json')
VERSION = 'competition-controller-3'


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
    return hashlib.sha256(b''.join(Path(__file__).with_name(p).read_bytes().replace(b'\r\n', b'\n') for p in
                         ('controller.py', 'api.py', 'feed.py', 'policy.json', 'strategy.py','config.py','limit_orders.py'))).hexdigest()


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
        if not row: raise Blocked('Initialize the competition controller first')
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

    def exists(self, intent):
        with closing(sqlite3.connect(self.path)) as db:
            return bool(db.execute('SELECT 1 FROM attempts WHERE id=?', (intent,)).fetchone())

    def hour_count(self,hour):
        with closing(sqlite3.connect(self.path)) as db:
            return sum(json.loads(r[0]).get('hour')==hour for r in db.execute('SELECT plan FROM attempts'))

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
        if asset not in ('USD',)+tuple(p.split('/')[0] for p in PAIRS) and any(number(row.get(k, 0)) != 0 for k in ('Free', 'Lock', 'ShortCollateral')):
            raise Blocked('Unexpected asset in competition account')
    return {'spot':spot, 'shorts':shorts['Positions'], 'usd':str(number(spot['USD']['Free']))}


def match_account(state, observed):
    near(observed['usd'], state['usd'])
    if any(p not in PAIRS for p in state['positions']): raise Blocked('Unrecognized recorded position')
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
    if state.get('version')!=VERSION: raise Blocked('Journal version needs migration')
    if state['fingerprint'] != fingerprint(): raise Blocked('Code changed; preserve journal and review migration')


def policy(now):
    try: return validate(raw_policy(),now)
    except (ValueError, KeyError, ArithmeticError, TypeError) as exc: raise Blocked(str(exc))


LADDER_DEFAULTS = dict(pause_until=-1, recover_until=-1, flatten_pending=False, sleeves={}, last_fill_day=None, last_rotation_hour=-1000000, rotation_day=-1, rotation_count=0)


def local_day(seconds, cfg):
    return int(seconds + cfg['guard']['utc_offset_hours']*3600)//86400


def initialize(client, store, now=None, purpose="COMPETITION"):
    now = time.time() if now is None else now
    with closing(sqlite3.connect(store.path)) as db:
        if db.execute('SELECT 1 FROM state').fetchone() or db.execute('SELECT 1 FROM attempts').fetchone():
            raise Blocked('Already initialized; do not reset the execution database')
    observed = account(client)
    if purpose not in ('COMPETITION','TESTING'): raise Blocked('Unknown account purpose')
    if purpose == 'COMPETITION' and not Decimal('99900') <= number(observed['usd']) <= Decimal('100100'):
        raise Blocked('Expected unused official competition wallet near USD 100000; testing credentials must not be used')
    cfg = policy(now)
    if number(observed['usd']) <= 100: raise Blocked('Account balance too small')
    state = dict(account=hashlib.sha256(client.key.encode()).hexdigest(), fingerprint=fingerprint(),
                 version=VERSION, purpose=purpose, risk_peak=observed['usd'], usd=observed['usd'], initial_usd=observed['usd'], positions={},
                 last_exit={}, last_hour=-1, started=now, expires=cfg['end_epoch'],
                 budget=str(number(observed['usd'])*number(cfg['position_fraction'])),
                 peak_equity=observed['usd'], last_equity=observed['usd'], stop_reason=None,
                 **LADDER_DEFAULTS)
    match_account(state, observed)
    store.save(state, 'INITIALIZED', {'usd':state['usd'], 'expires':state['expires']})
    emit('V3_CONTROLLER_INITIALIZED', purpose=purpose, usd=state['usd'], scheduled_end=state['expires'], target_usd_per_pair=state['budget'],
         note='No orders placed. Account purpose is user-confirmed, not identified by the API.')


def public_market(client, required=()):
    info = client.request('/v3/exchangeInfo')
    stamp = client.request('/v3/serverTime')['ServerTime']
    ticks = client.request('/v3/ticker', {'timestamp':str(stamp)})
    if info.get('IsRunning') is not True or ticks.get('Success') is not True:
        raise ValueError('Public market unavailable')
    if abs(int(ticks['ServerTime'])-int(time.time()*1000)) > 60000:
        raise ValueError('Stale quotes or incorrect machine clock')
    for pair in required:
        quote = ticks['Data'][pair]
        if dec(quote['MaxBid']) > dec(quote['MinAsk']): raise ValueError('Crossed quote')
        if info['TradePairs'][pair].get('CanTrade') is not True: raise ValueError('Pair not tradable')
    return info, ticks


def signals(path=None):
    path = path or ROOT/'candles_v3.sqlite3'
    boundary,batches,failures=refresh(path,tuple(p.split('/')[0]+'USDT' for p in PAIRS))
    result={}
    for symbol,rows in batches.items():
        try: result[symbol[:-4]+'/USD']=features(rows,regime=(symbol=='BTCUSDT'))
        except (ValueError,ArithmeticError,KeyError) as exc: failures[symbol]=str(exc)
    emit('UNIVERSE_REFRESH', hour=boundary//HOUR, ready=len(result), skipped=failures)
    return boundary//HOUR,result


def raw_policy():
    """Policy values for bookkeeping paths that must keep working after the round deadline."""
    return json.loads(CONFIG.read_text())


def in_window(hour):
    now = time.time()*1000
    return int(now)//HOUR == hour and now-hour*HOUR <= 15*60000


def close_action(pos):
    return 'SELL' if pos['direction'] == 1 else 'SHORT_CLOSE'


def choose(state, pair, hour, signal, expired=False):
    """Exit decision for a held position. Sleeve positions exit only via their sleeve or a stop."""
    pos = state['positions'].get(pair)
    if not pos: return None
    if expired: return close_action(pos)
    if pos.get('probe_until') is not None and hour >= int(pos['probe_until']): return close_action(pos)
    if book(pos) != 'core' or signal is None: return None
    mom = number(signal['momentum']); close = dec(signal['close'])
    if pos['direction']*(close/dec(pos['entry'])-1) <= Decimal('-.08'): return close_action(pos)
    if pos['direction']*mom <= 0: return close_action(pos)
    return None


def plan_order(state, pair, action, info, ticks, hour, budget_override=None, owner='core', probe_until=None, trim_qty=None, signal=None, reason=None):
    budget = dec(budget_override or state['budget'])
    quote = ticks['Data'][pair]
    if pair not in PAIRS or action not in ('BUY','SELL','SHORT_OPEN','SHORT_CLOSE','TRIM','SHORT_TRIM'): raise Blocked('Invalid order plan')
    if info.get('IsRunning') is not True or info['TradePairs'][pair].get('CanTrade') is not True: raise ValueError('Market unavailable')
    if dec(quote['MaxBid'])>dec(quote['MinAsk']): raise ValueError('Crossed quote')
    if action in ('BUY', 'SHORT_OPEN'):
        if pair in state['positions']: raise Blocked('Exposure slot unavailable')
        cfg=policy(time.time())
        if state.get('stop_reason') or state.get('flatten_pending') or paused(state,hour):
            raise Blocked('Entry attempted during risk pause')
        if len(state['positions'])>=cfg['max_positions']: raise ValueError('Position limit')
        if signal is None: raise Blocked('Validated signal required for every entry')
        budget=min(budget,budget_for(state,signal,ticks,cfg,owner,risk_multiplier(state,cfg,hour)))
        budget=budget.quantize(Decimal('.01'),rounding=ROUND_DOWN)
        if budget<25: raise ValueError('Portfolio exposure or cash cap; entry deferred')
        if (dec(quote['MinAsk'])/dec(quote['MaxBid'])-1) > number(cfg['max_spread']):
            raise ValueError('Spread exceeds configured entry limit')
    if action in ('BUY', 'SELL'):
        amount = str(budget) if action == 'BUY' else state['positions'][pair]['quantity']
        plan = spot_plan(info, quote, pair, action, amount)
    elif action == 'TRIM':
        pos = state['positions'][pair]
        if pos['direction'] != 1 or trim_qty is None or dec(trim_qty) >= dec(pos['quantity']):
            raise Blocked('Invalid trim')
        plan = spot_plan(info, quote, pair, 'SELL', trim_qty)
    elif action == 'SHORT_OPEN':
        precision = info['TradePairs'][pair]['AmountPrecision']
        if type(precision) is not int or not 0 <= precision <= 12: raise Blocked('Invalid amount precision')
        step = Decimal(1).scaleb(-precision)
        qty = (budget/dec(quote['MaxBid'])).quantize(step, rounding=ROUND_DOWN)
        if qty <= 0 or qty*dec(quote['MaxBid']) <= dec(info['TradePairs'][pair]['MiniOrder']):
            raise ValueError('Short below exchange minimum')
        plan = {'endpoint':'/v6/short_open', 'params':{'pair':pair, 'collateral':str(budget)}}
    elif action == 'SHORT_TRIM':
        pos=state['positions'][pair]
        if pos['direction']!=-1 or trim_qty is None or not Decimal(0)<dec(trim_qty)<dec(pos['quantity']): raise Blocked('Invalid short trim')
        step=Decimal(1).scaleb(-info['TradePairs'][pair]['AmountPrecision'])
        qty=dec(trim_qty).quantize(step,rounding=ROUND_DOWN)
        if qty<=0:raise ValueError('Short trim rounds to zero')
        plan={'endpoint':'/v6/short_close','params':{'pair':pair,'close_qty':str(qty)}}
    else:
        plan = {'endpoint':'/v6/short_close', 'params':{'pair':pair, 'close_qty':state['positions'][pair]['quantity']}}
    if action=='BUY' and raw_policy()['execution']['entry_type']=='LIMIT_TEST_ONLY':
        if state.get('purpose')!='TESTING': raise Blocked('Limit execution is testing-only in this release')
        rules=info['TradePairs'][pair]; prec=rules.get('PricePrecision')
        if type(prec) is not int or not 0<=prec<=12: raise Blocked('Invalid price precision')
        price=dec(quote['MaxBid']).quantize(Decimal(1).scaleb(-prec),rounding=ROUND_DOWN)
        qty=(budget/(price*Decimal('1.001'))).quantize(Decimal(1).scaleb(-rules['AmountPrecision']),rounding=ROUND_DOWN)
        if qty<=0 or qty*price<=dec(rules['MiniOrder']): raise ValueError('Limit below minimum')
        plan['params']=dict(pair=pair,side='BUY',type='LIMIT',quantity=str(qty),price=str(price))
        plan['created_utc']=time.time();plan['limit_timeout_seconds']=raw_policy()['execution']['limit_timeout_seconds']
    held = state['positions'].get(pair)
    plan.update(pair=pair, action=action, hour=hour, budget=str(budget), precision=info['TradePairs'][pair]['AmountPrecision'],
                book=owner if action in ('BUY','SHORT_OPEN') else book(held), probe_until=probe_until, reason=reason)
    if action in ('BUY','SHORT_OPEN'):plan['stop_fraction']=str(stop_distance(signal,raw_policy()))
    return plan


def reconcile(client, store, intent, plan, response, terminal=None):
    state = store.load(); identity(client, state)
    after = json.loads(json.dumps(state))
    pair, action = plan['pair'], plan['action']
    usd = number(state['usd']); receipt = response
    budget = dec(plan.get('budget',state['budget']))
    owner = plan.get('book', 'core')
    def opened(direction, qty, price, collateral, ident):
        pos = dict(direction=direction, quantity=str(qty), entry=str(price), collateral=str(collateral), id=ident,
                   book=owner, opened_hour=plan['hour'], stop_fraction=plan.get('stop_fraction','0.08'),
                   extreme_price=str(price), partial_taken=False)
        if plan.get('probe_until') is not None: pos['probe_until'] = int(plan['probe_until'])
        after['positions'][pair] = pos
    if action in ('BUY', 'SELL', 'TRIM'):
        side = 'SELL' if action == 'TRIM' else action
        order_id = response.get('OrderDetail', {}).get('OrderID')
        if order_id is None: raise Blocked('Acknowledged spot order missing its ID')
        if terminal is None:
            queried=client.request('/v3/query_order',{'order_id':str(order_id)},method='POST',signed=True)
            found=[x for x in queried.get('OrderMatched',[]) if str(x.get('OrderID'))==str(order_id)] if queried.get('Success') is True else []
            if len(found)!=1:raise Blocked('Spot order cannot be found; no resubmission')
            receipt=found[0]
        else:receipt=terminal
        typ=plan['params']['type']; limit=typ=='LIMIT'
        if any(receipt.get(k)!=v for k,v in [('Pair',pair),('Side',side),('Type',typ)]):raise Blocked('Spot fill identity mismatch')
        if receipt.get('Status') not in (('FILLED','CANCELED','CANCELLED','REJECTED','EXPIRED') if limit else ('FILLED',)):
            raise Blocked('Spot fill not terminal')
        qty=number(receipt.get('FilledQuantity',0));requested=dec(plan['params']['quantity'])
        if dec(receipt['Quantity'])!=requested or not 0<=qty<=requested or (not limit and qty!=requested):raise Blocked('Unexpected spot quantity')
        if receipt.get('Status')=='FILLED' and qty!=requested:raise Blocked('FILLED quantity differs from requested amount')
        if qty==0:
            if number(receipt.get('CommissionChargeValue',0))!=0:raise Blocked('Fee without a fill')
            match_account(after,account(client))
            store.save(after,'ORDER_CANCELED_UNFILLED',{'intent':intent,'order_id':order_id},applied=intent)
            return
        price=dec(receipt['FilledAverPrice']);fee=number(receipt['CommissionChargeValue'])
        if receipt.get('CommissionCoin')!='USD' or fee<0:raise Blocked('Unsupported commission asset or fee')
        if limit and price>dec(plan['params']['price']):raise Blocked('Buy filled above limit')
        if action == 'BUY':
            if qty*price+fee > budget*Decimal('1.02'): raise Blocked('Actual buy cost exceeded configured allowance; reconcile exposure')
            usd -= qty*price+fee
            opened(1, qty, price, '0', str(order_id))
        elif action == 'TRIM':
            held = dec(state['positions'][pair]['quantity'])
            if qty >= held: raise Blocked('Trim quantity not below holding')
            usd += qty*price-fee
            after['positions'][pair]['quantity'] = str(held-qty)
            after['positions'][pair]['partial_taken'] = True
        else:
            if qty != dec(state['positions'][pair]['quantity']): raise Blocked('Sell quantity mismatch')
            usd += qty*price-fee
            del after['positions'][pair]; after['last_exit'][pair] = plan['hour']
    elif action == 'SHORT_OPEN':
        if response.get('Status') != 'OPEN' or response.get('Pair') != pair or response.get('ID') is None:
            raise Blocked('Short opening identity/status mismatch')
        qty = dec(response['ShortQty']); price = dec(response['EntryPrice']); collateral = dec(response['Collateral']); fee = number(response['OpenFee'])
        step = Decimal(1).scaleb(-plan['precision'])
        if qty != (budget/price).quantize(step, rounding=ROUND_DOWN) or collateral > budget or fee < 0:
            raise Blocked('Unexpected short size')
        if collateral != budget and abs(collateral-qty*price) > Decimal('.01'):
            raise Blocked('Short collateral differs from notional')
        near(fee, qty*price*Decimal('.001'), Decimal('.000002'))
        usd -= collateral+fee
        opened(-1, qty, price, collateral, str(response['ID']))
    else:
        pos=state['positions'][pair];held=dec(pos['quantity']);collateral=dec(pos['collateral'])
        qty=dec(response['ClosedQty']);full=response.get('FullyClosed')
        if type(full) is not bool or not 0<qty<=held:raise Blocked('Unexpected short close quantity')
        requested=dec(plan['params']['close_qty'])
        if action=='SHORT_CLOSE' and (not full or qty!=held):raise Blocked('Short not fully closed')
        if action=='SHORT_TRIM' and qty!=requested and not (full and qty==held):raise Blocked('Short trim quantity mismatch')
        price=dec(response['ClosePrice']);fee=number(response['CloseFee']);pnl=number(response['RealizedPNL']);returned=number(response['ReturnAmount'])
        released=collateral*qty/held
        if fee<0:raise Blocked('Invalid close fee')
        near(fee,qty*price*Decimal('.001'),Decimal('.000002'))
        near(pnl,max(qty*(dec(pos['entry'])-price),-released))
        near(returned,released+pnl-fee)
        usd+=returned
        if full:
            if qty!=held:raise Blocked('FullyClosed quantity mismatch')
            del after['positions'][pair];after['last_exit'][pair]=plan['hour']
        else:
            near(response['RemainingQty'],held-qty,Decimal('1e-10'))
            near(response['RemainingCollateral'],collateral-released)
            after['positions'][pair].update(quantity=str(held-qty),collateral=str(response['RemainingCollateral']),partial_taken=True)
    after['usd'] = str(usd)
    after['last_fill_day'] = local_day(time.time(), raw_policy())
    observed = account(client)
    match_account(after, observed)
    # Anchor subsequent cash calculations to exchange precision after bounded reconciliation.
    after['usd'] = observed['usd']
    store.save(after, 'FILL_RECONCILED', {'intent':intent, 'fill':receipt, 'usd':after['usd'], 'book':owner}, applied=intent)
    emit('COMPETITION_FILL_RECONCILED', intent=intent, action=action, pair=pair, book=owner, usd_free=after['usd'])


def recover_acknowledged(client, store):
    pending=store.pending()
    if len(pending)>1:raise Blocked('Multiple unresolved intents')
    for intent,status,encoded,response in pending:
        plan=json.loads(encoded); response=json.loads(response) if response else {}
        limit=plan['params'].get('type')=='LIMIT'
        if status not in (('ACK','CANCEL_SENDING','CANCEL_UNKNOWN','CANCEL_ACK') if limit else ('ACK',)):
            raise Blocked('Unresolved '+status+' intent '+intent+'; no write retry')
        terminal=settle(client,store,intent,plan,response,status,Blocked) if limit else None
        reconcile(client,store,intent,plan,response,terminal)


def submit(client, store, intent, plan):
    if plan['action'] in ('BUY','SHORT_OPEN') and not in_window(plan['hour']):
        raise ValueError('Decision window ended before transmission')
    store.reserve(intent,plan)
    try:
        deadline=plan['hour']*3600+900 if plan['action'] in ('BUY','SHORT_OPEN') else None
        response=client.request(plan['endpoint'],plan['params'],method='POST',signed=True,deadline=deadline)
    except NotSent:
        store.save(store.load(),'ORDER_NOT_SENT',{'intent':intent,'reason':'DECISION_DEADLINE'},applied=intent)
        raise ValueError('Entry expired before transmission')
    except Exception as exc:
        store.acknowledge(intent,'UNKNOWN',{'error_type':type(exc).__name__})
        raise Blocked('Write response lost; no resubmission')
    status='ACK' if response.get('Success') is True else 'REJECTED' if response.get('Success') is False else 'UNKNOWN'
    store.acknowledge(intent,status,response)
    if status!='ACK':raise Blocked('Write '+status+'; inspect journal')
    recover_acknowledged(client,store)


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


def risk_mark(client, store):
    state=store.load();_,ticks=public_market(client,tuple(state['positions']))
    cfg=raw_policy();lad=cfg['ladder'];equity=mark_equity(state,ticks);hour=int(time.time())//3600
    state['last_equity']=str(equity)
    state['peak_equity']=str(max(equity,number(state['peak_equity'])))  # lifetime peak NEVER reset
    state.setdefault('risk_peak',state['peak_equity'])
    if state['pause_until']>=0 and hour>=state['pause_until'] and not state['positions'] and not state['flatten_pending']:
        state['risk_peak']=str(equity);state['recover_until']=hour+lad['recover_hours'];state['pause_until']=-1
        emit('LADDER_PAUSE_ENDED',hour=hour,equity=str(equity))
    state['risk_peak']=str(max(equity,number(state['risk_peak'])))
    if state['pause_until']<0 and not state['flatten_pending'] and equity<=number(state['risk_peak'])*(1-dec(lad['hard'])):
        state['flatten_pending']=True;state['pause_until']=hour+lad['pause_hours']
    if state['expires'] is not None and time.time()>=state['expires']:state['stop_reason']='COMPETITION_DEADLINE'
    for pair,pos in state['positions'].items():
        q=ticks['Data'][pair];px=dec(q['MaxBid'] if pos['direction']==1 else q['MinAsk'])
        previous=number(pos.get('extreme_price',pos['entry']))
        pos['extreme_price']=str(max(previous,px) if pos['direction']==1 else min(previous,px))
    store.save(state,'EQUITY',dict(equity=str(equity),peak=state['peak_equity'],risk_peak=state['risk_peak'],
        stop_reason=state['stop_reason'],pause_until=state['pause_until'],flatten_pending=state['flatten_pending']))
    return state,ticks


def execute(client, store, intent, pair, action, hour, **kw):
    """Fresh account check + quotes, then one journaled write."""
    state=store.load()
    match_account(state,account(client))
    info,ticks=public_market(client,tuple(state['positions'])+((pair,) if pair not in state['positions'] else ()))
    if action in ('BUY','SHORT_OPEN'):
        cfg=policy(time.time());universe=kw.pop('universe_signals',None)
        if universe is None or pair not in universe:raise Blocked('Full signal context required')
        reason=entry_reason(pair,1 if action=='BUY' else -1,universe[pair],universe,info,ticks,cfg,state,hour)
        if reason:raise ValueError(reason)
        kw['signal']=universe[pair]
        count=store.hour_count(hour)
        if count>=cfg['execution']['max_writes_per_hour']:raise ValueError('Hourly turnover cap')
    submit(client,store,intent,plan_order(state,pair,action,info,ticks,hour,**kw))


def flatten(client, store, hour):
    for pair in list(store.load()['positions']):
        state=store.load()
        if pair not in state['positions'] or store.exists(str(hour)+':'+pair+':FLAT'): continue
        execute(client,store,str(hour)+':'+pair+':FLAT',pair,close_action(state['positions'][pair]),hour)
    state,_=risk_mark(client,store)
    if not state['positions']:
        state['flatten_pending']=False
        state['pause_until']=max(state['pause_until'],hour+raw_policy()['ladder']['pause_hours'])
        store.save(state,'LADDER_FLATTENED',dict(hour=hour,pause_until=state['pause_until']))


def update_targets(store,sig,cfg,hour):
    state=store.load()
    for sleeve in cfg['sleeves']:
        current=state['sleeves'].get(sleeve['name'])
        if current is None or hour>=int(current['hour'])+sleeve['rebalance_hours']:
            target=sleeve_target(sleeve,sig,PAIRS,state,risk_multiplier(state,cfg,hour))
            if target is not None:state['sleeves'][sleeve['name']]=dict(hour=hour,target=target)
    store.save(state,'SLEEVE_TARGETS',dict(hour=hour,targets=state['sleeves']))


def protective_exits(client,store,hour,ticks,cfg):
    for pair,pos in list(store.load()['positions'].items()):
        px=dec(ticks['Data'][pair]['MaxBid'] if pos['direction']==1 else ticks['Data'][pair]['MinAsk'])
        entry=dec(pos['entry']);direction=pos['direction'];dist=dec(pos.get('stop_fraction','0.08'))
        gain=direction*(px/entry-1);extreme=dec(pos.get('extreme_price',entry))
        reason='PRICE_STOP' if gain<=-dist else None
        if pos.get('probe_until') is not None and hour>=int(pos['probe_until']):reason='LEGACY_PROBE_EXPIRED'
        if cfg['risk']['atr_exits'] and pos.get('partial_taken'):
            # Breakeven covers estimated two-sided fee/slippage, not just entry price.
            be=2*(dec(cfg['execution']['fee'])+dec(cfg['execution']['slippage']))
            if gain<=be:reason='COST_BREAKEVEN'
            peak_gain=direction*(extreme/entry-1)
            if peak_gain>=dec(cfg['risk']['trail_at_r'])*dist and direction*(extreme-px)/entry>=dec(cfg['risk']['trail_r'])*dist:reason='TRAIL'
        tag=str(hour)+':'+pair+':PROTECT'
        if reason and not store.exists(tag):
            execute(client,store,tag,pair,close_action(pos),hour,reason=reason)
        elif cfg['risk']['atr_exits'] and not pos.get('partial_taken') and gain>=dec(cfg['risk']['partial_at_r'])*dist:
            tag=str(hour)+':'+pair+':TAKE_PARTIAL'
            if not store.exists(tag):
                try:execute(client,store,tag,pair,'TRIM' if direction==1 else 'SHORT_TRIM',hour,trim_qty=str(dec(pos['quantity'])/2),reason='PARTIAL_PROFIT')
                except ValueError:pass  # below minimum: retain, protective full exit remains available


def activity(store,cfg,hour):
    state=store.load();day=local_day(time.time(),cfg)
    if (hour+8)%24>=cfg['guard']['local_hour'] and state.get('last_fill_day')!=day and state.get('activity_warning_day')!=day:
        state['activity_warning_day']=day
        store.save(state,'ACTIVITY_WARNING',dict(day=day,note='No strategy fills today. No forced trade. Organizer eligibility is not guaranteed.'))
        emit('ACTIVITY_WARNING',note='No fills today; no forced trades')


def cycle(client,store):
    state=store.load();identity(client,state);recover_acknowledged(client,store)
    state=store.load();match_account(state,account(client));cfg=policy(time.time())
    state,ticks=risk_mark(client,store);hour=int(time.time())//3600
    if state['stop_reason'] or state['flatten_pending']:
        flatten(client,store,hour)
        state=store.load()
        if state['stop_reason'] and not state['positions']:
            emit('COMPETITION_RUN_COMPLETE',reason=state['stop_reason'],usd=state['usd']);return True
        return False
    protective_exits(client,store,hour,ticks,cfg)
    state,ticks=risk_mark(client,store)
    if state['flatten_pending']:flatten(client,store,hour);return False
    activity(store,cfg,hour)
    state=store.load()
    if state['last_hour']>=hour or not in_window(hour):
        emit('COMPETITION_RISK_CHECK',hour=hour,equity=state['last_equity']);return False
    signal_hour,sig=signals()
    if signal_hour!=hour or int(time.time())//3600!=hour:raise ValueError('Stale signal hour')
    update_targets(store,sig,cfg,hour);state=store.load();actions=[]
    # Exit rules always precede entries; they do not need the entry decision window.
    for pair,pos in list(state['positions'].items()):
        current=store.load()
        if pair not in current['positions'] or store.already(hour,pair):continue
        action=choose(current,pair,hour,sig.get(pair))
        owner=book(pos)
        if owner!='core' and owner in current['sleeves'] and pair in sig:
            target=current['sleeves'][owner]['target'].get(pair)
            if target is None or int(target['direction'])!=pos['direction']:action=close_action(pos)
        if action:
            execute(client,store,str(hour)+':'+pair,pair,action,hour,reason='SIGNAL_OR_SLEEVE_EXIT')
            actions.append(dict(pair=pair,action=action))
    state,ticks=risk_mark(client,store)
    if state['stop_reason'] or state['flatten_pending']:return False
    missing=[p for p in state['positions'] if p not in sig]
    if not missing and not paused(state,hour):
        info,ticks=public_market(client,tuple(state['positions']));mult=risk_multiplier(state,cfg,hour)
        picks,why=candidates(state,sig,info,ticks,cfg,hour,mult)
        # Prefer vacant capacity. Replace only when eligible opportunities cannot be admitted.
        rotate=rotation(state,sig,info,ticks,cfg,hour,mult) if not picks else None
        if rotate and in_window(hour):
            old=rotate['sell_pair'];new=rotate['buy_pair'];day=(hour+8)//24
            if not store.already(hour,old) and not store.already(hour,new):
                # Durable rotation decision BEFORE exit. Crash after exit never repeats it.
                state['last_rotation_hour']=hour
                state['rotation_count']=(state.get('rotation_count',0) if state.get('rotation_day')==day else 0)+1
                state['rotation_day']=day
                store.save(state,'ROTATION_DECISION',dict(hour=hour,**rotate))
                execute(client,store,str(hour)+':'+old,old,close_action(state['positions'][old]),hour,reason='PORTFOLIO_ROTATION')
                actions.append(dict(pair=old,action='ROTATION_EXIT'))
                state,ticks=risk_mark(client,store)
                info,ticks=public_market(client,tuple(state['positions']))
                picks,why=candidates(state,sig,info,ticks,cfg,hour,risk_multiplier(state,cfg,hour))
        store.save(store.load(),'SELECTION',dict(hour=hour,candidates=picks,skipped=why))
        for pick in picks:
            p=pick['pair'];state=store.load()
            if store.already(hour,p) or p in state['positions'] or not in_window(hour):continue
            state,_=risk_mark(client,store)
            if state['stop_reason'] or state['flatten_pending'] or paused(state,hour):break
            try:execute(client,store,str(hour)+':'+p,p,pick['action'],hour,budget_override=pick['budget'],owner=pick['book'],universe_signals=sig,reason='RANKED_ENTRY')
            except ValueError as exc:
                store.save(store.load(),'ENTRY_DEFERRED',dict(pair=p,reason=str(exc)));continue
            actions.append(dict(pair=p,action=pick['action'],book=pick['book']))
    state,_=risk_mark(client,store)
    if not missing:state['last_hour']=hour
    store.save(state,'CYCLE',dict(hour=hour,actions=actions,missing_held=missing))
    emit('COMPETITION_CYCLE_COMPLETE',hour=hour,equity=state['last_equity'],actions=actions,positions=state['positions'],missing_held=missing)
    return False


def credentials(path, purpose="COMPETITION"):
    if os.name != 'nt' and path.stat().st_mode & 0o077: raise Blocked('Credentials file must have mode 600')
    cfg = json.loads(path.read_text())
    if cfg.get('purpose') != purpose or not cfg.get('key') or not cfg.get('secret'):
        raise Blocked('Expected explicitly labeled COMPETITION credentials')
    other=Path.home()/'.config/dynamic-profits'/('testing.json' if purpose=='COMPETITION' else 'competition.json')
    if other.exists() and json.loads(other.read_text()).get('key')==cfg['key']:
        raise Blocked('Testing and competition files contain the same API key')
    return Client(cfg['key'], cfg['secret'])


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('preview','initialize-competition','check-competition','execute-competition','initialize-testing','execute-testing','report'):
        modes.add_argument('--'+name, action='store_true')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--credentials', type=Path, default=Path.home()/'.config/dynamic-profits/competition.json')
    args = parser.parse_args()
    if args.watch and not (args.execute_competition or args.execute_testing): parser.error('--watch requires --execute-competition')
    purpose='TESTING' if args.initialize_testing or args.execute_testing else 'COMPETITION'
    if purpose=='TESTING':
        ROOT=Path('data/v3_testing')
        if args.credentials==Path.home()/'.config/dynamic-profits/competition.json':args.credentials=Path.home()/'.config/dynamic-profits/testing.json'
    try:
        if args.preview:
            hour,sig=signals()
            if len(sig)<3: raise ValueError('Too few valid histories for deployment preview')
            info,ticks=public_market(Client()); cfg=policy(time.time())
            mock=dict(stop_reason=None,initial_usd='100000',last_equity='100000',peak_equity='100000',usd='100000',positions={},last_exit={},**LADDER_DEFAULTS)
            sleeves={s['name']:sleeve_target(s,sig,PAIRS,mock,Decimal(1)) for s in cfg['sleeves']}
            mock['sleeves']={n:dict(hour=hour,target=t) for n,t in sleeves.items() if t is not None}
            picks,reasons=candidates(mock,sig,info,ticks,cfg,hour)
            emit('PUBLIC_PREVIEW_ONLY',hour=hour,candidates=picks,skipped=reasons,sleeve_targets=sleeves,
                 histories={p:x['candles'] for p,x in sig.items()},
                 note='Hypothetical empty wallet. No credentials, no orders, no performance claim. '
                      'A null sleeve target means the 14/30-day history is unavailable.')
            return
        if args.report:
            with closing(sqlite3.connect((ROOT/'execution.sqlite3').resolve().as_uri()+'?mode=ro', uri=True)) as db:
                for row in db.execute('SELECT utc,kind,body FROM events ORDER BY id DESC LIMIT 20'):
                    emit('COMPETITION_EVENT', utc=row[0], kind=row[1], detail=json.loads(row[2]))
                for row in db.execute("SELECT id,status,response FROM attempts WHERE status!='APPLIED'"):
                    emit('UNRESOLVED', intent=row[0], state=row[1], response=json.loads(row[2]) if row[2] else None)
            return
        client = credentials(args.credentials,purpose)
        with process_lock(ROOT/'controller.lock'):
            store = Store(ROOT/'execution.sqlite3')
            if args.initialize_competition or args.initialize_testing: initialize(client,store,purpose=purpose);return
            if store.load().get('purpose','COMPETITION')!=purpose:raise Blocked('Journal account purpose mismatch')
            if args.check_competition:
                state = store.load(); identity(client, state)
                if store.pending(): raise Blocked('Unresolved intent; inspect --report')
                match_account(state, account(client))
                emit('COMPETITION_ACCOUNT_RECONCILED', usd=state['usd'], positions=state['positions'], expires_utc=datetime.fromtimestamp(state['expires'], timezone.utc).isoformat() if state['expires'] is not None else None)
                return
            while True:
                try:
                    if cycle(client, store): return
                except Blocked: raise
                except Exception as exc:
                    emit('COMPETITION_DATA_ERROR', error_type=type(exc).__name__, error=str(exc))
                    if not args.watch: raise SystemExit(1)
                    time.sleep(60); continue
                if not args.watch: break
                deadline = store.load()['expires']
                delay = min(300,3600-time.time()%3600+120)
                if store.load()['stop_reason'] or store.load()['flatten_pending']: delay=1
                if deadline is not None: delay = min(delay, deadline-time.time())
                time.sleep(max(1, delay))
    except Blocked as exc:
        emit('COMPETITION_HALTED_RECONCILE_REQUIRED', error=str(exc)); raise SystemExit(78)


if __name__ == '__main__': main()
