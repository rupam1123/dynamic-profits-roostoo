# Automated testing-account execution check

This adds an end-to-end spot execution check to the existing adapter. It is not a competition trading strategy and does not enable short trades.

## What it does

1. Confirms no BTC/other spot holdings, pending orders, locked balances or short positions; expects the unused testing wallet near USD 50,000.
2. Fetches current rules and quotes, rounds a roughly USD 10 BTC buy down to valid quantity.
3. Records intent durably, sends the buy exactly once, and looks up the returned order ID.
4. Verifies filled side/pair/type/quantity and reconciles actual USD/BTC balance changes including reported USD fee.
5. Fetches fresh market data and automatically sells the acquired quantity, then reconciles that fill too.
6. Reports final USD change and residual BTC. Fees/spread normally reduce the mock balance slightly.

Run once only on the supplied TESTING account. The API does not identify account purpose for us; wallet size alone is not proof. The console requires a TEST ACCOUNT acknowledgement and hidden testing-key entry. Keys are held in memory and are not written to disk. No credentials are required for preview.

## Install and run

Copy all Python files into the existing project. No pip installation needed, Python 3.9+.

Windows offline verification:

    py -m unittest test_roostoo_adapter test_roundtrip_checks -v

Commit the scripts/tests, push to GitHub, then on AWS pull with git pull --ff-only origin main. AWS verification and execution:

    python3 -m unittest test_roostoo_adapter test_roundtrip_checks -v
    python3 test_roundtrip.py --execute-testing

Without --execute-testing it performs public-data preview only. The execution command places actual mock-account orders. Do not use competition keys or run another trader on this testing account at the same time. The existing collector and paper tester may continue running; they place no orders. This script is a one-shot foreground test, not a systemd service. Allow around two minutes; actual time depends on network responses.

## Uncertainty and recovery

The execution journal is data/execution_test/roundtrip.sqlite3. Do not commit it, delete it, change working directory to circumvent it, or rerun with fresh state after an uncertain result. A run marker blocks accidental repetition even after completion. Gate intent records survive interruption. The client never retries a placement; polling only queries orders.

HALTED_DO_NOT_RESUBMIT can mean an order executed but its response was lost, or a fill/balance didn't match. BTC may remain if buy succeeded and later verification failed. No automatic compensating trade is attempted on uncertain information. Use this read-only report and share its output for reconciliation:

    python3 test_roundtrip.py --report

An exact order-ID query must confirm FILLED; partial/unconfirmed fills halt after three read polls. Fee currencies other than USD halt. Balance tolerances are USD 0.02 and BTC 0.0000000001. These checks assume exclusive testing-account use. No order is inferred from fuzzy history matching. Missing order IDs halt. The complete response and verified receipt are journaled without secrets. This is a controlled smoke test, not a general recovery engine.

## Verification and limits

Seven offline tests passed on Linux, using a fake exchange for end-to-end execution. They cover valid precision/budget, minimum rejection, signature ordering, explicit connection closure, successful buy/sell and repeat blocking, a response lost after a simulated fill, partial fills, mismatched balances, and pre-existing BTC. These tests did not place Roostoo orders. Windows/AWS execution of this new test suite remains to be verified by the user.

Market orders have no guaranteed price cap; the buy has a 1% sizing allowance and an assumed 0.1% fee reserve. Actual fees and fills are verified afterward. This does not test short permission or establish strategy profitability. Full strategy-to-execution integration, short lifecycle and production recovery remain outstanding. The snapshot paper strategy still requires its own 169-hour warm-up; this execution check does not remove that requirement.

Reference: https://github.com/roostoo/Roostoo-API-Documents (reviewed October 4, 2026).
