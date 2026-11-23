"""Offline MTPCS entry comparison using timestamped candle exports."""

import argparse
import asyncio
from bisect import bisect_right
from collections import defaultdict
import csv
from datetime import datetime, timedelta
import importlib
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import structlog
import yaml

from strategies.btc_eth.market_state import get_market_state, get_market_state_behavior

FRAMES = {"1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1)}
VARIANTS = ("btc_eth", "btc_eth_aggressive")


def retracement(bars, direction, price):
    """Record the first occurrence of each extreme in an ordered closed window."""
    if not bars:
        return None
    high_index, high_bar = max(enumerate(bars), key=lambda item: item[1]["high"])
    low_index, low_bar = min(enumerate(bars), key=lambda item: item[1]["low"])
    high, low = high_bar["high"], low_bar["low"]
    if high <= low or not low <= price <= high:
        return None
    if direction == "LONG" and low_index < high_index:
        depth = (high - price) / (high - low)
    elif direction == "SHORT" and high_index < low_index:
        depth = (price - low) / (high - low)
    else:
        return None
    return {
        "depth": depth,
        "high": high,
        "low": low,
        "high_at": high_bar.get("open_time"),
        "low_at": low_bar.get("open_time"),
        "fibonacci": min(abs(depth - level) for level in (0.382, 0.618)) <= 0.03,
        "ordinary": min(abs(depth - level) for level in (0.30, 0.70)) <= 0.03,
    }


class CandleMarket:
    """Only candle-derived fields are available in this historical snapshot."""

    async def get_account_info(self):
        raise RuntimeError("account data is outside the candle replay")

    async def get_funding_rate(self, symbol):
        raise LookupError("historical funding quote unavailable")

    async def get_orderbook(self, symbol, limit):
        raise LookupError("historical bid and ask unavailable")

    async def get_ticker(self, symbol):
        return {"priceChangePercent": self.price_change_24h}


class ReplayClock(datetime):
    @classmethod
    def now(cls):
        return cls.at


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


def snapshot(series, symbol, at):
    result = {}
    for frame, duration in FRAMES.items():
        closes, rows = series.get((symbol, frame), ([], []))
        stop = bisect_right(closes, at)
        bars = rows[max(0, stop - 100):stop]
        if (
            len(bars) < 55
            or not timedelta(0) <= at - bars[-1]["close_time"] < duration
            or any(b["open_time"] - a["open_time"] != duration for a, b in zip(bars, bars[1:]))
        ):
            return None
        result[frame] = bars
    return result


def prepare_strategy(variant, config_path, market):
    module = importlib.import_module(f"strategies.{variant}.strategy")
    config = yaml.safe_load(config_path.read_text())
    config.setdefault("instance", {})["execution_mode"] = "shadow_batch"
    strategy = module.BTCEthStrategy(config, market, None, None)

    async def no_limit_quote(symbol, direction):
        return None

    strategy._get_optimized_price = no_limit_quote
    return module, strategy


async def evaluate(module, strategy, symbol, bars, at):
    indicators = strategy._build_indicators(bars)
    cfg = strategy.risk_config["market_state"]
    if cfg.get("enabled", True):
        state, state_reason = get_market_state(
            indicators["4h"], pd.Series([bar["close"] for bar in bars["4h"]]),
            indicators_1d=indicators["1d"], config=cfg,
        )
        market_state = state.value
        behavior = get_market_state_behavior(state, cfg)
    else:
        market_state, state_reason = "RANGING", "market_state_disabled_fallback"
        fallback = cfg.get("fallback", {})
        behavior = {key: fallback.get(key, default) for key, default in (
            ("can_trade", True), ("min_grade", "C"), ("vol_boost", 0.0)
        )}
    result = {
        "market_state": market_state,
        "state_reason": state_reason,
        "route": behavior.get("strategy_mode", "trend"),
    }
    if not behavior["can_trade"]:
        return {**result, "reject_reason": "market_state_disabled"}
    ReplayClock.at = at
    with patch.object(module, "datetime", ReplayClock):
        signal, reason = await strategy._analyze_stationary_signal(
            symbol, bars, indicators, market_state, behavior
        )
    if signal is None:
        return {**result, "reject_reason": reason}
    result.update(signal)
    if signal["entry_intent"] == "trend":
        for length in (24, 48, 72):
            hourly = bars["1h"]
            result[f"retracement_{length}h"] = retracement(
                hourly[-length:], signal["direction"], float(signal["entry_price"])
            ) if len(hourly) >= length else None
    return result


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
