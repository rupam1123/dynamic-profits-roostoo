"""Create a read-only JSON/HTML audit report from the competition controller journal."""
import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
import html
import json
from pathlib import Path
import sqlite3


def build(database, output):
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
        if not row: raise ValueError('Controller is not initialized')
        state = json.loads(row[0])
        events = [dict(utc=r[0],kind=r[1],detail=json.loads(r[2])) for r in
                  db.execute('SELECT utc,kind,body FROM events ORDER BY id')]
        attempts = [dict(intent=r[0],status=r[1],plan=json.loads(r[2]),response=json.loads(r[3]) if r[3] else None) for r in
                    db.execute('SELECT id,status,plan,response FROM attempts ORDER BY rowid')]
    fill_days = sorted({datetime.fromisoformat(e['utc']).astimezone(timezone(timedelta(hours=8))).date().isoformat() for e in events if e['kind']=='FILL_RECONCILED'})
    summary = dict(fill_days_gmt8=fill_days, days_with_fills=len(fill_days), activity_note='A day with a fill is not proof that the organizer considers it sufficiently active.', generated_utc=datetime.now(timezone.utc).isoformat(), mode='COMPETITION',
                   initial_usd=state['initial_usd'], free_usd=state['usd'], last_equity_estimate=state['last_equity'],
                   peak_equity_estimate=state['peak_equity'], positions=state['positions'],
                   stop_reason=state['stop_reason'], expires_utc=datetime.fromtimestamp(state['expires'],timezone.utc).isoformat() if state['expires'] is not None else None,
                   reconciled_fills=sum(a['status']=='APPLIED' for a in attempts),
                   unresolved=sum(a['status']!='APPLIED' for a in attempts),
                   equity_measured_utc=next((e['utc'] for e in reversed(events) if e['kind']=='EQUITY'), None),
                   measurement_note='Equity is the last recorded quote-based liquidation estimate, including estimated close fees. It is not a live quote or a backtest.')
    output.mkdir(parents=True,exist_ok=True)
    (output/'controller_audit.json').write_text(json.dumps(dict(summary=summary,events=events,attempts=attempts),indent=2),encoding='utf-8')
    esc=lambda v:html.escape(str(v))
    cards=[('Free USD',summary['free_usd']),('Last estimated equity',summary['last_equity_estimate']),
           ('Reconciled fills',summary['reconciled_fills']),('Unresolved intents',summary['unresolved'])]
    card_html=''.join('<div class="card"><span>'+esc(k)+'</span><strong>'+esc(v)+'</strong></div>' for k,v in cards)
    positions=''.join('<tr><td>'+esc(pair)+'</td><td>'+('LONG' if p['direction']==1 else 'SHORT')+'</td><td>'+esc(p['quantity'])+'</td><td>'+esc(p['entry'])+'</td></tr>' for pair,p in summary['positions'].items())
    rows=''.join('<tr><td>'+esc(a['intent'])+'</td><td>'+esc(a['plan']['action'])+'</td><td>'+esc(a['status'])+'</td></tr>' for a in attempts[-50:])
    page='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dynamic Profits · Competition audit</title>
<style>body{font:16px system-ui;background:#101821;color:#e8eff5;max-width:1100px;margin:48px auto;padding:0 24px}h1{font-size:36px}p,span{color:#a7b9c9}.tag{color:#61d9b2;letter-spacing:2px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin:28px 0}.card{background:#1a2733;padding:24px;border-radius:12px}strong{display:block;margin-top:12px;font-size:26px}table{width:100%;border-collapse:collapse;margin:20px 0 38px}th,td{text-align:left;padding:14px;border-bottom:1px solid #314352}aside{border-left:3px solid #e8bb67;padding:12px 20px;color:#e8bb67}footer{font-size:13px;color:#a7b9c9;margin-top:40px}</style>
<p class="tag">DYNAMIC PROFITS / COMPETITION</p><h1>Execution audit</h1><p>Generated UTC: '''+esc(summary['generated_utc'])+'</p><p>Equity measured UTC: '+esc(summary['equity_measured_utc'] or 'No market mark recorded')+'</p><main>'+card_html+'</main><aside>Stop reason: '+esc(summary['stop_reason'] or 'None recorded')+' · Scheduled end: '+esc(summary['expires_utc'])+'</aside><h2>Recorded positions</h2><table><tr><th>Pair</th><th>Direction</th><th>Quantity</th><th>Entry</th></tr>'+positions+'</table><h2>Latest execution intents</h2><table><tr><th>Intent</th><th>Action</th><th>Status</th></tr>'+rows+'</table><footer>'+esc(summary['measurement_note'])+' The JSON export contains the complete event and execution history. Reconciled fills count order executions, not completed round trips. Days with fills are reported in GMT+8; organizer activity qualification is separate.</footer></html>'
    (output/'controller_audit.html').write_text(page,encoding='utf-8')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=Path('data/competition/execution.sqlite3'))
    parser.add_argument('--output',type=Path,default=Path('data/competition/report'))
    args=parser.parse_args()
    print(json.dumps(build(args.db,args.output),indent=2))
    print('Audit files saved in '+str(args.output))


if __name__=='__main__': main()
