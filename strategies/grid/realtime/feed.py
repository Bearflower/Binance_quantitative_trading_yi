"""行情接入层：原始成交(trade) WS 流、退避重连、去重、REST 缺口补齐。

口径权威：需求 §5/§8.1、计划 §2。
- WS：wss://fstream.binance.com/ws/{symbol}@trade（原始成交；@aggTrade 已停推）。
- 接收停顿/断开/静默无数据即 DEGRADED：静默时 receive 超时转 FeedInterrupted 走重连。
- 重连指数退避（transport.reconnect_initial_seconds → reconnect_max_seconds）。
- 去重：按原始成交 id（WS 字段 t / REST 字段 id），重复/乱序旧笔丢弃，
  不用旧价格报警（AC-11）。
- REST 补齐（/fapi/v1/trades 无 fromId，单次取最近一批）仅补原始 ID
  的连续前缀；缺口不可补齐时开启新观察段，main 清空旧窗口并重新预热。
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
_REST_PATH = "/fapi/v1/trades"
_PAGE_LIMIT = 1000


class FeedInterrupted(RuntimeError):
    """WS 停顿或断开（触发退避重连）。"""


# 消息解析与校验（纯函数）

def parse_ws_message(data: Dict, symbol: str) -> Optional[AggTrade]:
    """解析 trade 原始成交推送；身份/品种/正价量/时间非法返回 None。"""
    if data.get("e") != "trade" or data.get("s") != symbol:
        return None
    try:
        trade_id = int(data["t"])
        price = Decimal(str(data["p"]))
        quantity = Decimal(str(data["q"]))
        trade_time = int(data["T"])
    except (KeyError, ValueError, ArithmeticError):
        return None
    if trade_id <= 0 or trade_time <= 0 or price <= 0 or quantity <= 0:
        return None
    return AggTrade(trade_id, price, quantity, trade_time, int(bool(data.get("m"))))


def parse_rest_row(row: Dict) -> Optional[AggTrade]:
    """解析 REST 最近成交行（币安原始成交字段：id/price/qty/time/isBuyerMaker）。"""
    try:
        trade_id = int(row["id"])
        price = Decimal(str(row["price"]))
        quantity = Decimal(str(row["qty"]))
        trade_time = int(row["time"])
    except (KeyError, ValueError, ArithmeticError):
        return None
    if trade_id <= 0 or trade_time <= 0 or price <= 0 or quantity <= 0:
        return None
    return AggTrade(trade_id, price, quantity, trade_time,
                    int(bool(row.get("isBuyerMaker"))))


# 端点（环境变量覆盖，不按 cwd 猜测）

def ws_url(symbol: str) -> str:
    base = os.getenv("BINANCE_WS_BASE_URL", _DEFAULT_WS_BASE).rstrip("/")
    return f"{base}/ws/{symbol.lower()}@trade"


def rest_url() -> str:
    return os.getenv("BINANCE_REST_BASE_URL", _DEFAULT_REST_BASE).rstrip("/") + _REST_PATH


HealthCallback = Callable[[bool, str], Awaitable[None]]
WsFactory = Callable[[str], Awaitable]
RestFetcher = Callable[[], Awaitable[List[AggTrade]]]
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
        self._missing_until: Optional[int] = None
        self._backoff = cfg["transport"]["reconnect_initial_seconds"]

    @property
    def last_id(self) -> Optional[int]:
        """本进程已消费的最大原始成交 ID（不与聚合成交库 ID 混用）。"""
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
            try:
                async for trade in self._refill():
                    yield trade
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                await self._set_health(False, f"REST 补齐失败: {type(exc).__name__}")
                if self._missing_until is not None:
                    self._last_id = None
                    self._missing_until = None
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
                if (self._last_id is not None
                        and trade.agg_trade_id > self._last_id + 1):
                    self._missing_until = trade.agg_trade_id - 1
                    raise FeedInterrupted(
                        f"原始成交 ID 缺口: {self._last_id + 1}..{self._missing_until}")
                self._last_id = trade.agg_trade_id
                self._backoff = self._cfg["transport"]["reconnect_initial_seconds"]
                await self._set_health(True, "")
                yield trade
        finally:
            await self._close_ws(ws)

    async def _read_one(self, ws, timeout: float) -> Optional[AggTrade]:
        """读一条消息并解析；非文本跳过，关闭/超时/静默抛 FeedInterrupted。"""
        try:
            msg = await ws.receive(timeout=timeout)
        except asyncio.TimeoutError:
            raise FeedInterrupted("WS 静默无数据")
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
        """单次取最近一批原始成交补齐缺口；无游标/失败则不补齐。

        /fapi/v1/trades 无 fromId，无法按游标翻页，故单次拉取最近 _PAGE_LIMIT
        笔并按 id 升序补齐 last_id 之后的缺口（短断连足够；更久缺口交由特征层
        anchor gap 兜底，重连后仅判断最新状态，不补发过时事件）。
        """
        fetcher = self._rest_fetcher or self._default_rest_fetch
        if self._last_id is None:
            return
        rows = await fetcher()
        fresh = sorted((t for t in rows if t.agg_trade_id > self._last_id),
                       key=lambda tr: tr.agg_trade_id)
        for trade in fresh:
            if trade.agg_trade_id != self._last_id + 1:
                break
            self._last_id = trade.agg_trade_id
            yield trade
        if self._missing_until is not None:
            if self._last_id < self._missing_until:
                await self._set_health(False, "原始成交缺口未补齐，重新预热")
                self._last_id = None
            self._missing_until = None

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

    async def _default_rest_fetch(self) -> List[AggTrade]:
        """在线 REST 最近成交（单次发送不内部退避；失败返回空行交给重连）。"""
        params = {"symbol": self._symbol, "limit": _PAGE_LIMIT}
        session = aiohttp.ClientSession()
        try:
            async with session.get(rest_url(), params=params) as resp:
                if resp.status != 200:
                    return []
                rows = await resp.json(content_type=None)
        finally:
            await session.close()
        return [t for t in (parse_rest_row(r) for r in rows) if t is not None]

    async def _close_ws(self, ws) -> None:
        await ws.close()


def now_ms() -> int:
    """墙钟毫秒（main 节拍时间戳用）。"""
    return int(time.time() * 1000)
