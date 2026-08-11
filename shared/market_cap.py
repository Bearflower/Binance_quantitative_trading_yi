"""
CoinGecko 市值服务
提供加密货币市值查询，支持降级兜底策略
"""
import asyncio
from typing import Dict, Optional
import aiohttp
import structlog


logger = structlog.get_logger()


class CoinGeckoError(Exception):
    """CoinGecko API 异常"""
    pass


class MarketCapService:
    """
    CoinGecko 市值查询服务

    功能：
    - 通过 CoinGecko 免费 API 查询币种市值
    - 支持降级兜底：CoinGecko 不可用时使用 OI/24h成交额 替代
    """

    # CoinGecko 币种 ID 缓存（symbol -> coingecko_id）
    _coin_id_cache: Dict[str, str] = {}
    _cache_loaded: bool = False

    def __init__(
        self,
        timeout: int = 10,
        use_pro_api: bool = False,
        api_key: Optional[str] = None
    ):
        """
        初始化市值服务

        Args:
            timeout: 请求超时时间（秒）
            use_pro_api: 是否使用 CoinGecko Pro API
            api_key: CoinGecko Pro API 密钥（仅 pro 模式需要）
        """
        self.timeout = timeout
        self.use_pro_api = use_pro_api
        self.api_key = api_key

        if use_pro_api:
            self._base_url = "https://pro-api.coingecko.com/api/v3"
        else:
            self._base_url = "https://api.coingecko.com/api/v3"

        self.session: Optional[aiohttp.ClientSession] = None

        logger.info(
            "市值服务初始化",
            use_pro_api=use_pro_api,
            base_url=self._base_url
        )

    async def _init_session(self) -> None:
        """初始化 HTTP 会话"""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        """关闭 HTTP 会话"""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None

    async def _ensure_coin_list(self) -> None:
        """
        确保币种 ID 列表已加载

        首次调用时从 CoinGecko 获取币种列表并缓存
        """
        if self._cache_loaded:
            return

        await self._init_session()

        url = f"{self._base_url}/coins/list"
        params = {}
        if self.use_pro_api and self.api_key:
            params["x_cg_pro_api_key"] = self.api_key

        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("获取CoinGecko币种列表失败", status=resp.status)
                    return
                data = await resp.json()
                for coin in data:
                    symbol = coin.get("symbol", "").upper()
                    if symbol:
                        # 如果同一 symbol 有多个 ID，保留第一个
                        if symbol not in self._coin_id_cache:
                            self._coin_id_cache[symbol] = coin["id"]
                self._cache_loaded = True
                logger.info("CoinGecko币种列表已加载", count=len(self._coin_id_cache))
        except Exception as e:
            logger.warning("加载CoinGecko币种列表异常", error=str(e))

    def _get_coin_id(self, symbol: str) -> Optional[str]:
        """
        根据交易对 symbol 获取 CoinGecko coin_id

        例：BTCUSDT -> BTC -> bitcoin

        Args:
            symbol: 交易对（如 BTCUSDT）

        Returns:
            CoinGecko coin_id，如果未找到则返回 None
        """
        # 去除 USDT 后缀
        base = symbol.replace("USDT", "").upper()
        return self._coin_id_cache.get(base)

    async def get_market_cap(self, symbol: str) -> Optional[float]:
        """
        获取币种市值（美元）

        Args:
            symbol: 交易对（如 BTCUSDT）

        Returns:
            市值（美元），如果获取失败返回 None
        """
        await self._ensure_coin_list()

        coin_id = self._get_coin_id(symbol)
        if not coin_id:
            logger.warning("未找到CoinGecko币种ID", symbol=symbol)
            return None

        await self._init_session()

        url = f"{self._base_url}/coins/{coin_id}"
        params = {
            "localization": "false",
            "tickers": "false",
            "community_data": "false",
            "developer_data": "false",
        }
        if self.use_pro_api and self.api_key:
            params["x_cg_pro_api_key"] = self.api_key

        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status == 429:
                    logger.warning("CoinGecko API 频率限制")
                    return None
                if resp.status != 200:
                    logger.warning("获取市值失败", symbol=symbol, status=resp.status)
                    return None
                data = await resp.json()
                if data is None:
                    logger.warning("获取市值返回空数据", symbol=symbol, coin_id=coin_id)
                    return None
                market_cap = data.get("market_data", {}).get("market_cap", {}).get("usd")
                if market_cap is not None:
                    logger.info("市值获取成功", symbol=symbol, market_cap=market_cap)
                    return float(market_cap)
                return None
        except Exception as e:
            logger.warning("获取市值异常", symbol=symbol, error=str(e) or type(e).__name__)
            return None

    async def get_market_cap_with_fallback(
        self,
        symbol: str,
        oi_usd: float,
        volume_24h_usd: float
    ) -> float:
        """
        获取市值，带降级兜底策略

        降级策略：
        1. 优先使用 CoinGecko 获取市值
        2. CoinGecko 不可用时，使用 OI/24h成交额 估算（OI × 10 作为粗略市值估算）

        Args:
            symbol: 交易对
            oi_usd: 持仓量（美元）
            volume_24h_usd: 24小时成交额（美元）

        Returns:
            市值（美元），如果获取失败返回 0
        """
        cap = await self.get_market_cap(symbol)
        if cap is not None:
            return cap

        # 降级兜底：使用 OI 估算
        # 这里简单的做法是 OI × 10 作为粗略市值估算
        # 实际中 OI/市值比通常在 0.01~0.2 之间
        # 所以默认用 OI / 0.05 作为兜底估算（偏保守）
        fallback_cap = oi_usd * 20.0 if oi_usd > 0 else 0.0
        logger.info(
            "市值降级兜底",
            symbol=symbol,
            fallback_cap=fallback_cap,
            oi_usd=oi_usd
        )
        return fallback_cap