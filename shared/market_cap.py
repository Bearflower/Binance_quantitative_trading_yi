"""
CoinGecko 市值服务
提供加密货币市值查询，支持降级兜底策略
V2: 新增批量接口 /coins/markets，将 N 次请求压缩为 1-2 次；
    429 全局冷却期 + asyncio.Lock 防止并发竞态
"""
import asyncio
import time
from typing import Dict, List, Optional
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
    - 支持批量查询：/coins/markets 一次获取最多 250 个币种市值
    - 支持降级兜底：CoinGecko 不可用时使用 OI/24h成交额 替代
    - 支持市值缓存、网络异常重试、币种列表加载失败冷却
    - 429 全局冷却期：触发限流后 N 秒内直接返回 None
    """

    # /coins/markets 单次请求最多返回的币种数量（CoinGecko 上限）
    _BATCH_PAGE_SIZE = 250

    def __init__(
        self,
        timeout: int = 10,
        use_pro_api: bool = False,
        api_key: Optional[str] = None,
        retry_count: int = 2,
        retry_interval: float = 1.0,
        cache_ttl_seconds: int = 3600,
        coin_list_cool_down_seconds: float = 60.0,
        request_interval: float = 2.0,
        rate_limit_cooldown_seconds: float = 60.0
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
                              用 asyncio.Lock 保证原子性
            rate_limit_cooldown_seconds: 触发 HTTP 429 后的全局冷却期（秒），
                                         冷却期内所有请求直接返回 None，不再打 API
        """
        self.timeout = timeout
        self.use_pro_api = use_pro_api
        self.api_key = api_key
        self.retry_count = retry_count
        self.retry_interval = retry_interval
        self.cache_ttl_seconds = cache_ttl_seconds
        self.coin_list_cool_down_seconds = coin_list_cool_down_seconds
        self.request_interval = request_interval
        self.rate_limit_cooldown_seconds = rate_limit_cooldown_seconds

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
        # 429 全局冷却期结束时间戳（冷却期内直接返回 None）
        self._rate_limit_cooldown_until: float = 0.0
        # 请求间隔锁，保证 _enforce_request_interval 的原子性，防止并发竞态
        self._request_lock = asyncio.Lock()

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

    def _is_rate_limited(self) -> bool:
        """是否处于 429 全局冷却期"""
        if self._rate_limit_cooldown_until <= 0:
            return False
        return time.time() < self._rate_limit_cooldown_until

    def _record_rate_limit(self) -> None:
        """记录 429 限流，进入全局冷却期"""
        self._rate_limit_cooldown_until = time.time() + self.rate_limit_cooldown_seconds
        logger.warning(
            "CoinGecko 进入全局冷却期",
            cooldown_seconds=self.rate_limit_cooldown_seconds
        )

    async def _enforce_request_interval(self) -> None:
        """
        确保连续两次 CoinGecko API 请求之间至少间隔 request_interval 秒

        使用 asyncio.Lock 保证原子性，防止并发协程绕过间隔检查一起发出请求。
        """
        async with self._request_lock:
            if self._last_request_time > 0:
                elapsed = time.time() - self._last_request_time
                if elapsed < self.request_interval:
                    await asyncio.sleep(self.request_interval - elapsed)
            self._last_request_time = time.time()

    async def _ensure_coin_list(self) -> None:
        """
        确保币种 ID 列表已加载（带失败冷却）

        首次调用时从 CoinGecko 获取币种列表并缓存；
        加载失败后进入冷却期，冷却期内跳过加载。
        """
        if self._cache_loaded:
            return

        # 429 全局冷却期内跳过
        if self._is_rate_limited():
            logger.debug("CoinGecko 429 冷却期内，跳过币种列表加载")
            return

        # 币种列表加载失败冷却
        if time.time() < self._coin_list_retry_after:
            logger.debug("CoinGecko 币种列表处于冷却期，跳过加载")
            return

        await self._init_session()
        await self._enforce_request_interval()

        url = f"{self._base_url}/coins/list"
        params = {}
        if self.use_pro_api and self.api_key:
            params["x_cg_pro_api_key"] = self.api_key

        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status == 429:
                    self._record_rate_limit()
                    self._record_coin_list_failure()
                    return
                if resp.status != 200:
                    logger.warning("获取CoinGecko币种列表失败", status=resp.status)
                    self._record_coin_list_failure()
                    return
                data = await resp.json()
                for coin in data:
                    symbol = coin.get("symbol", "").upper()
                    if symbol and symbol not in self._coin_id_cache:
                        self._coin_id_cache[symbol] = coin["id"]
                self._cache_loaded = True
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
        base = symbol.replace("USDT", "").upper()
        return self._coin_id_cache.get(base)

    def _symbol_to_coin_id_map(self, symbols: List[str]) -> Dict[str, str]:
        """
        批量转换 symbol -> coin_id 映射

        Args:
            symbols: 交易对列表（如 ["BTCUSDT", "ETHUSDT"]）

        Returns:
            {symbol: coin_id} 字典，未找到的 symbol 不在返回值中
        """
        result: Dict[str, str] = {}
        for s in symbols:
            cid = self._get_coin_id(s)
            if cid:
                result[s] = cid
        return result

    def _get_cached_market_cap(self, symbol: str) -> Optional[float]:
        """从缓存读取市值，未命中或已过期返回 None"""
        cached = self._market_cap_cache.get(symbol)
        if cached is None:
            return None
        market_cap, expire_at = cached
        if time.time() >= expire_at:
            del self._market_cap_cache[symbol]
            return None
        return market_cap

    def _set_cached_market_cap(self, symbol: str, market_cap: float) -> None:
        """写入市值缓存"""
        expire_at = time.time() + self.cache_ttl_seconds
        self._market_cap_cache[symbol] = (market_cap, expire_at)

    def _set_cached_market_caps(self, caps: Dict[str, float]) -> None:
        """批量写入市值缓存"""
        expire_at = time.time() + self.cache_ttl_seconds
        for symbol, cap in caps.items():
            self._market_cap_cache[symbol] = (cap, expire_at)

    def _get_uncached_symbols(self, symbols: List[str]) -> List[str]:
        """从 symbols 中筛选出缓存未命中的部分"""
        return [s for s in symbols if self._get_cached_market_cap(s) is None]

    async def get_batch_market_caps(self, symbols: List[str]) -> Dict[str, float]:
        """
        批量获取币种市值（推荐方式，大幅减少 API 调用）

        使用 CoinGecko /coins/markets 接口，一次请求最多 250 个币种，
        将候选池扫描的 N 次单独请求压缩为 1-2 次批量请求。

        流程：
        1. 先过滤掉缓存已命中的 symbol
        2. 429 冷却期内直接跳过，返回空字典
        3. 未找到 coin_id 的 symbol 跳过
        4. 分批调用 /coins/markets 接口
        5. 结果写入缓存

        Args:
            symbols: 交易对列表（如 ["BTCUSDT", "ETHUSDT", ...]）

        Returns:
            {symbol: 市值} 字典，获取失败的 symbol 不在返回值中
        """
        # 1. 缓存过滤
        uncached = self._get_uncached_symbols(symbols)
        if not uncached:
            logger.debug("批量市值全部命中缓存", total=len(symbols))
            return {}  # 全部命中缓存，无新数据

        # 2. 429 冷却期检查
        if self._is_rate_limited():
            logger.debug("CoinGecko 429 冷却期内，跳过批量请求",
                         cooldown_left=round(
                             self._rate_limit_cooldown_until - time.time(), 1))
            return {}

        # 3. 确保币种列表已加载
        await self._ensure_coin_list()

        # 4. symbol -> coin_id 映射
        symbol_to_id = self._symbol_to_coin_id_map(uncached)
        if not symbol_to_id:
            logger.debug("批量请求中无有效 coin_id", total=len(uncached))
            return {}

        logger.info("开始批量获取市值",
                    request_symbols=len(symbols),
                    uncached_symbols=len(uncached),
                    valid_coin_ids=len(symbol_to_id))

        # 5. 分批请求（每批最多 _BATCH_PAGE_SIZE 个 coin_id）
        # 先构建反向映射 coin_id -> symbol，用于把批量结果的 coin_id 转回 symbol
        id_to_symbol: Dict[str, str] = {cid: sym for sym, cid in symbol_to_id.items()}

        # symbol -> market_cap，最终要返回和缓存的格式
        batch_results: Dict[str, float] = {}
        coin_ids = list(symbol_to_id.values())

        await self._init_session()

        for page_start in range(0, len(coin_ids), self._BATCH_PAGE_SIZE):
            if self._is_rate_limited():
                logger.warning("CoinGecko 429 冷却期，停止后续批量请求")
                break

            page_ids = coin_ids[page_start:page_start + self._BATCH_PAGE_SIZE]
            # page_result: {coin_id: market_cap}
            page_result = await self._fetch_coins_markets(page_ids)

            if page_result is None:
                # 429 或请求失败，停止后续批次
                break

            # coin_id -> symbol -> market_cap
            for cid, cap in page_result.items():
                sym = id_to_symbol.get(cid)
                if sym:
                    batch_results[sym] = cap

        # 6. 写入缓存
        if batch_results:
            self._set_cached_market_caps(batch_results)
            logger.info("批量市值获取完成",
                        total_requested=len(uncached),
                        success=len(batch_results))
        else:
            logger.warning("批量市值获取无结果")

        return batch_results

    async def _fetch_coins_markets(
        self, coin_ids: List[str]
    ) -> Optional[Dict[str, float]]:
        """
        调用 CoinGecko /coins/markets 接口获取一批币种市值

        Args:
            coin_ids: CoinGecko coin_id 列表（最多 _BATCH_PAGE_SIZE 个）

        Returns:
            {coin_id: market_cap} 字典；429 返回 None；其他异常返回空字典
        """
        await self._enforce_request_interval()

        url = f"{self._base_url}/coins/markets"
        params = {
            "vs_currency": "usd",
            "ids": ",".join(coin_ids),
            "per_page": len(coin_ids),
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "",
        }
        if self.use_pro_api and self.api_key:
            params["x_cg_pro_api_key"] = self.api_key

        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status == 429:
                    logger.warning("CoinGecko /coins/markets 触发 429 限流")
                    self._record_rate_limit()
                    return None
                if resp.status != 200:
                    logger.warning("CoinGecko /coins/markets 请求失败",
                                   status=resp.status)
                    return {}
                data = await resp.json()
                return self._parse_coins_markets_response(data, coin_ids)
        except Exception as e:
            logger.warning("CoinGecko /coins/markets 请求异常",
                           error=str(e) or type(e).__name__)
            return {}

    def _parse_coins_markets_response(
        self, data: list, requested_ids: List[str]
    ) -> Dict[str, float]:
        """
        解析 /coins/markets 响应，提取 coin_id -> market_cap 映射

        Args:
            data: CoinGecko 返回的列表数据
            requested_ids: 本次请求的 coin_id 列表（用于调试）

        Returns:
            {coin_id: market_cap} 字典
        """
        result: Dict[str, float] = {}
        for item in data:
            coin_id = item.get("id")
            market_cap = item.get("market_cap")
            if coin_id and market_cap is not None:
                result[coin_id] = float(market_cap)
        if len(result) < len(requested_ids):
            missing = [cid for cid in requested_ids if cid not in result]
            logger.debug("批量市值部分币种缺失",
                         requested=len(requested_ids),
                         returned=len(result),
                         missing_count=len(missing))
        return result

    async def get_market_cap(self, symbol: str) -> Optional[float]:
        """
        获取币种市值（美元）

        优先从缓存读取；缓存未命中时走单币请求（不推荐，推荐用 get_batch_market_caps）。
        429 冷却期内直接返回 None。

        Args:
            symbol: 交易对（如 BTCUSDT）

        Returns:
            市值（美元），如果获取失败返回 None
        """
        # 缓存命中直接返回
        cached = self._get_cached_market_cap(symbol)
        if cached is not None:
            return cached

        # 429 冷却期内跳过 API 请求
        if self._is_rate_limited():
            logger.debug("CoinGecko 429 冷却期内，跳过单币市值请求", symbol=symbol)
            return None

        await self._ensure_coin_list()

        coin_id = self._get_coin_id(symbol)
        if not coin_id:
            logger.warning("未找到CoinGecko币种ID", symbol=symbol)
            return None

        market_cap = await self._request_market_cap(symbol, coin_id)
        if market_cap is not None:
            self._set_cached_market_cap(symbol, market_cap)
        return market_cap

    async def _fetch_market_cap_once(self, symbol: str, coin_id: str) -> Optional[float]:
        """
        执行单币市值请求（保留兼容性，新代码请用 get_batch_market_caps）

        HTTP 429 时设置全局冷却期并抛出 CoinGeckoError。
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

        await self._enforce_request_interval()

        async with self.session.get(url, params=params) as resp:
            if resp.status == 429:
                self._record_rate_limit()
                raise CoinGeckoError(f"CoinGecko 限流 HTTP 429: {symbol}")
            if resp.status != 200:
                logger.warning("获取市值失败", symbol=symbol, status=resp.status)
                return None
            return await self._parse_market_cap_response(symbol, coin_id, resp)

    async def _request_market_cap(self, symbol: str, coin_id: str) -> Optional[float]:
        """
        单币请求带重试（保留兼容性）

        对网络/超时异常重试；429 不重试并设置全局冷却期。
        """
        await self._init_session()

        for attempt in range(self.retry_count + 1):
            if self._is_rate_limited():
                return None
            try:
                return await self._fetch_market_cap_once(symbol, coin_id)
            except CoinGeckoError:
                logger.warning("CoinGecko API 频率限制", symbol=symbol)
                return None
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
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
        """从单币响应解析市值"""
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
        2. CoinGecko 不可用时，使用 OI × 20 粗略估算

        Args:
            symbol: 交易对
            oi_usd: 持仓量（美元）
            volume_24h_usd: 24小时成交额（美元，当前未使用）

        Returns:
            市值（美元），降级兜底时也返回正值
        """
        cap = await self.get_market_cap(symbol)
        if cap is not None:
            return cap

        fallback_cap = oi_usd * 20.0 if oi_usd > 0 else 0.0
        logger.info(
            "市值降级兜底",
            symbol=symbol,
            fallback_cap=fallback_cap,
            oi_usd=oi_usd
        )
        return fallback_cap
