"""Download checksum-verified Binance monthly 1h archives for the fixed universe."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import csv, hashlib, io, json, zipfile, urllib.request, time
ASSETS='BTC ETH SOL BNB XRP ADA DOGE AVAX LINK DOT LTC NEAR'.split()
ROOT=Path(__file__).resolve().parents[1]/'history12'
ROOT.mkdir(exist_ok=True)
CACHE=ROOT/'archives'; CACHE.mkdir(exist_ok=True)
def fetch(a,y,m):
 name=f'{a}USDT-1h-{y}-{m:02}.zip';p=CACHE/name
 url='https://data.binance.vision/data/spot/monthly/klines/'+a+'USDT/1h/'+name
 for n in range(3):
  try:
   payload=p.read_bytes() if p.exists() else urllib.request.urlopen(url,timeout=25).read()
   chk=urllib.request.urlopen(url+'.CHECKSUM',timeout=25).read().decode().split()[0] if not p.with_suffix('.sha256').exists() else p.with_suffix('.sha256').read_text()
   if hashlib.sha256(payload).hexdigest()!=chk:raise ValueError('Checksum mismatch')
   p.write_bytes(payload);p.with_suffix('.sha256').write_text(chk)
   with zipfile.ZipFile(io.BytesIO(payload)) as z: rows=list(csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]))))
   return a,y,m,rows,chk
  except Exception:
   if n==2:raise
   time.sleep(n+1)
def main():
 tasks=[(a,y,m) for a in ASSETS for y in (2025,2026) for m in range(1,13) if (y,m)<=(2026,9)]
 out={a:[] for a in ASSETS};manifest=[]
 with ThreadPoolExecutor(max_workers=6) as pool:
  fs=[pool.submit(fetch,*x) for x in tasks]
  for n,f in enumerate(as_completed(fs),1):
   a,y,m,rows,h=f.result();out[a]+=rows;manifest.append(dict(asset=a,year=y,month=m,sha256=h))
   if n%36==0:print('ARCHIVES',n,'/',len(tasks),flush=True)
 for a,rows in out.items():
  rows.sort(key=lambda r:int(r[0]));prev=None
  p=ROOT/f'{a}USDT_1h_2025-01-01_2026-09-30.csv'
  with p.open('w') as f:
   w=csv.writer(f);w.writerow(['open_time_utc','open','high','low','close','volume','quote_volume'])
   for r in rows:
    t=int(r[0]);t=t/1000000 if t>10**14 else t/1000
    if prev is not None and t-prev!=3600:raise ValueError('Gap '+a)
    prev=t;w.writerow([datetime.fromtimestamp(t,timezone.utc).isoformat()]+r[1:6]+[r[7]])
  print(a,len(rows),flush=True)
 (ROOT/'provenance.json').write_text(json.dumps(manifest,indent=2))
if __name__=='__main__':main()
