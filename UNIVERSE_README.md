# Full-universe research and public-data collector

This is an addition to the deployed **V3.1 project**, not a replacement trading controller. It discovers every current Roostoo USD asset, verifies exact Binance USDT symbol mappings, and collects completed hourly candles. No credentials are requested and no orders are sent. The existing competition service continues unchanged.

Read `UNIVERSE_RESULTS.md` for the measured comparison and deployment decision. More data and more symbols do not guarantee higher returns.

## Windows: install and upload the collector

1. Extract `Dynamic_Profits_Universe_Study.zip` into a new folder in Downloads.
2. Open PowerShell in that extracted folder and run:

```powershell
& .\INSTALL_UNIVERSE.ps1
```

The default project is `C:\Users\dasr3\Downloads\Dynamic_Profits_Starter\dynamic-profits-starter`. For another location, pass `-Project 'C:\path\to\project'`.

After the seven standard-library tests pass, upload only the new files:

```powershell
git add universe_research test_universe_watch.py test_universe_research.py START_UNIVERSE_WATCH_AWS.sh UNIVERSE_README.md UNIVERSE_RESULTS.md
if ($LASTEXITCODE -ne 0) { throw 'Git add failed' }
git diff --cached --stat
git commit -m "Add full-universe public data collection and matched research"
if ($LASTEXITCODE -ne 0) { throw 'Commit failed; inspect output' }
git push origin main
if ($LASTEXITCODE -ne 0) { throw 'Push failed; inspect output' }
```

Inspect the staged file list before committing. Candle data, archive caches and Binance's large exchange metadata response are ignored by the new directory's `.gitignore`. No API secrets or trading journal belongs in this package.

## AWS: start continuous data collection

In the existing SSM terminal:

```bash
cd /home/ssm-user/dynamic-profits-roostoo &&
git pull --ff-only origin main &&
bash START_UNIVERSE_WATCH_AWS.sh
```

Then inspect:

```bash
systemctl is-active roostoo-competition roostoo-universe-watch
sudo journalctl -u roostoo-universe-watch -n 15 --no-pager
```

A completed scan prints `PUBLIC_SCAN_SAVED ... assets ... eligible_crypto ...`. The first scan takes several minutes; later scans run hourly, shortly after the hourly candle closes. This service does not restart or edit `roostoo-competition`.

Read its current eligibility results:

```bash
python3 - <<'PY'
import json
from pathlib import Path
p = Path('data/universe_watch/latest.json')
if not p.exists():
    print('First scan has not finished; inspect the service log.')
else:
    r = json.loads(p.read_text())
    print('Scan UTC:', r['generated_utc'])
    for asset, row in r['assets'].items():
        print(asset, row['reason'], 'hours=', row.get('closed_hours'), '24h volume=', row.get('quote_volume_24h'))
PY
```

Outputs: `data/universe_watch/latest.json` and `data/universe_watch/candles.sqlite3`. Each scan stores up to 1,000 recent completed hourly candles per mapped active asset and accumulates new candles over time. This is market-data research; the live bot does not consume this new database yet. A restart after an outage longer than 1,000 hours cannot automatically fill older gaps.

## What is admitted for research

The catalog contains both crypto and tokenized-stock listings. Stock candles are collected but excluded from this crypto-strategy comparison. Unavailable markets, unverified mappings, stale/gapped history and invalid exchange rules are flagged, not silently renamed or filled with invented prices.

Extra crypto candidates require 721 consecutive completed hourly candles and at least 5 million USDT of quote volume in the previous 24 hours. The public audit additionally requires a fresh Roostoo quote and spread no wider than 0.3%. It records momentum, volatility, anti-chase features, price/quantity precision and minimum order rules. USDT volume is a USD proxy.

Newly discovered assets are collected automatically; they are marked `evaluated_universe: false` until included in a new study. Passing a current data check does not authorize trading or prove a profitable signal. Old held positions retain their exit signals even if an extra asset loses liquidity eligibility.

## Reproduce the offline comparison

Extract the optional `Dynamic_Profits_Universe_Data.zip` into the project root. It supplies `history12/`, `universe_research/data/` and a separate public current-candle snapshot. Do not commit these datasets. Install NumPy in a research environment:

```bash
python3 -m pip install numpy
python3 -m unittest test_universe_research test_universe_watch -q
python3 -m universe_research.compare --small
python3 -m universe_research.compare
```

On Windows replace `python3` with `py`. The collector itself uses only the standard library plus the existing V3.1 pure strategy module; it does not require NumPy.

Alternatively fetch archives yourself with `python3 -m universe_research.download --workers 6`. Cached archives are SHA-256 checked against Binance CHECKSUM files. Missing baseline files are also downloaded. `python3 -m universe_research.catalog` refreshes the catalog and research specification; that changes the study universe, so retain the supplied evidence/specification to reproduce this exact run.

The replay executes the existing production controller against a fake exchange and an in-memory journal. It never contacts an account. It compares 12, 22 and 65-symbol policies on identical windows/cost assumptions, with API pacing and the existing decision deadline. The static live policy is never overwritten.

The supplied historical dataset ends September 30, 2026; the continuous collector supplies recent observations. There is no claim of a gap-free merged dataset between those two sources. See data inventory and per-asset provenance for actual coverage, missing archives and listing start dates.

## Diagnose low live order frequency

After uploading, run this on AWS and paste its output:

```bash
python3 -m universe_research.activity_report --days 7
```

This opens the existing competition journal in SQLite read-only mode. It reports selection/deferral reasons, pauses, pending reconciliation and recorded actions. It does not place trades, restart services or reset state. The leader's order count and turnover do not identify its strategy; order size, fills versus cancelled orders, fees, exposure and measurement periods must also be comparable.

The additional offline activity study changes only portfolio rebalance intervals from 72/168 hours to 24/72 hours. Run `python3 -m universe_research.activity_study` to reproduce it after installing the datasets and NumPy. This is a single exploratory hypothesis, not a parameter search or automatic deployment.
