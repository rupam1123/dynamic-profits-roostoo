"""Journal-preserving v1 to v2 upgrade. No order submission; no reinitialization."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from competition_bot import controller as old
from . import controller as new


def preflight(store):
    state=store.load()
    if state['version']==new.VERSION:
        if state['fingerprint']!=new.fingerprint(): raise new.Blocked('Installed v2 differs from migrated release')
        return state
    if state['version']!='competition-controller-1': raise new.Blocked('Unrecognized legacy version')
    expected=json.loads(Path(__file__).with_name('legacy_manifest.json').read_text())
    for name,digest in expected.items():
        actual=hashlib.sha256(Path(old.__file__).with_name(name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
        if actual!=digest: raise new.Blocked('Legacy source differs: '+name+'; review before migration')
    if state['fingerprint']!=old.fingerprint(): raise new.Blocked('Legacy journal/code mismatch')
    if store.pending(): raise new.Blocked('Unresolved intent: preserve journal and reconcile before upgrade')
    if state['stop_reason']: raise new.Blocked('Existing stop is latched; upgrade cannot reset it')
    if any(p not in new.PAIRS for p in state['positions']): raise new.Blocked('Held asset absent from v2 universe')
    return state


def migrate(client,store,backup_dir):
    state=preflight(store)
    if state['version']==new.VERSION:
        new.identity(client,state); new.match_account(state,new.account(client))
        return None
    old.identity(client,state)
    observed=new.account(client)
    new.match_account(state,observed)
    new.policy(__import__('time').time())
    backup_dir.mkdir(parents=True,exist_ok=True)
    name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup=backup_dir/('execution_before_v2_'+name+'.sqlite3')
    with closing(sqlite3.connect(store.path)) as src, closing(sqlite3.connect(backup)) as dst:
        src.backup(dst)
    before=dict(state)
    state['version']=new.VERSION; state['fingerprint']=new.fingerprint()
    # Preserve balance, positions, orders, peak, drawdown latch, cooldowns, expiry and processed hour.
    store.save(state,'ARCHITECTURE_MIGRATION',dict(from_version=before['version'],to_version=new.VERSION,
               previous_fingerprint=before['fingerprint'],new_fingerprint=state['fingerprint'],
               preserved_positions=state['positions'],backup=str(backup)))
    return backup


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preflight',action='store_true')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if args.preflight==args.apply: parser.error('Choose exactly one of --preflight or --apply')
    path=new.ROOT/'execution.sqlite3'
    if not path.is_file(): raise SystemExit('Existing competition database missing; initialization is not migration')
    if args.preflight:
        state=preflight(new.Store(path))
        print(json.dumps(dict(status='UPGRADE_PREFLIGHT_OK',version=state['version'],positions=state['positions'])))
        return
    with new.process_lock(new.ROOT/'controller.lock'):
        client=new.credentials(Path.home()/'.config/dynamic-profits/competition.json')
        backup=migrate(client,new.Store(path),new.ROOT/'backups')
        print(json.dumps(dict(status='UPGRADE_MIGRATED',backup=str(backup) if backup else None,note='No orders sent; existing position and history preserved.')))

if __name__=='__main__': main()
