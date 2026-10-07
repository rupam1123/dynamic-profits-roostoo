"""Read-only explanation of the deployed controller's recorded activity."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3


def report(path,days):
    cutoff=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        raw=db.execute('SELECT body FROM state WHERE id=1').fetchone()
        state=json.loads(raw[0]) if raw else {}
        rows=db.execute('SELECT utc,kind,body FROM events WHERE utc>=? ORDER BY id',(cutoff,)).fetchall()
        pending=db.execute("SELECT COUNT(*) FROM attempts WHERE status!='APPLIED'").fetchone()[0]
    events=Counter();reasons=Counter();actions=Counter();latest=None
    for utc,kind,body in rows:
        events[kind]+=1;latest=utc;body=json.loads(body)
        if kind=='SELECTION':reasons.update(str(v) for v in body.get('skipped',{}).values())
        if kind in ('ENTRY_DEFERRED','SLEEVE_SKIPPED'):reasons.update([str(body.get('reason','UNKNOWN'))])
        if kind=='CYCLE':actions.update(a.get('action','UNKNOWN') for a in body.get('actions',[]))
    return dict(mode='READ_ONLY',days=days,latest_event_utc=latest,event_counts=dict(events),selection_or_deferral_reasons=dict(reasons),recorded_cycle_actions=dict(actions),
        open_positions=len(state.get('positions',{})),stop_reason=state.get('stop_reason'),pause_until_hour=state.get('pause_until'),flatten_pending=state.get('flatten_pending'),unresolved=pending,
        note='Counts describe recorded decisions, not guaranteed fills or leaderboard volume. No account calls, state writes or orders.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,default=Path('data/competition/execution.sqlite3'))
    p.add_argument('--days',type=int,default=7);a=p.parse_args()
    if a.days<1:p.error('--days must be positive')
    print(json.dumps(report(a.db,a.days),indent=2))


if __name__=='__main__':main()
