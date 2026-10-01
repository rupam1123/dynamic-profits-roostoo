# Working plan — October 1, 2026

## Immediate inputs

- AWS invitation status; acceptance within seven days is stated in the supplied email.
- GitHub repository URL, or confirmation that no repository exists yet.
- PDF exports of the info-session slides, Data Sources Pack, AWS guide, and FAQ.
- Team size, experience, availability, and any existing code.

## Proposed work sequence

1. Engineering: validate API signing on the general/testing account; build an
   execution adapter with order reconciliation, timeouts, recovery, and logs.
2. Research: acquire permitted historical data, reconcile symbols and timestamps,
   and compare momentum and mean-reversion hypotheses with simple benchmarks.
3. Validation: use chronological train/validation/test periods, next-period
   execution, fees, spreads, and sensitivity checks. Avoid future information
   and selecting parameters on the final test period.
4. Risk: choose position/exposure limits, cash reserve, volatility sizing,
   stale-data handling, and drawdown responses based on measured results.
5. Operations: rehearse the complete bot on the testing account, including process
   restarts and ambiguous order responses. Deploy to organizer-provided AWS only
   after account access and cloud constraints are established.
6. Submission: track real changes in Git and document decisions and actual results.

If the team has fewer people than workstreams, combine responsibilities. Names and
owners remain unassigned until team details arrive.

## Project milestones

- October 1: data collection and API adapter.
- October 2: first comparable backtests and baseline strategy selection.
- October 3: testing-account rehearsal and AWS deployment preparation.
- October 4–17: published competition window; exact times/timezone need confirmation.
- October 12: proposed internal README/repository readiness target.
- Before October 14: published repository submission deadline.

## Unresolved organizer questions

- What precisely counts as an active trading day and sufficient daily trades?
- What are the competition start/end instants and day-boundary timezone?
- What rate limits apply per account and endpoint?
- Which shorting capabilities are enabled for this competition account?
- How are return frequency, annualization, zero denominators, and risk-free rates
  handled in official performance metrics?
- What EC2 region, instance limits, permissions, and spending limits apply?
- Are there specific AI-assistance disclosure requirements?

## Fees and execution

Model the competition-stated taker/maker fees, verify actual fills in testing, and
do not infer current fees or starting balance from old sample API responses.
Limit orders must not be assumed to fill immediately or always receive maker fees.

## Current evidence

The public server-time endpoint responded from this workspace. Authentication,
testing-account balance, historical data, strategy performance, and AWS deployment
have not yet been verified. No orders have been submitted.
