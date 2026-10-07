# Dynamic Profits V3

V3 is a new controller built from the supplied V2.1 code. Installing its files does not replace the running service. Live migration is a separate explicit command. Existing journals and account positions must never be reset.

## What is implemented

- The original 24/72/168-hour trend core, plus V2.1's 14-day cross-sectional and 30-day time-series momentum targets. Twelve assets, one owner per asset.
- One portfolio allocator for every strategy: maximum five positions, 50% gross admission ceiling, 12% per-position ceiling, 0.5% modeled risk per position and 2.5% total modeled stop risk. Actual deployment is often substantially below the gross ceiling. These are admission limits, not guaranteed loss bounds; price moves can exceed them.
- Volume, spread, correlation, BTC regime, anti-chase and fee/slippage hurdle checks apply to all new entries, including sleeve entries. Core candidates get first claim; a sleeve gets a symbol only without a core claim. No forced rebalance merely to fill slots.
- Replacement of a core holding only when a new qualifying candidate has at least 1.5 times the score, a 0.5 score advantage, and a directional trend-move advantage exceeding twice the estimated combined trading cost. Minimum holding 24 hours, rotation interval 12 hours, maximum two rotations per GMT+8 day. The trend-move proxy is not a statistically calibrated expected return. A rotation exit is not atomic with its replacement: if conditions deteriorate, the bot can remain in cash.
- Quote-based protective exits on every risk cycle, including outside the hourly decision window and during a Binance outage. Exchange quote outages still prevent these exits. Default stop distance is 8%; risk checks are approximately five minutes plus API/processing time. Stops are software-side, not resting exchange orders.
- Configurable ATR stops, a one-time 50% partial profit, cost-aware breakeven and trailing exits. Absolute ATR and ATR fraction are explicitly distinct. These exits are OFF in the default policy because the supplied V2.1 research rejected them. The replay also evaluates the ATR alternative; this is an experiment, not an automatic promotion.
- Partial spot sales and partial short reductions reconcile quantities, fees, returned collateral and remaining positions; a server-forced full short close is handled.
- Drawdown ladder: half-size entries at 1.5% episode drawdown; flatten and pause at 3%; 24-hour pause measured from confirmed flattening; 48-hour reduced sizing afterwards. Below 94% of initial equity, sizing falls to one quarter. The lifetime peak is retained; a separate episode peak supports recovery. No entries bypass a pause. Drawdown triggers are not maximum-loss guarantees.
- Activity warning after 20:00 GMT+8 without fills. No directional probes, discretionary trims or trades solely to produce activity. This cannot guarantee the organizer's active-day requirement.
- Durable intent before each write, exact-ID reconciliation, account identity and source fingerprint checks, exclusive process lock, no blind retry of uncertain submissions.
- Bounded limit BUY handling for the TESTING account only: partial/full fill, timeout, exact-ID cancellation, fill/cancel race, lost cancel response and terminal reconciliation. No automatic market replacement or cancel-all operation. Competition execution stays MARKET; live maker accounting is not yet proven.
- Separate testing journal; read-only JSON/HTML audit including lifetime and recovery peaks, pauses, rotation decisions, reconciled fills and unresolved intents. Canceled unfilled orders do not count as fills.

## Differences from the pasted proposal

The actual uploaded V2.1 did not contain ATR exits or risk-based five-position sizing; its research said these had underperformed. V3 implements the optional ATR path but does not silently enable it. Rank-1's entry signals and holding durations remain unknown. V3 does not claim to reproduce its strategy or predict its returns.

## Requirements

Python 3.9+ standard library for the bot and safety tests. Research comparison additionally uses NumPy (the packaged run used 2.3.5). The existing repository must include competition_v2. If migrating from V2.1, its exact original competition_v21 source must also be present.

No API credentials or live journals are included. Never commit credentials or data/.

## Install and verify on Windows PowerShell

Extract the ZIP into the project root, alongside competition_v2, not inside it. The supplied package does not overwrite V2/V2.1 controllers.

```powershell
Set-Location "C:\Users\dasr3\Downloads\Dynamic_Profits_Starter\dynamic-profits-starter"
py -m unittest test_competition_v3 test_v3_limits -q
if ($LASTEXITCODE -ne 0) { throw "V3 tests failed" }
py -m competition_v3.controller --preview
if ($LASTEXITCODE -ne 0) { throw "Preview failed" }
```

Preview uses public APIs and writes only the V3 candle cache/rate-limiter database. It does not read account credentials or place orders. It models an empty $100k portfolio, not your actual holdings.

After verification, commit only the release files:

```powershell
git add competition_v3 v3_tests test_competition_v3.py test_v3_limits.py research_v3 validation/v3_comparison.json validation/v3_data_provenance.json V3_README.md V3_VALIDATION.md V3_FILES.sha256
git commit -m "Add V3 unified risk allocation and cost-aware rotation"
if ($LASTEXITCODE -ne 0) { throw "Commit failed; inspect output" }
git push origin main
```

## AWS read-only preparation

```bash
cd /home/ssm-user/dynamic-profits-roostoo
git pull --ff-only origin main
python3 -m unittest test_competition_v3 test_v3_limits -q
python3 -m competition_v3.controller --preview
python3 -m competition_v3.migrate --preflight
python3 -m competition_v2.report
sudo journalctl -u roostoo-competition -n 60 --no-pager
```

If V2.1 is running, use its report instead. The preflight detects the journal version. It requires the recognized legacy source fingerprint and no unresolved attempts. It preserves existing holdings even when V2.1 has more than five; new entries remain blocked until capacity becomes available through strategy exits.

## Testing account

Use only the separate testing credentials, saved locally with `purpose: TESTING`, `key` and `secret` at `~/.config/dynamic-profits/testing.json`, mode 600. Do not paste them into chat. The initializer requires a flat account, with no orders or positions managed by another testing controller. It does not adopt or close someone else's positions.

```bash
python3 -m competition_v3.controller --initialize-testing
python3 -m competition_v3.controller --execute-testing --watch
```

Testing is real API execution against the virtual testing account, with policy-sized orders; it is not paper mode. The journal is `data/v3_testing/execution.sqlite3`. Do not run this while the earlier testing-account controller is trading. Use public preview or offline replay when the testing account is occupied. Configuration changes after initialization require a reviewed migration; never delete an existing journal to bypass a fingerprint check.

## Competition migration (separate from installation)

Read V3_VALIDATION.md before choosing to activate V3. A passing test suite establishes tested engineering behavior, not a profit advantage. The deploy command verifies the current active controller, committed unchanged code, and an idle UTC minute 16–49 after the hourly cycle has completed. In India, those minute ranges are :46–:59 and :00–:19, but use the UTC check in the tool as authoritative.

```bash
python3 -m competition_v3.deploy --apply
systemctl show roostoo-competition -p ExecStart -p ActiveState --no-pager
sudo journalctl -u roostoo-competition -n 80 --no-pager
python3 -m competition_v3.report
```

This backs up the execution database, verifies account identity/holdings, migrates state, writes `40-architecture-v3.conf`, and starts the same service using competition_v3. It sends no discretionary orders during migration. Subsequent strategy cycles can trade. No second competition service is created.

A latched V2 drawdown stop is not silently cleared. An unknown intent or a passed competition deadline cannot be bypassed. If a migration fails after the journal changes, preserve the journal and backups and inspect logs; do not restore an old controller over new state. A process showing `active` is not proof that fills reconciled.

The inherited round deadline remains unchanged; `null` means no configured deadline. Confirm the organizer's exact end timestamp before scheduling one, and implement it as a committed configuration migration rather than resetting the journal.

## Research

Run `python3 research_v3/download.py` to obtain checksum-verified Binance monthly archives, then `python3 research_v3/compare.py --data history12`. This can take time and disk space. The download never needs exchange credentials. Dataset hashes are in the comparison JSON. The comparison invokes the actual controller decision and accounting functions through an offline exchange; it is not a parallel handwritten strategy simulator. Archived baseline source is isolated under research_v3/reference for reproducibility, not used by the live V3 process.

Sources checked: https://github.com/roostoo/Roostoo-API-Documents and https://luma.com/coghwiyt . Fees, endpoint semantics and competition rules can change; verify before changing execution assumptions.
