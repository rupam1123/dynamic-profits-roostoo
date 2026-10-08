"""Read-only account-journal checkpoints and printed IST/UTC rollout schedule. No API calls/orders."""
import argparse,json,re,sqlite3
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path
IST=timezone(timedelta(hours=5,minutes=30))


def schedule(start=None):
    now=datetime.now(timezone.utc)
    if start:
        point=datetime.fromisoformat(start.replace('Z','+00:00'))
        if point.tzinfo is None:raise ValueError('Start needs a timezone, e.g. +05:30')
    else:
        point=now.replace(second=0,microsecond=0)+timedelta(minutes=1)
        while not (16<=point.minute<=49 and point.minute%5==3):point+=timedelta(minutes=1)
    checks=[(0,'Deploy if preflight and handover gate pass'),(2,'Service module and fresh heartbeat'),(5,'Quote freshness, dynamic data, errors and pending intents'),(15,'First execution audit; do not force a fill'),(60,'Fees, fill/cancel outcomes and rejection reasons'),(180,'Compare execution quality and fast/momentum activity'),(1440,'24h evidence review before changing strategy settings')]
    return [dict(ist=(point+timedelta(minutes=m)).astimezone(IST).isoformat(),utc=(point+timedelta(minutes=m)).astimezone(timezone.utc).isoformat(),check=label) for m,label in checks]


def snapshot(database,label,output):
    if not re.fullmatch('[A-Za-z0-9_-]{1,60}',label):raise ValueError('Use a simple checkpoint label')
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN');s=json.loads(db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        attempts=[(r[0],r[1],json.loads(r[2])) for r in db.execute('SELECT id,status,plan FROM attempts')]
        events=[(r[0],r[1],r[2],json.loads(r[3])) for r in db.execute('SELECT id,utc,kind,body FROM events ORDER BY id')]
    origin=max((e[0] for e in events if e[2]=='BASELINE_MIGRATION' and e[3].get('to_version')=='competition-controller-4-active-1'),default=0)
    recent=[e for e in events if e[0]>origin];fills=[e for e in recent if e[2]=='FILL_RECONCILED'];plans={i:p for i,_,p in attempts}
    out=dict(generated_utc=datetime.now(timezone.utc).isoformat(),label=label,version=s['version'],equity=s['last_equity'],free_usd=s['usd'],positions=len(s['positions']),stop_reason=s['stop_reason'],flatten_pending=s['flatten_pending'],pause_until=s['pause_until'],unresolved=[dict(intent=i,status=status) for i,status,_ in attempts if status!='APPLIED'],rejection_counts=s.get('rejection_counts',{}),fills_since_revision=len(fills),canceled_unfilled_since_revision=sum(e[2]=='ORDER_CANCELED_UNFILLED' for e in recent),equity_recorded_utc=next((e[1] for e in reversed(events) if e[2]=='EQUITY'),None),fill_groups={})
    for _,_,_,body in fills:
        p=plans.get(body.get('intent'),{});group='addition' if p.get('adding') else p.get('setup',p.get('action','unknown'))
        out['fill_groups'][group]=out['fill_groups'].get(group,0)+1
    out['note']='Journal snapshot only. Equity is not a live quote. Fill groups are executions, not matched round-trip profit. Compare to deployment baseline; inherited exposure also affects equity.'
    output.mkdir(parents=True,exist_ok=True);path=output/(label+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json');path.write_text(json.dumps(out,indent=2)+'\n')
    return out,path


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--schedule',action='store_true');p.add_argument('--start');p.add_argument('--label');p.add_argument('--db',type=Path,default=Path('data/competition/execution.sqlite3'));p.add_argument('--output',type=Path,default=Path('data/competition/checkpoints'));a=p.parse_args()
    if a.schedule:print(json.dumps(schedule(a.start),indent=2));return
    if not a.label:p.error('Use --schedule or --label')
    result,path=snapshot(a.db,a.label,a.output);print(json.dumps(result,indent=2));print('Checkpoint saved:',path)
if __name__=='__main__':main()
