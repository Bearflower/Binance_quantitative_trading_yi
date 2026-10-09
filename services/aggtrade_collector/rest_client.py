"""币安合约 aggTrades REST 客户端（公共行情，signed=False）。

口径：docs/plans/grid_realtime_alert_实施计划.md §1.1/§1.4。
- 重试口径：
  * 429（IP 级分钟窗口限流）：优先尊重 Retry-After 响应头，无头时等待一个完整
    限流窗口（rate_limit_wait_seconds），等待受 rate_limit_max_wait_seconds 封顶；
  * 5xx/连接错误/SSL/超时：指数退避 1/2/4s，最多 3 次；
  * 其余 4xx 属请求错误，直接放弃抛出。
- 轨道A：1h 时间切片 + 满页 fromId 翻页双保险。
- 轨道B：fromId 增量逐页。
本模块只发行情请求，不调用任何交易接口（FR-03）。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import AsyncIterator, Dict, List, Optional

import aiohttp

from .config import CollectorConfig
from .models import REST_FIELD_MAP, normalize_trade

logger = logging.getLogger(__name__)

_MS_PER_HOUR = 3600 * 1000
# 走指数退避的传输层瞬时故障（429/5xx 是 ClientResponseError，在重试主流程单列）
_RETRYABLE_ERRORS = (
    aiohttp.ClientConnectionError,
    aiohttp.ClientSSLError,
    asyncio.TimeoutError,
)


class RESTClientError(RuntimeError):
    """不可重试的 REST 错误（4xx 非 429）。"""

    def __init__(self, status: int, payload: str):
        super().__init__(f"aggTrades REST 请求被拒绝 status={status}: {payload[:200]}")
        self.status = status
        self.payload = payload


class AggTradesRESTClient:
    """aggTrades 单页拉取与分页迭代。"""

    def __init__(self, cfg: CollectorConfig):
        self._cfg = cfg
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "AggTradesRESTClient":
        timeout = aiohttp.ClientTimeout(total=self._cfg.timeout_seconds)
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch_agg_trades(self, symbol: str, *, start_time: Optional[int] = None,
                              end_time: Optional[int] = None, from_id: Optional[int] = None,
                              limit: Optional[int] = None) -> List[Dict]:
        """单页拉取（§1.4 签名）；三种参数形态互斥，遵循币安约束。"""
        params = self._build_params(symbol, start_time, end_time, from_id, limit)
        rows = await self._get_with_retry(params)
        received_at_ms = _now_ms()
        return [normalize_trade(r, REST_FIELD_MAP, symbol, received_at_ms) for r in rows]

    def _build_params(self, symbol, start_time, end_time, from_id, limit) -> Dict:
        """组装互斥参数：startTime+endTime+limit / fromId+limit / 仅 limit。"""
        page_limit = limit or self._cfg.page_limit
        params: Dict[str, int] = {"symbol": symbol, "limit": page_limit}
        if from_id is not None:
            if start_time is not None or end_time is not None:
                raise ValueError("fromId 与 startTime/endTime 互斥（币安约束）")
            params["fromId"] = from_id
        elif start_time is not None and end_time is not None:
            if end_time - start_time > self._cfg.slice_hours * _MS_PER_HOUR:
                raise ValueError(
                    f"startTime+endTime 窗口不得超过 {self._cfg.slice_hours} 小时（币安硬约束）")
            params["startTime"] = start_time
            params["endTime"] = end_time
        elif start_time is not None or end_time is not None:
            raise ValueError("startTime 与 endTime 必须同时给出")
        return params

    async def _get_with_retry(self, params: Dict) -> List[Dict]:
        """按 §1.1 重试单页请求：429 按限流窗口等待，5xx/连接故障指数退避。"""
        url = f"{self._cfg.base_url}{self._cfg.agg_trades_path}"
        for attempt in range(self._cfg.max_retries + 1):
            try:
                return await self._get_once(url, params)
            except aiohttp.ClientResponseError as exc:
                if exc.status == 429:
                    await self._wait_rate_limit(attempt, exc)
                else:
                    await self._backoff_or_raise(
                        attempt, f"服务端错误 status={exc.status}")
            except _RETRYABLE_ERRORS as exc:
                await self._backoff_or_raise(attempt, f"连接故障 {type(exc).__name__}")
            except aiohttp.ClientError as exc:
                # 非连接/SSL 类的协议错误不重试
                raise RESTClientError(0, f"客户端错误 {type(exc).__name__}: {exc}")
        # 不可达：末次 attempt 的可重试错误已由等待方法保证抛出
        raise RESTClientError(0, "超过最大重试次数")  # pragma: no cover

    async def _get_once(self, url: str, params: Dict) -> List[Dict]:
        assert self._session is not None, "客户端未进入 async with 上下文"
        async with self._session.get(url, params=params) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
            text = await resp.text()
            if resp.status == 429 or resp.status >= 500:
                # 复制响应头：异常会在响应释放后才被重试逻辑读取（Retry-After）
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history, status=resp.status,
                    message=text, headers=dict(resp.headers))
            raise RESTClientError(resp.status, text)

    async def _wait_rate_limit(self, attempt: int,
                               exc: aiohttp.ClientResponseError) -> None:
        """429 限流：Retry-After 头优先（封顶），无头则等一个完整限流窗口。"""
        if attempt >= self._cfg.max_retries:
            raise RESTClientError(429, "触发币安 IP 限流（429），已达最大重试次数")
        wait = self._retry_after_seconds(exc)
        logger.warning("aggTrades 触发 IP 限流 429，第 %s 次等待 %.0fs 后重试",
                       attempt + 1, wait)
        await self._sleep(wait)

    def _retry_after_seconds(self, exc: aiohttp.ClientResponseError) -> float:
        """解析 Retry-After（币安给整数秒）；缺失/非法回退默认窗口，并按上限封顶。"""
        cap = self._cfg.rate_limit_max_wait_seconds
        raw = (exc.headers or {}).get("Retry-After")
        if raw is not None:
            try:
                return max(0.0, min(float(raw.strip()), cap))
            except ValueError:
                logger.warning("Retry-After 头无法解析: %r，改用默认限流等待", raw)
        return min(self._cfg.rate_limit_wait_seconds, cap)

    async def _sleep(self, seconds: float) -> None:
        """统一 sleep 入口（重试退避/页间限速共用，便于测试注入）。"""
        await asyncio.sleep(seconds)

    async def _backoff_or_raise(self, attempt: int, reason: str) -> None:
        """未达重试上限则指数退避，否则抛出最后故障。"""
        if attempt >= self._cfg.max_retries:
            raise RESTClientError(0, f"{reason}，已达最大重试次数")
        wait = self._cfg.backoff_base_seconds * (2 ** attempt)
        logger.warning("aggTrades 请求失败，第 %s 次退避 %.0fs：%s", attempt + 1, wait, reason)
        await self._sleep(wait)

    async def fetch_range_agg_trades(
            self, symbol: str, start_time: int, end_time: int) -> AsyncIterator[Dict]:
        """轨道A：1h 切片迭代，切片内满 1000 条以末条 a 作 fromId 翻页（§1.1）。"""
        slice_width = self._cfg.slice_hours * _MS_PER_HOUR
        for slice_start in range(start_time, end_time, slice_width):
            slice_end = min(slice_start + slice_width, end_time)
            async for trade in self._fetch_slice(symbol, slice_start, slice_end):  # pragma: no branch
                yield trade

    async def _fetch_slice(self, symbol: str, slice_start: int,
                          slice_end: int) -> AsyncIterator[Dict]:
        """单 1h 切片：时间窗首页 + fromId 翻页，按 [start,end) 客户端截断。"""
        # 币安 endTime 含该毫秒，半开区间 [s,e) 用 e-1
        rows = await self.fetch_agg_trades(
            symbol, start_time=slice_start, end_time=slice_end - 1)
        async for trade in self._emit_in_range(rows, slice_end):  # pragma: no branch
            yield trade
        cursor = rows[-1]["agg_trade_id"] if rows else None
        while cursor is not None and len(rows) == self._cfg.page_limit:
            await self._page_sleep()
            rows = await self.fetch_agg_trades(symbol, from_id=cursor)
            if not rows or rows[-1]["agg_trade_id"] == cursor:
                break
            # 零产出弧不可达：fromId 含游标，而游标（上一页末条）在界内，
            # 故每页至少含 1 条界内行；越界页在下方 any(...) 处即停。例外留 C 复核。
            async for trade in self._emit_in_range(rows, slice_end):  # pragma: no branch
                yield trade
            cursor = rows[-1]["agg_trade_id"]
            if any(t["trade_time_ms"] >= slice_end for t in rows):
                break

    async def _emit_in_range(self, rows: List[Dict],
                             slice_end: int) -> AsyncIterator[Dict]:
        """产出半开区间内的成交（越界行丢弃，翻页停止由调用方判定）。"""
        for trade in rows:
            if trade["trade_time_ms"] < slice_end:
                yield trade

    async def fetch_incremental_agg_trades(
            self, symbol: str, from_id: int,
            until_ms: Optional[int] = None) -> AsyncIterator[Dict]:
        """轨道B：从库内 max id（含）逐页补齐；空页结束，可设 until_ms 截到当前。"""
        cursor = from_id
        while True:
            rows = await self.fetch_agg_trades(symbol, from_id=cursor)
            if not rows:
                break
            for trade in rows:
                if until_ms is None or trade["trade_time_ms"] <= until_ms:
                    yield trade
            if len(rows) < self._cfg.page_limit:
                break
            if until_ms is not None and rows[-1]["trade_time_ms"] >= until_ms:
                break
            cursor = rows[-1]["agg_trade_id"] + 1
            await self._page_sleep()

    async def _page_sleep(self) -> None:
        """页间限速 0.3~0.5s（生产 IP 共享，配置项，§1.1）。"""
        await self._sleep(random.uniform(
            self._cfg.page_sleep_min_seconds, self._cfg.page_sleep_max_seconds))


def _now_ms() -> int:
    """本地取得时刻（wall clock 毫秒）；REST 行不代表行情到达时刻（见 schema 注释）。"""
    return int(time.time() * 1000)
