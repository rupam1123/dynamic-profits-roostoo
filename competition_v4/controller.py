"""V4 dynamic-universe, one/five-minute entries and independently observed quote risk.
Public preview places no orders. No claim of improved profitability.
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
from . import timing
from .api import Client, dec, spot_plan, NotSent
from .limit_orders import settle
from .strategy import features, candidates, gross_exposure, risk_multiplier, paused, sleeve_target, book, opportunity_candidates

PAIRS = tuple(x+'/USD' for x in json.loads(Path(__file__).with_name('policy.json').read_text())['universe'])
ROOT = Path('data/competition')
CONFIG = Path(__file__).with_name('policy.json')
VERSION = 'competition-controller-4'
MARKET = None

def adopt_pairs(state, catalog=()):
    global PAIRS
    from . import api
    PAIRS=tuple(sorted(set(PAIRS)|set(state.get('known_pairs',[]))|set(state['positions'])|set(catalog)))
    api.PAIRS=PAIRS



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
                         ('controller.py', 'api.py', 'feed.py', 'policy.json', 'strategy.py','limit_orders.py','timing.py','fast.py','market.py','universe.py','runtime.py','management.py'))).hexdigest()


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


POLICY_KEYS = {'position_fraction','end_utc','gross_fraction','max_positions','daily_vol_target','min_quote_volume_24h',
               'max_spread','max_correlation','universe','ladder','guard','filters','sleeves','base_universe','expansion','execution','runtime','management'}


def policy(now):
    cfg = raw_policy()
    if set(cfg) != POLICY_KEYS:
        raise Blocked('Unexpected competition policy fields')
    fraction = dec(cfg['position_fraction'])
    if fraction > Decimal('.20'):
        raise Blocked('Policy exceeds release sizing limits')
    if dec(cfg['gross_fraction']) > Decimal('.30') or not isinstance(cfg['max_positions'], int) or not 1 <= cfg['max_positions'] <= 3:
        raise Blocked('Gross exposure or position limit exceeds release cap')
    if dec(cfg['daily_vol_target']) > Decimal('.03') or dec(cfg['max_spread']) > Decimal('.005') or dec(cfg['max_correlation']) >= 1:
        raise Blocked('Invalid volatility/spread/correlation policy')
    dec(cfg['min_quote_volume_24h'])
    if not isinstance(cfg['universe'],list) or len(cfg['universe'])!=len(set(cfg['universe'])) or not 3<=len(cfg['universe'])<=100:
        raise Blocked('Invalid universe')
    if cfg['base_universe'] != ['BTC','ETH','SOL','BNB','XRP','ADA','DOGE','AVAX','LINK','DOT','LTC','NEAR']:
        raise Blocked('Invalid asset mapping')
    if not set(cfg['base_universe']).issubset(cfg['universe']) or any(not isinstance(a,str) or not a.isalnum() for a in cfg['universe']):raise Blocked('Invalid expanded symbols')
    e=cfg['expansion']
    expected={'gross_fraction','position_fraction','max_positions','min_quote_volume_24h','max_daily_vol','stop_fraction','trail_activation','trail_fraction','minimum_hold_hours','rotation_score_ratio','rotation_hours','max_hold_hours'}
    if set(e)!=expected:raise Blocked('Invalid expansion configuration')
    for k in expected-{'max_positions','minimum_hold_hours','rotation_hours','max_hold_hours'}:dec(e[k])
    if dec(e['gross_fraction'])>Decimal('.40') or dec(e['position_fraction'])>Decimal('.05') or type(e['max_positions']) is not int or not 1<=e['max_positions']<=20:raise Blocked('Expansion exceeds release cap')
    if dec(e['min_quote_volume_24h'])<Decimal('10000000') or dec(e['max_daily_vol'])>Decimal('.10') or dec(e['stop_fraction'])>Decimal('.04'):raise Blocked('Expansion quality/risk limit')
    if dec(e['trail_fraction'])>Decimal('.04') or dec(e['trail_activation'])<dec(e['trail_fraction']) or dec(e['rotation_score_ratio'])<Decimal('1.5'):raise Blocked('Invalid expansion exit/rotation')
    if any(type(e[k]) is not int or not 1<=e[k]<=72 for k in ('minimum_hold_hours','rotation_hours','max_hold_hours')):raise Blocked('Invalid expansion timing')
    execution=cfg['execution']
    if execution != {'interval_seconds':300,'risk_fraction':'0.003','atr_multiple':'2.5','minimum_stop':'0.015','maximum_stop':'0.06','volume_ratio':'1.2','rotation_cost_hurdle':'0.008','max_new_orders_per_cycle':2,'max_new_orders_per_day':60,'legacy_entries':False,'forced_activity':False,'timing_required':True}:
        raise Blocked('Unrecognized V4 execution policy')
    runtime=cfg['runtime']
    if runtime['quote_seconds']!=10 or runtime['max_quote_age_seconds']>30 or not Decimal(0)<dec(runtime['portfolio_stop_risk_fraction'])<=Decimal('.02') or runtime['request_spacing_seconds']<3.1:raise Blocked('Invalid runtime limits')
    if cfg['management'] != {'initial_fraction':'0.5','max_additions':2,'addition_interval_seconds':900,'addition_fraction':'0.5','partial_at_r':'1.5','partial_fraction':'0.5','fee_per_side':'0.001','slippage_per_side':'0.0005','cost_multiple':'2','reentry_cooldown_hours':1,'reentry_cooldown_seconds':300,'fast_stop_fraction':'0.006','fast_max_hold_seconds':900,'fast_min_target':'0.006'}:raise Blocked('Unrecognized active-management policy')
    lad, grd, flt = cfg['ladder'], cfg['guard'], cfg['filters']
    ints = lambda v, lo, hi: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi
    if set(lad) != {'soft','hard','pause_hours','recover_hours','floor_fraction'} or not \
       (Decimal(0) < dec(lad['soft']) < dec(lad['hard']) <= Decimal('.10') and ints(lad['pause_hours'],1,72)
        and ints(lad['recover_hours'],0,168) and Decimal('.5') <= dec(lad['floor_fraction']) < 1):
        raise Blocked('Invalid drawdown ladder')
    if set(grd) != {'local_hour','utc_offset_hours','entry_mult','trim_fraction','probe_fraction','probe_hours'} or not \
       (ints(grd['local_hour'],0,23) and ints(grd['utc_offset_hours'],-12,14) and dec(grd['entry_mult']) <= 1
        and dec(grd['trim_fraction']) <= Decimal('.5') and dec(grd['probe_fraction']) <= Decimal('.05') and ints(grd['probe_hours'],1,72)):
        raise Blocked('Invalid activity guard')
    if set(flt) != {'z_max','r6_max','btc_m24_off','btc_vol_ratio'} or number(flt['btc_m24_off']) >= 0 \
       or dec(flt['btc_vol_ratio']) <= 1:
        raise Blocked('Invalid entry filters')
    dec(flt['z_max']); dec(flt['r6_max'])
    sleeves = cfg['sleeves']; names = set(); total = Decimal(0)
    if not isinstance(sleeves, list) or len(sleeves) > 3: raise Blocked('Invalid sleeves')
    for s in sleeves:
        if set(s) != {'name','family','lookback','rebalance_hours','gross','k'} or s['name'] in names or s['name'] == 'core' \
           or not str(s['name']).isalnum() or s['family'] not in ('xs','ts') or s['lookback'] not in (336, 720) \
           or not ints(s['rebalance_hours'],1,336) or dec(s['gross']) > Decimal('.15') \
           or (s['family'] == 'xs' and not ints(s['k'],1,3)):
            raise Blocked('Invalid sleeve definition')
        names.add(s['name']); total += dec(s['gross'])
    if total > Decimal('.25'): raise Blocked('Sleeve gross exceeds release cap')
    end = cfg['end_utc']
    epoch = None
    if end is not None:
        parsed = datetime.fromisoformat(end.replace('Z','+00:00'))
        if parsed.tzinfo is None: raise Blocked('Round end needs explicit timezone')
        epoch = parsed.timestamp()
    return dict(cfg, end_epoch=epoch)


LADDER_DEFAULTS = dict(pause_until=-1, recover_until=-1, flatten_pending=False, sleeves={}, last_fill_day=None)


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
    emit('V4_CONTROLLER_INITIALIZED', purpose=purpose, usd=state['usd'], scheduled_end=state['expires'], target_usd_per_pair=state['budget'],
         note='No orders placed. Account purpose is user-confirmed, not identified by the API.')


def public_market(client, required=()):
    if MARKET is not None:return MARKET.snapshot(required,raw_policy()['runtime']['max_quote_age_seconds'])
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


_SIGNALS_CACHE = {}


def signals(path=None):
    path = path or ROOT/'candles_v4.sqlite3'
    current_hour=int(time.time())//3600
    key=str(path.resolve())
    cached=_SIGNALS_CACHE.get(key)
    if cached and cached[0]==current_hour:return cached

    boundary,batches,failures=refresh(path,tuple(p.split('/')[0]+'USDT' for p in PAIRS))
    result={}
    for symbol,rows in batches.items():
        try: result[symbol[:-4]+'/USD']=features(rows,regime=(symbol=='BTCUSDT'))
        except (ValueError,ArithmeticError,KeyError) as exc: failures[symbol]=str(exc)
    emit('UNIVERSE_REFRESH', hour=boundary//HOUR, ready=len(result), skipped=failures)
    _SIGNALS_CACHE[key]=(boundary//HOUR,result)
    return boundary//HOUR,result


def raw_policy():
    """Policy values for bookkeeping paths that must keep working after the round deadline."""
    return json.loads(CONFIG.read_text())


def in_window(hour):
    now = time.time()*1000
    return int(now)//HOUR == hour and now % timing.INTERVAL < timing.INTERVAL-5000


def close_action(pos):
    return 'SELL' if pos['direction'] == 1 else 'SHORT_CLOSE'


def choose(state, pair, hour, signal, expired=False):
    """Exit decision for a held position. Sleeve positions exit only via their sleeve or a stop."""
    pos = state['positions'].get(pair)
    if not pos: return None
    if expired: return close_action(pos)
    if signal is None:return None
    d=pos['direction'];price=number(signal['close']);entry=dec(pos['entry'])
    if pos.get('probe_until') is not None and hour>=int(pos['probe_until']):return close_action(pos)
    if d*number(signal['momentum_24h'])<=0 and d*(price-number(signal.get('sma20',signal['close'])))<0:return close_action(pos)
    age=hour-int(pos['opened_hour'])
    if age>=raw_policy()['expansion']['max_hold_hours'] and d*(price/entry-1)<Decimal('.005'):return close_action(pos)
    return None


def plan_order(state, pair, action, info, ticks, hour, budget_override=None, owner='core', probe_until=None, trim_qty=None, adding=False):
    budget = dec(budget_override or state['budget'])
    quote = ticks['Data'][pair]
    if pair not in PAIRS or action not in ('BUY','SELL','SHORT_OPEN','SHORT_CLOSE','TRIM','SHORT_TRIM'): raise Blocked('Invalid order plan')
    if info.get('IsRunning') is not True or info['TradePairs'][pair].get('CanTrade') is not True: raise ValueError('Market unavailable')
    if dec(quote['MaxBid'])>dec(quote['MinAsk']): raise ValueError('Crossed quote')
    if action in ('BUY', 'SHORT_OPEN'):
        entry_allowed(state, hour)
        if pair in state['positions'] and not (adding and action=='BUY' and state['positions'][pair]['direction']==1): raise Blocked('Exposure slot unavailable')
        if adding and (pair not in state['positions'] or action!='BUY'):raise Blocked('Invalid addition')
        cfg=policy(time.time())
        equity=min(number(state['initial_usd']),number(state['last_equity']))
        cash=max(Decimal(0),number(state['usd'])-1)/Decimal('1.02')
        if owner == 'core':
            if probe_until is None:
                if sum(1 for p in state['positions'].values() if book(p) == 'core') >= cfg['max_positions']:
                    raise Blocked('Exposure slot unavailable')
                room=max(Decimal(0),equity*number(cfg['gross_fraction'])-gross_exposure(state,ticks))
                budget=min(budget,equity*number(cfg['position_fraction']),room/Decimal('1.02'),cash)
            else:
                budget=min(budget,equity*number(cfg['guard']['probe_fraction']),cash)
        elif owner=='opportunity':
            e=cfg['expansion']
            if action not in ('BUY','SHORT_OPEN'):raise Blocked('Invalid strategy entry')
            if not adding and len(state['positions'])>=e['max_positions']:raise Blocked('Expansion position limit')
            room=max(Decimal(0),equity*number(e['gross_fraction'])-gross_exposure(state,ticks,'opportunity'))
            held_notional=number(state['positions'][pair]['quantity'])*number(quote['MinAsk']) if adding else Decimal(0)
            budget=min(budget,max(Decimal(0),equity*number(e['position_fraction'])-held_notional)/Decimal('1.02'),room/Decimal('1.02'),cash)
        else:
            sleeve=[s for s in cfg['sleeves'] if s['name'] == owner]
            if len(sleeve) != 1: raise Blocked('Unknown position owner')
            budget=min(budget,equity*number(sleeve[0]['gross']),cash)
        # Enforce the aggregate budget declared by the original books, including probes.
        # This is an admission ceiling; mark-to-market moves can exceed it.
        gross = sum((gross_exposure(state,ticks,owner=b) for b in ['core','opportunity']+[s['name'] for s in cfg['sleeves']]), Decimal(0))
        cap = number(cfg['gross_fraction']) + sum((number(s['gross']) for s in cfg['sleeves']), Decimal(0))
        budget = min(budget, max(Decimal(0), equity*cap-gross)/Decimal('1.02'))
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
    else:
        if action=='SHORT_TRIM':
            held=state['positions'][pair]
            if held['direction']!=-1 or trim_qty is None or not Decimal(0)<dec(trim_qty)<dec(held['quantity']):raise Blocked('Invalid short trim')
        plan = {'endpoint':'/v6/short_close', 'params':{'pair':pair, 'close_qty':str(trim_qty) if action=='SHORT_TRIM' else state['positions'][pair]['quantity']}}
    if adding:plan['adding']=True;plan['position_id']=str(state['positions'][pair]['id'])
    held = state['positions'].get(pair)
    plan.update(pair=pair, action=action, hour=hour, budget=str(budget), precision=info['TradePairs'][pair]['AmountPrecision'],
                book=owner if action in ('BUY','SHORT_OPEN') else book(held), probe_until=probe_until)
    if action in ('BUY','SHORT_OPEN'):plan['decision_deadline']=(int(time.time())//300+1)*300-5
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
        pos['opened_at']=plan.get('created_at',plan['hour']*3600)
        for k in ('setup','setup_level','decision_bar','take_profit_fraction','max_hold_seconds'):
            if k in plan:pos[k]=plan[k]
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
            if plan.get('adding'):
                pos=after['positions'][pair]
                if pos['direction']!=1 or str(pos['id'])!=plan['position_id']:raise Blocked('Addition position identity changed')
                oldqty=dec(pos['quantity']);total=oldqty+qty
                pos.update(entry=str((oldqty*dec(pos['entry'])+qty*price)/total),quantity=str(total),
                           stop_fraction=str(min(dec(pos['stop_fraction']),dec(plan['stop_fraction']))),
                           extreme_price=str(max(dec(pos['extreme_price']),price)),
                           addition_count=int(pos.get('addition_count',0))+1,last_addition_bar=plan['decision_bar'],
                           last_addition_level=plan['setup_level'])
                pos.setdefault('addition_order_ids',[]).append(str(order_id))
            else:opened(1, qty, price, '0', str(order_id))
        elif action == 'TRIM':
            held = dec(state['positions'][pair]['quantity'])
            if qty >= held: raise Blocked('Trim quantity not below holding')
            usd += qty*price-fee
            after['positions'][pair]['quantity'] = str(held-qty)
            after['positions'][pair]['partial_taken'] = True
        else:
            if qty != dec(state['positions'][pair]['quantity']): raise Blocked('Sell quantity mismatch')
            usd += qty*price-fee
            del after['positions'][pair]; after['last_exit'][pair] = plan['hour'];after.setdefault('last_exit_seconds',{})[pair]=time.time()
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
            del after['positions'][pair];after['last_exit'][pair]=plan['hour'];after.setdefault('last_exit_seconds',{})[pair]=time.time()
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
    if plan['action'] in ('BUY','SHORT_OPEN') and MARKET is not None and MARKET.has_risk():raise ValueError('Risk exit has priority over entry')
    store.reserve(intent,plan)
    try:
        deadline=plan.get('decision_deadline',plan['hour']*3600+900) if plan['action'] in ('BUY','SHORT_OPEN') else None
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
    observed_peak=equity
    if MARKET is not None:
        with MARKET.lock:
            if MARKET.episode==state.get('recover_until',-1):observed_peak=MARKET.observed_peak or equity
    state['risk_peak']=str(max(equity,number(state['risk_peak']),observed_peak))
    if state['pause_until']<0 and not state['flatten_pending'] and equity<=number(state['risk_peak'])*(1-dec(lad['hard'])):
        state['flatten_pending']=True;state['pause_until']=hour+lad['pause_hours']
    if state['expires'] is not None and time.time()>=state['expires']:state['stop_reason']='COMPETITION_DEADLINE'
    for pair,pos in state['positions'].items():
        q=ticks['Data'][pair];px=dec(q['MaxBid'] if pos['direction']==1 else q['MinAsk'])
        previous=number(pos.get('extreme_price',pos['entry']))
        if MARKET is not None:
            with MARKET.lock:observed=MARKET.extremes.get((pair,str(pos['id'])),previous)
            previous=max(previous,observed) if pos['direction']==1 else min(previous,observed)
        pos['extreme_price']=str(max(previous,px) if pos['direction']==1 else min(previous,px))
    store.save(state,'EQUITY',dict(equity=str(equity),peak=state['peak_equity'],risk_peak=state['risk_peak'],
        stop_reason=state['stop_reason'],pause_until=state['pause_until'],flatten_pending=state['flatten_pending']))
    return state,ticks


def execute(client, store, intent, pair, action, hour, **kw):
    """Fresh account check + quotes, then one journaled write."""
    state=store.load()
    context=kw.pop('signal_context',None)
    if action in ('BUY','SHORT_OPEN'):
        entry_allowed(state,hour)
        if MARKET is not None and (MARKET.has_risk() or not MARKET.allowed(pair,raw_policy()['runtime']['catalog_max_age_seconds'])):raise ValueError('Urgent risk or stale/ineligible dynamic mapping')
        if context is None or pair not in context or any(p not in context for p in state['positions']):
            raise ValueError('Fresh complete held-asset signal context required for entry')
    match_account(state,account(client))
    info,ticks=public_market(client,tuple(state['positions'])+((pair,) if pair not in state['positions'] else ()))
    pick=None
    if action in ('BUY','SHORT_OPEN'):
        if kw.get('owner')!='opportunity':raise Blocked('Legacy/forced entry paths are disabled')
        if kw.get('adding'):
            from .management import addition_candidate
            candidate=addition_candidate(state,pair,context,info,ticks,raw_policy(),hour)
            selected=[candidate] if candidate else []
        else:selected,_=opportunity_candidates(state,context,info,ticks,raw_policy(),hour,risk_multiplier(state,raw_policy(),hour))
        selected=[p for p in selected if p['pair']==pair and p['action']==action]
        if not selected:raise ValueError('Setup no longer passes admission checks')
        pick=selected[0]
        boundary=int(time.time()*1000)//timing.INTERVAL*timing.INTERVAL
        bar=context[pair].get('timing')
        from . import fast
        if pick.get('adding'):
            if not timing.confirms(context[pair],bar,boundary):raise ValueError('Fresh addition confirmation required')
        elif pick['setup']=='ONE_MINUTE_BREAKOUT':
            if not fast.breakout(context[pair],context[pair].get('fast'),int(time.time())//60*60000,raw_policy()['runtime']):raise ValueError('Fresh one-minute breakout required')
        elif not timing.confirms(context[pair],bar,boundary):raise ValueError('Fresh five-minute entry confirmation required')
        if daily_entries(store)>=raw_policy()['execution']['max_new_orders_per_day']:raise ValueError('Daily entry turnover limit')
        kw['budget_override']=str(min(dec(kw.get('budget_override',pick['budget'])),dec(pick['budget'])))
    plan=plan_order(state,pair,action,info,ticks,hour,**kw)
    if pick:
        plan.update(stop_fraction=pick['stop_fraction'],setup=pick['setup'],setup_level=pick['level'],decision_bar=boundary,created_at=time.time())
        if pick['setup']=='ONE_MINUTE_BREAKOUT':
            plan.update(take_profit_fraction=pick['take_profit_fraction'],max_hold_seconds=raw_policy()['management']['fast_max_hold_seconds'])
        plan['decision_deadline']=(int(time.time())//60+1)*60-2 if pick['setup']=='ONE_MINUTE_BREAKOUT' else boundary/1000+295
    if pick and pick['setup']=='ONE_MINUTE_BREAKOUT' and action=='BUY':
        # One passive limit attempt; cancel/query terminal before any later entry. Never chase automatically.
        precision=info['TradePairs'][pair]['PricePrecision']
        if type(precision) is not int or not 0<=precision<=12:raise Blocked('Invalid price precision')
        price=dec(ticks['Data'][pair]['MaxBid']).quantize(Decimal(1).scaleb(-precision),rounding=ROUND_DOWN)
        if price<=0 or dec(plan['params']['quantity'])*price<=dec(info['TradePairs'][pair]['MiniOrder']):raise ValueError('Limit below minimum')
        plan['params'].update(type='LIMIT',price=str(price))
        plan.update(created_utc=time.time(),limit_timeout_seconds=12)
    submit(client,store,intent,plan)


def daily_entries(store):
    day=local_day(time.time(),raw_policy())
    with closing(sqlite3.connect(store.path)) as db:
        plans=db.execute('SELECT plan FROM attempts').fetchall()
    return sum(1 for (encoded,) in plans if (lambda p: p['action'] in ('BUY','SHORT_OPEN') and local_day(int(p['hour'])*3600,raw_policy())==day)(json.loads(encoded)))


def entry_allowed(state,hour):
    """All entry paths, including the legacy activity guard, share these checks."""
    if state.get('stop_reason') or state.get('flatten_pending') or paused(state,hour):
        raise Blocked('No entries while stopped, flattening or paused')
    if state.get('expires') is not None and time.time()>=state['expires']:
        raise ValueError('No entries after the competition deadline')
    if not in_window(hour):
        raise ValueError('Entry decision window has ended')


def protect_inherited(client,store,hour,ticks):
    """Quote stops on every book; inherited initial stops stay unchanged. No candle dependency."""
    for pair,pos in list(store.load()['positions'].items()):
        d=pos['direction'];px=dec(ticks['Data'][pair]['MaxBid'] if d==1 else ticks['Data'][pair]['MinAsk'])
        entry=dec(pos['entry']);extreme=dec(pos.get('extreme_price',pos['entry']))
        stop=dec(pos.get('stop_fraction','0.08'))
        loss=d*(px/entry-1)
        excursion=d*(extreme/entry-1)
        # At +1R protect fee-adjusted break-even; at +1.5R trail by 1R.
        hit=loss<=-stop or (excursion>=stop and loss<=Decimal('.003'))
        trail=excursion>=stop*Decimal('1.5') and d*(px/extreme-1)<=-stop
        intent=str(hour)+':'+pair+':RISK_STOP'
        if (hit or trail) and not store.exists(intent):execute(client,store,intent,pair,close_action(pos),hour)


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


def run_sleeve(client, store, sleeve, sig, hour, mult):
    state=store.load(); name=sleeve['name']; acts=[]
    current=state['sleeves'].get(name)
    if current is None or (hour % sleeve['rebalance_hours'] == 0 and int(current['hour']) != hour):
        target=sleeve_target(sleeve,sig,tuple(a+'/USD' for a in raw_policy()['base_universe']),state,mult)
        if target is None:
            store.save(state,'SLEEVE_SKIPPED',dict(sleeve=name,hour=hour,reason='LOOKBACK_UNAVAILABLE'))
            return acts
        state['sleeves'][name]=dict(hour=hour,target=target)
        store.save(state,'SLEEVE_TARGET',dict(sleeve=name,hour=hour,target=target))
    target=state['sleeves'][name]['target']
    for pair,pos in list(state['positions'].items()):
        if book(pos)!=name: continue
        want=target.get(pair)
        if want is not None and int(want['direction'])==pos['direction']: continue
        if not in_window(hour): return acts
        if store.already(hour,pair): continue
        execute(client,store,str(hour)+':'+pair,pair,close_action(pos),hour)
        acts.append(dict(pair=pair,action=close_action(pos),book=name))
    for pair,want in target.items():
        state=store.load()
        if paused(state,hour) or state['stop_reason'] or state['flatten_pending']: break
        if pair in state['positions'] or store.already(hour,pair): continue
        notional=Decimal(want['notional'])
        if notional<25 or notional*Decimal('1.02')>number(state['usd'])-1: continue
        if not in_window(hour): break
        action='BUY' if int(want['direction'])==1 else 'SHORT_OPEN'
        try:
            execute(client,store,str(hour)+':'+pair,pair,action,hour,budget_override=str(notional),owner=name,signal_context=sig)
        except ValueError as exc:
            store.save(store.load(),'ENTRY_DEFERRED',dict(pair=pair,book=name,reason=str(exc))); continue
        acts.append(dict(pair=pair,action=action,book=name,budget=str(notional)))
        state,_=risk_mark(client,store)
    return acts


def run_opportunities(client,store,sig,hour,mult):
    state=store.load();cfg=raw_policy();e=cfg['expansion'];actions=[]
    if paused(state,hour) or state['stop_reason'] or state['flatten_pending'] or not in_window(hour):return actions
    if any(p not in sig for p in state['positions']):
        store.save(state,'SELECTION',dict(hour=hour,candidates=[],skipped={'portfolio':'HELD_HISTORY_UNAVAILABLE'}));return actions
    info,ticks=public_market(client,tuple(state['positions']))
    picks,reasons=opportunity_candidates(state,sig,info,ticks,cfg,hour,mult)
    # Rotation is assessed once per hour, never more than once per six hours.
    if len(state['positions'])>=e['max_positions'] and hour-int(state.get('last_opportunity_rotation',-1000000))>=6:
        mature=[p for p,v in state['positions'].items() if hour-int(v['opened_hour'])>=e['minimum_hold_hours']]
        if mature:
            weakest=min(mature,key=lambda p:(number(sig[p]['score']),p))
            replacement,_=opportunity_candidates(state,sig,info,ticks,cfg,hour,mult,replacing=weakest)
            if replacement:
                best=replacement[0]['pair'];d=sig[best]['direction'];old_d=state['positions'][weakest]['direction']
                bar=int(time.time()*1000)//timing.INTERVAL*timing.INTERVAL
                cost_edge=d*number(sig[best]['momentum_24h'])-old_d*number(sig[weakest]['momentum_24h'])
                intent=str(hour)+':'+weakest+':ROTATE'
                if number(sig[best]['score'])>max(Decimal('.1'),number(sig[weakest]['score']))*dec(e['rotation_score_ratio']) and cost_edge>dec(cfg['execution']['rotation_cost_hurdle']) and timing.confirms(sig[best],sig[best].get('timing'),bar) and not store.exists(intent):
                    execute(client,store,intent,weakest,close_action(state['positions'][weakest]),hour)
                    state=store.load();state['last_opportunity_rotation']=hour
                    store.save(state,'ROTATION',dict(hour=hour,closed=weakest,candidate=best))
                    actions.append(dict(pair=weakest,action='CLOSE',reason='ROTATION'))
                    state,ticks=risk_mark(client,store)
                    picks,reasons=opportunity_candidates(state,sig,info,ticks,cfg,hour,mult)
    store.save(store.load(),'SELECTION',dict(hour=hour,candidates=picks,skipped=reasons))
    confirmed=[p for p in picks if timing.confirms(sig[p['pair']],sig[p['pair']].get('timing'),int(time.time()*1000)//timing.INTERVAL*timing.INTERVAL)]
    for pick in confirmed[:cfg['execution']['max_new_orders_per_cycle']]:
        state=store.load();pair=pick['pair']
        if not in_window(hour) or paused(state,hour) or state['stop_reason'] or state['flatten_pending']:break
        if pair in state['positions'] or store.already(hour,pair):continue
        try:execute(client,store,str(hour)+':'+pair,pair,pick['action'],hour,budget_override=pick['budget'],owner='opportunity',signal_context=sig)
        except ValueError as exc:
            store.save(store.load(),'ENTRY_DEFERRED',dict(pair=pair,reason=str(exc)));continue
        actions.append(dict(pair=pair,action=pick['action'],budget=pick['budget']))
        risk_mark(client,store)
    return actions


def guard(client,store,cfg,sig,hour,mult):
    """Activity is reported, never manufactured through discretionary probes."""
    return []


def cycle(client, store):
    state=store.load(); identity(client,state)
    recover_acknowledged(client,store)
    state=store.load(); match_account(state,account(client))
    state,risk_ticks=risk_mark(client,store)
    hour=int(time.time()*1000)//HOUR
    if state['stop_reason']:
        # Deadline exits do not depend on Binance or an entry window.
        for pair in list(state['positions']):
            state=store.load()
            if store.already(hour,pair,True): continue
            execute(client,store,str(hour)+':'+pair+':STOP',pair,choose(state,pair,hour,None,True),hour)
        state,_=risk_mark(client,store)
        if not state['positions']:
            emit('COMPETITION_RUN_COMPLETE',reason=state['stop_reason'],usd=state['usd'])
            return True
        return False
    if state['flatten_pending']:
        flatten(client,store,hour)
        return False
    protect_inherited(client,store,hour,risk_ticks)
    state,_=risk_mark(client,store)
    if state['flatten_pending']:
        flatten(client,store,hour)
        return False
    slot=int(time.time())//300
    if state.get('last_slot',-1)>=slot:
        emit('COMPETITION_RISK_CHECK',hour=hour,equity=state['last_equity']);return False
    cfg=policy(time.time());signal_hour,sig=signals()
    if signal_hour!=hour or int(time.time())//3600!=hour:raise ValueError('Hourly context is stale')
    # Five-minute fetches are bounded to held assets and eight strongest hourly setups.
    from .strategy import entry_setup
    shortlist=sorted((p for p in sig if entry_setup(sig[p],cfg)),key=lambda p:(-number(sig[p]['score']),p))[:8]
    boundary,fast,failures=timing.refresh(set(shortlist)|set(state['positions']))
    if boundary//300000!=slot or int(time.time())//300!=slot:raise ValueError('Five-minute decision expired while collecting data')
    for pair,data in fast.items():
        if pair in sig:sig[pair]=dict(sig[pair],timing=data)
    actions=[];missing=[p for p in state['positions'] if p not in sig]
    for pair in list(state['positions']):
        state=store.load()
        if pair not in state['positions']:continue
        action=choose(state,pair,hour,sig.get(pair))
        pos=state['positions'][pair];bar=fast.get(pair)
        if bar and pos.get('setup')=='BREAKOUT' and hour>int(pos['opened_hour']):
            if pos['direction']*(number(bar['close'])/number(pos['setup_level'])-1)<Decimal('-.003'):action=close_action(pos)
        intent=str(hour)+':'+pair+':EXIT'
        if action and not store.exists(intent):
            execute(client,store,intent,pair,action,hour);actions.append(dict(pair=pair,action=action))
    state,_=risk_mark(client,store)
    if not state['stop_reason'] and not state['flatten_pending'] and int(time.time())//300==slot:
        actions+=run_opportunities(client,store,sig,hour,risk_multiplier(state,cfg,hour))
    state,_=risk_mark(client,store)
    state['last_slot']=slot
    if not missing:state['last_hour']=hour
    store.save(state,'CYCLE',dict(hour=hour,slot=slot,actions=actions,missing_held=missing,timing_failures=failures))
    emit('V4_FIVE_MINUTE_CYCLE',hour=hour,slot=slot,actions=actions,equity=state['last_equity'],open_positions=len(state['positions']),missing_held=missing,timing_failures=failures)
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
    if args.watch and not (args.execute_competition or args.execute_testing): parser.error('--watch requires an execution mode')
    purpose='TESTING' if args.initialize_testing or args.execute_testing else 'COMPETITION'
    if purpose=='TESTING':
        ROOT=Path('data/v4_testing')
        if args.credentials==Path.home()/'.config/dynamic-profits/competition.json':args.credentials=Path.home()/'.config/dynamic-profits/testing.json'
    try:
        if args.preview:
            from .runtime import preview
            preview();return
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
            adopt_pairs(store.load())
            if store.load().get('purpose','COMPETITION')!=purpose:raise Blocked('Journal account purpose mismatch')
            if args.check_competition:
                state = store.load(); identity(client, state)
                if store.pending(): raise Blocked('Unresolved intent; inspect --report')
                match_account(state, account(client))
                emit('COMPETITION_ACCOUNT_RECONCILED', usd=state['usd'], positions=state['positions'], expires_utc=datetime.fromtimestamp(state['expires'], timezone.utc).isoformat() if state['expires'] is not None else None)
                return
            if not args.watch:raise Blocked('V4 execution requires --watch; use --preview for public checks')
            from .runtime import Runner
            Runner(client,store).run()
    except Blocked as exc:
        emit('COMPETITION_HALTED_RECONCILE_REQUIRED', error=str(exc)); raise SystemExit(78)


if __name__ == '__main__': main()
