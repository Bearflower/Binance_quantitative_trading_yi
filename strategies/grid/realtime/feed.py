"""行情接入层：aggTrades WS 流、退避重连、去重、REST 缺口补齐。

口径权威：需求 §5/§8.1、计划 §2。
- WS：wss://fstream.binance.com/ws/{symbol}@aggTrade；接收停顿/断开即 DEGRADED。
- 重连指数退避（transport.reconnect_initial_seconds → reconnect_max_seconds）。
- 去重：按 agg_trade_id，重复/乱序旧笔丢弃，不用旧价格报警（AC-11）。
- REST 补齐（/fapi/v1/aggTrades fromId，限速）只用于窗口连续性；DEGRADED
  期间 main 不触发事件，故不补发过时事件；重连后只判断最新状态。
- WS/REST 端点为技术常量，允许环境变量覆盖；不调用任何交易接口。
"""
from __future__ import annotations

import asyncio
import json as json_lib
import os
import time
from decimal import Decimal
from typing import AsyncIterator, Awaitable, Callable, Dict, List, Optional

import aiohttp

from .features import AggTrade

_DEFAULT_WS_BASE = "wss://fstream.binance.com"
_DEFAULT_REST_BASE = "https://fapi.binance.com"
_REST_PATH = "/fapi/v1/aggTrades"
_PAGE_LIMIT = 1000


class FeedInterrupted(RuntimeError):
    """WS 停顿或断开（触发退避重连）。"""


# 消息解析与校验（纯函数）

def parse_ws_message(data: Dict, symbol: str) -> Optional[AggTrade]:
    """解析 aggTrade 推送；身份/品种/正价量/时间非法返回 None。"""
    if data.get("e") != "aggTrade" or data.get("s") != symbol:
        return None
    try:
        trade_id = int(data["a"])
        price = Decimal(str(data["p"]))
        quantity = Decimal(str(data["q"]))
        trade_time = int(data["T"])
    except (KeyError, ValueError, ArithmeticError):
        return None
    if trade_id <= 0 or trade_time <= 0 or price <= 0 or quantity <= 0:
        return None
    return AggTrade(trade_id, price, quantity, trade_time, int(bool(data.get("m"))))


def parse_rest_row(row: Dict, symbol: str) -> Optional[AggTrade]:
    """解析 REST aggTrades 行（币安字段：a/p/q/T/m）。"""
    try:
        trade_id = int(row["a"])
        price = Decimal(str(row["p"]))
        quantity = Decimal(str(row["q"]))
        trade_time = int(row["T"])
    except (KeyError, ValueError, ArithmeticError):
        return None
    if trade_id <= 0 or trade_time <= 0 or price <= 0 or quantity <= 0:
        return None
    return AggTrade(trade_id, price, quantity, trade_time, int(bool(row.get("m"))))


# 端点（环境变量覆盖，不按 cwd 猜测）

def ws_url(symbol: str) -> str:
    base = os.getenv("BINANCE_WS_BASE_URL", _DEFAULT_WS_BASE).rstrip("/")
    return f"{base}/ws/{symbol.lower()}@aggTrade"


def rest_url() -> str:
    return os.getenv("BINANCE_REST_BASE_URL", _DEFAULT_REST_BASE).rstrip("/") + _REST_PATH


HealthCallback = Callable[[bool, str], Awaitable[None]]
WsFactory = Callable[[str], Awaitable]
RestFetcher = Callable[[int], Awaitable[List[AggTrade]]]
SleepFunc = Callable[[float], Awaitable[None]]


class _WsConnection:
    """WS + session 包装：receive 透传，close 同时释放两者。"""

    def __init__(self, ws, session):
        self._ws = ws
        self._session = session

    async def receive(self, timeout: float):
        return await self._ws.receive(timeout=timeout)

    async def close(self) -> None:
        try:
            await self._ws.close()
        finally:
            await self._session.close()


class TradeFeed:
    """WS 成交流（含重连与补齐）；通过 health 回调上报 DEGRADED/恢复。"""

    def __init__(self, cfg: Dict, *, on_health: Optional[HealthCallback] = None,
                 ws_factory: Optional[WsFactory] = None,
                 rest_fetcher: Optional[RestFetcher] = None,
                 sleep: SleepFunc = asyncio.sleep):
        self._cfg = cfg
        self._symbol = cfg["symbol"]
        self._on_health = on_health
        self._ws_factory = ws_factory or self._default_ws_factory
        self._rest_fetcher = rest_fetcher
        self._sleep = sleep
        self._last_id: Optional[int] = None
        self._backoff = cfg["transport"]["reconnect_initial_seconds"]

    @property
    def last_id(self) -> Optional[int]:
        """库内已知最大 agg_trade_id（去重/补齐游标）。"""
        return self._last_id

    async def stream(self) -> AsyncIterator[AggTrade]:
        """有序成交流：WS → (断线) DEGRADED/退避/REST 补齐 → 重连。"""
        while True:
            try:
                async for trade in self._consume_ws():  # pragma: no branch
                    yield trade
            except FeedInterrupted as exc:
                reason = str(exc)
            await self._set_health(False, reason)
            await self._sleep(self._backoff)
            async for trade in self._refill():
                yield trade
            self._increase_backoff()

    async def _consume_ws(self) -> AsyncIterator[AggTrade]:
        """单次 WS 连接消费；停顿超时/连接关闭抛 FeedInterrupted。"""
        ws = await self._ws_factory(ws_url(self._symbol))
        timeout = self._cfg["transport"]["heartbeat_seconds"]
        try:
            while True:
                trade = await self._read_one(ws, timeout)
                if trade is None:
                    continue
                if self._last_id is not None and trade.agg_trade_id <= self._last_id:
                    continue
                self._last_id = trade.agg_trade_id
                self._backoff = self._cfg["transport"]["reconnect_initial_seconds"]
                await self._set_health(True, "")
                yield trade
        finally:
            await self._close_ws(ws)

    async def _read_one(self, ws, timeout: float) -> Optional[AggTrade]:
        """读一条消息并解析；非文本跳过，关闭/超时抛 FeedInterrupted。"""
        msg = await ws.receive(timeout=timeout)
        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            raise FeedInterrupted("WS 连接关闭或出错")
        if msg.type != aiohttp.WSMsgType.TEXT:
            return None
        try:
            data = json_lib.loads(msg.data)
        except json_lib.JSONDecodeError:
            return None
        return parse_ws_message(data, self._symbol)

    async def _refill(self) -> AsyncIterator[AggTrade]:
        """REST 从 last_id+1 补齐缺口（限速）；无游标/失败则不补齐。"""
        fetcher = self._rest_fetcher or self._default_rest_fetch
        if self._last_id is None:
            return
        cursor = self._last_id + 1
        while True:
            rows = await fetcher(cursor)
            fresh = [t for t in rows if t.agg_trade_id > self._last_id]
            for trade in fresh:
                self._last_id = trade.agg_trade_id
                yield trade
            if len(rows) < _PAGE_LIMIT or not fresh:
                break
            cursor = self._last_id + 1
            rps = self._cfg["transport"]["refill_requests_per_second"]
            await self._sleep(1 / rps)

    def _increase_backoff(self) -> None:
        """指数退避封顶 reconnect_max_seconds。"""
        cap = self._cfg["transport"]["reconnect_max_seconds"]
        self._backoff = min(self._backoff * 2, cap)

    async def _set_health(self, healthy: bool, reason: str) -> None:
        if self._on_health is not None:
            await self._on_health(healthy, reason)

    async def _default_ws_factory(self, url: str) -> "_WsConnection":
        """在线 WS 连接（aiohttp；连接失败转 FeedInterrupted 走重连）。"""
        session = aiohttp.ClientSession()
        try:
            ws = await session.ws_connect(url, heartbeat=None)
        except Exception as exc:
            await session.close()
            raise FeedInterrupted(f"WS 连接失败: {type(exc).__name__}")
        return _WsConnection(ws, session)

    async def _default_rest_fetch(self, from_id: int) -> List[AggTrade]:
        """在线 REST 单页（单次发送不内部退避；失败返回空行交给重连）。"""
        params = {"symbol": self._symbol, "fromId": from_id,
                  "limit": _PAGE_LIMIT}
        session = aiohttp.ClientSession()
        try:
            async with session.get(rest_url(), params=params) as resp:
                if resp.status != 200:
                    return []
                rows = await resp.json(content_type=None)
        finally:
            await session.close()
        return [t for t in (parse_rest_row(r, self._symbol) for r in rows)
                if t is not None]

    async def _close_ws(self, ws) -> None:
        await ws.close()


def now_ms() -> int:
    """墙钟毫秒（main 节拍时间戳用）。"""
    return int(time.time() * 1000)
