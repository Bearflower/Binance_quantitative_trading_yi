from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import structlog

from .models import (
    MarketSnapshot,
    _SENTIMENT_FORMULA_VERSION,
    _ANNUALIZED_RATE_FACTOR,
)


logger = structlog.get_logger()


@dataclass(frozen=True)
class CollectionSummary:
    requested: int
    complete: int
    proxy: int
    missing: int
    failed: int


@dataclass(frozen=True)
class SymbolBinanceData:
    """阶段一产出的中间结构：单个 symbol 的 binance 采集结果"""
    symbol: str
    oi: float
    funding: float
    price: float
    oi_usd: float


class SnapshotCollector:
    def __init__(self, repository, binance, market_cap, concurrency: int = 5) -> None:
        self.repository = repository
        self.binance = binance
        self.market_cap = market_cap
        self._semaphore = asyncio.Semaphore(max(1, concurrency))

    # ------------------------------------------------------------------
    # 主入口：三阶段流水线
    # ------------------------------------------------------------------

    async def collect_once(self, as_of: datetime) -> CollectionSummary:
        universe = sorted(await self.repository.latest_universe("hrs", as_of))
        # 阶段一：并发采集 binance 数据
        data_map, oi_map, failed = await self._collect_binance_phase(universe)
        symbols_ok = list(data_map.keys())
        # 阶段二：批量获取市值 provenance（信号量外，一次调用）
        cap_map = await self.market_cap.get_batch_market_caps_with_provenance(
            symbols_ok, oi_map
        ) if symbols_ok else {}
        # 阶段三：顺序组装 + 入库 + 计数（单 symbol 异常计入 failed，不影响其他 symbol）
        complete, proxy, missing, persist_failed = await self._persist_phase(
            data_map, cap_map, as_of
        )
        return CollectionSummary(
            len(universe), complete, proxy, missing, failed + persist_failed
        )

    # ------------------------------------------------------------------
    # 阶段一：并发采集 binance 数据
    # ------------------------------------------------------------------

    async def _collect_binance_phase(
        self, universe: List[str]
    ) -> Tuple[Dict[str, SymbolBinanceData], Dict[str, float], int]:
        """并发采集所有 symbol 的 binance 数据，返回 (data_map, oi_map, failed_count)

        单 symbol 异常 → 计入 failed、不入库、不进 oi_map，不影响其他 symbol。
        """
        results = await asyncio.gather(
            *(self._collect_binance_symbol(sym) for sym in universe),
            return_exceptions=True,
        )
        data_map: Dict[str, SymbolBinanceData] = {}
        oi_map: Dict[str, float] = {}
        failed = 0
        for item in results:
            if isinstance(item, Exception):
                logger.warning("阶段一 binance 采集异常", error=str(item))
                failed += 1
            else:
                data_map[item.symbol] = item
                oi_map[item.symbol] = item.oi_usd
        return data_map, oi_map, failed

    async def _collect_binance_symbol(self, symbol: str) -> SymbolBinanceData:
        """持信号量采集单个 symbol 的 OI / funding / ticker，算 oi_usd"""
        async with self._semaphore:
            oi = float(await self.binance.get_open_interest(symbol))
            funding = float(await self.binance.get_funding_rate(symbol))
            ticker = await self.binance.get_ticker(symbol)
            price = float(ticker["lastPrice"])
            oi_usd = oi * price
            return SymbolBinanceData(symbol, oi, funding, price, oi_usd)

    # ------------------------------------------------------------------
    # 阶段三：顺序组装 + 入库 + 计数
    # ------------------------------------------------------------------

    async def _persist_phase(
        self,
        data_map: Dict[str, SymbolBinanceData],
        cap_map: Dict[str, Any],
        as_of: datetime,
    ) -> Tuple[int, int, int, int]:
        """顺序组装每个 symbol 的 MarketSnapshot，入库并统计分类

        cap_map 契约由 shared 层保证：对传入的每个 symbol 都返回 provenance，
        这里直接用 [] 访问，缺失即 KeyError 暴露问题。

        单 symbol 组装或入库异常 → 计入 failed 并继续下一个 symbol，
        避免一个坏样本导致整桶后续样本全部丢弃。
        """
        complete = proxy = missing = failed = 0
        for symbol, datum in data_map.items():
            try:
                cap = cap_map[symbol]
                snapshot = self._build_snapshot(symbol, as_of, datum, cap)
                await self.repository.store_market_snapshot(snapshot)
            except Exception as e:
                logger.warning("阶段三快照组装/入库异常", symbol=symbol, error=str(e) or type(e).__name__)
                failed += 1
                continue
            if snapshot.is_complete:
                complete += 1
            elif snapshot.market_cap_source == "oi_proxy":
                proxy += 1
            else:
                missing += 1
        return complete, proxy, missing, failed

    def _build_snapshot(
        self, symbol: str, as_of: datetime, datum: SymbolBinanceData, cap: Any
    ) -> MarketSnapshot:
        """组装单个 MarketSnapshot（含 raw_payload / payload_hash）"""
        market_cap_usd = float(cap.value) if cap.value else None
        ratio = (
            datum.oi_usd / market_cap_usd
            if market_cap_usd and datum.oi_usd > 0
            else None
        )
        raw_payload = self._build_raw_payload(datum, cap, market_cap_usd, ratio)
        payload_hash = hashlib.sha256(
            json.dumps(raw_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        bucket_start = as_of.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        source_ts = datetime.fromtimestamp(float(cap.as_of), tz=timezone.utc)
        return MarketSnapshot(
            strategy_id="hrs",
            symbol=symbol,
            observed_at=as_of,
            bucket_start=bucket_start,
            open_interest_contracts=datum.oi,
            open_interest_usd=datum.oi_usd,
            funding_rate=datum.funding,
            market_cap_usd=market_cap_usd,
            oi_market_cap_ratio=ratio,
            sentiment_score=datum.funding * _ANNUALIZED_RATE_FACTOR,
            sentiment_formula_version=_SENTIMENT_FORMULA_VERSION,
            oi_source="binance",
            funding_source="binance",
            market_cap_source=cap.source,
            source_observed_at=source_ts,
            payload_hash=payload_hash,
            raw_payload=raw_payload,
        )

    def _build_raw_payload(
        self,
        datum: SymbolBinanceData,
        cap: Any,
        market_cap_usd: Optional[float],
        ratio: Optional[float],
    ) -> Dict[str, Any]:
        """组装 12 键 raw_payload（键名保持不变）"""
        return {
            "open_interest_contracts": datum.oi,
            "open_interest_usd": datum.oi_usd,
            "last_price": datum.price,
            "funding_rate": datum.funding,
            "market_cap_usd": market_cap_usd,
            "market_cap_source": cap.source,
            "market_cap_as_of": cap.as_of,
            "market_cap_available_at": cap.available_at,
            "market_cap_missing_reason": cap.missing_reason,
            "oi_market_cap_ratio": ratio,
            "sentiment_score": datum.funding * _ANNUALIZED_RATE_FACTOR,
            "sentiment_formula_version": _SENTIMENT_FORMULA_VERSION,
        }

    # ------------------------------------------------------------------
    # 回溯：保持 backfill_supported 完全不动
    # ------------------------------------------------------------------

    async def backfill_supported(self, start: datetime, end: datetime) -> CollectionSummary:
        requested = missing = failed = 0
        bucket = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        while bucket < end:
            bucket_end = min(bucket + timedelta(hours=1), end)
            universe = sorted(await self.repository.latest_universe("hrs", bucket_end))
            for symbol in universe:
                requested += 1
                try:
                    start_ms = int(bucket.timestamp() * 1000)
                    end_ms = int(bucket_end.timestamp() * 1000) - 1
                    oi_rows, funding_rows = await asyncio.gather(
                        self.binance.get_open_interest_hist(
                            symbol, period="1h", start_time=start_ms, end_time=end_ms, limit=1
                        ),
                        self.binance.get_funding_rate_history(
                            symbol, start_time=start_ms, end_time=end_ms, limit=1
                        ),
                    )
                    if not oi_rows or not funding_rows:
                        failed += 1
                        continue
                    oi_row = oi_rows[-1]
                    funding_row = funding_rows[-1]
                    oi = float(oi_row.get("sumOpenInterest", 0) or 0)
                    oi_usd = float(oi_row.get("sumOpenInterestValue", 0) or 0)
                    funding = float(funding_row.get("fundingRate", 0) or 0)
                    source_ms = max(
                        int(oi_row.get("timestamp", start_ms)),
                        int(funding_row.get("fundingTime", start_ms)),
                    )
                    raw_payload = {"open_interest": oi_row, "funding": funding_row}
                    snapshot = MarketSnapshot(
                        strategy_id="hrs", symbol=symbol, observed_at=bucket_end,
                        bucket_start=bucket, open_interest_contracts=oi,
                        open_interest_usd=oi_usd or None, funding_rate=funding,
                        market_cap_usd=None, oi_market_cap_ratio=None,
                        sentiment_score=funding * _ANNUALIZED_RATE_FACTOR,
                        sentiment_formula_version=_SENTIMENT_FORMULA_VERSION,
                        oi_source="binance_history", funding_source="binance_history",
                        market_cap_source="missing",
                        source_observed_at=datetime.fromtimestamp(source_ms / 1000, tz=timezone.utc),
                        payload_hash=hashlib.sha256(
                            json.dumps(raw_payload, sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest(), raw_payload=raw_payload,
                    )
                    await self.repository.store_market_snapshot(snapshot)
                    missing += 1
                except Exception:
                    failed += 1
            bucket = bucket_end
        return CollectionSummary(requested, 0, 0, missing, failed)
