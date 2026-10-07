# Expanded universe: completed research results

Decision: **deploy the public-data collector and activity diagnostics; do not automatically widen the competition trading policy.** The requested-22 and full-65 comparisons both failed the predeclared promotion gate. More orders alone did not establish a better risk/return tradeoff.

## Comparable retrospective results

Fourteen independent 14-day windows across 2025–2026. Each starts flat with $100,000. The table uses 0.1% taker commission plus 0.15% slippage per side, and estimated closing costs on ending positions. Returns are per-window averages, not monthly/annual forecasts. The existing production controller generates every simulated decision.

| Policy | Mean return | Median return | Worst drawdown | Mean fills / 14 days | Mean gross exposure |
|---|---:|---:|---:|---:|---:|
| Original 12 | +0.160% | +0.086% | 2.496% | 34.7 | 13.55% |
| Requested 22 | -0.075% | -0.188% | 2.480% | 49.9 | 15.47% |
| All 65 crypto candidates | +2.160% | +0.482% | 9.966% | 114.9 | 20.60% |
| 65 candidates, faster 24/72h rotation | +1.833% | +0.132% | 9.643% | 161.4 | 20.96% |

The 65-candidate policy beat the baseline in 10/14 stress-cost windows, but its worst drawdown exceeded the allowed baseline-plus-0.5-percentage-point tolerance. Its better mean depends in part on a few large winning windows; it is not evidence of reliable seven-day returns.

The faster-rotation variant is one exploratory 24/72-hour hypothesis with unchanged allocations and risk settings. It is not an optimized or independently held-out winner. Its result does not authorize deployment.

## Data collected

- 88 catalog assets; 973,832 historical hourly candles, all checked for valid OHLCV, order and duplicates.
- History spans January 2025 through September 2026 where the exact symbol existed. Newer listings begin at their actual available history; no predecessor tokens were stitched in.
- 1,103 downloaded monthly archives passed Binance SHA-256 checksum verification; 15 absent archive URLs were recorded explicitly. Original 12-asset history was reused and hashed. No internal hourly gaps were found in the assembled files.
- 86,000 additional recent hourly candles across 86 active mapped symbols are in the public snapshot. They overlap part of the archive history, so these are not 86,000 additional unique historical hours.
- 67 crypto listings: 65 currently mapped/tradable candidates and two unavailable markets (OMNI, TON). The other 21 are stock/tokenized-stock listings: data retained, excluded from this crypto strategy.
- The public eligibility scan at 2026-10-07T14:06:39.052020+00:00 admitted 42 crypto assets; 23 failed the 24-hour liquidity threshold. This is a dated snapshot, not a live status claim.

| Requested asset | Historical hourly candles | First collected hour UTC | Public scan result |
|---|---:|---|---|
| PEPE | 15,312 | 2025-01-01T00:00:00+00:00 | ELIGIBLE |
| PUMP | 9,228 | 2025-09-11T12:00:00+00:00 | ELIGIBLE |
| EDEN | 8,773 | 2025-09-30T11:00:00+00:00 | LOW_QUOTE_VOLUME |
| HEMI | 8,940 | 2025-09-23T12:00:00+00:00 | LOW_QUOTE_VOLUME |
| CRV | 15,312 | 2025-01-01T00:00:00+00:00 | LOW_QUOTE_VOLUME |
| FET | 15,312 | 2025-01-01T00:00:00+00:00 | ELIGIBLE |
| POL | 15,312 | 2025-01-01T00:00:00+00:00 | ELIGIBLE |
| FIL | 15,312 | 2025-01-01T00:00:00+00:00 | ELIGIBLE |
| ENA | 15,312 | 2025-01-01T00:00:00+00:00 | ELIGIBLE |
| APT | 15,312 | 2025-01-01T00:00:00+00:00 | ELIGIBLE |
| FLOKI | 15,312 | 2025-01-01T00:00:00+00:00 | LOW_QUOTE_VOLUME |
| BIO | 15,254 | 2025-01-03T10:00:00+00:00 | LOW_QUOTE_VOLUME |

All 65 research candidates:

BTC, ETH, SOL, BNB, XRP, ADA, DOGE, AVAX, LINK, DOT, LTC, NEAR, CRV, FET, POL, FIL, ENA, APT, FLOKI, BIO, PUMP, EDEN, 1000CHEEMS, AAVE, ARB, ASTER, AVNT, BMT, BONK, CAKE, CFX, EIGEN, FORM, HBAR, HEMI, ICP, LINEA, LISTA, MIRA, ONDO, OPEN, PAXG, PENDLE, PENGU, PEPE, PLUME, S, SEI, SHIB, SOMI, STO, SUI, TAO, TRUMP, TRX, TUT, UNI, VIRTUAL, WIF, WLD, WLFI, XLM, XPL, ZEC, ZEN

## Why live frequency is low

The deployed policy evaluates hourly but its portfolio strategies rebalance every 72 and 168 hours. Signal alignment, liquidity, spread, correlation, risk pauses and existing positions can further prevent entries. More assets can increase available signals, but also fees, concentration in volatile names and the time needed to reconcile orders.

The user reports about 50 orders/$70.9k turnover versus the leader’s 4,000 orders/$70m. Those totals were not independently verified and may cover different periods or include different order types. At an assumed 0.1% fee, $70m of executed turnover would imply $70,000 of commissions; this is an illustration, not a claim about that team’s actual costs. Order count does not reveal its strategy or net profitability.

Use `python3 -m universe_research.activity_report --days 7` on AWS to obtain the actual recorded blockers. The tool is read-only. Do not force discretionary trades or remove reconciliation controls to raise activity.

## Seven days remaining

1. Install the collector and diagnostics now using UNIVERSE_README.md. Keep the current competition controller running.
2. Inspect the activity report and latest competition report immediately. Resolve actual missing-data, stopped-service, paused-state or unresolved-order problems according to their cause; do not bypass risk pauses or reset journals.
3. Consider a narrow, evidence-backed strategy change only after a bounded forward check. This package does not establish that any broad expansion is safe to promote.
4. Keep commits, deployment evidence, daily equity/fill records and submission documentation current. Confirm the official end time before changing any shutdown schedule.

## Validation and limits

- 16 new offline tests cover candle validation, symbol/public-endpoint boundaries, short listing history, liquidity gates, future-data leakage, small-price numerical stability, execution deadlines, reconciliation and read-only diagnostics.
- 84 matched full comparison runs plus 14 faster-rotation runs completed. All simulated cycles finished without unresolved execution intents. The 65-candidate replay included an entry blocked before transmission by the original deadline.
- Every fake API call advances the clock by 3.1 seconds, including signed time synchronization. Real network delay and public candle download latency are not modeled. Some expanded cycles extend beyond the entry window while exits/reconciliation finish; no modeled entry is transmitted after the deadline.
- Hourly execution quotes do not model intrahour stops, gaps inside an hour, depth, outages or partial fills. The live five-minute risk loop may behave differently. A drawdown trigger is not a guaranteed loss cap.
- Current exchange listings, quantity precision and current universe selection are used retrospectively. Historical Roostoo listing dates are unknown; survivor/selection bias remains. Calendar windows have been examined before and are not untouched holdout data.
- Extra-asset admission uses completed, contiguous history and prior 24-hour quote volume. Missing/prelisting data never becomes a trading feature. Historical spread is approximated through slippage; actual spread checks run in the public audit.
- Production V3.1 and all 51 release-protected files remain unchanged. No credentials, account calls, actual orders or AWS service changes were made here.

Primary data: https://data.binance.vision/ ; https://data-api.binance.vision/api/v3/ ; https://mock-api.roostoo.com/v3/exchangeInfo . API documentation: https://github.com/roostoo/Roostoo-API-Documents . Exact per-asset hashes, comparison outputs and provenance are included under universe_research/evidence/.
