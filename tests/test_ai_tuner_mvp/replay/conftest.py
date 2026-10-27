"""回放测试夹具：构造最小可用的冻结快照目录 + 一个确定性假适配器。

注意（D4）：本文件的快照全部是**合成夹具**，只能验证引擎确定性/撮合/成本/
无未来数据/fail-closed，**不得**记为「实盘金样对账通过」。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytest

from ai_tuner.replay.clock import collect_bar_events
from ai_tuner.replay.data_portal import compute_manifest_sha256, sha256_file
from ai_tuner.replay.models import ExternalSnapshot, OrderIntent, UniverseSlice, ConfigHashes

UTC = timezone.utc


def _write(path, payload: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_bars(
    symbol: str,
    interval: str,
    start: datetime,
    prices: Iterable[Tuple[str, str, str, str]],
    step_minutes: int = 60,
) -> List[Dict[str, Any]]:
    """按价格序列生成 K 线行（open/high/low/close）。"""
    rows: List[Dict[str, Any]] = []
    cursor = start
    for open_p, high_p, low_p, close_p in prices:
        rows.append(
            {
                "symbol": symbol,
                "interval": interval,
                "open_time": _iso(cursor),
                "close_time": _iso(cursor + timedelta(minutes=step_minutes)),
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": "10",
                "quote_volume": "100",
                "trades": 5,
            }
        )
        cursor = cursor + timedelta(minutes=step_minutes)
    return rows


def write_snapshot(
    directory: str,
    strategy_id: str,
    as_of: datetime,
    window_start: datetime,
    bars_by_key: Dict[str, List[Dict[str, Any]]],
    symbols: List[str],
    intervals: List[str],
    funding_by_symbol: Optional[Dict[str, List[Tuple[datetime, str]]]] = None,
    market_snapshots: Optional[List[Dict[str, Any]]] = None,
    trade_signals: Optional[List[Dict[str, Any]]] = None,
    universe_source: str = "strategy_config",
    extra_files: Optional[Dict[str, str]] = None,
    skip_files: Optional[Iterable[str]] = None,
) -> str:
    """写出快照目录与 manifest.json，返回顶层 SHA-256。"""
    skip = set(skip_files or ())
    files: Dict[str, Dict[str, Any]] = {}

    def add(rel: str, text: str) -> None:
        if rel in skip:
            return
        _write(os.path.join(directory, rel), text)
        files[rel] = {
            "path": rel,
            "sha256": sha256_file(os.path.join(directory, rel)),
        }

    for key in sorted(bars_by_key):
        symbol, interval = key.split("|")
        rel = "klines/{0}_{1}.jsonl".format(symbol, interval)
        add(rel, "".join(json.dumps(row, sort_keys=True) + "\n" for row in bars_by_key[key]))

    add(
        "universe_slices.json",
        json.dumps(
            [
                {
                    "strategy_id": strategy_id,
                    "valid_from": _iso(window_start),
                    "valid_to": _iso(as_of + timedelta(days=365)),
                    "observed_at": _iso(window_start),
                    "symbols": symbols,
                    "intervals": intervals,
                    "source": universe_source,
                    "config_hash": "a" * 64,
                }
            ],
            sort_keys=True,
        ),
    )
    add(
        "config_hashes.json",
        json.dumps(
            {
                "strategy_id": strategy_id,
                "as_of": _iso(as_of),
                "base_config_hash": "b" * 64,
                "params_manifest_hash": "c" * 64,
                "overlay_hash": "d" * 64,
            },
            sort_keys=True,
        ),
    )
    add(
        "exchange_info.json",
        json.dumps(
            {
                "symbols": [
                    {
                        "symbol": symbol,
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                            {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }
                    for symbol in symbols
                ]
            },
            sort_keys=True,
        ),
    )

    funding_rows: List[Dict[str, Any]] = []
    for symbol in sorted(funding_by_symbol or {}):
        for funding_time, rate in funding_by_symbol[symbol]:
            funding_rows.append(
                {"symbol": symbol, "funding_time": _iso(funding_time), "funding_rate": rate}
            )
    add(
        "funding_rates.jsonl",
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in funding_rows),
    )

    add(
        "market_snapshots.jsonl",
        "".join(
            json.dumps(row, sort_keys=True) + "\n" for row in (market_snapshots or [])
        ),
    )
    add(
        "trade_signals.jsonl",
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in (trade_signals or [])),
    )

    # 覆盖项最后写入：允许用例替换默认 universe / config_hashes 等文件
    for rel, text in sorted((extra_files or {}).items()):
        add(rel, text)

    top = compute_manifest_sha256(files)
    manifest = {
        "strategy_id": strategy_id,
        "as_of": _iso(as_of),
        "window_start": _iso(window_start),
        "window_end": _iso(as_of),
        "files": files,
        "sha256": top,
    }
    _write(os.path.join(directory, "manifest.json"), json.dumps(manifest, sort_keys=True))
    return top


class ScriptedAdapter:
    """确定性脚本适配器：只用于验证引擎接线，不含任何真实策略逻辑。"""

    strategy_id = "fixture"
    requires_external_snapshot = False

    def __init__(self, universe: UniverseSlice, config_hashes: ConfigHashes, script):
        self._universe = universe
        self._config_hashes = config_hashes
        self._script = script
        self.seen_bars = []

    def prepare(self, data):
        from ai_tuner.replay.adapters.base import PreparedContext
        from ai_tuner.replay.broker import PortfolioView

        return PreparedContext(
            strategy_id=self.strategy_id,
            as_of=data.as_of,
            universe=self._universe,
            config_hashes=self._config_hashes,
            portfolio=PortfolioView(positions=(), last_prices={}, realized_equity=Decimal("0")),
        )

    def on_bar(self, bar, ctx):
        self.seen_bars.append(bar)

    def on_snapshot(self, snap, ctx):
        return None

    def intents(self, ctx):
        if not self.seen_bars:
            return ()
        return tuple(self._script(self.seen_bars[-1], ctx))

    def protective_prices(self, symbol):
        return {}


@pytest.fixture
def hourly_prices():
    """4 根 1h K 线：先上涨后回落，high/low 足够触发止损与止盈。"""
    return [
        ("100", "101", "99", "100.5"),
        ("100.5", "102", "100", "101.5"),
        ("101.5", "103", "101", "102.5"),
        ("102.5", "103.5", "98", "99"),
    ]
