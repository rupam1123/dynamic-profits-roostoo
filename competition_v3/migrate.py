"""Verify and preserve an existing V2/V2.1 journal; never reset trading history."""
import argparse,hashlib,importlib,json,sqlite3,time
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
from . import controller as new

MODULES={'competition-controller-2':'competition_v2.controller','competition-controller-2.1':'competition_v21.controller'}

def legacy(state):
    if state['version'] not in MODULES:raise new.Blocked('Unsupported source version')
    return importlib.import_module(MODULES[state['version']])

def preflight(store,resume_after_stop=False):
    state=store.load()
    if store.pending():raise new.Blocked('Unresolved intent; reconcile before migration')
    if state['version']==new.VERSION:
        if state['fingerprint']!=new.fingerprint():raise new.Blocked('V3 source/journal mismatch')
        return state
    old=legacy(state)
    expected=json.loads(Path(__file__).with_name('legacy_manifest.json').read_text())[state['version']]
    for name,digest in expected.items():
        actual=hashlib.sha256(Path(old.__file__).with_name(name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
        if actual!=digest:raise new.Blocked('Unrecognized legacy source '+name)
    if state['fingerprint']!=old.fingerprint():raise new.Blocked('Legacy journal/source mismatch')
    if state.get('stop_reason'):
        if state['stop_reason']!='PORTFOLIO_DRAWDOWN' or not resume_after_stop:raise new.Blocked('Existing stop requires explicit drawdown-resume decision; deadline cannot be cleared')
    if any(p not in new.PAIRS for p in state['positions']):raise new.Blocked('Unsupported held asset')
    return state

def upgrade_state(state,now):
    out=json.loads(json.dumps(state));hour=int(now)//3600
    out.update(version=new.VERSION,fingerprint=new.fingerprint(),purpose='COMPETITION')
    for k,v in new.LADDER_DEFAULTS.items():out.setdefault(k,json.loads(json.dumps(v)))
    out.setdefault('risk_peak',state['peak_equity'])
    out.pop('drawdown_usd',None)
    if state.get('stop_reason')=='PORTFOLIO_DRAWDOWN':
        out['stop_reason']=None;out['flatten_pending']=bool(out['positions'])
        out['pause_until']=hour+new.raw_policy()['ladder']['pause_hours']
    for pos in out['positions'].values():
        pos.setdefault('book','core');pos.setdefault('opened_hour',hour)
        pos.setdefault('stop_fraction','0.08');pos.setdefault('extreme_price',pos['entry']);pos.setdefault('partial_taken',False)
    # Existing >5 holdings are grandfathered; new entries are blocked until natural exits free capacity.
    return out

def migrate(client,store,backup_dir,resume_after_stop=False,now=None):
    now=time.time() if now is None else now
    state=preflight(store,resume_after_stop);cfg=new.policy(now)
    if state['version']==new.VERSION:
        new.identity(client,state);new.match_account(state,new.account(client));return None
    legacy(state).identity(client,state);observed=new.account(client);new.match_account(state,observed)
    backup_dir.mkdir(parents=True,exist_ok=True)
    backup=backup_dir/('execution_before_v3_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite3')
    with closing(sqlite3.connect(store.path)) as src,closing(sqlite3.connect(backup)) as dst:src.backup(dst)
    after=upgrade_state(state,now)
    if cfg['end_epoch'] is not None:
        if after['expires'] is not None and cfg['end_epoch']>after['expires']:raise new.Blocked('Migration may not extend existing deadline')
        after['expires']=cfg['end_epoch']
    new.match_account(after,observed)
    store.save(after,'ARCHITECTURE_MIGRATION',dict(from_version=state['version'],to_version=new.VERSION,
        previous_fingerprint=state['fingerprint'],new_fingerprint=after['fingerprint'],preserved_positions=after['positions'],backup=str(backup)))
    return backup

def main():
    p=argparse.ArgumentParser(description=__doc__);g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--preflight',action='store_true');g.add_argument('--apply',action='store_true')
    p.add_argument('--resume-after-drawdown-stop',action='store_true');a=p.parse_args()
    path=new.ROOT/'execution.sqlite3'
    if not path.exists():raise SystemExit('Existing journal missing; do not initialize')
    if a.preflight:
        state=preflight(new.Store(path),a.resume_after_drawdown_stop)
        print(json.dumps(dict(status='V3_PREFLIGHT_OK',version=state['version'],positions=state['positions'])));return
    with new.process_lock(new.ROOT/'controller.lock'):
        client=new.credentials(Path.home()/'.config/dynamic-profits/competition.json')
        result=migrate(client,new.Store(path),new.ROOT/'backups',a.resume_after_drawdown_stop)
        print(json.dumps(dict(status='V3_MIGRATED',backup=str(result),note='No orders submitted')))
if __name__=='__main__':main()
