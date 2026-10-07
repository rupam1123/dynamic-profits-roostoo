"""One offline faster-rebalance hypothesis; never deploys or sends orders."""
import json
import numpy as np
from datetime import datetime, timezone
from . import compare as c


def main():
    spec=json.loads((c.ROOT/'activity_spec.json').read_text())
    stamps,data,qv,valid,hashes=c.load()
    windows=[]
    for start in c.SPEC['starts_gmt8']:
        ts=int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())-8*3600
        first=int(np.searchsorted(stamps,ts));windows.append(list(range(first,first+c.SPEC['window_days']*24)))
    prepared,_,error=c.prepare(stamps,data,qv,valid,{i for w in windows for i in w})
    metadata=json.loads((c.ROOT/'evidence/roostoo_exchange.json').read_text())
    result=dict(spec=spec,mode='OFFLINE_RESEARCH_ONLY',data_sha256=hashes,feature_max_absolute_error=error,rows=[])
    output=c.ROOT/'evidence/activity_comparison.json'
    if output.exists():
        old=json.loads(output.read_text())
        if old['spec']!=spec or old['data_sha256']!=hashes:raise ValueError('Resume inputs changed')
        result['rows']=old['rows']
    done={r['start_gmt8'] for r in result['rows']}
    for start,window in zip(c.SPEC['starts_gmt8'],windows):
        if start in done:continue
        row=c.run('expanded',window,stamps,data,prepared,spec['slippage_bps']/10000,metadata,spec)
        result['rows'].append(dict(start_gmt8=start,result=row))
        tmp=output.with_suffix('.tmp');tmp.write_text(json.dumps(result,indent=2)+'\n');tmp.replace(output)
        print(start,round(row['return_pct'],4),row['fills'],flush=True)
    rows=[r['result'] for r in result['rows']]
    result['summary']=dict(mean_return_pct=float(np.mean([r['return_pct'] for r in rows])),median_return_pct=float(np.median([r['return_pct'] for r in rows])),worst_drawdown_pct=max(r['max_drawdown_pct'] for r in rows),mean_fills=float(np.mean([r['fills'] for r in rows])),mean_gross_pct=float(np.mean([r['mean_gross_pct'] for r in rows])))
    output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result['summary']),flush=True)


if __name__=='__main__':main()
