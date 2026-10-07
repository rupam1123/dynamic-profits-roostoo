# Dynamic Profits — final release (V3.1)

This freezes the actual uploaded V2.1 trading strategy and combines it with V3's durable order journal, account reconciliation and failure handling. It replaces the deployed V3 through an account-preserving migration. The Python module is `competition_v31`; the service remains `roostoo-competition`.

The previous V3's portfolio rotation and broad entry restrictions did not earn promotion in the retrospective comparison. They are removed. This is a tested baseline restoration, not a claim of a new profit edge, guaranteed returns or a winning rank. Rank-1's internal strategy is unknown.

## Trading rules that are frozen

- Twelve assets: BTC, ETH, SOL, BNB, XRP, ADA, DOGE, AVAX, LINK, DOT, LTC and NEAR.
- Hourly decisions use completed Binance candles. Execution and account reconciliation use Roostoo. Entries require the first 15 minutes of the hour; late entries are deferred.
- Core: aligned 24/72/168-hour momentum, volatility-scaled ranking/sizing, anti-chase filters, BTC regime, volume/spread and direction-adjusted correlation checks. At most three core positions, 10% entry budget per core position and 30% core gross budget.
- Two smaller portfolios retain V2.1 priority: cross-sectional 14-day momentum, strongest two long/weakest two short, rebalanced every 72 hours; and 30-day directional trend with inverse-volatility weights, rebalanced every 168 hours. Each has a 10% gross target. Targets rebalance on UTC epoch-hour boundaries. Same-direction positions are not continuously resized.
- A coin can have one owner. There can be up to twelve small positions overall; the three-position limit is only for the core. With the two portfolios taking priority, the core may often have no free assets. This matches the actual V2.1 code and replay.
- New core positions exit when weekly momentum reverses, their closed-hour price loss reaches 8%, or a probe expires. The small portfolios exit by their stored targets or portfolio risk control. No new ATR, breakeven, partial-profit or trailing-stop overlay is enabled.
- V2.1's daily activity guard remains: after 20:00 GMT+8 on a day without a fill it tries a smaller eligible entry, a 25% trim of the weakest long, a 2% time-limited directional probe, or closing the smallest short. The probe is based on 24-hour momentum and need not meet the core's normal entry filters. These trades incur costs, are not a demonstrated source of edge, and do not guarantee organizer activity qualification. They cannot bypass a risk pause, aggregate exposure ceiling or missing held-asset history.

The actual V2.1 source differs from the earlier proposed ATR/five-position specification. This release follows the supplied implementation that was tested, not the unvalidated proposal.

## Safety behavior

- Half-size core/new target sizing at 1.5% episode drawdown; flatten at 3%; 24-hour pause counted from confirmed flattening, then 48-hour reduced sizing. Below 94% of initial equity, the sizing multiplier is one quarter. Lifetime and recovery-episode peaks are separate. Existing holdings are not continuously scaled down by the soft threshold.
- Combined 50% gross entry admission ceiling includes the core, small portfolios and probes. Price moves or adopted legacy positions can exceed an admission ceiling; it is not a guaranteed exposure or loss maximum.
- Portfolio risk checks run approximately every five minutes plus API/processing time. New core stops use closed-hour candles. Adopted V3 holdings keep their existing quote-based stop distance until they close. Software stops depend on a functioning service and exchange quotes; they are not resting exchange orders.
- Before every write: fresh account reconciliation, fresh quotes and durable intent. An uncertain submission blocks subsequent orders; it is never blindly resent. A known acknowledgment can reconcile exactly once after restart. Partial fills/reductions and fees preserve account quantities and cash.
- One process lock, purpose-labeled credentials, account identity and source fingerprints prevent accidental competing controllers or unreviewed source changes. Signed requests use server time and a shared per-account limiter. Competition execution remains MARKET.

## Install on Windows PowerShell

Save `Dynamic_Profits_Final.zip` in Downloads. Paste the entire block at a fresh `PS ...>` prompt. This stages the ZIP, validates your installed V3, adds only this release's files, runs 42 offline tests, commits and pushes. Existing tracked edits cause it to stop for review; untracked data/ZIP files are not staged.

```powershell
$ErrorActionPreference = 'Stop'
$zip = Get-ChildItem -LiteralPath "$env:USERPROFILE\Downloads" -Filter 'Dynamic_Profits_Final*.zip' -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $zip) { throw 'Download Dynamic_Profits_Final.zip into Downloads first.' }
$stage = Join-Path $env:TEMP ('roostoo-final-' + [guid]::NewGuid().ToString('N'))
Expand-Archive -LiteralPath $zip.FullName -DestinationPath $stage
& (Join-Path $stage 'INSTALL_FINAL.ps1')
```

If PowerShell blocks execution of the downloaded script, invoke it for this process only:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $stage 'INSTALL_FINAL.ps1')
```

The installer expects the existing project at `C:\Users\dasr3\Downloads\Dynamic_Profits_Starter\dynamic-profits-starter`, on branch `main`. An alternative path can be passed with `-Project`. No credentials are requested or stored by the installer. A Git authentication prompt, if needed, is from your own Git installation.

## Deploy on the existing AWS instance

Run through Session Manager on your existing Sydney instance:

```bash
cd /home/ssm-user/dynamic-profits-roostoo &&
git pull --ff-only origin main &&
bash DEPLOY_FINAL_AWS.sh
```

This verifies release files, runs 42 tests under AWS's Python, previews public data and performs read-only migration preflight. Then it switches the same service using `competition_v31.deploy --apply`.

The switch requires UTC minutes **16–49**, a completed current-hour cycle and no unresolved order. If it reports an idle-window refusal, it leaves V3 running. After the next completed cycle, run:

```bash
python3 -m competition_v31.deploy --apply
```

Use `date -u` for the authoritative time. No extra 26-hour testing wait is imposed by this package.

The migration checks the exact V3 source, credentials/account identity, free cash, quantities, collateral and position IDs. It makes a SQLite backup, preserves positions, all attempts/events, lifetime peak, drawdown pause/recovery, cooldowns, target ownership and any existing deadline. It changes the new-entry policy and version/fingerprint, and appends the deployment commit. It sends no orders itself; later autonomous cycles can trade.

**Do not run `--initialize-competition`, delete a database, manually close holdings or launch a second competition process.** This is an upgrade of the existing journal.

## Confirm deployment

```bash
systemctl show roostoo-competition -p ExecStart -p ActiveState --no-pager
sudo journalctl -u roostoo-competition --since '10 minutes ago' -n 50 --no-pager
python3 -m competition_v31.report
```

Expected: `competition_v31.controller` in ExecStart; `ActiveState=active`; report `controller_version: competition-controller-3.1`, `unresolved: 0`; then a new `COMPETITION_RISK_CHECK` or `COMPETITION_CYCLE_COMPLETE` in the journal. A late-hour skip can be normal. New fills occur only when the strategy acts; a running service alone is not evidence of fills or leaderboard attribution. Read the report's equity timestamp before treating its equity as current.

The current deployment is not changed by downloading this package. It changes only when you run the AWS deployment command. This delivery was verified offline; AWS service health and real-account migration must be confirmed with the output above.

## If a service switch is interrupted

Before the journal commits, the helper requests restart of the original V3 service. After the journal commits, it never restarts V3 against the new state. Preserve the database and backup. Inspect the reported error, then finish the same migration with:

```bash
python3 -m competition_v31.deploy --repair-service
```

This mode only accepts an already-migrated V3.1 journal and reconciles the account before finishing the service override. Do not roll back the database while the exchange has newer trades. Unknown or rejected orders require reconciliation; resetting the journal is not recovery.

## Deadline and final submission

An inherited deadline is preserved and cannot be extended by this migration. `expires_utc: null` still means no scheduled end. The exact official cutoff has not been established in this package; do not infer it from a calendar date. Scheduling it requires a committed configuration migration using the organizer's explicit timestamp, rather than changing policy underneath the running fingerprint.

Keep this release frozen while collecting live evidence. The public repository, audit files and actual leaderboard attribution are the remaining submission evidence. The historical comparison is not a substitute for live performance.

## Reproduce validation

Runtime and safety tests require Python 3.9+ standard library. Replay alone requires NumPy. See `FINAL_VALIDATION.md` for the measured results and limits.

```bash
python3 final_release.py --verify
python3 -m unittest test_competition_v31 test_v31_deploy -q
# Optional research on a separate local machine, not a prerequisite for deployment:
python3 -m pip install numpy
python3 research_v3/download.py
python3 -m research_v31.compare_baseline --data history12
```

The final ZIP adds files only. It depends on the exact previous V3 package already present in the repository, including its archived V2.1 reference and fake-exchange fixtures. History, credentials, live state, caches and private account reports are excluded.
