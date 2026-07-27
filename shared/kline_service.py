"""
K线服务客户端
从通用K线服务获取数据，支持失败重连
"""
import asyncio
from typing import Dict, List, Optional
from decimal import Decimal
import aiohttp
import structlog

from .utils import retry_on_failure


logger = structlog.get_logger()


class KLineServiceError(Exception):
    """K线服务异常"""
    pass


class KLineService:
    """K线服务客户端"""
    
    def __init__(
        self,
        service_url: str,
        timeout: int = 10,
        max_retries: int = 3
    ):
        self.service_url = service_url
        self.timeout = timeout
        self.max_retries = max_retries
        
        self.session: Optional[aiohttp.ClientSession] = None
        
        logger.info(
            "K线服务客户端初始化",
            service_url=service_url,
            timeout=timeout
        )
    
    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self._init_session()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()
    
    async def _init_session(self):
        """初始化HTTP会话"""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self.session = aiohttp.ClientSession(timeout=timeout)
    
    async def close(self):
        """关闭客户端"""
        if self.session and not self.session.closed:
            await self.session.close()
    
    @retry_on_failure(max_retries=3, delay=1.0)
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100
    ) -> List[Dict]:
        """
        获取K线数据
        
        Args:
            symbol: 交易对
            interval: K线周期
            limit: 数量限制
        
        Returns:
            K线数据列表
        
        Raises:
            ValueError: 参数验证失败
            KLineServiceError: K线服务请求失败
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        if not interval or not interval.strip():
            raise ValueError("K线周期不能为空")
        
        valid_intervals = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
        if interval not in valid_intervals:
            raise ValueError(f"无效的K线周期: {interval}, 有效周期: {', '.join(valid_intervals)}")
        
        if limit <= 0 or limit > 1500:
            raise ValueError(f"数量限制必须在1-1500之间: {limit}")
        
        await self._init_session()
        
        url = f"{self.service_url}/klines/latest"
        params = {
            "symbol": symbol.strip().upper(),
            "interval": interval,
            "limit": min(limit, 100),  # K线服务API限制最大100
        }
        
        logger.debug(
            "获取K线数据",
            symbol=symbol,
            interval=interval,
            limit=limit
        )
        
        try:
            async with self.session.get(url, params=params) as response:
                data = await response.json()
                
                if response.status != 200:
                    raise KLineServiceError(
                        f"K线服务请求失败: {response.status}"
                    )
                
                if not isinstance(data, dict):
                    raise KLineServiceError(f"响应数据格式错误: 期望字典，实际为 {type(data).__name__}")
                
                if data.get('code') != 0:
                    raise KLineServiceError(
                        data.get('message', '未知错误')
                    )
                
                klines_data = data.get('data', [])
                
                if not isinstance(klines_data, list):
                    raise KLineServiceError(f"K线数据格式错误: 期望列表，实际为 {type(klines_data).__name__}")
                
                klines = []
                for kline in klines_data:
                    klines.append({
                        'open_time': kline.get('open_time'),
                        'open': Decimal(str(kline.get('open_price', 0))),
                        'high': Decimal(str(kline.get('high_price', 0))),
                        'low': Decimal(str(kline.get('low_price', 0))),
                        'close': Decimal(str(kline.get('close_price', 0))),
                        'volume': Decimal(str(kline.get('volume', 0))),
                        'close_time': kline.get('close_time'),
                        'quote_volume': Decimal(str(kline.get('quote_volume', 0))),
                        'trades': kline.get('trade_count', 0),
                    })
                
                logger.info(
                    "K线数据获取成功",
                    symbol=symbol,
                    interval=interval,
                    count=len(klines)
                )
                
                return klines
        
        except aiohttp.ClientError as e:
            logger.error(
                "K线服务连接失败",
                error=str(e)
            )
            raise KLineServiceError(f"连接失败: {e}")

    async def register_symbol(
        self,
        symbol: str,
        intervals: List[str]
    ) -> bool:
        """
        向K线服务注册新币种（自动创建数据表）

        Args:
            symbol: 交易对
            intervals: K线周期列表，如 ['1h']

        Returns:
            是否注册成功
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")

        symbol = symbol.strip().upper()

        if not intervals:
            raise ValueError("K线周期列表不能为空")

        await self._init_session()

        url = f"{self.service_url}/register"
        body = {"symbol": symbol, "intervals": intervals}

        logger.info("向K线服务注册新币种", symbol=symbol, intervals=intervals)

        try:
            async with self.session.post(url, json=body) as response:
                data = await response.json()

                if response.status == 200 and data.get('code') == 0:
                    logger.info("K线服务注册成功", symbol=symbol, intervals=intervals)
                    return True
                else:
                    logger.warning(
                        "K线服务注册返回非预期状态",
                        symbol=symbol,
                        status=response.status,
                        data=data
                    )
                    return False

        except aiohttp.ClientError as e:
            logger.error("K线服务注册失败", symbol=symbol, error=str(e))
            return False
    
    async def unregister_symbol(self, symbol: str) -> bool:
        """
        向K线服务注销币种（停止采集K线数据）
        
        Args:
            symbol: 交易对符号（如 BTCUSDT）
        
        Returns:
            是否注销成功
        
        Raises:
            ValueError: 参数验证失败
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        symbol = symbol.strip().upper()
        
        await self._init_session()
        
        url = f"{self.service_url}/register"
        params = {"symbol": symbol}
        
        logger.info("向K线服务注销币种", symbol=symbol)
        
        try:
            async with self.session.delete(url, params=params) as response:
                if response.status == 200:
                    logger.info("K线服务注销成功", symbol=symbol)
                    return True
                elif response.status == 404:
                    logger.debug("K线服务注销：币种未注册，跳过", symbol=symbol)
                    return True
                else:
                    logger.warning(
                        "K线服务注销失败",
                        symbol=symbol,
                        status=response.status
                    )
                    return False
        
        except aiohttp.ClientError as e:
            logger.warning("K线服务注销异常", symbol=symbol, error=str(e))
            return False
    
    async def get_multi_timeframe_data(
        self,
        symbol: str,
        intervals: List[str] = ["1h", "4h", "1d"]
    ) -> Dict[str, List[Dict]]:
        """
        获取多时间框架数据
        
        Args:
            symbol: 交易对
            intervals: 时间框架列表
        
        Returns:
            {interval: klines}
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        if not intervals:
            raise ValueError("时间框架列表不能为空")
        
        if not isinstance(intervals, list):
            raise ValueError(f"时间框架必须是列表，实际为 {type(intervals).__name__}")
        
        valid_intervals = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
        for interval in intervals:
            if interval not in valid_intervals:
                raise ValueError(f"无效的时间框架: {interval}, 有效周期: {', '.join(valid_intervals)}")
        
        tasks = [
            self.get_klines(symbol, interval, self._get_limit(interval))
            for interval in intervals
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        data = {}
        for interval, result in zip(intervals, results):
            if isinstance(result, Exception):
                logger.error(
                    "获取多时间框架数据失败",
                    symbol=symbol,
                    interval=interval,
                    error=str(result)
                )
            else:
                data[interval] = result
        
        return data
    
    def _get_limit(self, interval: str) -> int:
        """根据周期获取K线数量"""
        limits = {
            '1d': 100,
            '4h': 100,
            '1h': 100,
            '15m': 100,
            '5m': 100,
            '1m': 100
        }
        return limits.get(interval, 100)
