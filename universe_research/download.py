"""Public checksum-verified 1h archives for the full exchange catalog. No account access."""
import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parent
SPEC = json.loads((ROOT/'spec.json').read_text())
DATA = ROOT/'data'
CACHE = DATA/'archives'


def fetch(asset, year, month):
    name = f'{asset}USDT-1h-{year}-{month:02}.zip'
    url = 'https://data.binance.vision/data/spot/monthly/klines/'+asset+'USDT/1h/'+name
    path = CACHE/name
    for attempt in range(3):
        try:
            payload = path.read_bytes() if path.exists() else urlopen(url, timeout=25).read()
            checksum = path.with_suffix('.sha256')
            expected = checksum.read_text().strip() if checksum.exists() else urlopen(url+'.CHECKSUM', timeout=25).read().decode().split()[0]
            if hashlib.sha256(payload).hexdigest() != expected:
                if path.exists():path.unlink()
                raise ValueError('Archive checksum mismatch: '+name)
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                rows = list(csv.reader(io.TextIOWrapper(archive.open(archive.namelist()[0]))))
            temporary=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
            temporary.write_bytes(payload);temporary.replace(path)
            temporary=checksum.with_name(checksum.name+'.'+str(os.getpid())+'.tmp')
            temporary.write_text(expected);temporary.replace(checksum)
            return dict(asset=asset, year=year, month=month, status='OK', sha256=expected, rows=rows)
        except HTTPError as exc:
            if exc.code == 404:
                return dict(asset=asset, year=year, month=month, status='ARCHIVE_ABSENT')
            if attempt == 2: raise
        except Exception:
            if attempt == 2: raise
        time.sleep(attempt+1)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers',type=int,default=6)
    args=parser.parse_args()
    if not 1<=args.workers<=24:parser.error('--workers must be between 1 and 24')
    CACHE.mkdir(parents=True, exist_ok=True)
    assets=[a for a in SPEC.get('collect_assets',SPEC['additional']) if a not in SPEC['baseline'] or not list((ROOT.parent/'history12').glob(a+'USDT_1h_*.csv'))]
    catalog=json.loads((ROOT/'evidence/catalog.json').read_text())['assets'] if (ROOT/'evidence/catalog.json').exists() else {}
    tasks=[]
    for asset in assets:
        first=catalog.get(asset,{}).get('first_open_utc','2025-01-01T00:00:00+00:00')
        date=datetime.fromisoformat(first)
        tasks.extend((asset,y,m) for y in (2025,2026) for m in range(1,13) if max((2025,1),(date.year,date.month))<=(y,m)<=(2026,9))
    all_rows = {a:[] for a in assets}; evidence=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch,*task) for task in tasks]
        for count, future in enumerate(as_completed(futures),1):
            record = future.result()
            all_rows[record['asset']].extend(record.pop('rows',[]));evidence.append(record)
            if count%30==0:print('ARCHIVES',count,'/',len(tasks),flush=True)
    summary={}
    for asset, rows in all_rows.items():
        rows.sort(key=lambda r:int(r[0]));previous=None;gaps=[]
        folder=ROOT.parent/'history12' if asset in SPEC['baseline'] else DATA
        folder.mkdir(parents=True,exist_ok=True)
        output=folder/(asset+'USDT_1h_2025-01-01_2026-09-30.csv')
        with output.open('w',newline='') as f:
            writer=csv.writer(f);writer.writerow(['open_time_utc','open','high','low','close','volume','quote_volume'])
            for row in rows:
                stamp=int(row[0]);stamp=stamp//1000000 if stamp>10**14 else stamp//1000
                if stamp%3600:raise ValueError('Unaligned candle '+asset)
                if previous is not None:
                    if stamp<=previous:raise ValueError('Duplicate/unordered candle '+asset)
                    if stamp-previous!=3600:gaps.append([previous,stamp])
                previous=stamp
                writer.writerow([datetime.fromtimestamp(stamp,timezone.utc).isoformat()]+row[1:6]+[row[7]])
        summary[asset]=dict(rows=len(rows),gaps=gaps,sha256=hashlib.sha256(output.read_bytes()).hexdigest())
        if rows:
            for key,row in [('first',rows[0]),('last',rows[-1])]:
                stamp=int(row[0]);stamp=stamp//1000000 if stamp>10**14 else stamp//1000
                summary[asset][key]=datetime.fromtimestamp(stamp,timezone.utc).isoformat()
        print(asset,json.dumps(summary[asset]),flush=True)
    evidence.sort(key=lambda r:(r['asset'],r['year'],r['month']))
    (ROOT/'evidence').mkdir(exist_ok=True)
    (ROOT/'evidence/history_provenance.json').write_text(json.dumps(dict(archives=evidence,assets=summary),indent=2)+'\n')


if __name__=='__main__':main()
