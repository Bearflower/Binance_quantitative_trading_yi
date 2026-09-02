"""
CoinGecko 市值服务
提供加密货币市值查询，支持降级兜底策略
"""
import asyncio
import time
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
    - 支持市值缓存、网络异常重试、币种列表加载失败冷却
    """

    def __init__(
        self,
        timeout: int = 10,
        use_pro_api: bool = False,
        api_key: Optional[str] = None,
        retry_count: int = 2,
        retry_interval: float = 1.0,
        cache_ttl_seconds: int = 3600,
        coin_list_cool_down_seconds: float = 60.0,
        request_interval: float = 2.0
    ):
        """
        初始化市值服务

        Args:
            timeout: 请求超时时间（秒）
            use_pro_api: 是否使用 CoinGecko Pro API
            api_key: CoinGecko Pro API 密钥（仅 pro 模式需要）
            retry_count: 网络/超时异常的重试次数
            retry_interval: 每次重试前的休眠间隔（秒）
            cache_ttl_seconds: 市值缓存存活时间（秒）
            coin_list_cool_down_seconds: 币种列表加载失败后的冷却时长（秒）
            request_interval: 连续两次 CoinGecko API 请求的最小间隔（秒），
                              用于避免触发免费 API 的频率限制（~10-30 次/分钟）
        """
        self.timeout = timeout
        self.use_pro_api = use_pro_api
        self.api_key = api_key
        self.retry_count = retry_count
        self.retry_interval = retry_interval
        self.cache_ttl_seconds = cache_ttl_seconds
        self.coin_list_cool_down_seconds = coin_list_cool_down_seconds
        self.request_interval = request_interval

        # CoinGecko 币种 ID 缓存（symbol -> coingecko_id），实例级避免多实例状态共享
        self._coin_id_cache: Dict[str, str] = {}
        self._cache_loaded: bool = False

        if use_pro_api:
            self._base_url = "https://pro-api.coingecko.com/api/v3"
        else:
            self._base_url = "https://api.coingecko.com/api/v3"

        self.session: Optional[aiohttp.ClientSession] = None
        # 市值缓存：symbol -> (市值, 过期时间戳)，减少 CoinGecko API 调用
        self._market_cap_cache: Dict[str, tuple] = {}
        # 币种列表加载失败后的下次可重试时间戳（冷却机制，0 表示无冷却）
        self._coin_list_retry_after: float = 0.0
        # 上次 CoinGecko API 请求的时间戳（用于请求间隔控制）
        self._last_request_time: float = 0.0

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

    async def _enforce_request_interval(self) -> None:
        """
        确保连续两次 CoinGecko API 请求之间至少间隔 request_interval 秒

        CoinGecko 免费 API 的频率限制约为 10-30 次/分钟。
        候选池扫描时数十个币种串行请求，容易触发 429 限流。
        此方法在每次实际发出 HTTP 请求前调用，保证请求间隔。
        """
        if self._last_request_time > 0:
            elapsed = time.time() - self._last_request_time
            if elapsed < self.request_interval:
                await asyncio.sleep(self.request_interval - elapsed)
        self._last_request_time = time.time()

    async def _ensure_coin_list(self) -> None:
        """
        确保币种 ID 列表已加载（带失败冷却）

        首次调用时从 CoinGecko 获取币种列表并缓存；
        加载失败后进入冷却期，冷却期内跳过加载，避免每次全量重拉 /coins/list 大响应。
        """
        if self._cache_loaded:
            return

        # 冷却期内直接跳过加载，避免频繁重拉 /coins/list 大响应
        if time.time() < self._coin_list_retry_after:
            logger.debug("CoinGecko币种列表处于冷却期，跳过加载")
            return

        await self._init_session()

        # 确保请求间隔，避免触发 CoinGecko 频率限制
        await self._enforce_request_interval()

        url = f"{self._base_url}/coins/list"
        params = {}
        if self.use_pro_api and self.api_key:
            params["x_cg_pro_api_key"] = self.api_key

        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("获取CoinGecko币种列表失败", status=resp.status)
                    self._record_coin_list_failure()
                    return
                data = await resp.json()
                for coin in data:
                    symbol = coin.get("symbol", "").upper()
                    if symbol:
                        # 如果同一 symbol 有多个 ID，保留第一个
                        if symbol not in self._coin_id_cache:
                            self._coin_id_cache[symbol] = coin["id"]
                self._cache_loaded = True
                # 加载成功，重置冷却时间戳
                self._coin_list_retry_after = 0.0
                logger.info("CoinGecko币种列表已加载", count=len(self._coin_id_cache))
        except Exception as e:
            logger.warning("加载CoinGecko币种列表异常", error=str(e))
            self._record_coin_list_failure()

    def _record_coin_list_failure(self) -> None:
        """记录币种列表加载失败，进入冷却期"""
        self._coin_list_retry_after = time.time() + self.coin_list_cool_down_seconds

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

    def _get_cached_market_cap(self, symbol: str) -> Optional[float]:
        """
        从缓存读取市值

        Args:
            symbol: 交易对

        Returns:
            市值（美元），未命中或已过期返回 None
        """
        cached = self._market_cap_cache.get(symbol)
        if cached is None:
            return None
        market_cap, expire_at = cached
        # 缓存已过期则删除并视为未命中
        if time.time() >= expire_at:
            del self._market_cap_cache[symbol]
            return None
        logger.debug("市值命中缓存", symbol=symbol, market_cap=market_cap)
        return market_cap

    def _set_cached_market_cap(self, symbol: str, market_cap: float) -> None:
        """
        写入市值缓存

        Args:
            symbol: 交易对
            market_cap: 市值（美元）
        """
        expire_at = time.time() + self.cache_ttl_seconds
        self._market_cap_cache[symbol] = (market_cap, expire_at)

    async def get_market_cap(self, symbol: str) -> Optional[float]:
        """
        获取币种市值（美元）

        优先从缓存读取，未命中时请求 CoinGecko，带重试与失败降级。

        Args:
            symbol: 交易对（如 BTCUSDT）

        Returns:
            市值（美元），如果获取失败返回 None
        """
        # 缓存命中且未过期时直接返回，减少 CoinGecko API 调用
        cached = self._get_cached_market_cap(symbol)
        if cached is not None:
            return cached

        await self._ensure_coin_list()

        coin_id = self._get_coin_id(symbol)
        if not coin_id:
            logger.warning("未找到CoinGecko币种ID", symbol=symbol)
            return None

        # 请求 CoinGecko 获取市值（内部带重试）
        market_cap = await self._request_market_cap(symbol, coin_id)
        if market_cap is not None:
            self._set_cached_market_cap(symbol, market_cap)
        return market_cap

    async def _fetch_market_cap_once(self, symbol: str, coin_id: str) -> Optional[float]:
        """
        执行单次市值请求并解析响应

        HTTP 429 时抛出 CoinGeckoError，由上层决定不重试；非200/解析失败返回 None。

        Args:
            symbol: 交易对
            coin_id: CoinGecko 币种 ID

        Returns:
            市值（美元）；非200状态码或响应解析失败返回 None

        Raises:
            CoinGeckoError: CoinGecko API 触发频率限制（HTTP 429），不应重试
            asyncio.TimeoutError / aiohttp.ClientError: 网络/超时类异常，由上层重试
        """
        url = f"{self._base_url}/coins/{coin_id}"
        params = {
            "localization": "false",
            "tickers": "false",
            "community_data": "false",
            "developer_data": "false",
        }
        if self.use_pro_api and self.api_key:
            params["x_cg_pro_api_key"] = self.api_key

        # 确保请求间隔，避免触发 CoinGecko 频率限制
        await self._enforce_request_interval()

        async with self.session.get(url, params=params) as resp:
            if resp.status == 429:
                # 触发限流，抛出异常由上层决定不重试，避免加剧限流
                raise CoinGeckoError(f"CoinGecko 限流 HTTP 429: {symbol}")
            if resp.status != 200:
                logger.warning("获取市值失败", symbol=symbol, status=resp.status)
                return None
            return await self._parse_market_cap_response(symbol, coin_id, resp)

    async def _request_market_cap(self, symbol: str, coin_id: str) -> Optional[float]:
        """
        请求 CoinGecko 市值接口，带重试逻辑

        对 asyncio.TimeoutError / aiohttp.ClientError 类异常重试 retry_count 次，
        每次重试前休眠 retry_interval 秒；HTTP 429 不重试，直接返回避免加剧限流。

        Args:
            symbol: 交易对
            coin_id: CoinGecko 币种 ID

        Returns:
            市值（美元），获取失败返回 None
        """
        await self._init_session()

        for attempt in range(self.retry_count + 1):
            try:
                return await self._fetch_market_cap_once(symbol, coin_id)
            except CoinGeckoError:
                # HTTP 429 不重试，直接返回避免加剧限流
                logger.warning("CoinGecko API 频率限制", symbol=symbol)
                return None
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                # 网络/超时类异常：未用尽重试次数则重试
                if attempt < self.retry_count:
                    logger.warning("获取市值网络异常，准备重试", symbol=symbol,
                                   attempt=attempt + 1,
                                   error=str(e) or type(e).__name__)
                    await asyncio.sleep(self.retry_interval)
                else:
                    logger.warning("获取市值重试次数用尽", symbol=symbol,
                                   error=str(e) or type(e).__name__)
            except Exception as e:
                logger.warning("获取市值异常", symbol=symbol,
                               error=str(e) or type(e).__name__)
                return None
        return None

    async def _parse_market_cap_response(
        self,
        symbol: str,
        coin_id: str,
        resp: aiohttp.ClientResponse
    ) -> Optional[float]:
        """
        从 CoinGecko 响应解析市值

        Args:
            symbol: 交易对
            coin_id: CoinGecko 币种 ID
            resp: HTTP 响应

        Returns:
            市值（美元），解析失败返回 None
        """
        data = await resp.json()
        if data is None:
            logger.warning("获取市值返回空数据", symbol=symbol, coin_id=coin_id)
            return None
        market_data = data.get("market_data")
        if market_data is None:
            logger.warning("获取市值返回空 market_data", symbol=symbol, coin_id=coin_id)
            return None
        market_cap = (market_data.get("market_cap") or {}).get("usd")
        if market_cap is not None:
            logger.info("市值获取成功", symbol=symbol, market_cap=market_cap)
            return float(market_cap)
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