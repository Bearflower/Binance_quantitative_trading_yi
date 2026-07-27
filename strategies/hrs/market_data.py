"""
市场数据模块
负责获取候选池币种的市场数据：价格、OI、成交量、资金费率等
"""
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta, timezone
import structlog

from shared.binance_api import BinanceClient
from shared.market_cap import MarketCapService
from shared.kline_service import KLineService


logger = structlog.get_logger()


class MarketDataProvider:
    """
    市场数据提供者

    功能：
    - 获取所有交易对的24h行情数据
    - 获取OI数据
    - 获取资金费率（历史快照）
    - 获取市值数据（CoinGecko + 降级兜底）
    - 获取K线数据
    """

    def __init__(
        self,
        binance_api: BinanceClient,
        kline_service: KLineService,
        config: Dict[str, Any]
    ):
        """
        初始化市场数据提供者

        Args:
            binance_api: 币安API客户端
            kline_service: K线服务客户端
            config: 配置字典
        """
        self.binance_api = binance_api
        self.kline_service = kline_service
        self.config = config
        self.market_cap_service = MarketCapService()

        # 从配置读取API限制
        kline_config = config.get("kline", {})
        self.max_api_limit = kline_config.get("max_api_limit", 100)

        # 资金费率缓存（避免重复请求）
        self._funding_rate_cache: Dict[str, Dict[int, float]] = {}
        # 从配置读取缓存TTL
        funding_config = config.get("funding_rate", {})
        self._funding_rate_cache_ttl = funding_config.get("cache_ttl_seconds", 28800)
        # 结算间隔（小时），默认8小时
        self._settlement_interval = funding_config.get("settlement_interval_hours", 8)

        # 4h K线合成间隔，默认4小时
        self._synthetic_4h_interval = kline_config.get("synthetic_4h_interval", 4)
        # EMA周期，默认20
        self._ema_period = kline_config.get("ema_period", 20)

        logger.info("市场数据提供者初始化完成")

    async def close(self) -> None:
        """关闭资源"""
        await self.market_cap_service.close()

    async def get_all_tickers(self) -> List[Dict[str, Any]]:
        """
        获取所有交易对的24h行情数据

        Returns:
            所有交易对的24h统计列表
        """
        try:
            tickers = await self.binance_api.get_all_tickers()
            logger.info("获取所有交易对行情", count=len(tickers))
            return tickers
        except Exception as e:
            logger.error("获取所有交易对行情失败", error=str(e))
            return []

    async def get_oi_usd(self, symbol: str) -> float:
        """
        获取持仓量（OI，美元价值）

        Args:
            symbol: 交易对

        Returns:
            OI美元价值
        """
        try:
            data = await self.binance_api._request(
                "GET",
                "/fapi/v1/openInterest",
                params={"symbol": symbol},
                signed=False
            )
            oi = float(data.get("openInterest", 0))
            # 获取标记价格以计算OI美元价值
            ticker = await self.binance_api._request(
                "GET",
                "/fapi/v1/premiumIndex",
                params={"symbol": symbol},
                signed=False
            )
            mark_price = float(ticker.get("markPrice", 0))
            oi_usd = oi * mark_price if mark_price > 0 else 0
            logger.debug("获取OI", symbol=symbol, oi=oi, oi_usd=oi_usd)
            return oi_usd
        except Exception as e:
            logger.warning("获取OI失败", symbol=symbol, error=str(e))
            return 0.0

    async def get_funding_rate(self, symbol: str, at_time: Optional[int] = None) -> float:
        """
        获取资金费率

        HRS策略要求使用历史费率快照，而非实时费率。

        Args:
            symbol: 交易对
            at_time: 目标时间戳（毫秒），如果为None则获取最新费率

        Returns:
            资金费率（小数形式）
        """
        try:
            if at_time is not None:
                # 获取指定时间点的历史费率
                # 使用缓存避免重复请求
                settlement_time = self._get_settlement_time(at_time)
                cache_key = (symbol, settlement_time)
                if cache_key in self._funding_rate_cache:
                    return self._funding_rate_cache[cache_key]

                history = await self.binance_api.get_funding_rate_history(
                    symbol=symbol,
                    start_time=settlement_time,
                    end_time=settlement_time + 1,
                    limit=1
                )
                if history and len(history) > 0:
                    rate = float(history[0].get("fundingRate", 0))
                    self._funding_rate_cache[cache_key] = rate
                    return rate
                return 0.0
            else:
                # 获取最新费率
                rate = await self.binance_api.get_funding_rate(symbol)
                return rate
        except Exception as e:
            logger.warning("获取资金费率失败", symbol=symbol, error=str(e))
            return 0.0

    def _get_settlement_time(self, timestamp_ms: int) -> int:
        """
        获取最近一次资金费率结算时间

        币安每8小时结算一次（UTC 00:00, 08:00, 16:00）

        Args:
            timestamp_ms: 目标时间戳（毫秒）

        Returns:
            最近一次结算时间戳（毫秒）
        """
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        hour = dt.hour
        # 找到最近的结算时间（结算间隔从配置读取）
        settlement_hour = (hour // self._settlement_interval) * self._settlement_interval
        settlement_dt = dt.replace(hour=settlement_hour, minute=0, second=0, microsecond=0)
        return int(settlement_dt.timestamp() * 1000)

    async def get_market_cap(self, symbol: str, oi_usd: float, volume_24h_usd: float) -> float:
        """
        获取市值（带降级兜底）

        Args:
            symbol: 交易对
            oi_usd: OI美元价值
            volume_24h_usd: 24h成交额

        Returns:
            市值（美元）
        """
        return await self.market_cap_service.get_market_cap_with_fallback(
            symbol=symbol,
            oi_usd=oi_usd,
            volume_24h_usd=volume_24h_usd
        )

    def _get_4h_slot(self, timestamp_ms: int) -> int:
        """
        获取UTC时间边界对应的4h槽位起始时间戳

        按UTC小时边界分组：0-3, 4-7, 8-11, 12-15, 16-19, 20-23。

        Args:
            timestamp_ms: K线开盘时间戳（毫秒）

        Returns:
            4h槽位起始时间戳（毫秒）
        """
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        slot_hour = (dt.hour // self._synthetic_4h_interval) * self._synthetic_4h_interval
        slot_dt = dt.replace(hour=slot_hour, minute=0, second=0, microsecond=0)
        return int(slot_dt.timestamp() * 1000)

    async def get_ema20_4h(self, symbol: str, klines_1h: List[Dict]) -> float:
        """
        从1h K线合成4h EMA20

        不单独注册4h K线，使用1h K线合成。
        优先使用 get_ema20_from_4h_cache() 方法避免重复合成。

        Args:
            symbol: 交易对
            klines_1h: 1h K线数据列表

        Returns:
            EMA20(4h) 值，如果数据不足返回0
        """
        try:
            # 将1h K线按UTC时间边界合成为4h K线
            from collections import defaultdict
            klines_4h = []
            groups = defaultdict(list)
            for k in klines_1h:
                ot = k.get("open_time", 0)
                if isinstance(ot, (int, float)):
                    slot = self._get_4h_slot(int(ot))
                    groups[slot].append(k)

            for slot in sorted(groups.keys()):
                group = groups[slot]
                open_price = float(group[0].get("open", 0))
                close_price = float(group[-1].get("close", 0))
                high_price = max(float(k.get("high", 0)) for k in group)
                low_price = min(float(k.get("low", 0)) for k in group)
                volume = sum(float(k.get("volume", 0)) for k in group)
                klines_4h.append({
                    "open_time": slot,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": volume,
                })

            ema_config = self.config.get("kline", {})
            synthetic_count = ema_config.get("synthetic_4h_count", 50)
            klines_4h = klines_4h[-synthetic_count:]

            return self._calculate_ema(klines_4h)
        except Exception as e:
            logger.warning("4h EMA20合成失败", symbol=symbol, error=str(e))
            return 0.0

    def get_ema20_from_4h_cache(self, symbol: str, klines_4h: List[Dict]) -> float:
        """
        P0-5: 从已合成的4h K线缓存直接计算EMA20

        避免每次调用 get_ema20_4h() 时重复合成4h K线，
        直接使用 strategy 中维护的统一 _klines_4h_cache。

        Args:
            symbol: 交易对
            klines_4h: 已合成的4h K线列表

        Returns:
            EMA20(4h) 值，如果数据不足返回0
        """
        if not klines_4h:
            return 0.0
        return self._calculate_ema(klines_4h)

    def _calculate_ema(self, klines_4h: List[Dict]) -> float:
        """
        从4h K线列表计算EMA

        Args:
            klines_4h: 4h K线列表

        Returns:
            EMA值
        """
        if len(klines_4h) < self._ema_period:
            return 0.0

        close_prices = [k["close"] for k in klines_4h]
        ema = close_prices[0]
        multiplier = 2.0 / (self._ema_period + 1)
        for price in close_prices[1:]:
            ema = (price - ema) * multiplier + ema

        return ema

    async def synthesize_4h_klines(self, klines_1h: List[Dict]) -> List[Dict]:
        """
        从1h K线合成4h K线列表

        按UTC时间边界（0-3, 4-7, 8-11, 12-15, 16-19, 20-23）分组，
        返回完整的OHLCV数据。

        Args:
            klines_1h: 1h K线数据列表

        Returns:
            4h K线数据列表，每个元素包含 open_time, open, high, low, close, volume
        """
        from collections import defaultdict

        groups = defaultdict(list)
        for k in klines_1h:
            ot = k.get("open_time", 0)
            if isinstance(ot, (int, float)):
                slot = self._get_4h_slot(int(ot))
                groups[slot].append(k)

        klines_4h = []
        for slot in sorted(groups.keys()):
            group = groups[slot]
            open_price = float(group[0].get("open", 0))
            close_price = float(group[-1].get("close", 0))
            high_price = max(float(k.get("high", 0)) for k in group)
            low_price = min(float(k.get("low", 0)) for k in group)
            volume = sum(float(k.get("volume", 0)) for k in group)
            klines_4h.append({
                "open_time": slot,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "volume": volume,
            })

        return klines_4h

    async def get_klines_1h(
        self,
        symbol: str,
        limit: int = 100,
        from_binance: bool = False
    ) -> List[Dict]:
        """
        获取1h K线数据

        优先从K线服务获取，失败时回退到币安API。

        Args:
            symbol: 交易对
            limit: K线数量
            from_binance: 是否直接从币安API获取（不经过K线服务）

        Returns:
            K线数据列表
        """
        try:
            if from_binance:
                klines = await self.binance_api.get_klines(
                    symbol=symbol,
                    interval="1h",
                    limit=min(limit, self.max_api_limit)
                )
                return klines
            else:
                # 优先从K线服务获取
                try:
                    klines = await self.kline_service.get_klines(
                        symbol=symbol,
                        interval="1h",
                        limit=limit,
                    )
                    if klines:
                        return klines
                    logger.warning("K线服务返回空数据，回退到币安API", symbol=symbol)
                except Exception as e:
                    logger.warning("K线服务获取失败，回退到币安API", symbol=symbol, error=str(e))

                return await self.binance_api.get_klines(
                    symbol=symbol,
                    interval="1h",
                    limit=min(limit, self.max_api_limit)
                )
        except Exception as e:
            logger.error("获取K线失败", symbol=symbol, error=str(e))
            return []

    async def get_24h_volume(self, symbol: str, ticker_data: Optional[Dict] = None) -> float:
        """
        获取24h成交额

        Args:
            symbol: 交易对
            ticker_data: 已有的24h行情数据（可选，避免重复请求）

        Returns:
            24h成交额（USDT）
        """
        try:
            if ticker_data and ticker_data.get("symbol") == symbol:
                return float(ticker_data.get("quoteVolume", 0))
            ticker = await self.binance_api.get_ticker(symbol)
            return float(ticker.get("quoteVolume", 0))
        except Exception as e:
            logger.warning("获取24h成交额失败", symbol=symbol, error=str(e))
            return 0.0

    async def get_24h_price_change(self, symbol: str, ticker_data: Optional[Dict] = None) -> float:
        """
        获取24h涨跌幅

        Args:
            symbol: 交易对
            ticker_data: 已有的24h行情数据（可选）

        Returns:
            24h涨跌幅（小数，如0.12表示12%）
        """
        try:
            if ticker_data and ticker_data.get("symbol") == symbol:
                return float(ticker_data.get("priceChangePercent", 0)) / 100.0
            ticker = await self.binance_api.get_ticker(symbol)
            return float(ticker.get("priceChangePercent", 0)) / 100.0
        except Exception as e:
            logger.warning("获取24h涨跌幅失败", symbol=symbol, error=str(e))
            return 0.0