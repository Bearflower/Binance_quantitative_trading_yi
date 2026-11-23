"""Forward MTPCS research collector. Reads K lines and writes a virtual ledger."""

import argparse
from collections import deque
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3

import structlog

from strategies.btc_eth.shadow.entry_study import (
    CandleMarket, FRAMES, VARIANTS, evaluate, prepare_strategy, snapshot,
)
from shared.config_loader import compute_config_hash
from shared.kline_service import KLineService

ARMS = ("trend", "mean_reversion", "trend_fibonacci", "trend_ordinary", "mean_reversion_aligned")
HOUR = timedelta(hours=1)


def arms_for(variant, signal, alignment_direction=None):
    intent = signal.get("entry_intent")
    if intent == "trend":
        band = signal.get("retracement_48h") or {}
        return ("trend",) + tuple("trend_" + key for key in ("fibonacci", "ordinary") if band.get(key))
    if intent == "mean_reversion":
        if variant == "btc_eth_aggressive" and alignment_direction in ("LONG", "SHORT"):
            return ("mean_reversion", "mean_reversion_aligned")
        return ("mean_reversion",)
    return ()


def advance(state, signal, bar, fee_rate):
    """Risk-normalized one-lot model. Next bar open activates a pending signal."""
    position = state.get("position")
    event = None
    if pending := state.get("pending"):
        entry = float(bar["open"])
        risk = (entry - pending["stop"]) * pending["side"]
        if risk > 0 and (pending["target"] - entry) * pending["side"] > 0:
            position = {**pending, "entry": entry, "risk": risk, "age": 0}
        else:
            event = {"reason": "entry_gap", "net_r": None}
    if position:
        position = {**position, "age": position["age"] + 1}
        side = position["side"]
        price = None
        low, high, open_price = map(float, (bar["low"], bar["high"], bar["open"]))
        stop, target = position["stop"], position["target"]
        if (side == 1 and low <= stop) or (side == -1 and high >= stop):
            price, reason = (min(stop, open_price) if side == 1 else max(stop, open_price)), "stop"
        elif (side == 1 and high >= target) or (side == -1 and low <= target):
            price, reason = target, "tp1"
        elif position["age"] >= 36:
            price, reason = float(bar["close"]), "time"
        if price is not None:
            fees = (position["entry"] + price) * fee_rate
            event = {**position, "exit": price, "reason": reason,
                     "net_r": ((price - position["entry"]) * side - fees) / position["risk"]}
            position = None
    if position:
        return {"position": position}, event
    if signal:
        return {"pending": {
            "side": 1 if signal["direction"] == "LONG" else -1,
            "stop": float(signal["initial_stop_loss"]), "target": float(signal["tp1_price"]),
        }}, event
    return {}, event


def utc_ms(raw):
    return datetime.fromtimestamp(raw / 1000, timezone.utc).replace(tzinfo=None)


async def collect(client, symbols):
    keys = [(symbol, frame) for symbol in symbols for frame in FRAMES]
    collections = await asyncio.gather(*(client.get_klines(*key) for key in keys))
    output = {}
    for key, bars in zip(keys, collections):
        for bar in bars:
            for field in ("open_time", "close_time"):
                bar[field] = utc_ms(bar[field])
            for field in ("open", "high", "low", "close", "volume"):
                bar[field] = float(bar[field])
        bars.sort(key=lambda bar: bar["close_time"])
        output[key] = ([bar["close_time"] for bar in bars], bars)
    return output


def save_filters(strategies):
    return [
        {
            "atr": {symbol: list(values) for symbol, values in strategy.atr_filter._atr_history.items()}
            if strategy.atr_filter else {},
            "pauses": {symbol: until.isoformat() for symbol, until in strategy.symbol_extreme_pause_until.items()},
        }
        for _, strategy in strategies
    ]


def restore_filters(strategies, filters):
    for (_, strategy), stored in zip(strategies, filters):
        if strategy.atr_filter:
            strategy.atr_filter._atr_history = {
                symbol: deque(values, maxlen=strategy.atr_filter.lookback_hours)
                for symbol, values in stored["atr"].items()
            }
        strategy.symbol_extreme_pause_until = {
            symbol: datetime.fromisoformat(until) for symbol, until in stored["pauses"].items()
        }


async def collect_cycle(db, client, strategies, market, symbols, cutoff, fee_rate):
    previous = db.execute("SELECT at, state FROM hours ORDER BY at DESC LIMIT 1").fetchone()
    at = datetime.fromisoformat(previous[0]) + HOUR if previous else cutoff
    if at > cutoff:
        return
    lines = await collect(client, symbols)
    progress = json.loads(previous[1]) if previous else {"positions": {}, "filters": save_filters(strategies)}
    restore_filters(strategies, progress["filters"])
    state = progress["positions"]
    while at <= cutoff:
        contexts = {symbol: snapshot(lines, symbol, at) for symbol in symbols}
        if any(bars is None for bars in contexts.values()):
            raise RuntimeError(f"incomplete closed market snapshot at {at} UTC")
        records = []
        for symbol, bars in contexts.items():
            hourly = bars["1h"]
            market.price_change_24h = (hourly[-1]["close"] / hourly[-25]["close"] - 1) * 100
            for variant, (module, strategy) in zip(VARIANTS, strategies):
                candidate = await evaluate(module, strategy, symbol, bars, at)
                original_direction = candidate.get("direction")
                aligned, alignment_reason = False, None
                if variant == "btc_eth_aggressive" and candidate.get("entry_intent") == "mean_reversion":
                    aligned, alignment_reason = strategy._check_direction_alignment(
                        original_direction, strategy._build_indicators(bars),
                        strategy.risk_config["ranging_strategy"],
                    )
                active_arms = arms_for(variant, candidate, original_direction if aligned else None)
                record = {"variant": variant, "symbol": symbol, "candidate": candidate,
                          "arms": active_arms, "alignment_passed": aligned,
                          "alignment_reason": alignment_reason, "exits": {}}
                for arm in ARMS:
                    key = f"{variant}:{symbol}:{arm}"
                    order = candidate if arm in active_arms else None
                    state[key], event = advance(state.get(key, {}), order, hourly[-1], fee_rate)
                    if event:
                        record["exits"][arm] = event
                records.append(record)
        with db:
            db.execute("INSERT INTO hours VALUES (?, ?, ?)",
                       (at.isoformat(), json.dumps({"positions": state, "filters": save_filters(strategies)}), json.dumps(records, default=str, ensure_ascii=False)))
        print(json.dumps({"at_utc": at.isoformat(), "observations": len(records),
                          "candidates": sum(bool(r["arms"]) for r in records),
                          "closed": sum(len(r["exits"]) for r in records)}), flush=True)
        at += HOUR


async def run(args):
    market = CandleMarket()
    strategies = [
        prepare_strategy(variant, cfg, market)
        for variant, cfg in zip(VARIANTS, (args.original_config, args.aggressive_config))
    ]
    symbols = sorted(set.intersection(*(set(strategy.symbols) for _, strategy in strategies)))
    if not symbols:
        raise ValueError("strategies have no shared symbols")
    settings = json.dumps({
        "version": 1, "fee_rate": args.fee_rate,
        "configs": [compute_config_hash(strategy.config) for _, strategy in strategies],
    }, sort_keys=True)
    with sqlite3.connect(args.ledger) as db:
        db.execute("CREATE TABLE IF NOT EXISTS profile (settings TEXT UNIQUE NOT NULL)")
        stored = db.execute("SELECT settings FROM profile LIMIT 1").fetchone()
        if stored and stored[0] != settings:
            raise RuntimeError("ledger configuration changed")
        db.execute("INSERT OR IGNORE INTO profile VALUES (?)", (settings,))
        db.commit()
        db.execute("CREATE TABLE IF NOT EXISTS hours (at TEXT PRIMARY KEY, state TEXT, records TEXT)")
        async with KLineService(args.kline_url) as client:
            while True:
                # Give the market data service ten minutes to commit the closed buckets.
                cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(
                    minute=0, second=0, microsecond=0, tzinfo=None,
                )
                try:
                    await collect_cycle(db, client, strategies, market, symbols, cutoff, args.fee_rate)
                except Exception as error:
                    if args.once:
                        raise
                    print(json.dumps({"error": str(error), "cutoff_utc": cutoff.isoformat()}), flush=True)
                if args.once:
                    return
                await asyncio.sleep(60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kline-url", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--original-config", type=Path, required=True)
    parser.add_argument("--aggressive-config", type=Path, required=True)
    parser.add_argument("--fee-rate", type=float, default=0.0005)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.fee_rate < .01:
        parser.error("fee rate per side must be within [0, .01)")
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
