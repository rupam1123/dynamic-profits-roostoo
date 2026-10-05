# Dynamic Profits: bounded autonomous testing controller

This release connects the candle-based momentum baseline to the tested Roostoo spot and short APIs. It is a testing release, not a competition release or evidence of profitability. No credentials or execution journals are bundled.

## Delivered functionality

- Autonomous spot BUY/SELL and collateral-backed SHORT_OPEN/SHORT_CLOSE for BTC/USD, ETH/USD and SOL/USD.
- Existing completed-candle 168-hour momentum baseline: enter at 00:00 UTC above +1% or below -1%, exit hourly on sign reversal or an 8% adverse completed-hour close, with a 12-hour cooldown. At most one normal strategy decision per pair per hour; a latched risk/deadline closure can override that limit. A data-ready LONG label does not necessarily mean an immediate entry.
- Fixed testing sizing: target USD 10 per pair, at most three positions, no pyramiding. Spot quantity includes a price allowance and fee reserve. Market fills and marked values may differ from the approximate USD 30 aggregate entry target; this is not a guaranteed dollar exposure bound. Short collateral handles the observed USD 9.42 response correctly.
- Public-data preview by default mode selection, explicit testing initialization and execution modes, and account/code fingerprints.
- Durable SQLite intentions before submission; acknowledgements, fills, positions, balances and event history. A single process lock prevents overlapping controller instances on the same machine/state directory.
- Restart recovery for acknowledged orders using exchange reads. Unknown or rejected writes, partial fills, external holdings changes, and account/code mismatches halt execution. No automatic write retries. An UNKNOWN/SENDING intent requires investigation because an order may have succeeded.
- Freshness checks, contiguous completed candles, no look-ahead, spread filter on entries, expected-balance and position matching before more orders.
- A scheduled 26-hour test finish and an hourly USD 3 drawdown trigger. Each latches a no-new-entry state and attempts to close positions, then exits when flat. These controls are not guaranteed loss limits: monitoring is hourly, fills can move, and outages or unresolved orders can prevent closure.
- Read-only JSON and standalone HTML audit reports. Credentials are stored outside the repository with Linux file mode 600.

This release uses existing `roostoo_adapter.py` and `candle_feed.py` from the previous installed update. Those files are not overwritten by this patch, preserving the running paper experiment's code fingerprint. Tested reference copies are included under `controller_reference/` only.

## Step 1: Windows verification and commit

Extract the ZIP into the project. In PowerShell run the following as one block:

```powershell
& {
    $ErrorActionPreference = 'Stop'
    Set-Location 'C:\Users\dasr3\Downloads\Dynamic_Profits_Starter\dynamic-profits-starter'
    py -m unittest test_controller_checks -v
    if ($LASTEXITCODE -ne 0) { throw 'Controller tests failed' }
    git add test_controller.py test_controller_checks.py configure_test_controller.py controller_report.py roostoo-test-controller.service CONTROLLER_GUIDE.md controller_reference
    if ($LASTEXITCODE -ne 0) { throw 'Git add failed' }
    git commit -m 'Add bounded autonomous testing controller and audit reporting'
    if ($LASTEXITCODE -ne 0) { throw 'Git commit failed; inspect output' }
    git push origin main
    if ($LASTEXITCODE -ne 0) { throw 'Git push failed' }
    Write-Host 'CONTROLLER UPLOADED'
}
```

Tests use fake exchanges and no API keys. Sixteen controller tests cover autonomous long/short cycles, duplicate suppression, acknowledged-fill recovery, lost responses, rejected/partial fills, changed accounts/holdings/code, scheduled closure without candles, drawdown closure, process locking and audit output. Python 3.9 syntax was checked; run these tests on AWS as the runtime check.

## Step 2: AWS pull, tests and public preview

```bash
cd /home/ssm-user/dynamic-profits-roostoo &&
git pull --ff-only origin main &&
python3 -m unittest test_controller_checks -v &&
python3 test_controller.py --preview
```

Expected: tests pass, CANDLES_READY for three symbols and PUBLIC_PREVIEW_ONLY. No credentials or orders are used. Stop here if an error appears and preserve its exact output.

## Step 3: configure and initialize the SAME testing account

Do this only after the previous tests and public preview succeed, with position 4742 already closed and the testing wallet otherwise unused. Do not use competition credentials.

```bash
python3 configure_test_controller.py &&
python3 test_controller.py --initialize-testing &&
python3 test_controller.py --check-testing
```

Type TEST ACCOUNT and enter the same testing key and secret at hidden prompts. No credentials are shown, written to the repository, or sent in chat. Configuration is saved to `/home/ssm-user/.config/dynamic-profits/testing.json`, mode 600. Existing credentials are never overwritten by the helper; if it says they already exist, use `--initialize-testing` only if not already initialized, then `--check-testing`.

Expected: TEST_CONTROLLER_INITIALIZED and TEST_ACCOUNT_RECONCILED. Initialization requires a flat wallet near USD 50,000 and no pending orders. This numerical check is only an extra guard; the API does not independently identify testing versus competition keys. The 26-hour test clock starts at initialization, so start the service promptly afterwards. Never reset the database to restart or extend the test.

## Step 4: start autonomous TESTING orders on AWS

This step enables actual mock-exchange orders according to the programmed strategy. Do not run the controller on a second machine or run other order scripts against this testing account.

```bash
sudo cp roostoo-test-controller.service /etc/systemd/system/ &&
sudo systemctl daemon-reload &&
sudo systemctl enable --now roostoo-test-controller &&
sudo systemctl status roostoo-test-controller --no-pager
sudo journalctl -u roostoo-test-controller -n 20 --no-pager
```

The existing collector and both paper services can remain running. This controller uses its own `data/test_controller/` state and independent candle database. Keep the EC2 instance and credentials file available. Session Manager may be disconnected; the service continues.

Normal idle output is TEST_LATE_HOUR_SKIPPED, TEST_ALREADY_PROCESSED_HOUR or TEST_CYCLE_COMPLETE with no actions. Entries occur around 00:02 UTC (05:32 IST), subject to valid data and signals; initialization alone does not force a trade. There is no guarantee a particular coin will trade.

On order execution expect TEST_FILL_RECONCILED. This means the fill and actual account state were reconciled. A write whose result is uncertain produces TEST_HALTED_RECONCILE_REQUIRED and exits 78. The unit deliberately does not restart that exit. Preserve the database and inspect the report; do not repeat the order or delete the database.

The service checks at hourly boundaries, retries read/data failures after 60 seconds, and checks the scheduled expiry at its deadline. Expiry/drawdown closure needs current exchange data and reachable APIs but does not depend on downloading candle history. It exits successfully after becoming flat. An enabled systemd unit can start again at instance boot; the persisted deadline/stop latch prevents new entries after the test completes.

The imported client paces its own calls, including timestamp requests, at 3.1 seconds or more. There is no distributed limiter across arbitrary other clients; avoid additional API test scripts while this controller is executing. Current collector/paper traffic is low, but the organizer's account-wide call limit still applies.

## Step 5: inspect evidence without interfering with the running process

```bash
python3 test_controller.py --report
python3 controller_report.py
sudo journalctl -u roostoo-test-controller -n 30 --no-pager
```

The JSON/HTML reports are at `data/test_controller/report/controller_audit.json` and `controller_audit.html`. They are local exports, not a public dashboard or a fresh price feed. Reconciled-fill count counts order legs, not completed round trips. Market equity is timestamped and estimated using bid/ask and closing fees. No Sharpe or profitability claim is made from the short execution test.

Do not start `--execute-testing` manually while the systemd unit is running. The process lock prevents this. The `--check-testing` mode also requires the lock; use the journal report for monitoring an active service.

## Remaining competition gates

1. AWS tests, public feed and authenticated initialization must pass on the actual instance.
2. Observe an autonomous entry and exit, with exchange balance/position reconciliation and no unexplained orders. Do not manufacture trades if the signal is flat. If the test never enters, a trade-cycle gate remains unverified.
3. Review continuous-run and restart evidence. Offline crash tests are implemented; an AWS restart alone does not prove recovery from an actual interrupted API write. Uncertain submissions deliberately block and require investigation.
4. Validate strategy choices on held-out periods and forward observations, accounting for commissions, slippage, concentration and organizer activity requirements. The original strategy remains an unproven baseline; this release changes execution, not its historical evidence.
5. Create a separate competition configuration and versioned state with appropriate capital sizing, continuous duration and confirmed organizer rules. This testing controller must not be repointed at the competition account or enlarged ad hoc. Its USD 10 sizing, USD 3 drawdown test trigger and 26-hour deadline are testing controls.
6. Deploy the validated competition release, confirm leaderboard activity, and submit the public repository with clear methodology, logs and reproducible results.

Competition activation has not occurred. No return or placing is guaranteed.
