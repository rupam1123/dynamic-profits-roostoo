# V4 — activity revision

This is an update to delivered V4, implemented separately in `competition_v4a` so the active executor's code is not overwritten. Journal version: `competition-controller-4-active-1`. Use the new installer/check script in this ZIP. It can migrate the recognized V3.1, V3.2 or original V4 journal, preserves all positions and prior risk state, and replaces the executor within the existing roostoo-competition service. Never run two competition executors.

## Changes requested

| Control | Original V4 | Updated V4 |
|---|---|---|
| Daily entry/addition attempts | 60 | No fixed daily ceiling |
| Entry pacing | 2 per 5-minute slot | At most 1 per rolling 60 seconds, 5 per 5-minute slot |
| Re-entry after a full close | 5 minutes | 60 seconds, with a fresh qualifying signal |
| Addition count | At most 2 | No fixed count; risk/asset/gross/cash caps still bind |
| Addition spacing | 15 minutes | At least 5 minutes; a new confirmed cross above a higher breakout level |
| Fast target | At least 0.6% gross, with cost hurdle | Estimated round-trip costs + 0.1% margin; adjusted for actual entry fee after fill |

At zero spread, default costs are 0.1% entry fee + 0.1% exit fee + 0.05% slippage per side. The new target is approximately 0.4% gross. If a long entry's actual fee is 0.05%, its target becomes approximately 0.35% gross. A wider spread or higher actual entry fee raises the target. Neither the quoted target nor its margin guarantees a profitable realized fill. A 0.11% gross move does not cover two 0.1% fees. We do not infer Rank-1's net profit or hidden entry logic from rounded transaction values.

The historical candle range must support the cost hurdle before admission. Targets for positions inherited at migration are preserved. Actual entry fees are available in exchange fill receipts; the public API's example fee values are not treated as the current competition fee schedule.

API reference: https://github.com/roostoo/Roostoo-API-Documents

## Retained behavior

- Dynamic crypto discovery every 10 minutes; exact active Binance spot mappings only. All eligible assets get closed five-minute data; strong trends also get one-minute breakout evaluation. Hourly context uses up to 1,000 completed candles, at least 721 for entries.
- Minimum $10m 24h quote volume, maximum 10% daily volatility, spread, correlation, BTC regime and anti-chase checks.
- 20 distinct asset slots; 5% per-asset admission cap; 40% selection exposure budget; 50% defensive aggregate planning ceiling; 2% theoretical initial-stop-risk cap across all books. Initial entries use half their admitted budget. More capital is not automatically spent.
- Long additions only to eligible, profitable opportunity positions, before partial profit-taking, with at least 1R profit, fresh five-minute higher-level breakout and cost hurdle. No averaging down, short additions or additions to inherited core/xs/ts books. Addition size at most half current position, further constrained by portfolio and asset caps. Finite capacity naturally bounds additions despite removal of the count cap.
- One partial exit of 50% at 1.5R when costs and exchange minima permit; quote-based breakeven/trailing protection on residuals.
- Fast trades: 0.6% initial stop, cost-aware profit target, 15-minute maximum holding timer. Target/risk exits can happen within a minute. Momentum trades may hold longer. Timers do not manufacture trades.
- Independent 10-second quote/risk observation target; snapshots older than 25 seconds block trading. Quote observation continues while the single authenticated executor reconciles orders. Execution can take longer than ten seconds.
- Fast long entry: passive limit BUY; query and cancel open remainder after 12 seconds or an invalidated live guard. Resolve terminal partial fill/cancel by exact order ID. No blind retries, no automatic market chase of canceled unfilled limits. Other entries/additions/exits use market orders.
- One shared V4 request limiter spaces public and private requests by at least 3.1 seconds (up to 20 in a rolling minute), leaving margin below the organizer's stated 30 calls/minute. Legacy collectors do not share that limiter; aggregate exchange/IP use still matters.
- All entry paths use the durable rolling pacing check; exits and cancellation/reconciliation are exempt from entry pacing. Removing the daily ceiling is not a promise of high realized order count. Valid signals, rate limits, fill latency and risk capacity control activity.
- Re-entry clocks use precise exit timestamps where available; old inherited exits without those timestamps retain the conservative one-hour fallback. In this revision the configured precise cooldown is **60 seconds**.

## Validation and limitations

88 offline tests pass. Added cases verify rolling pacing across restart, exits remaining available during entry pacing, more than 60 daily attempts not blocking entry, fresh re-entry, additions beyond count two within caps, actual entry-fee target adjustment and migration of an original-V4 holding without orders. Existing tests cover uncertain writes, partial fills, cancellation uncertainty, dynamic mapping, independent quote observation, risk limits and deployment interruption/repair.

All 88 tests now pass on actual Python 3.9.25 (matching AWS) and Python 3.12. The original test setup used TestCase.enterContext, unavailable on Python 3.9; it is replaced with patch.start() and registered cleanup. Trading modules and journal fingerprints are unchanged. AWS runs the tests again before migration. The PowerShell installer was not executed here (PowerShell unavailable). Existing V3.1/V3.2/original-V4 release files are unchanged and checksum-verified. No production account, new release live fill or improved return was verified here. The previous public universe scan is not a return backtest; no matched V4 scalping replay is claimed.

## Install and monitor

Follow `V4_ACTIVE_ROLLOUT.md`. `CHECK_V4_ACTIVE_AWS.sh` performs checksum/tests/public preview/read-only preflight. `python3 -m competition_v4a.deploy --apply` performs the actual switch inside the existing guarded window. The migration preserves open-position targets and safety state, including drawdown pauses; changing the policy does not reset losses, balances or unknown orders.

Read-only journal checkpoint:

```bash
python3 -m competition_v4a.checkpoint --label review_15m
python3 -m competition_v4a.report
```

The checkpoint writes a timestamped JSON file beneath `data/competition/checkpoints`. It reports recorded equity/time, unresolved intents, fills/cancels since revision and rejection reasons. These are historical journal observations, not fresh API quotes or matched round-trip PnL.
