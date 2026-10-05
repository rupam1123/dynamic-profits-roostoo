# Short collateral fix and recovery

The observed Roostoo OPEN response for position 4742 reports quantity 0.00011 BTC, entry 85649.14, collateral 9.42 and opening fee 0.009421. The original validator incorrectly required the requested USD 10 collateral to be returned unchanged. The corrected validator checks rounded quantity against the requested budget and accepts reported collateral within one cent of executed notional (or the full requested budget).

Extract this patch into the existing Windows project. Replace short_roundtrip.py, add test_short_recovery.py and this document. Preserve the AWS execution journal and all data. Do not run --execute-testing again.

PowerShell, inside the project:

```powershell
py -m unittest test_roostoo_adapter test_roundtrip_checks test_next_stage test_short_recovery -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
git add short_roundtrip.py test_short_recovery.py SHORT_RECOVERY_README.md
git commit -m "Fix short collateral validation and add journaled test recovery"
if ($LASTEXITCODE -ne 0) { throw 'Commit failed; inspect output' }
git push origin main
```

On AWS:

```bash
cd /home/ssm-user/dynamic-profits-roostoo &&
git pull --ff-only origin main &&
python3 -m unittest test_short_recovery -v &&
python3 short_roundtrip.py --recover-testing --expected-position-id 4742
```

Type TEST ACCOUNT and use the SAME testing credentials that opened the position. This sends one actual mock-exchange short-close request only after matching the original account fingerprint, acknowledged position ID, pair, quantity, entry and collateral against the current single open position. It never opens another position. It refuses recovery if any previous close attempt is recorded. Do not run another program that modifies the testing account during recovery.

Expected completion: SHORT_RECOVERY_COMPLETE with open_short_positions: 0. The command also verifies an empty short-position list, zero locked collateral, zero spot BTC, and reconciles the free-USD balance change against returned collateral and closing PnL/fees. The original pre-open wallet was not persisted by the failed run, so this mode does not claim a full round-trip PnL.

If recovery halts, preserve its output and journal; do not delete or reset the journal or resubmit. A lost close response may still mean the close succeeded. Read-only reconciliation is then required. Inspect recorded responses with python3 short_roundtrip.py --report.

Sixteen offline tests pass for the complete bundle, including the exact observed opening response, identity mismatches, changed/missing positions, repeat recovery blocking, and a response lost after closing. Live closing is not verified until your AWS run completes.
