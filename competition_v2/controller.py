"""Autonomous competition controller. Preview and initialization place no orders."""
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
from .api import Client, dec, spot_plan
from .strategy import features, candidates, gross_exposure

PAIRS = tuple(x+'/USD' for x in json.loads(Path(__file__).with_name('policy.json').read_text())['universe'])
ROOT = Path('data/competition')
CONFIG = Path(__file__).with_name('policy.json')
VERSION = 'competition-controller-2'


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
                         ('controller.py', 'api.py', 'feed.py', 'policy.json', 'strategy.py'))).hexdigest()


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
    if state['fingerprint'] != fingerprint(): raise Blocked('Code changed; preserve journal and review migration')


def policy(now):
    cfg = json.loads(CONFIG.read_text())
    if set(cfg) != {'position_fraction','drawdown_fraction','end_utc','gross_fraction','max_positions','daily_vol_target','min_quote_volume_24h','max_spread','max_correlation','universe'}:
        raise Blocked('Unexpected competition policy fields')
    fraction = dec(cfg['position_fraction']); drawdown = dec(cfg['drawdown_fraction'])
    if fraction > Decimal('.20') or drawdown > Decimal('.10'):
        raise Blocked('Policy exceeds release sizing limits')
    if dec(cfg['gross_fraction']) > Decimal('.30') or not isinstance(cfg['max_positions'], int) or not 1 <= cfg['max_positions'] <= 3:
        raise Blocked('Gross exposure or position limit exceeds release cap')
    if dec(cfg['daily_vol_target']) > Decimal('.03') or dec(cfg['max_spread']) > Decimal('.005') or dec(cfg['max_correlation']) >= 1:
        raise Blocked('Invalid volatility/spread/correlation policy')
    dec(cfg['min_quote_volume_24h'])
    if not isinstance(cfg['universe'],list) or len(cfg['universe'])!=len(set(cfg['universe'])) or not 3<=len(cfg['universe'])<=20:
        raise Blocked('Invalid universe')
    if any(not isinstance(x,str) or not x.isalnum() or not x.isupper() for x in cfg['universe']):
        raise Blocked('Invalid asset mapping')
    end = cfg['end_utc']
    epoch = None
    if end is not None:
        parsed = datetime.fromisoformat(end.replace('Z','+00:00'))
        if parsed.tzinfo is None: raise Blocked('Round end needs explicit timezone')
        epoch = parsed.timestamp()
        if epoch <= now: raise Blocked('Round end is in the past')
    return dict(cfg, end_epoch=epoch)


def initialize(client, store, now=None):
    now = time.time() if now is None else now
    with closing(sqlite3.connect(store.path)) as db:
        if db.execute('SELECT 1 FROM state').fetchone() or db.execute('SELECT 1 FROM attempts').fetchone():
            raise Blocked('Already initialized; do not reset the execution database')
    observed = account(client)
    if not Decimal('99900') <= number(observed['usd']) <= Decimal('100100'):
        raise Blocked('Expected unused official competition wallet near USD 100000; testing credentials must not be used')
    cfg = policy(now)
    state = dict(account=hashlib.sha256(client.key.encode()).hexdigest(), fingerprint=fingerprint(),
                 version=VERSION, usd=observed['usd'], initial_usd=observed['usd'], positions={},
                 last_exit={}, last_hour=-1, started=now, expires=cfg['end_epoch'],
                 budget=str(number(observed['usd'])*number(cfg['position_fraction'])),
                 drawdown_usd=str(number(observed['usd'])*number(cfg['drawdown_fraction'])),
                 peak_equity=observed['usd'], last_equity=observed['usd'], stop_reason=None)
    match_account(state, observed)
    store.save(state, 'INITIALIZED', {'usd':state['usd'], 'expires':state['expires']})
    emit('COMPETITION_CONTROLLER_INITIALIZED', usd=state['usd'], scheduled_end=state['expires'], target_usd_per_pair=state['budget'],
         drawdown_trigger_usd=state['drawdown_usd'],
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


def signals(path=ROOT/'candles_v2.sqlite3'):
    boundary,batches,failures=refresh(path,tuple(p.split('/')[0]+'USDT' for p in PAIRS))
    result={symbol[:-4]+'/USD':features(rows) for symbol,rows in batches.items()}
    emit('UNIVERSE_REFRESH', hour=boundary//HOUR, ready=len(result), skipped=failures)
    return boundary//HOUR,result


def choose(state, pair, hour, signal, expired=False):
    pos = state['positions'].get(pair)
    if pos:
        if expired: return 'SELL' if pos['direction'] == 1 else 'SHORT_CLOSE'
        if signal is None: return None
        mom = number(signal['momentum']); close = dec(signal['close'])
        if pos['direction']*mom <= 0 or pos['direction']*(close/dec(pos['entry'])-1) <= Decimal('-.08'):
            return 'SELL' if pos['direction'] == 1 else 'SHORT_CLOSE'
    elif not expired and hour-int(state['last_exit'].get(pair, -10000000)) >= 12:
        mom = number(signal['momentum'])
        if mom > Decimal('.01'): return 'BUY'
        if mom < Decimal('-.01'): return 'SHORT_OPEN'
    return None


def plan_order(state, pair, action, info, ticks, hour, budget_override=None):
    budget = dec(budget_override or state['budget'])
    quote = ticks['Data'][pair]
    if pair not in PAIRS or action not in ('BUY','SELL','SHORT_OPEN','SHORT_CLOSE'): raise Blocked('Invalid order plan')
    if info.get('IsRunning') is not True or info['TradePairs'][pair].get('CanTrade') is not True: raise ValueError('Market unavailable')
    if dec(quote['MaxBid'])>dec(quote['MinAsk']): raise ValueError('Crossed quote')
    if action in ('BUY', 'SHORT_OPEN'):
        if pair in state['positions'] or len(state['positions']) >= 3: raise Blocked('Exposure slot unavailable')
        cfg=policy(time.time())
        equity=min(number(state['initial_usd']),number(state['last_equity']))
        room=max(Decimal(0),equity*number(cfg['gross_fraction'])-gross_exposure(state,ticks))
        budget=min(budget,equity*number(cfg['position_fraction']),room/Decimal('1.02'),
                   max(Decimal(0),number(state['usd'])-1)/Decimal('1.02')).quantize(Decimal('.01'),rounding=ROUND_DOWN)
        if budget<25: raise ValueError('Portfolio exposure or cash cap; entry deferred')
        if (dec(quote['MinAsk'])/dec(quote['MaxBid'])-1) > number(policy(time.time())['max_spread']):
            raise ValueError('Spread exceeds configured entry limit')
    if action in ('BUY', 'SELL'):
        amount = str(budget) if action == 'BUY' else state['positions'][pair]['quantity']
        plan = spot_plan(info, quote, pair, action, amount)
    elif action == 'SHORT_OPEN':
        precision = info['TradePairs'][pair]['AmountPrecision']
        if type(precision) is not int or not 0 <= precision <= 12: raise Blocked('Invalid amount precision')
        step = Decimal(1).scaleb(-precision)
        qty = (budget/dec(quote['MaxBid'])).quantize(step, rounding=ROUND_DOWN)
        if qty <= 0 or qty*dec(quote['MaxBid']) <= dec(info['TradePairs'][pair]['MiniOrder']):
            raise ValueError('Short below exchange minimum')
        plan = {'endpoint':'/v6/short_open', 'params':{'pair':pair, 'collateral':str(budget)}}
    else:
        plan = {'endpoint':'/v6/short_close', 'params':{'pair':pair, 'close_qty':state['positions'][pair]['quantity']}}
    plan.update(pair=pair, action=action, hour=hour, budget=str(budget), precision=info['TradePairs'][pair]['AmountPrecision'])
    return plan


def reconcile(client, store, intent, plan, response):
    state = store.load(); identity(client, state)
    after = json.loads(json.dumps(state))
    pair, action = plan['pair'], plan['action']
    usd = number(state['usd']); receipt = response
    budget = dec(plan.get('budget',state['budget']))
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
            if qty*price+fee > budget*Decimal('1.02'): raise Blocked('Actual buy cost exceeded configured allowance; reconcile exposure')
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
        if qty != (budget/price).quantize(step, rounding=ROUND_DOWN) or collateral > budget or fee < 0:
            raise Blocked('Unexpected short size')
        if collateral != budget and abs(collateral-qty*price) > Decimal('.01'):
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
    emit('COMPETITION_FILL_RECONCILED', intent=intent, action=action, pair=pair, usd_free=after['usd'])


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


def risk_mark(client, store):
    state=store.load()
    _,ticks=public_market(client,tuple(state['positions']))
    equity=mark_equity(state,ticks)
    state['last_equity']=str(equity)
    state['peak_equity']=str(max(equity,number(state['peak_equity'])))
    if number(state['peak_equity'])-equity>=dec(state['drawdown_usd']) and not state['stop_reason']:
        state['stop_reason']='PORTFOLIO_DRAWDOWN'
    if state['expires'] is not None and time.time()>=state['expires'] and not state['stop_reason']:
        state['stop_reason']='COMPETITION_DEADLINE'
    store.save(state,'EQUITY',dict(equity=str(equity),peak=state['peak_equity'],stop_reason=state['stop_reason']))
    return state,ticks


def cycle(client, store):
    state=store.load(); identity(client,state)
    recover_acknowledged(client,store)
    state=store.load(); match_account(state,account(client))
    state,risk_ticks=risk_mark(client,store)
    hour=int(time.time()*1000)//HOUR
    if state['stop_reason']:
        # Drawdown/deadline exits do not depend on Binance or an entry window.
        for pair in list(state['positions']):
            state=store.load()
            if store.already(hour,pair,True): continue
            match_account(state,account(client))
            info,ticks=public_market(client,tuple(state['positions']))
            action=choose(state,pair,hour,None,True)
            submit(client,store,str(hour)+':'+pair+':STOP',plan_order(state,pair,action,info,ticks,hour))
        state,_=risk_mark(client,store)
        if not state['positions']:
            emit('COMPETITION_RUN_COMPLETE',reason=state['stop_reason'],usd=state['usd'])
            return True
        return False
    if state['last_hour']>=hour:
        emit('COMPETITION_RISK_CHECK',hour=hour,equity=state['last_equity'])
        return False
    if time.time()*1000-hour*HOUR>15*60000:
        emit('COMPETITION_LATE_HOUR_SKIPPED',hour=hour)
        return False
    signal_hour,sig=signals()
    if signal_hour!=hour or int(time.time()*1000)//HOUR!=hour:
        raise ValueError('Hour changed while collecting candles')
    actions=[]; missing=[p for p in state['positions'] if p not in sig]
    # Every available held position gets its exit decision before new entries.
    for pair in list(state['positions']):
        state=store.load()
        if store.already(hour,pair): continue
        action=choose(state,pair,hour,sig.get(pair))
        if action:
            match_account(state,account(client))
            info,ticks=public_market(client,tuple(state['positions']))
            if int(time.time()*1000)//HOUR!=hour: raise ValueError('Stale exit signal')
            submit(client,store,str(hour)+':'+pair,plan_order(state,pair,action,info,ticks,hour))
            actions.append(dict(pair=pair,action=action))
    # Re-mark after exits; honor a newly latched drawdown before admitting entries.
    state,_=risk_mark(client,store)
    if state['stop_reason']: return False
    info,ticks=public_market(client,tuple(state['positions']))
    picks,reasons=candidates(state,sig,info,ticks,policy(time.time()),hour) if not missing else ([],{'portfolio':'HELD_HISTORY_UNAVAILABLE'})
    store.save(state,'SELECTION',dict(hour=hour,candidates=picks,skipped=reasons,missing_held=missing))
    for pick in picks:
        pair=pick['pair']; state=store.load()
        if store.already(hour,pair): continue
        if int(time.time()*1000)//HOUR!=hour or time.time()*1000-hour*HOUR>15*60000:
            break
        match_account(state,account(client))
        state,_=risk_mark(client,store)
        if state['stop_reason']: break
        info,ticks=public_market(client,tuple(state['positions']))
        try:
            plan=plan_order(state,pair,pick['action'],info,ticks,hour,pick['budget'])
        except ValueError as exc:
            store.save(state,'ENTRY_DEFERRED',dict(pair=pair,reason=str(exc))); continue
        # Account/public reads can consume the remainder of a decision window.
        if int(time.time()*1000)//HOUR!=hour or time.time()*1000-hour*HOUR>15*60000: break
        submit(client,store,str(hour)+':'+pair,plan)
        actions.append(dict(pair=pair,action=pick['action'],budget=pick['budget']))
    state,_=risk_mark(client,store)
    if not missing: state['last_hour']=hour
    store.save(state,'CYCLE',dict(hour=hour,actions=actions,missing_held=missing,
               signals={p:{k:v for k,v in x.items() if k!='returns'} for p,x in sig.items()}))
    emit('COMPETITION_CYCLE_COMPLETE',hour=hour,actions=actions,usd_free=state['usd'],
         equity=state['last_equity'],open_positions=state['positions'],missing_held=missing)
    return False


def credentials(path):
    if os.name != 'nt' and path.stat().st_mode & 0o077: raise Blocked('Credentials file must have mode 600')
    cfg = json.loads(path.read_text())
    if cfg.get('purpose') != 'COMPETITION' or not cfg.get('key') or not cfg.get('secret'):
        raise Blocked('Expected explicitly labeled COMPETITION credentials')
    test_path = Path.home()/'.config/dynamic-profits/testing.json'
    if test_path.exists() and json.loads(test_path.read_text()).get('key') == cfg['key']:
        raise Blocked('Competition key matches the saved testing key')
    return Client(cfg['key'], cfg['secret'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('preview','initialize-competition','check-competition','execute-competition','report'):
        modes.add_argument('--'+name, action='store_true')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--credentials', type=Path, default=Path.home()/'.config/dynamic-profits/competition.json')
    args = parser.parse_args()
    if args.watch and not args.execute_competition: parser.error('--watch requires --execute-competition')
    try:
        if args.preview:
            hour,sig=signals()
            if len(sig)<3: raise ValueError('Too few valid histories for deployment preview')
            info,ticks=public_market(Client()); cfg=policy(time.time())
            mock=dict(initial_usd='100000',last_equity='100000',usd='100000',positions={},last_exit={})
            picks,reasons=candidates(mock,sig,info,ticks,cfg,hour)
            emit('PUBLIC_PREVIEW_ONLY',hour=hour,candidates=picks,skipped=reasons,
                 note='Hypothetical empty wallet. No credentials, no orders, no performance claim.')
            return
        if args.report:
            with closing(sqlite3.connect((ROOT/'execution.sqlite3').resolve().as_uri()+'?mode=ro', uri=True)) as db:
                for row in db.execute('SELECT utc,kind,body FROM events ORDER BY id DESC LIMIT 20'):
                    emit('COMPETITION_EVENT', utc=row[0], kind=row[1], detail=json.loads(row[2]))
                for row in db.execute("SELECT id,status,response FROM attempts WHERE status!='APPLIED'"):
                    emit('UNRESOLVED', intent=row[0], state=row[1], response=json.loads(row[2]) if row[2] else None)
            return
        client = credentials(args.credentials)
        with process_lock(ROOT/'controller.lock'):
            store = Store()
            if args.initialize_competition: initialize(client, store); return
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
                if store.load()['stop_reason']: delay=1
                if deadline is not None: delay = min(delay, deadline-time.time())
                time.sleep(max(1, delay))
    except Blocked as exc:
        emit('COMPETITION_HALTED_RECONCILE_REQUIRED', error=str(exc)); raise SystemExit(78)


if __name__ == '__main__': main()
