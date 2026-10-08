# V4 updated rollout — 8 October 2026, IST

Use this updated ZIP instead of the earlier V4 installer. No live deployment has been performed from this workspace.

## 1. Windows: extract and upload through GitHub

Extract `Dynamic_Profits_V4_Active.zip`. Open PowerShell in the extracted directory containing `INSTALL_V4_ACTIVE.ps1`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\INSTALL_V4_ACTIVE.ps1
```

Default destination is `C:\Users\dasr3\Downloads\Dynamic_Profits_Starter\dynamic-profits-starter` on your account. For another location, append `-Project 'C:\your\project'`.

It verifies checksums, preserves recognized legacy source, runs tests, commits only package files and pushes `main`. Stop on errors and preserve the output. Do not chain another command onto the same PowerShell prompt without a newline. Keys and databases are not part of the ZIP or commit.

## 2. AWS: pull and check while the current bot continues

Use Session Manager on the existing Sydney instance:

```bash
cd /home/ssm-user/dynamic-profits-roostoo &&
git pull --ff-only origin main &&
bash CHECK_V4_ACTIVE_AWS.sh
```

Require all tests to pass and `V4_PREFLIGHT_OK`. Public preview can take several minutes for the universe. Errors or zero ready assets need investigation; do not force migration through a failed preflight.

Capture the baseline and calculate your next common handover window:

```bash
python3 -m competition_v4a.checkpoint --label before_upgrade
python3 -m competition_v4a.checkpoint --schedule
```

The schedule is printed only; it does not set reminders, deploy or place orders.

## 3. Deploy within the guarded window

Example next shared window at package preparation: **8 October, 3:48:00–3:49:10 PM IST** (10:18:00–10:19:10 UTC). Existing code requires UTC minutes 16–49 after the current hourly cycle completes. V3.2/original V4 additionally requires seconds 180–250 of the current completed five-minute cycle. The printed common window satisfies the clock portions of both gates; journal readiness is checked separately.

```bash
python3 -m competition_v4a.deploy --apply
systemctl show roostoo-competition -p ExecStart -p ActiveState --no-pager
sudo journalctl -u roostoo-competition --since '5 minutes ago' -n 40 --no-pager
python3 -m competition_v4a.checkpoint --label deployed
```

Require `ExecStart` to name `competition_v4a.controller`, ActiveState=active, then a fresh heartbeat/risk record. An active service alone is insufficient. Do not manually stop the service to bypass a gate. If a handover window is missed, run `--schedule` again and retry `--apply` at the next valid time; there is no need to repeat installation/tests.

If a migration committed but the service switch failed, preserve its journal and backup. Inspect the error and use `python3 -m competition_v4a.deploy --repair-service`; never run an old controller over the new journal. Unknown/unresolved orders must be reconciled, not deleted or resubmitted.

## 4. Testing timestamps

These timestamps assume successful deployment at **3:48 PM IST on 8 October**. Shift them relative to your actual start if different, or print an updated schedule:

```bash
python3 -m competition_v4a.checkpoint --schedule --start '2026-10-08T15:48:00+05:30'
```

| IST time | What to check | Evidence |
|---|---|---|
| Oct 8, 3:50 PM | Correct executor and fresh heartbeat | ExecStart=competition_v4a.controller; no startup traceback |
| Oct 8, 3:53 PM | Quotes, data coverage, account reconciliation | Quote age normally near cadence and below 25s admission limit; current data boundaries; no unresolved intents |
| Oct 8, 4:03 PM | First execution audit | If fills occurred: journal/account reconcile, no duplicate entries, cancels terminal; no forced test order if signals absent |
| Oct 8, 4:48 PM | First-hour activity and execution costs | Entry/exit/cancel counts, rejected signals, realized fill fees, fresh/expired orders, actual monitoring gaps |
| Oct 8, 6:48 PM | Three-hour diagnosis | Is inactivity explained by filters/costs/risk/latency? Compare fast and momentum outcomes; do not equate turnover with profit |
| Oct 9, 3:48 PM | 24-hour review | Net equity change from deployment baseline, costs and drawdown; separate inherited exposure; decide one justified improvement |

At each checkpoint, replace the label with `plus_2m`, `plus_5m`, `plus_15m`, `plus_1h`, `plus_3h`, or `plus_24h`:

```bash
systemctl is-active roostoo-competition
sudo journalctl -u roostoo-competition --since '15 minutes ago' -n 100 --no-pager
python3 -m competition_v4a.checkpoint --label plus_15m
python3 -m competition_v4a.report
```

Send the checkpoint JSON and recent logs. Never send credentials. A lack of fills is not itself a failure; inspect the rejection counters and data freshness. An unresolved intent, repeat API errors, missing held quotes or repeated quote gaps above 25 seconds is a concrete execution problem to investigate before increasing activity further.

## 5. Further improvement based on evidence

- High `STALE_STRATEGY_DATA`/quote-gap counts: investigate feed latency and API utilization first.
- Many canceled or expired fast limits: measure actual fill rate and execution latency before changing limit timeout or targets.
- High `PORTFOLIO_STOP_RISK_LIMIT`/correlation rejection: inherited holdings may consume capacity; do not erase state to bypass it.
- Repeated net losing fast trades: evaluate fees, slippage and setup quality using matched fills; use a committed strategy change rather than discretionary trades.
- No valid signals despite healthy data: evaluate recent one-minute history in replay; do not lower every filter simply to generate orders.

These are diagnosis checkpoints, not a request to wait 24 hours before starting an approved deployment. No settings will change automatically based on the printed schedule.
