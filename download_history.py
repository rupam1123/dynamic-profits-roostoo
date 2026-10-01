"""Download and validate Binance spot hourly candles. No account or keys needed.

Defaults: BTCUSDT, ETHUSDT, SOLUSDT, July through September 2026.
Sources: https://github.com/binance/binance-public-data
Binance USDT candles are research proxies for Roostoo USD pairs, not identical fills.
"""
import argparse
import calendar
import csv
import hashlib
import io
import json
import math
import re
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

BASE = "https://data.binance.vision/data/spot"


def fetch(url):
    for attempt in range(3):
        try:
            with urlopen(url, timeout=30) as response:
                return response.read()
        except HTTPError as error:
            if error.code == 404:
                return None
            if error.code != 429 and error.code < 500:
                raise
            if attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** (attempt + 1))


def archive(symbol, period, frequency, cache):
    filename = f"{symbol}-1h-{period}.zip"
    url = f"{BASE}/{frequency}/klines/{symbol}/1h/{filename}"
    path = cache / filename
    checksum_path = cache / (filename + ".CHECKSUM")
    checksum = checksum_path.read_bytes() if checksum_path.exists() else fetch(url + ".CHECKSUM")
    if checksum is None:
        return None
    expected = checksum.decode().split()[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError(f"Invalid checksum for {filename}")
    content = path.read_bytes() if path.exists() else fetch(url)
    if content is None:
        raise ValueError(f"Archive missing despite checksum: {filename}")
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError(f"Checksum mismatch: {path}; remove this cached file and retry")
    path.write_bytes(content)
    checksum_path.write_bytes(checksum)
    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
        members = [name for name in bundle.namelist() if name.endswith('.csv')]
        if len(members) != 1:
            raise ValueError(f"Unexpected archive contents: {filename}")
        with bundle.open(members[0]) as stream:
            rows = list(csv.reader(io.TextIOWrapper(stream, encoding="utf-8-sig")))
    return rows, {"url": url, "sha256": expected}


def seconds(raw):
    value = int(raw)
    return value // (1_000_000 if value >= 100_000_000_000_000 else 1000)


def download(symbol, start, end, output):
    cache = output / "raw"
    cache.mkdir(parents=True, exist_ok=True)
    first = int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp())
    last = int(datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())
    candles, sources = {}, []
    month = start.replace(day=1)
    while month <= end:
        month_end = month.replace(day=calendar.monthrange(month.year, month.month)[1])
        period = month.strftime("%Y-%m")
        print(f"{symbol}: checking {period}", flush=True)
        batch = archive(symbol, period, "monthly", cache)
        batches = []
        if batch is not None:
            batches.append(batch)
        else:
            day = max(start, month)
            print("  Monthly file unavailable; using daily archives", flush=True)
            while day <= min(end, month_end):
                batch = archive(symbol, day.isoformat(), "daily", cache)
                if batch is None:
                    raise ValueError(f"Daily archive not published: {symbol} {day}")
                batches.append(batch)
                day += timedelta(days=1)
        for rows, source in batches:
            sources.append(source)
            for row in rows:
                if len(row) < 12:
                    raise ValueError(f"Malformed candle in {source['url']}")
                stamp = seconds(row[0])
                if not first <= stamp < last:
                    continue
                values = [float(value) for value in row[1:6]]
                op, high, low, close, volume = values
                if not all(math.isfinite(value) for value in values):
                    raise ValueError("Nonfinite price/volume")
                if min(op, high, low, close) <= 0 or volume < 0:
                    raise ValueError("Invalid price/volume")
                if high < max(op, close, low) or low > min(op, close, high):
                    raise ValueError("Inconsistent candle prices")
                record = [datetime.fromtimestamp(stamp, timezone.utc).isoformat(), *row[1:6]]
                if stamp in candles and candles[stamp] != record:
                    raise ValueError(f"Conflicting duplicate at {stamp}")
                candles[stamp] = record
        month = month_end + timedelta(days=1)
    expected = set(range(first, last, 3600))
    if set(candles) != expected:
        raise ValueError(f"{symbol}: {len(expected - candles.keys())} missing and "
                         f"{len(candles.keys() - expected)} misaligned hourly candles; not filling gaps")
    name = f"{symbol}_1h_{start}_{end}"
    path = output / (name + ".csv")
    temp = output / (name + ".csv.tmp")
    with temp.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["open_time_utc", "open", "high", "low", "close", "volume"])
        writer.writerows(candles[stamp] for stamp in sorted(candles))
    temp.replace(path)
    manifest = {"symbol": symbol, "interval": "1h", "rows": len(candles),
                "start": str(start), "end_inclusive": str(end), "sources": sources,
                "retrieved_utc": datetime.now(timezone.utc).isoformat(),
                "csv_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (output / (name + ".json")).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"SAVED {symbol}: {len(candles)} validated candles -> {path}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 7, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 30))
    parser.add_argument("--output", type=Path, default=Path("data/history"))
    args = parser.parse_args()
    if args.start > args.end or args.end >= datetime.now(timezone.utc).date():
        parser.error("Use an ordered date range ending before today UTC")
    for symbol in args.symbols:
        if not re.fullmatch(r"[A-Z0-9]+", symbol):
            parser.error("Symbols must contain only uppercase letters and digits")
    try:
        for symbol in args.symbols:
            download(symbol, args.start, args.end, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise SystemExit(f"DOWNLOAD FAILED: {error}")
    print("Historical data ready. No trades were placed.", flush=True)


if __name__ == "__main__":
    main()
