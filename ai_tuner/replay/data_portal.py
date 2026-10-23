"""无未来数据访问门户（DataPortal）。

唯一数据读取面：所有查询显式携带 ``as_of``，越界抛 ``FutureDataAccess``，
缺数据抛 ``DataBlocked``（结构化 gaps），绝不回填、绝不 fallback 到当前 API 值。

快照目录结构（由 scripts/export_replay_snapshot.py 生成，git 忽略）：:

    var/replay/snapshots/<strategy_id>/<as_of>/
        manifest.json                 # 顶层 SHA-256 + 逐文件 SHA-256
        config_hashes.json            # base/params/overlay 三个哈希
        universe_slices.json          # 历史 universe 切片（MTPCS=静态，HRS=历史）
        exchange_info.json            # tickSize / stepSize / minNotional
        klines/<SYMBOL>_<interval>.jsonl
        market_snapshots.jsonl        # HRS 外部时点快照（MTPCS 可缺）
        funding_rates.jsonl           # 资金费结算点
        trade_signals.jsonl           # 有效历史信号（金样/样本量门禁用）
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import (
    Bar,
    ConfigHashes,
    DataBlocked,
    ExternalSnapshot,
    FutureDataAccess,
    UniverseSlice,
    dec,
    parse_utc,
    to_utc_z,
    ZERO,
)

INTERVAL_MINUTES: Dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "6h": 360,
    "8h": 480,
    "12h": 720,
    "1d": 1440,
}

# 工具：快照哈希（搬迁 backtest/btc_eth/replay/snapshot.py 口径，不 import backtest）


def sha256_file(path: str) -> str:
    """文件内容 SHA-256（分块读取，避免大文件占内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_manifest_sha256(files: Dict[str, Dict[str, Any]]) -> str:
    """基于每个文件的 sha256 字段组合顶层哈希（canonical JSON）。

    与 verify_snapshot 共用，防「改文件 + 同步改嵌套 sha256 + 保留旧顶层哈希」。
    """
    payload = {name: info["sha256"] for name, info in sorted(files.items())}
    return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def resolve_snapshot_dir(root: Any, strategy_id: str, as_of: datetime) -> Path:
    """解析快照目录：支持 ``20261022T000000Z`` 与 ``2026-10-22T00:00:00Z`` 两种命名。"""
    base = Path(root) / strategy_id
    compact = as_of.strftime("%Y%m%dT%H%M%SZ")
    iso = to_utc_z(as_of)
    for candidate in (base / compact, base / iso, base / as_of.isoformat()):
        if candidate.is_dir():
            return candidate
    # 未命中时返回标准命名，由调用方在读取时 fail-closed
    return base / compact


class DataPortal:
    """冻结快照的只读门户。"""

    def __init__(
        self,
        snapshot_dir: Any,
        strategy_id: str,
        as_of: datetime,
        window_start: datetime,
        window_end: Optional[datetime] = None,
        required_datasets: Iterable[str] = (),
    ) -> None:
        self.snapshot_dir = Path(snapshot_dir)
        self.strategy_id = strategy_id
        self.as_of = as_of
        self.window_start = window_start
        self.window_end = window_end or as_of
        self.required_datasets = tuple(required_datasets)

        self.manifest: Dict[str, Any] = {}
        self.manifest_sha256 = ""
        self._jsonl_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._json_cache: Dict[str, Any] = {}
        self._bars_cache: Dict[str, Tuple[Bar, ...]] = {}
        self.future_rows_filtered = 0
        self._load_and_verify_manifest()

    # ------------------------------------------------------------------
    # manifest 与校验
    # ------------------------------------------------------------------
    def _load_and_verify_manifest(self) -> None:
        manifest_path = self.snapshot_dir / "manifest.json"
        if not manifest_path.is_file():
            raise DataBlocked(["manifest_missing:{0}".format(manifest_path)])
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        files = manifest.get("files") or {}
        if not files:
            raise DataBlocked(["manifest_files_empty"])

        for name in sorted(files):
            info = files[name]
            raw_path = info.get("path")
            if not raw_path:
                raise DataBlocked(["manifest_file_path_missing:{0}".format(name)])
            path = Path(raw_path)
            if not path.is_absolute():
                path = self.snapshot_dir / path
            if not path.is_file():
                raise DataBlocked(["snapshot_file_missing:{0}".format(name)])
            actual = sha256_file(str(path))
            if actual != info.get("sha256"):
                raise DataBlocked(["snapshot_tampered:{0}".format(name)])

        actual_top = compute_manifest_sha256(files)
        if actual_top != manifest.get("sha256"):
            raise DataBlocked(["snapshot_manifest_hash_mismatch"])

        self.manifest = manifest
        self.manifest_sha256 = actual_top

    def _require_dataset(self, name: str) -> None:
        files = self.manifest.get("files") or {}
        if name not in files:
            raise DataBlocked(["dataset_missing:{0}".format(name)])

    # ------------------------------------------------------------------
    # as_of 守卫
    # ------------------------------------------------------------------
    def cutoff(self, as_of: Optional[datetime], interface: str) -> datetime:
        """返回本次查询的截止时间；超过门户 as_of 即 FutureDataAccess。"""
        if as_of is None:
            return self.as_of
        if as_of > self.as_of:
            raise FutureDataAccess(interface, self.as_of, "as_of", to_utc_z(as_of))
        return as_of

    def guard(self, ts: datetime, interface: str, field: str, cutoff: datetime) -> None:
        """单条记录的时间字段越过截止时间 → FutureDataAccess（不捕获、不降级）。"""
        if ts > cutoff:
            raise FutureDataAccess(interface, self.as_of, field, to_utc_z(ts))

    # ------------------------------------------------------------------
    # 原始读取（带缓存）
    # ------------------------------------------------------------------
    def _read_jsonl(self, rel_path: str) -> List[Dict[str, Any]]:
        if rel_path in self._jsonl_cache:
            return self._jsonl_cache[rel_path]
        path = self.snapshot_dir / rel_path
        if not path.is_file():
            raise DataBlocked(["file_missing:{0}".format(rel_path)])
        rows: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if text:
                    rows.append(json.loads(text))
        self._jsonl_cache[rel_path] = rows
        return rows

    def _read_json(self, rel_path: str, optional: bool = False) -> Any:
        if rel_path in self._json_cache:
            return self._json_cache[rel_path]
        path = self.snapshot_dir / rel_path
        if not path.is_file():
            if optional:
                return None
            raise DataBlocked(["file_missing:{0}".format(rel_path)])
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self._json_cache[rel_path] = payload
        return payload

    # ------------------------------------------------------------------
    # 查询接口（契约 §3）
    # ------------------------------------------------------------------
    def bars(
        self,
        symbol: str,
        interval: str,
        as_of: Optional[datetime] = None,
    ) -> Tuple[Bar, ...]:
        """返回 ``close_time <= as_of`` 的全部已收盘 K 线（有序）。"""
        cutoff = self.cutoff(as_of, "bars")
        cache_key = "{0}|{1}".format(symbol, interval)
        cached = self._bars_cache.get(cache_key)
        if cached is None:
            rel = "klines/{0}_{1}.jsonl".format(symbol, interval)
            rows = self._read_jsonl(rel)
            parsed: List[Bar] = []
            for row in rows:
                parsed.append(self._parse_bar(symbol, interval, row))
            cached = tuple(parsed)
            self._bars_cache[cache_key] = cached

        result: List[Bar] = []
        for bar in cached:
            if bar.close_time <= cutoff:
                result.append(bar)
            else:
                self.future_rows_filtered += 1
        return tuple(result)

    def bar_at(self, symbol: str, interval: str, close_time: datetime) -> Bar:
        """按收盘时间精确取一根 K 线；越界立即 FutureDataAccess。"""
        self.guard(close_time, "bars", "close_time", self.as_of)
        for bar in self.bars(symbol, interval):
            if bar.close_time == close_time:
                return bar
        raise DataBlocked(["bar_missing:{0}:{1}:{2}".format(symbol, interval, to_utc_z(close_time))])

    def universe(self, as_of: Optional[datetime] = None) -> UniverseSlice:
        """返回该 as_of 时点真实生效的 universe 切片。"""
        cutoff = self.cutoff(as_of, "universe")
        raw = self._read_json("universe_slices.json")
        if isinstance(raw, dict):
            raw = raw.get("slices", [])
        if not raw:
            raise DataBlocked(["{0}_universe_missing".format(self.strategy_id)])

        candidates: List[Tuple[datetime, int, UniverseSlice]] = []
        for index, item in enumerate(raw):
            observed_raw = item.get("observed_at") or item.get("created_at") or item.get("valid_from")
            observed_at = parse_utc(observed_raw)
            self.guard(observed_at, "universe", "observed_at", cutoff)
            valid_from = parse_utc(item["valid_from"])
            valid_to = parse_utc(item["valid_to"])
            slice_ = UniverseSlice(
                strategy_id=item["strategy_id"],
                valid_from=valid_from,
                valid_to=valid_to,
                symbols=tuple(item["symbols"]),
                intervals=tuple(item["intervals"]),
                source=item["source"],
                config_hash=item["config_hash"],
            )
            if valid_from < cutoff:
                candidates.append((valid_from, index, slice_))

        if not candidates:
            raise DataBlocked(["{0}_universe_not_effective".format(self.strategy_id)])

        candidates.sort(key=lambda entry: (entry[0], entry[1]))
        picked = candidates[-1][2]
        if picked.valid_to <= self.window_start:
            raise DataBlocked(["{0}_universe_not_covering_window".format(self.strategy_id)])
        return picked

    def external_snapshot(
        self,
        symbol: str,
        as_of: Optional[datetime] = None,
    ) -> Tuple[ExternalSnapshot, ...]:
        """返回 ``bucket_end <= as_of`` 的外部时点快照（有序）。"""
        cutoff = self.cutoff(as_of, "external_snapshot")
        rows = self._read_jsonl("market_snapshots.jsonl")
        result: List[ExternalSnapshot] = []
        for row in rows:
            bucket_start = parse_utc(row["bucket_start"])
            bucket_end = parse_utc(row["bucket_end"])
            self.guard(bucket_end, "external_snapshot", "bucket_end", cutoff)
            if row.get("symbol") != symbol:
                continue
            source_observed_at = parse_utc(
                row.get("source_observed_at") or row.get("observed_at") or bucket_end
            )
            source_ok = source_observed_at <= bucket_end
            snap = ExternalSnapshot(
                strategy_id=row.get("strategy_id", self.strategy_id),
                symbol=row["symbol"],
                observed_at=parse_utc(row["observed_at"]),
                bucket_start=bucket_start,
                open_interest_usd=_optional_dec(row.get("open_interest_usd")),
                funding_rate=_optional_dec(row.get("funding_rate")),
                market_cap_usd=_optional_dec(row.get("market_cap_usd")),
                oi_market_cap_ratio=_optional_dec(row.get("oi_market_cap_ratio")),
                sentiment_score=_optional_dec(row.get("sentiment_score")),
                oi_source=row.get("oi_source", "missing"),
                funding_source=row.get("funding_source", "missing"),
                market_cap_source=row.get("market_cap_source", "missing"),
                payload_hash=row.get("payload_hash", ""),
                is_complete=bool(row.get("is_complete")) and source_ok,
            )
            result.append(snap)
        return tuple(result)

    def funding_rates(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        as_of: Optional[datetime] = None,
    ) -> Tuple[Tuple[datetime, Decimal], ...]:
        """返回窗口内资金费结算点 ``[(funding_time, rate)]``（有序）。"""
        cutoff = self.cutoff(as_of, "funding_rates")
        rows = self._read_jsonl("funding_rates.jsonl")
        result: List[Tuple[datetime, Decimal]] = []
        for row in rows:
            if row.get("symbol") != symbol:
                continue
            funding_time = parse_utc(row["funding_time"])
            self.guard(funding_time, "funding_rates", "funding_time", cutoff)
            if start < funding_time <= end:
                result.append((funding_time, dec(row["funding_rate"])))
        result.sort(key=lambda item: item[0])
        return tuple(result)

    def trade_signals(self, as_of: Optional[datetime] = None) -> Tuple[Dict[str, Any], ...]:
        """返回 ``signal_time <= as_of`` 的有效历史信号（样本量与金样对账用）。"""
        cutoff = self.cutoff(as_of, "trade_signals")
        rows = self._read_jsonl("trade_signals.jsonl")
        result: List[Dict[str, Any]] = []
        for row in rows:
            signal_time = parse_utc(row["signal_time"])
            self.guard(signal_time, "trade_signals", "signal_time", cutoff)
            result.append(dict(row))
        result.sort(key=lambda item: (parse_utc(item["signal_time"]), item.get("signal_id", "")))
        return tuple(result)

    def config_version(self, as_of: Optional[datetime] = None) -> ConfigHashes:
        """返回生效配置身份三件套；任一缺失即 BLOCKED_DATA（版本兼容门禁）。"""
        self.cutoff(as_of, "config_version")
        raw = self._read_json("config_hashes.json")
        missing = [
            key
            for key in ("base_config_hash", "params_manifest_hash", "overlay_hash")
            if not raw.get(key)
        ]
        if missing:
            raise DataBlocked(["config_hash_missing:{0}".format("|".join(missing))])
        return ConfigHashes(
            strategy_id=raw.get("strategy_id", self.strategy_id),
            as_of=parse_utc(raw.get("as_of") or self.as_of),
            base_config_hash=raw["base_config_hash"],
            params_manifest_hash=raw["params_manifest_hash"],
            overlay_hash=raw["overlay_hash"],
        )

    def symbol_filters(self, symbol: str) -> Dict[str, Decimal]:
        """返回 ``{tick_size, step_size, min_notional}``（来自 exchangeInfo 快照）。"""
        raw = self._read_json("exchange_info.json")
        symbols = raw.get("symbols") or []
        for item in symbols:
            if item.get("symbol") == symbol:
                filters = {f["filterType"]: f for f in item.get("filters", [])}
                price_filter = filters.get("PRICE_FILTER", {})
                lot_size = filters.get("LOT_SIZE", {})
                min_notional = filters.get("MIN_NOTIONAL", {})
                return {
                    "tick_size": dec(price_filter.get("tickSize", "0.01")),
                    "step_size": dec(lot_size.get("stepSize", "0.001")),
                    "min_notional": dec(min_notional.get("notional") or min_notional.get("minNotional") or "5"),
                }
        raise DataBlocked(["exchange_info_symbol_missing:{0}".format(symbol)])

    # ------------------------------------------------------------------
    # 覆盖度报告（写入 data_quality.json）
    # ------------------------------------------------------------------
    def coverage_report(self) -> Dict[str, Any]:
        """计算 K 线覆盖、外部快照覆盖、缺口清单与数据完整性结论。"""
        missing_symbol_intervals: List[Dict[str, Any]] = []
        coverage_ratios: List[Decimal] = []

        try:
            universe = self.universe()
        except DataBlocked as exc:
            return {
                "data_complete": False,
                "kline_coverage": "0",
                "external_snapshot_coverage": "0",
                "missing_symbol_intervals": [],
                "universe_gaps": list(exc.gaps),
                "funding_gaps": [],
                "proxy_ignored": 0,
                "future_rows_filtered": self.future_rows_filtered,
                "status": "BLOCKED_DATA",
            }

        for symbol in universe.symbols:
            for interval in universe.intervals:
                expected = self._expected_bar_count(interval)
                try:
                    actual = len(self.bars(symbol, interval))
                except DataBlocked:
                    actual = 0
                ratio = (
                    Decimal("0")
                    if expected <= 0
                    else min(Decimal("1"), Decimal(actual) / Decimal(expected))
                )
                coverage_ratios.append(ratio)
                if actual < expected:
                    missing_symbol_intervals.append(
                        {
                            "symbol": symbol,
                            "interval": interval,
                            "expected": expected,
                            "actual": actual,
                        }
                    )

        kline_coverage = (
            sum(coverage_ratios, ZERO) / Decimal(len(coverage_ratios))
            if coverage_ratios
            else Decimal("0")
        )

        external_coverage, proxy_ignored, external_gaps = self._external_coverage(universe)
        funding_gaps = self._funding_gaps(universe)

        data_complete = (
            not missing_symbol_intervals
            and kline_coverage >= Decimal("1")
            and not external_gaps
            and not funding_gaps
            and all(name in (self.manifest.get("files") or {}) for name in self.required_datasets)
        )

        return {
            "data_complete": bool(data_complete),
            "kline_coverage": _pct(kline_coverage),
            "external_snapshot_coverage": external_coverage,
            "missing_symbol_intervals": missing_symbol_intervals,
            "universe_gaps": [],
            "funding_gaps": funding_gaps,
            "proxy_ignored": proxy_ignored,
            "future_rows_filtered": self.future_rows_filtered,
            "status": "OK" if data_complete else "BLOCKED_DATA",
        }

    def _expected_bar_count(self, interval: str) -> int:
        minutes = INTERVAL_MINUTES.get(interval)
        if not minutes:
            return 0
        span = self.window_end - self.window_start
        total_minutes = int(span.total_seconds() // 60)
        return max(0, total_minutes // minutes)

    def _external_coverage(
        self, universe: UniverseSlice
    ) -> Tuple[str, int, List[str]]:
        """外部快照覆盖率；``oi_proxy``/``missing`` 不计入完整覆盖。"""
        if "market_snapshots.jsonl" not in (self.manifest.get("files") or {}):
            return "0", 0, ["market_snapshots_dataset_missing"]

        expected_per_symbol = max(1, int((self.window_end - self.window_start).total_seconds() // 3600))
        expected_total = expected_per_symbol * len(universe.symbols)
        complete = 0
        proxy_ignored = 0
        gaps: List[str] = []
        for symbol in universe.symbols:
            try:
                snaps = self.external_snapshot(symbol)
            except DataBlocked:
                gaps.append("external_snapshot_missing:{0}".format(symbol))
                continue
            for snap in snaps:
                if snap.oi_source in ("oi_proxy", "missing") or snap.funding_source == "missing":
                    proxy_ignored += 1
                    continue
                if snap.market_cap_source not in ("coingecko", "cache_hit"):
                    proxy_ignored += 1
                    continue
                if snap.is_complete:
                    complete += 1
            if len(snaps) < expected_per_symbol:
                gaps.append("external_snapshot_rows_short:{0}".format(symbol))

        ratio = Decimal("0") if expected_total <= 0 else Decimal(complete) / Decimal(expected_total)
        return _pct(min(Decimal("1"), ratio)), proxy_ignored, gaps

    def _funding_gaps(self, universe: UniverseSlice) -> List[Dict[str, Any]]:
        """资金费缺口（影响净盈亏，不可近似为 0）。"""
        gaps: List[Dict[str, Any]] = []
        if "funding_rates.jsonl" not in (self.manifest.get("files") or {}):
            return [{"symbol": "*", "gap": "funding_rates_dataset_missing"}]
        for symbol in universe.symbols:
            try:
                rows = self.funding_rates(symbol, self.window_start, self.window_end)
            except DataBlocked:
                gaps.append({"symbol": symbol, "gap": "funding_rates_file_missing"})
                continue
            if not rows:
                gaps.append({"symbol": symbol, "gap": "funding_rates_empty"})
        return gaps

    # ------------------------------------------------------------------
    # 内部解析
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_bar(symbol: str, interval: str, row: Dict[str, Any]) -> Bar:
        return Bar(
            symbol=row.get("symbol", symbol),
            interval=row.get("interval", interval),
            open_time=parse_utc(row["open_time"]),
            close_time=parse_utc(row["close_time"]),
            open=dec(row["open"]),
            high=dec(row["high"]),
            low=dec(row["low"]),
            close=dec(row["close"]),
            volume=dec(row.get("volume")),
            quote_volume=dec(row.get("quote_volume")),
            trades=int(row.get("trades") or row.get("count") or 0),
        )


def _optional_dec(raw: Any) -> Optional[Decimal]:
    if raw is None or raw == "":
        return None
    return dec(raw)


def _pct(ratio: Decimal) -> str:
    """比例 → 百分数字符串（保留 4 位，Decimal，避免浮点）。"""
    return str((ratio * Decimal("100")).quantize(Decimal("0.0001")))


def window_bounds(as_of: datetime, diagnostic_weeks: int, blind_weeks: int) -> Tuple[datetime, datetime]:
    """按诊断区/盲测区周数计算回放窗口 ``[start, as_of]``。"""
    total_weeks = diagnostic_weeks + blind_weeks
    start = as_of - timedelta(weeks=total_weeks)
    return start, as_of


def interval_delta(interval: str) -> timedelta:
    minutes = INTERVAL_MINUTES.get(interval)
    if not minutes:
        raise ValueError("不支持的 interval：{0}".format(interval))
    return timedelta(minutes=minutes)


def ensure_dir(path: Any) -> Path:
    target = Path(path)
    os.makedirs(str(target), exist_ok=True)
    return target
