# Dynamic Profits: recent candles, forward paper trading, and short execution test

This update removes the seven-day data collection wait. It does not establish a profitable strategy or activate competition trading. All modules use the Python standard library and are compatible with Python 3.9+.

## What is included

- `candle_feed.py`: requests the latest 200 completed hourly Binance BTCUSDT, ETHUSDT and SOLUSDT candles; requires at least 169, checks timestamps, OHLCV values, gaps, duplicates, recency and clock alignment before saving all three pairs in one transaction.
- `candle_paper.py`: seeds the existing 168-hour momentum baseline from these candles and uses current Roostoo bid/ask quotes for future simulated decisions. It makes public data requests only. History supplies signals; historical trades and profits are not added to forward results.
- `short_roundtrip.py`: testing-account execution check. It opens a BTC short using USD 10 collateral, verifies the position and balance, then closes it automatically and reconciles the result. This script sends actual orders to the Roostoo mock exchange when explicitly selected.
- `test_next_stage.py` and existing dependency/test files: offline tests including candle validation, paper restart behavior, short reconciliation, permission denial, and ambiguous submissions.
- `roostoo-candle-paper.service`: an additional AWS service. Existing collector and snapshot paper services can remain running.

## Install on Windows

Copy the files from this archive into your existing project directory. Existing dependency files are included for reproducibility; preserve any newer local changes before replacing them. No credentials, databases or historical results are included.

Run from PowerShell in the project:

```powershell
py -m unittest test_roostoo_adapter test_roundtrip_checks test_next_stage -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed; do not publish this update.' }
git add candle_feed.py candle_paper.py short_roundtrip.py test_next_stage.py NEXT_STAGE_README.md roostoo-candle-paper.service
git commit -m "Add validated candle bootstrap, forward paper runner and short execution check"
git push origin main
```

If Git reports modified dependency files, review those changes and commit the required dependencies too. Never add `.env`, keys, execution journals or databases to Git.

## Verify and start on AWS

Use the existing instance through Session Manager:

```bash
cd /home/ssm-user/dynamic-profits-roostoo &&
git pull --ff-only origin main &&
python3 -m unittest test_roostoo_adapter test_roundtrip_checks test_next_stage -v &&
python3 candle_paper.py
```

Successful data access prints three `CANDLES_READY` rows and `CANDLE_PAPER_ONLY`. A repeat during the same hour can print `ALREADY_PROCESSED_HOUR`. `LATE_HOUR_SKIPPED` is expected when starting more than ten minutes after the hour. This is different from missing history: candles are already available, but execution waits for a timely future observation.

Only after that command succeeds, install the additional paper service:

```bash
sudo cp roostoo-candle-paper.service /etc/systemd/system/ &&
sudo systemctl daemon-reload &&
sudo systemctl enable --now roostoo-candle-paper &&
sudo systemctl status roostoo-candle-paper --no-pager
sudo journalctl -u roostoo-candle-paper -n 12 --no-pager
```

The service refreshes around minute 2 of each UTC hour. A data error prevents that cycle's simulated decisions and triggers a retry after 60 seconds. If access to Binance fails, retain the error for diagnosis; no substitute or proxy is automatically used. AWS network reachability must be verified on your instance.

## Test actual short support

Run once from the AWS project directory:

```bash
python3 short_roundtrip.py --execute-testing
```

Type `TEST ACCOUNT` and enter only the testing API key and secret at hidden prompts. The script cannot independently identify whether credentials belong to testing or competition. The preflight expects an otherwise empty wallet near USD 50,000, including the USD 49,999.97 balance after the successful spot test.

Expected completion: `SHORT_OPEN_VERIFIED`, then `SHORT_ROUNDTRIP_COMPLETE` with zero open positions. Fees and price movement can reduce the balance. Reading short positions successfully does not establish permission to open them.

If `HALTED_DO_NOT_RESUBMIT` appears, preserve the journal and output. Do not delete it or repeat orders: a lost response may hide an accepted order and an open short. Inspect the read-only local record with:

```bash
python3 short_roundtrip.py --report
```

Reconciliation with the exchange is required before further execution. The successful spot round-trip script is included only as a shared dependency; do not rerun its execution mode.

## Strategy and evidence limits

The paper baseline uses completed-hour closes, a 168-hour momentum signal and three separate equal cash sleeves. New positions are considered at 00:00 UTC when momentum exceeds +1% or falls below -1%, with half of each sleeve's available cash allocated. Hourly exits use momentum reversal or an 8% adverse close move, with a 12-hour re-entry cooldown. It is normal for a ready feed to produce no immediate trade.

The model includes current bid/ask spread, 5 basis points of assumed slippage per side and 0.1% fees. Binance USDT candle signals and Roostoo USD execution quotes are different instruments; basis differences remain a limitation. Paper sizing is continuous rather than exchange-quantized. The hourly 8% condition is not a guaranteed maximum loss or an exchange stop order. Outages and skipped hours can delay exits. This remains a research baseline; its earlier retrospective performance was uneven and concentrated in SOL.

State is isolated under `data/paper_candles/`; candle data lives under `data/candles/`; short execution evidence lives under `data/execution_test/`. These do not reset the old snapshot paper experiment. Decisions and simulated trades are stored atomically with an hourly deduplication key. A fingerprint of strategy dependencies blocks silently changing an existing experiment; preserve its history and review a migration before deploying changed code.

Offline tests use simulated exchange responses. They do not verify AWS networking, actual short authorization, profitability, competition eligibility or sufficient competition trading activity. Paper trades do not count as competition trades.

## Remaining work before a competition release

1. Verify this data feed and the short execution test on AWS, including zero residual exposure after testing.
2. Compare a small, predeclared set of strategy variants out of sample against cash and buy-and-hold, with costs, concentration, drawdown and activity analysis. Do not select a winner from one attractive historical window.
3. Build the continuous execution controller: reconcile balances/orders/shorts, persist intentions and fills, enforce portfolio exposure and stale-data rules, coordinate the shared API request budget, and recover safely after crashes or ambiguous responses.
4. Run the controller on testing credentials, verify restart recovery and collect forward evidence. The two one-shot execution tests are not a continuous bot.
5. Confirm current organizer requirements and competition credentials, then deploy the versioned autonomous bot and verify leaderboard activity. Commit all strategy/code changes.
6. Complete the reproducible README, results report, trade logs, submission repository and presentation.

## Protocol references

- Roostoo API: https://github.com/roostoo/Roostoo-API-Documents
- Binance public market-data service: https://developers.binance.com/docs/binance-spot-api-docs/faqs/market_data_only
- Binance klines: https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints

No implementation can guarantee a hackathon placing or future returns.
