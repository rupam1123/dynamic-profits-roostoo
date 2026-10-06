# Dynamic Profits competition release

The earlier 26-hour testing duration was an engineering choice, not a hackathon requirement. User-provided AWS logs now demonstrate two autonomous entries, a signal-driven SOL exit, three reconciled fills, and zero unresolved intents. The open testing BTC position can continue under its existing scheduled test closure while a separate competition account operates.

This release is a deployable baseline. It is not proof of a profitable strategy, compliance with activity criteria, or a winning entry. Offline tests do not establish official-account permissions; initialization checks those without orders. No competition credentials have been received by the assistant and no competition orders have been placed by it.

## Exact changes from the tested controller

- New `competition_bot/` package, `data/competition/` state and `~/.config/dynamic-profits/competition.json` credentials. The running test and paper files remain unchanged.
- Requires an initially flat official account near USD 100,000; rejects the saved testing key and a USD 50,000 wallet. Balance is only an extra guard; account purpose must be confirmed from the organizer's key labels.
- The original 168-hour momentum lookback and +/-1% entry thresholds are retained, with 12-hour cooldown and hourly zero-crossing/adverse-close exits.
- Entries are now considered on each completed hour, not just 00:00 UTC. This is an explicit strategy change, committed before use. Its profitability relative to daily entry has not been validated. There are no discretionary forced orders for leaderboard display.
- Target entry allocation is fixed at 10% of initial USD per coin, at most three positions. With an initial USD 100,000 balance this is approximately USD 10,000 per coin and USD 30,000 total entry allocation. Price movement, slippage, and fees mean this is not a guaranteed maximum marked exposure. Spot plans reserve fees/slippage; short entry fees are additional to collateral.
- Automated portfolio closure is triggered when the hourly estimated liquidation equity falls USD 3,000 from its recorded peak for a USD 100,000 start (3% of initial capital). This latches permanently and prevents further entries. Hourly monitoring and API availability mean it is not a guaranteed maximum loss. Existing 8% adverse completed-close exits remain.
- No arbitrary 26-hour cutoff. `policy.json` has `end_utc: null` by default and the service runs continuously. If the organizer supplies an exact end timestamp, set that timezone-qualified value in policy BEFORE initialization, commit it, and use the release consistently. The event page gives days, not a precise cutoff time; none is invented here. Exchange-side liquidation or account changes cause reconciliation to halt rather than silently replacing positions.
- Normal cycles are scheduled around minute 6 UTC of each hour to reduce overlap with the testing and paper runners at minute 2. Startup also evaluates the current hour if within its first ten minutes. Entries remain conditional, never guaranteed on startup.
- Acknowledged-fill recovery, durable intent journaling, process locking, account and code fingerprints, no automatic write retry, precise position checks, and local audit exports are retained.
- Audit reports count calendar dates with reconciled fills in GMT+8. That count is NOT a certification of the organizer's 'sufficient trades' requirement.

## 1. Windows: install, test, commit and push

Extract the supplied ZIP into the existing project. No existing controller modules are overwritten. Then run as a single PowerShell block:

```powershell
& {
    $ErrorActionPreference = 'Stop'
    Set-Location 'C:\Users\dasr3\Downloads\Dynamic_Profits_Starter\dynamic-profits-starter'
    py -m unittest test_competition_checks -v
    if ($LASTEXITCODE -ne 0) { throw 'Competition tests failed' }
    git add competition_bot test_competition_checks.py roostoo-competition.service COMPETITION_DEPLOYMENT.md
    if ($LASTEXITCODE -ne 0) { throw 'Git add failed' }
    git commit -m 'Add isolated competition deployment with hourly entries and capital limits'
    if ($LASTEXITCODE -ne 0) { throw 'Commit failed; inspect output' }
    git push origin main
    if ($LASTEXITCODE -ne 0) { throw 'Push failed' }
}
```

## 2. AWS: verify and configure official credentials

In Session Manager:

```bash
cd /home/ssm-user/dynamic-profits-roostoo &&
git pull --ff-only origin main &&
python3 -m unittest test_competition_checks -v &&
python3 -m competition_bot.controller --preview &&
python3 -m competition_bot.setup &&
python3 -m competition_bot.controller --initialize-competition &&
python3 -m competition_bot.controller --check-competition
```

Type COMPETITION ACCOUNT and enter the official competition key and secret at the hidden prompts. This entire block uses only public/read-only exchange calls; it does not place orders. Expected final statuses: COMPETITION_CONTROLLER_INITIALIZED and COMPETITION_ACCOUNT_RECONCILED.

If the credentials file already exists, do not delete it blindly. If initialization already succeeded, skip initialization and use the check while the service is not running. Never delete/reset `data/competition/` to bypass a failed check. If the official wallet is not flat or its initial balance differs, inspect its read-only state and organizer instructions rather than weakening checks to proceed.

## 3. AWS: activate autonomous competition orders

Only after the block above succeeds:

```bash
sudo cp roostoo-competition.service /etc/systemd/system/ &&
sudo systemctl daemon-reload &&
sudo systemctl enable --now roostoo-competition &&
sudo systemctl status roostoo-competition --no-pager
```

This starts autonomous orders on the official competition account. There is no dependency on the test's remaining timer. Do not run a second copy or another trading process on that account. Keep the EC2 instance running; Session Manager can disconnect.

After the first minute:

```bash
sudo journalctl -u roostoo-competition -n 40 --no-pager
python3 -m competition_bot.report
```

Normal statuses include COMPETITION_LATE_HOUR_SKIPPED, COMPETITION_CYCLE_COMPLETE and, when a strategy order executes, COMPETITION_FILL_RECONCILED. Verify that orders also appear under the correct team on the organizer's leaderboard; no local status alone proves leaderboard attribution.

If COMPETITION_HALTED_RECONCILE_REQUIRED appears, retain the database and exact message. Unknown submissions may have succeeded; never resubmit or remove the journal. The service does not restart exit 78. An ordinary data/network error retries after 60 seconds without repeating writes. Code/policy changes require a reviewed migration and traceable Git history; editing running fingerprinted files can halt the next cycle.

## 4. Submission and activity requirements

The event page checked October 6 lists live trading October 4-17, at least eight active trading days with sufficient strategy-generated trades, and repository submission before October 14. It requires autonomous trade execution, transparent commits, open-source code, and forbids HFT, market-making and arbitrage. This release uses a directional hourly momentum strategy, not any of those prohibited styles.

Submit https://github.com/rupam1123/dynamic-profits-roostoo through the organizer's official submission channel before the stated deadline. The assistant has not submitted it. Preserve fill logs, read-only reports and the deployed commit identifier:

```bash
git rev-parse HEAD
python3 -m competition_bot.report
```

Reports are under `data/competition/report/`. Include the strategy changes and limitations in the main repository README before submission. Never publish credentials. This strategy can hold positions across several days and therefore may not produce eight qualifying active days; monitor actual fills and obtain the organizer's precise definition. Do not claim eligibility or manufacture trades. Performance research and rule verification can proceed while this versioned baseline runs.

Primary references:
- https://luma.com/coghwiyt
- https://github.com/roostoo/Roostoo-API-Documents

## Validation performed

Eighteen offline controller tests pass, including long/short cycles, acknowledgement recovery, unknown-write blocking, partial-fill rejection, account separation, no test deadline, hourly entries, late-hour gates, configured drawdown and expiry handling, duplicate suppression and audit export. Python 3.9 syntax compatibility was checked. Actual official-key access, official trading authorization, future returns, and leaderboard participation remain to be verified on AWS and the organizer's frontend.
