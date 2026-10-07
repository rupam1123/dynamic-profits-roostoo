"""Journal-preserving V2 -> V2.1 upgrade. No order submission; no reinitialization.

Positions, cash, peak equity, cooldowns, processed hour and the full attempt/event journal are kept.
Existing positions become the 'core' book. A V2 permanent drawdown stop is NOT silently cleared:
it is converted to a ladder pause only with --resume-after-drawdown-stop.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from competition_v2 import controller as old
from . import controller as new


def preflight(store, resume_after_stop=False):
    state=store.load()
    if state['version']==new.VERSION:
        if state['fingerprint']!=new.fingerprint(): raise new.Blocked('Installed v2.1 differs from migrated release')
        return state
    if state['version']!=old.VERSION: raise new.Blocked('Unrecognized legacy version')
    expected=json.loads(Path(__file__).with_name('legacy_manifest.json').read_text())
    for name,digest in expected.items():
        actual=hashlib.sha256(Path(old.__file__).with_name(name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
        if actual!=digest: raise new.Blocked('Legacy source differs: '+name+'; review before migration')
    if state['fingerprint']!=old.fingerprint(): raise new.Blocked('Legacy journal/code mismatch')
    if store.pending(): raise new.Blocked('Unresolved intent: preserve journal and reconcile before upgrade')
    if state['stop_reason']=='COMPETITION_DEADLINE': raise new.Blocked('Round already ended')
    if state['stop_reason'] and not resume_after_stop:
        raise new.Blocked('V2 drawdown stop is latched; rerun with --resume-after-drawdown-stop only if the team decides to resume')
    if any(p not in new.PAIRS for p in state['positions']): raise new.Blocked('Held asset absent from v2.1 universe')
    return state


def upgrade_state(state, now):
    after=json.loads(json.dumps(state))
    hour=int(now)//3600
    lad=new.raw_policy()['ladder']
    after.update(version=new.VERSION, fingerprint=new.fingerprint(), sleeves={}, last_fill_day=None,
                 flatten_pending=False, pause_until=-1, recover_until=-1)
    after.pop('drawdown_usd', None)
    if state['stop_reason']=='PORTFOLIO_DRAWDOWN':
        # Treat the old latch as a ladder pause that starts now.
        after['stop_reason']=None; after['pause_until']=hour+lad['pause_hours']
    for pos in after['positions'].values():
        pos.setdefault('book','core')
    return after


def migrate(client,store,backup_dir,resume_after_stop=False,now=None):
    now=time.time() if now is None else now
    state=preflight(store,resume_after_stop)
    if state['version']==new.VERSION:
        new.identity(client,state); new.match_account(state,new.account(client))
        return None
    old.identity(client,state)
    observed=new.account(client)
    new.match_account(state,observed)
    new.policy(now)
    backup_dir.mkdir(parents=True,exist_ok=True)
    name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup=backup_dir/('execution_before_v21_'+name+'.sqlite3')
    with closing(sqlite3.connect(store.path)) as src, closing(sqlite3.connect(backup)) as dst:
        src.backup(dst)
    after=upgrade_state(state,now)
    new.match_account(after,observed)
    store.save(after,'ARCHITECTURE_MIGRATION',dict(from_version=state['version'],to_version=new.VERSION,
               previous_fingerprint=state['fingerprint'],new_fingerprint=after['fingerprint'],
               preserved_positions=after['positions'],converted_stop=state['stop_reason'],backup=str(backup)))
    return backup


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preflight',action='store_true')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--resume-after-drawdown-stop',action='store_true')
    args=parser.parse_args()
    if args.preflight==args.apply: parser.error('Choose exactly one of --preflight or --apply')
    path=new.ROOT/'execution.sqlite3'
    if not path.is_file(): raise SystemExit('Existing competition database missing; initialization is not migration')
    if args.preflight:
        state=preflight(new.Store(path),args.resume_after_drawdown_stop)
        print(json.dumps(dict(status='UPGRADE_PREFLIGHT_OK',version=state['version'],positions=state['positions'],stop_reason=state['stop_reason'])))
        return
    with new.process_lock(new.ROOT/'controller.lock'):
        client=new.credentials(Path.home()/'.config/dynamic-profits/competition.json')
        backup=migrate(client,new.Store(path),new.ROOT/'backups',args.resume_after_drawdown_stop)
        print(json.dumps(dict(status='UPGRADE_MIGRATED',backup=str(backup) if backup else None,note='No orders sent; existing positions and history preserved.')))

if __name__=='__main__': main()
