"""Fixed long/cash baselines for the first Roostoo research pass.

Run: python backtest.py
Inputs: data/history/{BTCUSDT,ETHUSDT,SOLUSDT}_1h_2026-07-01_2026-09-30.csv
Outputs: data/backtests/{summary,equity,trades}.csv and assumptions.txt
Requires pandas and numpy. Does not connect to any API or place orders.
"""
import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
PERIODS = [
    ("July_development", "2026-07-04", "2026-08-01"),
    ("August_validation", "2026-08-01", "2026-09-01"),
    ("September_initial_holdout", "2026-09-01", "2026-10-01"),
]
STRATEGIES = ["cash", "buy_hold", "momentum", "mean_reversion"]


def load_data(folder):
    frames = {}
    expected = pd.date_range("2026-07-01", "2026-10-01", freq="h", inclusive="left", tz="UTC")
    for symbol in SYMBOLS:
        path = folder / f"{symbol}_1h_2026-07-01_2026-09-30.csv"
        frame = pd.read_csv(path)
        frame["open_time_utc"] = pd.to_datetime(frame["open_time_utc"], utc=True)
        frame = frame.set_index("open_time_utc").sort_index()
        if not frame.index.equals(expected):
            raise ValueError(f"{symbol}: expected exactly 2,208 unique hourly candles, July–September UTC")
        columns = ["open", "high", "low", "close", "volume"]
        frame[columns] = frame[columns].apply(pd.to_numeric, errors="raise")
        if not np.isfinite(frame[columns].to_numpy()).all():
            raise ValueError(f"{symbol}: missing or nonfinite prices/volume")
        if (frame[columns[:4]] <= 0).any().any() or (frame.volume < 0).any():
            raise ValueError(f"{symbol}: invalid prices/volume")
        if (frame.high < frame[["open", "low", "close"]].max(axis=1)).any() or (frame.low > frame[["open", "high", "close"]].min(axis=1)).any():
            raise ValueError(f"{symbol}: inconsistent high/low")
        frames[symbol] = frame
    return frames


def indicators(frame):
    close = frame.close
    fast = close.rolling(24, min_periods=24).mean()
    slow = close.rolling(72, min_periods=72).mean()
    spread = close.rolling(24, min_periods=24).std(ddof=0)
    zscore = (close - fast) / spread.replace(0, np.nan)
    # Each decision at candle open sees indicators only through the previous close.
    return pd.DataFrame({"momentum": (fast > slow) & slow.notna(), "zscore": zscore}).shift(1)


def simulate(frames, strategy, start, end, initial, fee, slip):
    start, end = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    curves, trades = [], []
    for symbol, frame in frames.items():
        signals = indicators(frame)
        sample = frame.loc[(frame.index >= start) & (frame.index < end)]
        if sample.empty:
            raise ValueError("Evaluation period has no candles")
        cash, units = initial / len(frames), 0.0
        values = []
        for stamp, row in sample.iterrows():
            held = units > 0
            want = held
            if strategy == "cash":
                want = False
            elif strategy == "buy_hold":
                want = True
            elif strategy == "momentum":
                signal = signals.at[stamp, "momentum"]
                want = bool(signal) if pd.notna(signal) else False
            elif strategy == "mean_reversion":
                z = signals.at[stamp, "zscore"]
                if pd.notna(z):
                    if not held and z < -1.5:
                        want = True
                    elif held and z >= 0:
                        want = False
            else:
                raise ValueError("Unknown strategy")
            if want and not held:
                price = float(row.open) * (1 + slip)
                units = cash / (price * (1 + fee))
                commission = units * price * fee
                cash = 0.0
                trades.append([stamp, symbol, "BUY", price, units, commission, "signal"])
            elif held and not want:
                price = float(row.open) * (1 - slip)
                commission = units * price * fee
                cash += units * price - commission
                trades.append([stamp, symbol, "SELL", price, units, commission, "signal"])
                units = 0.0
            values.append(cash + units * float(row.close))
        # Independent monthly evaluations: close every remaining position at period end.
        if units > 0:
            price = float(sample.iloc[-1].close) * (1 - slip)
            commission = units * price * fee
            cash += units * price - commission
            values[-1] = cash
            trades.append([end, symbol, "SELL", price, units, commission, "period_end"])
        close_times = sample.index + pd.Timedelta(hours=1) - pd.Timedelta(microseconds=1)
        curves.append(pd.Series(values, index=close_times, name=symbol))
    curve = pd.concat(curves, axis=1).sum(axis=1)
    log = pd.DataFrame(trades, columns=["time_utc", "symbol", "side", "price", "quantity", "fee", "reason"])
    return curve, log


def metrics(curve, trades, initial):
    daily = curve.resample("D").last().dropna()
    previous = np.r_[initial, daily.to_numpy()[:-1]]
    returns = daily.to_numpy() / previous - 1
    deviation = returns.std(ddof=1) if len(returns) > 1 else 0.0
    downside = np.sqrt(np.mean(np.minimum(returns, 0) ** 2))
    values = np.r_[initial, curve.to_numpy()]
    drawdown = float(np.max(1 - values / np.maximum.accumulate(values)))
    profit = float(curve.iloc[-1] / initial - 1)
    annual = (1 + profit) ** (365 / len(daily)) - 1
    return {
        "return_pct": 100 * profit,
        "max_drawdown_pct": 100 * drawdown,
        "sharpe": math.sqrt(365) * returns.mean() / deviation if deviation > 1e-12 else np.nan,
        "sortino": math.sqrt(365) * returns.mean() / downside if downside > 1e-12 else np.nan,
        "calmar": annual / drawdown if drawdown > 1e-12 else np.nan,
        "orders": len(trades),
        "fees": float(trades.fee.sum()),
        "active_days": int(trades.loc[trades.reason == "signal", "time_utc"].dt.floor("D").nunique()) if len(trades) else 0,
        "final_equity": float(curve.iloc[-1]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/history"))
    parser.add_argument("--output", type=Path, default=Path("data/backtests"))
    parser.add_argument("--initial", type=float, default=50000)
    parser.add_argument("--fee-bps", type=float, default=10)
    parser.add_argument("--slippage-bps", type=float, default=5)
    args = parser.parse_args()
    if not math.isfinite(args.initial) or args.initial <= 0 or not 0 <= args.fee_bps < 10000 or not 0 <= args.slippage_bps < 10000:
        parser.error("Require finite positive initial capital and costs between 0 and 10,000 bps")
    frames = load_data(args.data)
    summaries, equity, logs = [], [], []
    for period, start, end in PERIODS:
        for strategy in STRATEGIES:
            curve, trades = simulate(frames, strategy, start, end, args.initial, args.fee_bps / 10000, args.slippage_bps / 10000)
            summaries.append({"period": period, "strategy": strategy, **metrics(curve, trades, args.initial)})
            equity.append(pd.DataFrame({"time_utc": curve.index, "period": period, "strategy": strategy, "equity": curve.values}))
            logs.append(trades.assign(period=period, strategy=strategy))
    args.output.mkdir(parents=True, exist_ok=True)
    summary = pd.DataFrame(summaries)
    summary.to_csv(args.output / "summary.csv", index=False)
    pd.concat(equity, ignore_index=True).to_csv(args.output / "equity.csv", index=False)
    pd.concat([log for log in logs if not log.empty], ignore_index=True).to_csv(args.output / "trades.csv", index=False)
    notes = f"""INITIAL RESEARCH BASELINES — NOT A DEPLOYABLE BOT
Parameters fixed before results: momentum SMA24 > SMA72; mean-reversion z-score
24 hours, entry below -1.5, exit at or above 0. Long/cash only; no shorting yet.
One third of initial capital is assigned to each coin, with no transfers between
coin sleeves or periodic rebalancing. Each monthly evaluation resets holdings/cash.
July 1–3 is indicator warm-up; July evaluation starts July 4 for every strategy.
Previous completed candle signals execute at the next candle open.
Initial capital: {args.initial}; fee per execution: {args.fee_bps} bps;
adverse slippage per execution: {args.slippage_bps} bps. Slippage is an assumption,
not measured exchange performance. Fees and slippage apply on both entry and exit.
All remaining holdings are liquidated at the last period close, with costs.
Quantity precision, minimum orders, liquidity limits, partial fills, outages, and
actual Roostoo fill behaviour are not modeled. USDT prices proxy Roostoo USD pairs.
Metrics use UTC daily returns, 365-day annualization, zero risk-free/target return,
downside deviation over ALL daily observations, and hourly equity drawdown including
initial capital. Calmar uses annualized return; short-window annualization is unstable.
Undefined ratios are NaN. These calculations are research conventions, not verified
official scoring formulas. Active days count signal-order dates, exclude final forced
liquidation, and do not certify organizer activity requirements. No composite score.
July=development, August=validation, September=initial holdout. Once September results
are inspected, September is no longer unseen data for later tuning. Do not optimize
parameters on it and claim out-of-sample success. Three months and three selected
assets are a narrow baseline; broader periods and sensitivity checks remain necessary.
Source: https://github.com/binance/binance-public-data
Competition fees source: https://luma.com/coghwiyt (verify actual test fills later).
"""
    (args.output / "assumptions.txt").write_text(notes, encoding="utf-8")
    print(summary.round(3).to_string(index=False))
    print(f"\nBacktest complete. Results: {args.output.resolve()}")
    print("Research simulation only. No API calls or trades were made.")


if __name__ == "__main__":
    main()
