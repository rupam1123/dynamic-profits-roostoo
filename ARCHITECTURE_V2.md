# Dynamic Profits v2 — broader selection with bounded exposure

## Assessment

We can assess our source code. We cannot assess Team19's internal architecture from portfolio and order-history screenshots. Those screenshots show broad asset coverage, large orders and repeated limit-order trades, but not latency, exact holding periods, fees, failure handling or drawdown. Current rank does not establish long-run superiority.

The chosen architecture separates data validation, signal generation, portfolio admission, exchange execution and durable reconciliation. Its advantage is maintainability and explicit risk controls, not proven higher returns. The established execution journal is retained. No other team's code or exact decision rules were copied.

Official sources checked on 2026-10-06:
- https://luma.com/coghwiyt — autonomous execution, traceable updates, no HFT/market-making/arbitrage, commissions, risk-adjusted judging.
- https://github.com/roostoo/Roostoo-API-Documents — exchange metadata, market orders and short endpoints.
- https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints — completed hourly candles.

## Architecture

1. `feed.py`: public Binance hourly OHLCV; fixed completed-hour boundary; rejects gaps, malformed ranges and unfinished candles. Up to four parallel public-data reads. Failed symbols are excluded individually. Historical quote volume is stored separately from the original feed database.
2. `strategy.py`: pure functions for features, ranking, correlation and budget allocation. No credentials or exchange writes.
3. `controller.py`: reconciles account, marks equity, checks latched risk stops, evaluates existing exits, then selects entries. Every order has a durable intent before transmission.
4. `api.py`: paced signed Roostoo access, exchange precision and order minimums, no automatic write retry or credential redirects.
5. `report.py`: read-only audit with version, selection reasons, equity time, fills, unresolved intents and GMT+8 activity days.
6. `migrate.py` / `deploy.py`: verify the known v1 source and account, back up the database, preserve positions and history, record migration and Git commit, switch the existing service to the new module.

## Behaviour

Configured candidates: BTC, ETH, SOL, BNB, XRP, ADA, DOGE, AVAX, LINK, DOT, LTC, NEAR. A name in the configuration is not a promise of tradability: usable completed history, Roostoo CanTrade, current quotes and entry filters are required. The mapping is exact `ASSETUSDT` -> `ASSET/USD`; this assumes the same underlying asset and treats USDT as a USD price proxy, not an arbitrage signal.

Entries require 168-hour return above +1% (long) or below -1% (short), with 24- and 72-hour returns agreeing in direction. Rank is absolute weighted momentum (20%/30%/50% over 24/72/168 hours), divided by estimated daily volatility. This score is not a calibrated probability or expected profit.

Daily volatility = population standard deviation of the latest 72 hourly log returns × sqrt(24). Sizing = 10% of the lesser of initial and marked equity × min(1, 2% / daily volatility), with a 0.5% volatility floor. Allocation is rounded down. Minimum candidate budget is $25. New admissions are limited to three positions and 30% gross marked exposure, reserving 2% of proposed budgets for execution uncertainty. Prices can move after admission, so 30% is not a continuous hard guarantee. Profits do not automatically raise the original dollar sizing ceiling.

Entry filters: at least $5m of Binance quote volume in the latest 24 closed candles, bid/ask spread <=0.3%, 12-hour cooldown after a normal exit, and direction-adjusted 72-hour return correlation <=0.85 versus each held/selected position. These are fixed engineering defaults, not optimized parameters. Volume is a coarse liquidity proxy, not a depth or fill guarantee.

Exit rules retain v1 behaviour: 168-hour momentum crossing zero or an 8% adverse completed closing price from entry. Existing positions are evaluated first, without applying the entry volume, correlation or spread filters. Missing history for one held symbol does not suppress another held symbol's exit; it suppresses new entries and is reported. No forced rotation simply because a different coin ranks higher.

The $3,000 drawdown threshold and any configured deadline remain latched across migration. Risk/account checks run roughly every five minutes plus network time, even after that hour's strategy decision. On a portfolio trigger, the controller attempts to close holdings without requiring Binance candles or an entry window. Market failures or an unresolved write can still prevent closing. No stop is a guaranteed loss bound.

Strategy decisions use one set of completed hourly candles, with entries in the first 15 minutes of an hour. After a successful cycle, duplicate-hour signals do not create duplicate orders. Each uncertain write halts for reconciliation rather than retrying. Equity is marked again after trading, fixing the old pre-fill report ambiguity.

Market orders remain intentional. Adding reliable limit execution needs a separate partial-fill/cancellation state machine and realistic fill tests. A smaller quoted maker fee alone is not evidence that this would improve results. The two prices visible on another team's history are not enough to justify copying its execution pattern.

## Validation and honest limitations

51 tests passed locally: the 18 original controller regressions, 18 adapted v2 regressions and 15 additional architecture/migration tests. They cover long/short execution and closes, partial fills, acknowledged-write recovery, timeout blocking, duplicate prevention, account/code mismatch, equity stops, new-asset trading, aggregate sizing, correlation, missing data isolation and preservation of an existing BTC position through migration. These are simulated exchange tests, not a complete live exchange certification. Python 3.9 syntax compatibility is checked; tests ran on Python 3.12 locally and should be rerun on the AWS Python 3.9 environment.

A no-credential public preview fetched 200 completed candles for all 12 configured coins on 2026-10-06. The hypothetical empty-wallet selection included ADA, BTC and NEAR. That is a data-path check, not a trade recommendation, fixed next action or evidence of profitability. The live portfolio has an existing position and therefore different constraints.

Retrospective monthly replay on the existing January 2025–June 2026 BTC/ETH/SOL files:

| Metric, 0.1% fee each side + 0.05% slippage each side | v1 baseline approximation | v2 candidate approximation |
|---|---:|---:|
| Mean monthly return | +0.709% | +0.282% |
| Worst monthly return | -3.441% | -3.009% |
| Largest monthly drawdown | 4.272% | 3.009% |
| Mean monthly fills | 25.33 | 33.06 |

At 0.15% slippage each side, mean monthly returns were +0.458% baseline and +0.087% v2. Full results and file hashes are in `validation/v2_replay.json`. Do not multiply average monthly returns into a claimed cumulative result.

**This is evidence of a return/drawdown tradeoff, not proof that v2 is better overall.** These data had already been used in earlier research; they are not an untouched holdout. The added nine coins have not been historically validated in this release. The replay resets $100,000 at each month, executes at the next hour's open, approximates historical quote volume with close × base volume, and excludes intrahour five-minute polling, actual spreads/depth, precision and outages. Month-end equity includes estimated liquidation fees. It is not an exact exchange replay or a comparison against Team19. No backtest optimization was performed to make the result look better.

Other limitations: the request pacer covers this client's calls, not all other services on the EC2 IP; actual short permission on every new asset is not guaranteed by CanTrade; the 8-active-day requirement is not guaranteed by this strategy; exact competition end remains unset and must be confirmed; there are no guarantees of profits or rank. Limits cannot correct for unknown fills or a disabled exchange. Do not reset the journal to bypass a halt.

## Install into your repository on Windows

Unzip the patch into the existing repository root. It adds `competition_v2/`, two test files, this document, and a validation JSON; it does not overwrite `competition_bot/` or your data/credentials.

```powershell
& {
    Set-Location 'C:\Users\dasr3\Downloads\Dynamic_Profits_Starter\dynamic-profits-starter'
    py -m unittest test_competition_checks test_competition_v2 test_architecture_v2 -q
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; nothing will be committed.' }
    git add competition_v2 test_competition_v2.py test_architecture_v2.py ARCHITECTURE_V2.md validation/v2_replay.json
    if ($LASTEXITCODE -ne 0) { throw 'Git add failed.' }
    git commit -m 'Add broader universe ranking, bounded portfolio allocation and journal-preserving v2 migration'
    if ($LASTEXITCODE -ne 0) { throw 'Commit failed; inspect output.' }
    git push origin main
    if ($LASTEXITCODE -ne 0) { throw 'Push failed.' }
}
```

## AWS deployment

This is a code redeployment under the competition's allowed update process. It does not issue discretionary manual trades or close BTC to perform the upgrade. The updated autonomous strategy can subsequently place competition orders. Installing is optional: the current bot continues until you run `deploy --apply`.

First pull, test and preview; these commands do not stop the old bot or submit orders:

```bash
cd /home/ssm-user/dynamic-profits-roostoo &&
git pull --ff-only origin main &&
python3 -m unittest test_competition_checks test_competition_v2 test_architecture_v2 -q &&
python3 -m competition_v2.controller --preview &&
python3 -m competition_v2.migrate --preflight
```

Then deploy the committed release in UTC minute 16–49 after the current hourly cycle completes. For IST, that is local minute 46–59 or 00–19 of the following hour. The command verifies the time and journal before stopping anything; if the gate fails, leave the original bot running and use the displayed reason. This avoids interrupting the v1 order cycle. On an unexpected unresolved intent, do not override the gate.

```bash
cd /home/ssm-user/dynamic-profits-roostoo &&
python3 -m competition_v2.deploy --apply &&
systemctl is-active roostoo-competition
```

The installer verifies tracked, unchanged source; stops v1 only after its completed cycle; acquires the SAME exclusive controller lock; verifies credentials and current exchange balances; saves a consistent SQLite backup; retains the original positions, initial capital, cash, peak, cooldowns, processed-hour and deadline; appends a migration event; and starts the same systemd service with `competition_v2.controller`. Both versions use the same journal and process lock; they must never run in parallel.

If an error occurs before migration, the original service restart is requested. If state already migrated, do not restore v1 code or an old database: open positions may have changed. Inspect the journal and service configuration. Database backups are for forensic recovery, not blind rollback.

Verify after the service's network reads complete:

```bash
sudo journalctl -u roostoo-competition -n 40 --no-pager
cd /home/ssm-user/dynamic-profits-roostoo &&
python3 -m competition_v2.report
```

Expect `controller_version: competition-controller-2`, the carried-over BTC position (unless the strategy has since exited), and zero unresolved intents. A mid-hour upgrade normally first logs `COMPETITION_RISK_CHECK`; entry selection waits for the next hourly window. A service being active alone is not proof of successful trading. Check the next cycle and team leaderboard.

Do NOT initialize a new competition database, delete an execution journal, reset the stop flag, restore old state, use testing credentials, or manually issue orders as part of this upgrade. Keep the exact Git commit and migration event for submission.

## Reproduce the retrospective comparison

Put one continuous hourly CSV per BTCUSDT, ETHUSDT and SOLUSDT in a folder, with columns `open_time_utc,open,high,low,close,volume`:

```bash
python3 -m competition_v2.replay --data data/research/history --output data/research/v2_replay.json
```

This is read-only with respect to the exchange and uses no credentials.
