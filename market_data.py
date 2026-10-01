"""Public Roostoo market-data collector. No credentials or order endpoints."""
import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

BASE_URL = "https://mock-api.roostoo.com"


def fetch(endpoint, params=None):
    url = BASE_URL + endpoint
    if params:
        url += "?" + urlencode(params)
    for attempt in range(3):
        try:
            with urlopen(url, timeout=15) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError("Unexpected API response")
            if result.get("Success") is False:
                raise ValueError("Roostoo reported an unsuccessful request")
            return result
        except HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise
            if attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** (attempt + 1))


def snapshot(database):
    server_time = int(fetch("/v3/serverTime")["ServerTime"])
    info = fetch("/v3/exchangeInfo")
    if info.get("IsRunning") is not True:
        raise ValueError("Exchange is not running; no snapshot recorded")
    tickers = fetch("/v3/ticker", {"timestamp": server_time})
    data = tickers.get("Data")
    if not isinstance(data, dict) or not data:
        raise ValueError("Ticker response contains no market data")
    received = datetime.now(timezone.utc).isoformat()
    stamp = int(tickers.get("ServerTime", server_time))
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS snapshots (
            server_time INTEGER PRIMARY KEY,
            received_utc TEXT NOT NULL,
            exchange_json TEXT NOT NULL,
            ticker_json TEXT NOT NULL
        )""")
        connection.execute(
            "INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?, ?)",
            (stamp, received, json.dumps(info), json.dumps(tickers)),
        )
    available = info.get("TradePairs", {})
    pairs = sorted(pair for pair in data if available.get(pair, {}).get("CanTrade"))
    print(json.dumps({"received_utc": received, "tradable_pairs": len(pairs),
                      "sample_pairs": pairs[:8], "database": str(database)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/market.sqlite3")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=300,
                        help="Seconds between snapshots, minimum 60; not an official rate limit")
    args = parser.parse_args()
    if args.interval < 60:
        parser.error("Use an interval of at least 60 seconds")
    while True:
        try:
            snapshot(args.db)
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, sqlite3.Error) as error:
            print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
            if not args.watch:
                raise SystemExit(1)
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
