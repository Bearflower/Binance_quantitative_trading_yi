"""币安 API 客户端"""

import aiohttp
import asyncio
from typing import List, Dict, Optional
from datetime import datetime
import time

from shared.utils.logger import get_logger

logger = get_logger(__name__)


class BinanceClient:
    """币安 API 客户端（无需 API Key，仅公开数据）"""

    def __init__(self, base_url: str = "https://api.binance.com"):
        self.base_url = base_url
        self.session: Optional[aiohttp.ClientSession] = None
        self.request_count = 0
        self.last_request_time = 0

    async def connect(self):
        """创建 HTTP 会话"""
        if not self.session:
            self.session = aiohttp.ClientSession()
            logger.info("✅ 币安 API 客户端已连接")

    async def disconnect(self):
        """关闭 HTTP 会话"""
        if self.session:
            await self.session.close()
            self.session = None
            logger.info("🛑 币安 API 客户端已关闭")

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        retry: int = 3,
    ) -> Optional[Dict]:
        """
        发送 HTTP 请求

        Args:
            method: HTTP 方法
            endpoint: API 端点
            params: 请求参数
            retry: 重试次数

        Returns:
            响应数据，失败返回 None
        """
        url = f"{self.base_url}{endpoint}"

        for attempt in range(retry):
            try:
                # 频率控制：简单实现，每秒最多 5 次请求
                current_time = time.time()
                if current_time - self.last_request_time < 0.2:
                    await asyncio.sleep(0.2)

                self.last_request_time = time.time()
                self.request_count += 1

                async with self.session.request(
                    method, url, params=params, timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 429:
                        # 频率限制，等待后重试
                        wait_time = 2 ** attempt
                        logger.warning(
                            f"币安 API 频率限制，等待 {wait_time} 秒后重试 (attempt {attempt + 1}/{retry})"
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        logger.error(
                            f"币安 API 请求失败：{response.status} - {await response.text()}"
                        )
                        return None

            except asyncio.TimeoutError:
                logger.warning(f"请求超时，重试 (attempt {attempt + 1}/{retry})")
                await asyncio.sleep(2 ** attempt)
                continue
            except Exception as e:
                logger.error(f"请求异常：{e}，重试 (attempt {attempt + 1}/{retry})")
                await asyncio.sleep(2 ** attempt)
                continue

        logger.error(f"请求失败，已达到最大重试次数：{endpoint}")
        return None

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 500,
    ) -> Optional[List[List]]:
        """
        获取 K 线数据

        Args:
            symbol: 交易对（如 BTCUSDT）
            interval: 时间间隔（1m, 5m, 15m, 30m, 1h, 4h, 1d 等）
            start_time: 开始时间（毫秒）
            end_time: 结束时间（毫秒）
            limit: 每次请求数量（最大 500）

        Returns:
            K 线数据列表，每项格式：
            [
                1499040000000,      # 开盘时间
                "0.01634790",       # 开盘价
                "0.80000000",       # 最高价
                "0.01575800",       # 最低价
                "0.01577100",       # 收盘价
                "148976.11427815",  # 成交量
                1499644799999,      # 收盘时间
                "2434.19055334",    # 成交额
                300,                # 成交笔数
                "1756.87402397",    # 主动买入成交量
                "28.46694236",      # 主动买入成交额
                "17928899.62484339" # 忽略字段
            ]
        """
        params = {"symbol": symbol, "interval": interval, "limit": limit}

        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        data = await self._request("GET", "/api/v3/klines", params)
        return data

    async def get_symbol_info(self, symbol: str) -> Optional[Dict]:
        """
        获取交易对信息

        Args:
            symbol: 交易对（如 BTCUSDT）

        Returns:
            交易对信息，包含价格精度、数量精度等
        """
        exchange_info = await self._request("GET", "/api/v3/exchangeInfo")
        if not exchange_info:
            return None

        symbols = exchange_info.get("symbols", [])
        for s in symbols:
            if s["symbol"] == symbol:
                # 提取价格精度和数量精度
                filters = s.get("filters", [])
                price_filter = next(
                    (f for f in filters if f["filterType"] == "PRICE_FILTER"), None
                )
                lot_size_filter = next(
                    (f for f in filters if f["filterType"] == "LOT_SIZE"), None
                )

                return {
                    "symbol": symbol,
                    "base_asset": s.get("baseAsset"),
                    "quote_asset": s.get("quoteAsset"),
                    "price_precision": (
                        int(abs(float(price_filter["tickSize"])))
                        if price_filter
                        else 2
                    ),
                    "quantity_precision": (
                        int(abs(float(lot_size_filter["stepSize"])))
                        if lot_size_filter
                        else 2
                    ),
                    "status": s.get("status"),
                }

        return None

    async def get_all_symbols(self) -> List[str]:
        """
        获取所有交易对

        Returns:
            交易对列表
        """
        exchange_info = await self._request("GET", "/api/v3/exchangeInfo")
        if not exchange_info:
            return []

        symbols = exchange_info.get("symbols", [])
        return [s["symbol"] for s in symbols if s.get("status") == "TRADING"]

    async def get_server_time(self) -> Optional[int]:
        """
        获取服务器时间

        Returns:
            服务器时间戳（毫秒）
        """
        data = await self._request("GET", "/api/v3/time")
        if data:
            return data.get("serverTime")
        return None
