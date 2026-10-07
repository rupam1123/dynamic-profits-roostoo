# V3 validation and release decision

**Decision: engineering candidate for testing; do not infer a live performance advantage. V2/V2.1 has not been replaced by this work.**

## Verification performed

- 41 V3 offline tests pass. Alongside the 68 existing V1/V2/V2.1 tests: **109 tests pass** on Python 3.12.
- Every live V3 module parses using Python 3.9 syntax rules. An actual Python 3.9 runtime was not available for this verification; run the included tests on AWS before activation.
- Public preview: 12/12 symbols, 1,000 completed hourly candles each, no credentials or orders. Some signals were correctly rejected by the cost hurdle and anti-chase filters.
- Migration tests run the original V2 and V2.1 initializers, then verify backups, preserved cash/positions/peaks/deadlines, new fingerprints, account reconciliation, no orders and idempotent repeated migration.
- Fault tests cover full/partial/zero limit fills, cancellation/fill race, lost cancellation acknowledgement, unresolved cancellation, lost placement response, ACK recovery, partial long and short reductions, duplicate prevention, process lock, pauses, limits, stale decision window, deadline liquidation, missing held-asset data and quote-based stops without candles.
- Private exchange behavior and an AWS systemd migration have NOT been executed by the assistant. Testing-only limit entries are not enabled in competition mode.

## Matched historical comparison

Eight predeclared 14-day windows beginning March/May/July/September 1 in 2025 and 2026, aligned to GMT+8 days. Fixed twelve-asset universe. Historical source: 252 checksum-verified Binance monthly archives (January 2025–September 2026); each asset has 15,312 contiguous hourly candles. CSV hashes and all individual window results are in `validation/v3_comparison.json`.

The actual V2, uploaded V2.1 and V3 controller decision/accounting functions run against the same fake exchange, prices and time. The replay uses an in-memory journal; SQLite durability and restart behavior are tested separately. Indicators use only closed candles; fills use the next hourly open with adverse slippage. Market fees are 0.1% per side. Final marks include estimated liquidation fee and slippage. Strategies retain their respective default risk/allocation policies, so this is not an equal-exposure experiment.

| Variant | Mean return per window, 5bp slippage | Worst drawdown | Windows with >=8 fill days | Mean return, 15bp slippage |
|---|---:|---:|---:|---:|
| V2 | -0.444% | 3.223% | 3/8 | -0.497% |
| Uploaded V2.1 | +0.003% | 2.456% | 8/8 | -0.045% |
| V3 default | +0.131% | 3.447% | 7/8 | -0.311% |
| V3 with ATR exits | -0.415% | 2.817% | 8/8 | -0.983% |

The default V3 has the highest mean return in this sample at 5bp slippage, but this is not consistent superiority:

- In the four 2026 windows at 5bp, V3 averages **-0.892%**, versus V2.1 **-0.258%**.
- At 15bp, V3 averages **-0.311%**, worse than V2.1 **-0.045%**.
- V3 has a higher worst drawdown and misses eight fill-days in one window.
- ATR exits increase turnover substantially (about 91 versus 36 fills/window at 5bp) and reduce mean return. They remain OFF. No parameter search was performed to find a flattering result.

Rotation executed in all eight default V3 windows (2–6 decisions/window). Feature acceleration was checked against the direct live feature function; maximum checked absolute difference was approximately 1.9e-8.

## Limits and interpretation

These are retrospective, calendar-spaced research windows, not an untouched holdout or an estimate of expected future returns. Eight windows are a small sample. Prior V2.1 research had already examined overlapping periods. Results differ from earlier reports because the windows, alignment, quote-volume source and comparison paths differ; do not mix their averages.

Hourly candles do not establish intrahour stop fills, limit-order queue priority, depth, order latency, outages, partial fills or exchange-specific slippage. Historical constituents have survivorship bias. The simulation checks risk each hour; production checks are approximately five minutes plus processing time. It does not prove maker execution savings.

The 3% episode drawdown threshold is not a maximum-loss guarantee. A gap, latency or repeated drawdown episodes can exceed it. Lifetime drawdown is retained and reported separately from the recovery episode peak.

A day with a fill is not proof of sufficient competition activity. V3 warns about inactive days instead of generating trades merely for eligibility. If the organizer requires more activity than this strategy produces, resolve that strategy/eligibility issue before promotion.

## Practical next step

Install the files, run the safety tests and public preview, and review current account logs. Use a free, separate testing account for actual API verification. Keep the existing competition process and journal intact until the team makes a deployment decision from the evidence. The migration tool is supplied for that later decision; this package has not been deployed.
