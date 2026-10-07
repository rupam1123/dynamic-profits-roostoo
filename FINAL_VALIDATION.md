# Final release validation — 2026-10-07

## Decision

Ship V2.1 strategy behavior on the more defensive execution and migration implementation in `competition_v31`. Do not promote V3's rotation, core-first allocation, wider filter coverage, new risk sizing or optional ATR exits. Prior matched retrospective research did not justify those additions. This release deliberately restores the baseline; it makes no claim of an extra profit edge.

## Actual-controller replay

The comparison invokes both the archived V2.1 controller and the final controller. Their decisions and accounting run against the same fake exchange, candles, 0.1% taker fee per side and adverse slippage of either 5 or 15 basis points per fill. Completed-hour signals execute at the following hourly open. No strategy parameters were fitted in this final build.

Fourteen 14-day windows begin on March/May/July/September 1 in 2025 and 2026, plus October/November/December 1, 2025 and April/June/August 1, 2026. Boundaries use GMT+8 midnight. These windows were examined in prior research and are **not an untouched holdout**.

| Measure | 5bp slippage | 15bp slippage |
|---|---:|---:|
| Comparisons with exactly identical trade traces | 14/14 | 14/14 |
| Final simulated fills | 486 | 486 |
| Average 14-day portfolio return | +0.2103% | +0.1600% |
| Worst within-window drawdown | 2.4564% | 2.4960% |
| Unresolved simulated intents | 0 | 0 |

All **28 comparisons** match every submitted endpoint, timestamp, side, quantity/collateral and response/fill, and every reported metric. The 972 fills are summed over two alternative cost scenarios; they are not 972 independent market observations. Averages are arithmetic means of independently restarted windows, not compounded annual or monthly returns.

Feature calculations were checked against the production feature implementation; maximum absolute difference was `5.527457513920808e-08`. Dataset and source hashes, per-window metrics and complete trade-trace digests are in `validation/v31_parity.json`. The runner is `research_v31/compare_baseline.py`; archive provenance and download tools remain in the previous V3 package.

Replay limitations: no intrahour path, order-book depth, realistic latency, network outage, partial-fill dynamics or real funding/liquidation mechanics. End equity estimates close remaining positions with fees/slippage. A day with a fill is not proof of organizer activity qualification. Fresh-start trace parity does not imply identical behavior during failures or after adopting current holdings.

## Engineering checks

**151 tests passed** on Linux/Python 3.12.14: 109 pre-existing tests and 42 final-release tests. New tests cover:

- Exact V2.1 policy, core candidates, small-portfolio targets, owner priority and activity-probe behavior.
- No duplicate same-hour orders; no writes without complete held-asset history; entry gates shared by all paths.
- Deadline crossing after throttling prevents network transmission. A lost write response is recorded and never blindly retried; an acknowledged fill reconciles once.
- Partial spot and short accounting, process locking, version/account/source mismatches, read-only reporting and refused reinitialization.
- Separate lifetime/recovery peaks, pause enforcement, deadline exits without candles and inherited V3 quote stops during data outages.
- Migration preserving cash, long/short position identities, collateral, risk state, all history and deadlines; SQLite backup and idempotence; no migration orders.
- Autonomous continuation after adoption without duplicate positions; old V3 refuses the migrated journal.
- Deployment failure before state commit versus failure after a durable commit; safe repair after unit-file failure; unchanged-source and idle-window gates.

Source parses with Python 3.9 grammar. Actual Python 3.9 and Windows/PowerShell runtimes were not available in the build environment. The Windows installer runs the 42 tests locally; the AWS script runs them under the instance's own Python before switching services. No credentials or real exchange order endpoints were used for validation. Private account reconciliation occurs on AWS as part of deployment.

## Intentional safety differences from V2.1

The daily activity guard cannot bypass a drawdown pause. All entries require complete held-asset signal coverage and respect a combined exposure admission ceiling. All submitted writes have durable reconciliation. Lifetime peak accounting is not reset when a drawdown episode restarts. Adopted V3 positions retain their prior quote-based stops until closed. These changes can alter real trades in states not encountered in the 28 replay cases; parity is conditional evidence, not universal equivalence.

New positions retain V2.1's original exit semantics. This release does **not** add quote stops to every new small-portfolio position, ATR profit targets or trailing profits. The 3% portfolio threshold is a trigger, not a hard maximum loss. The daily activity guard retains a small probe fallback with no established edge. These tradeoffs are explicit rather than hidden under a version rating.

## Release boundary

Old V3 source and its 48-file manifest remain unchanged. The final package contains no account keys, private journals or historical price bundle. It is ready for installation; no AWS service was accessed or switched from this build environment. Confirm ExecStart, a fresh controller event, zero unresolved intents and leaderboard attribution after the user-run migration. No numerical probability of winning is supported by this validation.
