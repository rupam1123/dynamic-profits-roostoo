"""Preserve an existing V3 competition journal; never initialize or reset it."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from . import controller as new

OLD_VERSION = 'competition-controller-3.1'


class ReadOnlyStore:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        return sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)

    def load(self):
        with closing(self.connect()) as db:
            row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
        if row is None:
            raise new.Blocked('Existing initialized journal required')
        return json.loads(row[0])

    def pending(self):
        with closing(self.connect()) as db:
            return db.execute("SELECT id,status,plan,response FROM attempts WHERE status!='APPLIED'").fetchall()


def preflight(store):
    state = store.load()
    if store.pending():
        raise new.Blocked('Unresolved intent: reconcile with the existing controller before migration')
    if state.get('purpose') != 'COMPETITION':
        raise new.Blocked('Migration requires the existing competition journal')
    if state.get('version') == new.VERSION:
        if state.get('fingerprint') != new.fingerprint():
            raise new.Blocked('V3.2 source/journal mismatch')
        return state
    if state.get('version') != OLD_VERSION:
        raise new.Blocked('Only the recognized V3 release can migrate with this package')
    from competition_v31 import controller as old
    expected = json.loads(Path(__file__).with_name('legacy_manifest.json').read_text())
    for name, digest in expected.items():
        path = Path(old.__file__).with_name(name)
        actual = hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
        if actual != digest:
            raise new.Blocked('Unrecognized V3 source: ' + name)
    if state.get('fingerprint') != old.fingerprint():
        raise new.Blocked('V3 journal/source mismatch')
    owners = {'core','opportunity'} | {s['name'] for s in new.raw_policy()['sleeves']}
    for pair, pos in state['positions'].items():
        if pair not in new.PAIRS or pos.get('book', 'core') not in owners or pos['direction'] not in (-1, 1):
            raise new.Blocked('Unsupported inherited holding')
    return state


def upgrade_state(state, now):
    after = json.loads(json.dumps(state))
    cfg = new.policy(now)
    after.update(version=new.VERSION, fingerprint=new.fingerprint())
    # Preserve all risk episode, stop, cooldown, target and order-history fields.
    after.setdefault('risk_peak', state['peak_equity'])
    after['budget'] = str(new.number(state['initial_usd']) * new.number(cfg['position_fraction']))
    if cfg['end_epoch'] is not None:
        if after['expires'] is not None and cfg['end_epoch'] > after['expires']:
            raise new.Blocked('Migration cannot extend an existing deadline')
        after['expires'] = cfg['end_epoch']
    return after


def migrate(client, store, backup_dir, now=None):
    now = time.time() if now is None else now
    state = preflight(store)
    if state['version'] == new.VERSION:
        new.identity(client, state)
        new.match_account(state, new.account(client))
        return None
    from competition_v31 import controller as old
    old.identity(client, state)
    observed = new.account(client)
    new.match_account(state, observed)
    after = upgrade_state(state, now)
    new.match_account(after, observed)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / ('execution_before_v32_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite3')
    with closing(sqlite3.connect(store.path)) as src, closing(sqlite3.connect(backup)) as dst:
        src.backup(dst)
    store.save(after, 'BASELINE_MIGRATION', dict(from_version=state['version'], to_version=new.VERSION,
        previous_fingerprint=state['fingerprint'], new_fingerprint=after['fingerprint'],
        preserved_positions=state['positions'], old_new_entry_budget=state['budget'],
        new_entry_budget=after['budget'], backup=str(backup),
        note='No orders sent. Existing V3 quote stops remain attached to adopted holdings.'))
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preflight', action='store_true', required=True)
    parser.parse_args()
    path = new.ROOT / 'execution.sqlite3'
    if not path.is_file():
        raise SystemExit('Existing competition journal missing; do not initialize')
    state = preflight(ReadOnlyStore(path))
    print(json.dumps(dict(status='V32_PREFLIGHT_OK', version=state['version'], positions=state['positions'],
        pending=0, stop_reason=state['stop_reason'], pause_until=state.get('pause_until'),
        note='Read-only preflight. Service and journal unchanged.')))


if __name__ == '__main__':
    main()
