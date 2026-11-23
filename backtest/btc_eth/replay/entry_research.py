"""Offline MTPCS entry comparison using timestamped candle exports."""

import argparse
import asyncio
from collections import defaultdict
import csv
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path

import structlog

from strategies.btc_eth.shadow.entry_study import (
    CandleMarket, evaluate, prepare_strategy, snapshot, VARIANTS,
)


def read_klines(path):
    series = defaultdict(list)
    with path.open() as source:
        for row in csv.DictReader(source):
            row["open_time"] = datetime.fromisoformat(row["open_time"])
            row["close_time"] = datetime.fromisoformat(row["close_time"])
            for field in ("open", "high", "low", "close", "volume"):
                row[field] = float(row[field])
            series[row["symbol"], row["interval"]].append(row)
    for bars in series.values():
        bars.sort(key=lambda row: row["close_time"])
    return {key: ([bar["close_time"] for bar in rows], rows) for key, rows in series.items()}


async def run(args):
    series = read_klines(args.klines)
    market = CandleMarket()
    strategies = [
        prepare_strategy(variant, config, market)
        for variant, config in zip(VARIANTS, (args.original_config, args.aggressive_config))
    ]
    symbols = sorted(set.intersection(*(set(strategy.symbols) for _, strategy in strategies)))
    at = args.start
    with args.output.open("w") as output:
        while at < args.end:
            for symbol in symbols:
                bars = snapshot(series, symbol, at)
                row = {"symbol": symbol, "at_utc": at, "evidence": "candle_only"}
                if bars is None:
                    row["missing"] = "incomplete_candles"
                else:
                    hourly = bars["1h"]
                    market.price_change_24h = (hourly[-1]["close"] / hourly[-25]["close"] - 1) * 100
                    for variant, (module, strategy) in zip(VARIANTS, strategies):
                        row[variant] = await evaluate(module, strategy, symbol, bars, at)
                output.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
            at += timedelta(hours=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--klines", type=Path, required=True)
    parser.add_argument("--original-config", type=Path, required=True)
    parser.add_argument("--aggressive-config", type=Path, required=True)
    parser.add_argument("--start", type=datetime.fromisoformat, required=True)
    parser.add_argument("--end", type=datetime.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for endpoint in (args.start, args.end):
        if endpoint.tzinfo or endpoint.minute or endpoint.second or endpoint.microsecond:
            parser.error("start and end must be UTC hour boundaries without timezone suffix")
    if args.end <= args.start:
        parser.error("end must be after start")
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
