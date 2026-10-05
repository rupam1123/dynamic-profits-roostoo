"""One-shot collateral-backed BTC short test. TEST credentials only; no spot trades."""
import argparse
from contextlib import closing
from decimal import Decimal, ROUND_DOWN
import getpass
import hashlib
import json
from pathlib import Path
import sqlite3
import warnings
from roostoo_adapter import Client, dec
from test_roundtrip import wallet, money, market, emit

JOURNAL=Path('data/execution_test/short_roundtrip.sqlite3')
PAIR='BTC/USD'


def positions(client):
    data=client.short_positions()
    if data.get('Success') is not True or not isinstance(data.get('Positions'),list):
        raise ValueError('Cannot verify short positions')
    return data['Positions']


def free_usd(client):
    data=client.balance()
    if data.get('Success') is not True: raise ValueError('Balance failed')
    return money(data['SpotWallet']['USD']['Free'])


def write_once(client,path,name,endpoint,payload):
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('INSERT INTO steps VALUES (?,?,?,?,?)',(name,'SENDING',endpoint,json.dumps(payload),None))
    try:
        result=client.request(endpoint,payload,method='POST',signed=True)
        status='ACK' if result.get('Success') is True else 'REJECTED' if result.get('Success') is False else 'UNKNOWN'
    except Exception as exc:
        result={'error_type':type(exc).__name__}; status='UNKNOWN'
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('UPDATE steps SET status=?,response=? WHERE name=?',(status,json.dumps(result),name))
    if status!='ACK': raise ValueError(name+': '+json.dumps(result)+'; no retry')
    return result


def near(a,b,label):
    if abs(a-b)>Decimal('.02'): raise ValueError(label+' did not reconcile')


def record(path,label,values):
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('INSERT INTO receipts VALUES (?,?)',(label,json.dumps(values,default=str)))


def run(client,path=JOURNAL):
    path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS run (id INTEGER PRIMARY KEY, account TEXT, status TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS steps (name TEXT PRIMARY KEY,status TEXT,endpoint TEXT,payload TEXT,response TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS receipts (name TEXT PRIMARY KEY,body TEXT)')
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM run').fetchone(): raise ValueError('Existing short test. Do not delete journal or retry orders.')
        db.execute('INSERT INTO run VALUES (1,?,?)',(hashlib.sha256(client.key.encode()).hexdigest(),'STARTED'))
    before=wallet(client)
    if before['BTC']!=0 or not Decimal('49990')<=before['USD']<=Decimal('50010'):
        raise ValueError('Expected testing wallet near USD 50000, with no BTC')
    if positions(client): raise ValueError('Existing short position')
    pending=client.request('/v3/query_order',{'pending_only':'TRUE'},method='POST',signed=True)
    empty=(pending.get('Success') is False and pending.get('ErrMsg')=='no order matched') or (pending.get('Success') is True and pending.get('OrderMatched')==[])
    if not empty: raise ValueError('Could not verify no pending orders')
    info,ticker=market(client)
    rules=info['TradePairs'][PAIR]
    if info.get('IsRunning') is not True or rules.get('CanTrade') is not True: raise ValueError('Not tradable')
    places=rules['AmountPrecision']
    if type(places) is not int or not 0<=places<=12: raise ValueError('Bad precision')
    quantum=Decimal(1).scaleb(-places)
    if Decimal('10')/dec(ticker['MaxBid']) < quantum: raise ValueError('Collateral too small for one quantity step')
    emit('SUBMITTING_TEST_SHORT',collateral_usd='10')
    opened=write_once(client,path,'open','/v6/short_open',{'pair':PAIR,'collateral':'10'})
    if opened.get('Status')!='OPEN' or opened.get('Pair')!=PAIR: raise ValueError('Short was not confirmed OPEN')
    collateral=dec(opened['Collateral']); qty=dec(opened['ShortQty']); entry=dec(opened['EntryPrice']); fee=money(opened['OpenFee'])
    validate_size(collateral,qty,entry,quantum)
    pos=positions(client)
    if len(pos)!=1 or str(pos[0].get('ID'))!=str(opened['ID']) or pos[0].get('Pair')!=PAIR or dec(pos[0]['ShortQty'])!=qty or dec(pos[0]['Collateral'])!=collateral or dec(pos[0]['EntryPrice'])!=entry:
        raise ValueError('Short position does not match acknowledgement')
    after_open=free_usd(client)
    near(after_open,before['USD']-collateral-fee,'Open wallet')
    record(path,'open',{'ack':opened,'position':pos[0],'usd_free':after_open})
    emit('SHORT_OPEN_VERIFIED',quantity=str(qty),entry_price=str(entry),fee_usd=str(fee))
    after=close_verified(client,path,opened,after_open)
    emit('SHORT_ROUNDTRIP_COMPLETE',usd_before=before['USD'],usd_after=after['USD'],usd_change=after['USD']-before['USD'],open_short_positions=0)


def validate_size(collateral,qty,entry,quantum):
    budget=Decimal('10')
    expected=(budget/entry).quantize(quantum,rounding=ROUND_DOWN)
    if qty!=expected or not Decimal('0')<collateral<=budget:
        raise ValueError('Unexpected short size/collateral')
    # Observed API records rounded executed notional (e.g. 9.42), not always requested budget.
    if collateral!=budget and abs(collateral-qty*entry)>Decimal('.01'):
        raise ValueError('Collateral does not match executed notional')


def close_verified(client,path,opened,after_open):
    collateral=dec(opened['Collateral']); qty=dec(opened['ShortQty']); entry=dec(opened['EntryPrice'])
    closed=write_once(client,path,'close','/v6/short_close',{'pair':PAIR})
    if closed.get('FullyClosed') is not True or dec(closed['ClosedQty'])!=qty: raise ValueError('Short not fully closed')
    exitprice=dec(closed['ClosePrice']); closefee=money(closed['CloseFee'])
    realized=Decimal(str(closed['RealizedPNL'])); returned=Decimal(str(closed['ReturnAmount']))
    if not realized.is_finite() or not returned.is_finite(): raise ValueError('Nonfinite close response')
    near(realized,max(qty*(entry-exitprice),-collateral),'Realized PnL')
    near(returned,collateral+realized-closefee,'Return amount')
    if positions(client): raise ValueError('Position still present after close')
    after=wallet(client)
    near(after['USD'],after_open+returned,'Closing wallet')
    if after['BTC']!=0: raise ValueError('Unexpected spot BTC')
    record(path,'close',{'ack':closed,'usd_free':after['USD']})
    with closing(sqlite3.connect(path)) as db,db:
        db.execute("UPDATE run SET status='COMPLETE' WHERE id=1")
    return after


def recover(client,expected_id,path=JOURNAL):
    # Existing journal only. No open endpoint is reachable from this recovery path.
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        runrow=db.execute('SELECT account,status FROM run WHERE id=1').fetchone()
        ack=db.execute("SELECT status,response,payload,endpoint FROM steps WHERE name='open'").fetchone()
        attempted=db.execute("SELECT 1 FROM steps WHERE name='close'").fetchone()
    if not runrow or runrow[0]!=hashlib.sha256(client.key.encode()).hexdigest():
        raise ValueError('Credentials do not match original testing account')
    if runrow[1]!='STARTED' or attempted:
        raise ValueError('Close already attempted or run completed; no resubmission')
    if not ack or ack[0]!='ACK' or ack[3]!='/v6/short_open':
        raise ValueError('No acknowledged opening to recover')
    payload=json.loads(ack[2]); opened=json.loads(ack[1])
    if payload.get('pair')!=PAIR or dec(payload.get('collateral'))!=Decimal('10'):
        raise ValueError('Journal is not the expected USD 10 test')
    if opened.get('Success') is not True or opened.get('Status')!='OPEN' or opened.get('Pair')!=PAIR or str(opened.get('ID'))!=str(expected_id):
        raise ValueError('Opening acknowledgement does not match expected position ID')
    collateral=dec(opened['Collateral']); qty=dec(opened['ShortQty']); entry=dec(opened['EntryPrice'])
    if collateral>10 or qty*entry>10 or qty*entry<1:
        raise ValueError('Recorded exposure outside testing budget')
    pos=positions(client)
    if len(pos)!=1 or str(pos[0].get('ID'))!=str(expected_id) or pos[0].get('Pair')!=PAIR:
        raise ValueError('Expected single short position not found; no orders sent')
    for field in ('ShortQty','Collateral','EntryPrice'):
        if dec(pos[0][field])!=dec(opened[field]):
            raise ValueError('Current position differs from original acknowledgement')
    pending=client.request('/v3/query_order',{'pending_only':'TRUE'},method='POST',signed=True)
    if not ((pending.get('Success') is False and pending.get('ErrMsg')=='no order matched') or
            (pending.get('Success') is True and pending.get('OrderMatched')==[])):
        raise ValueError('Pending orders not confirmed empty')
    before_close=free_usd(client)
    record(path,'recovery_preclose',{'position':pos[0],'usd_free':before_close})
    emit('RECOVERY_POSITION_VERIFIED',position_id=expected_id,quantity=qty,collateral_usd=collateral)
    after=close_verified(client,path,opened,before_close)
    emit('SHORT_RECOVERY_COMPLETE',position_id=expected_id,usd_after=after['USD'],open_short_positions=0,
         note='Original pre-open balance was not recorded; no full round-trip PnL claimed.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--execute-testing',action='store_true')
    mode.add_argument('--report',action='store_true')
    mode.add_argument('--recover-testing',action='store_true')
    parser.add_argument('--expected-position-id',type=int)
    args=parser.parse_args()
    if args.report:
        with closing(sqlite3.connect(JOURNAL.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            for row in db.execute('SELECT name,status,response FROM steps'):
                emit('SHORT_JOURNAL',step=row[0],state=row[1],response=json.loads(row[2]) if row[2] else None)
        return
    if args.recover_testing and args.expected_position_id is None:
        parser.error('--recover-testing requires --expected-position-id')
    if args.recover_testing:
        print('Verifies and closes only the journaled TEST short. Does not open a position.')
    else:
        print('Opens a BTC short with USD 10 collateral, verifies it, and automatically closes it. Testing account only.')
    if input('Type TEST ACCOUNT to continue: ').strip()!='TEST ACCOUNT': raise SystemExit('Cancelled')
    warnings.simplefilter('error',getpass.GetPassWarning)
    key=getpass.getpass('Testing API key (hidden): ').strip(); secret=getpass.getpass('Testing API secret (hidden): ').strip()
    if not key or not secret: raise SystemExit('Empty credentials')
    try:
        client=Client(key,secret)
        if args.recover_testing: recover(client,args.expected_position_id)
        else: run(client)
    except Exception as exc:
        emit('HALTED_DO_NOT_RESUBMIT',error=str(exc),journal=str(JOURNAL))
        raise SystemExit(1)


if __name__=='__main__': main()
