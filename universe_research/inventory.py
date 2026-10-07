"""Validate downloaded OHLCV files and record their actual coverage and hashes."""
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from .download import ROOT, SPEC


def main():
    inventory={}
    for asset in SPEC['collect_assets']:
        folder=ROOT.parent/'history12' if asset in SPEC['baseline'] else ROOT/'data'
        path=next(folder.glob(asset+'USDT_1h_*.csv'))
        count=0;first=None;last=None;gaps=[]
        with path.open() as stream:
            for row in csv.DictReader(stream):
                stamp=int(datetime.fromisoformat(row['open_time_utc']).timestamp())
                values=[Decimal(row[k]) for k in ('open','high','low','close','volume','quote_volume')]
                if not all(v.is_finite() for v in values):raise ValueError('Nonfinite '+asset)
                op,hi,lo,cl,vol,qv=values
                if min(op,hi,lo,cl)<=0 or min(vol,qv)<0 or not lo<=min(op,cl)<=max(op,cl)<=hi:raise ValueError('Invalid OHLCV '+asset)
                if stamp%3600 or (last is not None and stamp<=last):raise ValueError('Unordered/unaligned '+asset)
                if last is not None and stamp-last!=3600:gaps.append([last,stamp])
                if first is None:first=stamp
                last=stamp;count+=1
        def iso(t):return datetime.fromtimestamp(t,timezone.utc).isoformat() if t is not None else None
        inventory[asset]=dict(rows=count,first_open_utc=iso(first),last_open_utc=iso(last),gaps=gaps,
            relative_path=str(path.relative_to(ROOT.parent)),sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    result=dict(total_assets=len(inventory),assets_with_history=sum(r['rows']>0 for r in inventory.values()),
        total_validated_candles=sum(r['rows'] for r in inventory.values()),assets=inventory)
    output=ROOT/'evidence/data_inventory.json';output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='assets'}))


if __name__=='__main__':main()
