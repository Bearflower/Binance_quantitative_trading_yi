"""
候选池模块
管理 HRS 策略的币种候选池，负责每日动态筛选和 K 线预热
V2.5：OR-2 逻辑 + 空池退避 + LV-RM 独立扫描范围
"""
import asyncio
from typing import Dict, List, Set, Optional, Any, Tuple
from datetime import datetime, timezone
import structlog

from .market_data import MarketDataProvider


logger = structlog.get_logger()


class DynamicThresholds:
    """
    V2.3 动态阈值数据类

    存储每日扫描时从全市场计算的分位数阈值，
    供候选池筛选和 EMM 评分使用。
    """
    __slots__ = (
        "funding_rate_short", "funding_rate_long",
        "oi_market_cap_short", "oi_market_cap_long",
        "ema20_short", "ema20_long",
        "funding_rate_emm_long", "funding_rate_emm_short",
        "oi_market_cap_emm",
        "sample_count", "computed_at",
    )

    def __init__(self) -> None:
        self.funding_rate_short: float = 0.0
        self.funding_rate_long: float = 0.0
        self.oi_market_cap_short: float = 0.0
        self.oi_market_cap_long: float = 0.0
        self.ema20_short: float = 0.0
        self.ema20_long: float = 0.0
        self.funding_rate_emm_long: float = 0.0
        self.funding_rate_emm_short: float = 0.0
        self.oi_market_cap_emm: float = 0.0
        self.sample_count: int = 0
        self.computed_at: Optional[datetime] = None

    def is_valid(self) -> bool:
        """检查动态阈值是否有效（已计算且有足够样本）"""
        return self.sample_count > 0 and self.computed_at is not None

    def to_dict(self) -> Dict[str, Any]:
        """转为字典（用于日志和持久化）"""
        return {
            "funding_rate_short": self.funding_rate_short,
            "funding_rate_long": self.funding_rate_long,
            "oi_market_cap_short": self.oi_market_cap_short,
            "oi_market_cap_long": self.oi_market_cap_long,
            "ema20_short": self.ema20_short,
            "ema20_long": self.ema20_long,
            "funding_rate_emm_long": self.funding_rate_emm_long,
            "funding_rate_emm_short": self.funding_rate_emm_short,
            "oi_market_cap_emm": self.oi_market_cap_emm,
            "sample_count": self.sample_count,
        }


class CandidatePool:
    """
    候选池管理器

    V2.5 变更：
    - OR-2 逻辑：满足 4 个条件中任意 2 个即可入池
    - 新增空池退避机制
    - 新增 get_lv_rm_scan_range() 独立扫描方法
    - _validate_* 重构为 _count_*，返回条件计数（0-4）

    功能：
    - 每日 8:05 扫描币种，更新候选池
    - 做空/做多双向筛选
    - 排除稳定币、杠杆代币
    - 排除 MCTPS 策略交易币种（BTC/ETH/BNB/SOL/XRP/TRX）
    - 排除新币做空策略当前开仓的币种
    - K线预热机制
    - 流动性门槛检查
    """

    def __init__(
        self,
        config: Dict[str, Any],
        market_data: MarketDataProvider,
        db: Optional[Any] = None,
    ):
        """
        初始化候选池

        Args:
            config: 配置字典
            market_data: 市场数据提供者
            db: 数据库管理器实例（可选，用于查询新币做空持仓）
        """
        self.config = config
        self.market_data = market_data
        self.db = db

        pool_config = config.get("candidate_pool", {})

        # V2.3 动态阈值配置
        dynamic_config = pool_config.get("dynamic_thresholds", {})
        self.dynamic_enabled = dynamic_config.get("enabled", False)
        self.dynamic_min_sample_size = dynamic_config.get("min_sample_size", 10)
        # V2.5: 统一使用 50 分位（中位数）作为回退默认值
        self.dynamic_rate_percentile_short = dynamic_config.get("funding_rate_percentile_short", 0.50)
        self.dynamic_rate_percentile_long = dynamic_config.get("funding_rate_percentile_long", 0.50)
        self.dynamic_oi_percentile_short = dynamic_config.get("oi_market_cap_percentile_short", 0.50)
        self.dynamic_oi_percentile_long = dynamic_config.get("oi_market_cap_percentile_long", 0.50)
        self.dynamic_ema_percentile_short = dynamic_config.get("ema20_deviation_percentile_short", 0.50)
        self.dynamic_ema_percentile_long = dynamic_config.get("ema20_deviation_percentile_long", 0.50)
        self.dynamic_rate_emm_long = dynamic_config.get("funding_rate_percentile_emm_long", 0.10)
        self.dynamic_rate_emm_short = dynamic_config.get("funding_rate_percentile_emm_short", 0.90)
        self.dynamic_oi_emm = dynamic_config.get("oi_market_cap_percentile_emm", 0.90)

        # 价格变化来源配置：'daily_kline' 使用日K线开盘价计算，'ticker' 使用24hr ticker的priceChangePercent
        # V2.5: 保持向后兼容，scan_and_update 中不再使用此配置（_count_* 直接使用 ticker）
        self.price_change_source = pool_config.get("price_change_source", "daily_kline")
        self.api_concurrency_limit = pool_config.get("api_concurrency_limit", 20)

        # V2.3 动态阈值（每次扫描时计算）
        self._dynamic_thresholds = DynamicThresholds()

        # 做空候选条件
        short_config = pool_config.get("short", {})
        self.short_price_change = short_config.get("price_change_24h", 0.08)
        self.short_funding_rate = short_config.get("funding_rate_annual", 0.80)
        self.short_ema20_deviation = short_config.get("ema20_deviation", 0.08)
        self.short_oi_market_cap_min = short_config.get("oi_market_cap_min", 0.10)

        # 做多候选条件
        long_config = pool_config.get("long", {})
        self.long_price_change = long_config.get("price_change_24h", -0.08)
        self.long_funding_rate = long_config.get("funding_rate_annual", -0.20)
        self.long_ema20_deviation = long_config.get("ema20_deviation", -0.06)
        self.long_oi_market_cap_max = long_config.get("oi_market_cap_max", 0.05)

        # 年化费率计算参数
        funding_config = config.get("funding_rate", {})
        self.settlements_per_day = funding_config.get("settlements_per_day", 3)
        self.days_per_year = funding_config.get("days_per_year", 365)

        # 流动性门槛
        liquidity = pool_config.get("liquidity", {})
        self.min_volume_24h = liquidity.get("min_volume_24h", 50000000)
        self.min_oi_usd = liquidity.get("min_oi_usd", 10000000)

        # 排除配置
        exclude = pool_config.get("exclude", {})
        self.exclude_stablecoins = set(exclude.get("stablecoins", []))
        self.exclude_leverage_keywords = exclude.get("leverage_tokens", [])
        self.exclude_symbols = set(exclude.get("symbols", []))

        # 新币策略冲突
        conflict_config = pool_config.get("new_coin_conflict", {})
        self.conflict_hours = conflict_config.get("hours", 72)

        # K线配置
        kline_config = config.get("kline", {})
        self.min_klines = kline_config.get("min_klines_for_analysis", 24)
        self.keep_count = kline_config.get("keep_count", 168)

        # V2.5: 空池退避配置
        backoff_config = pool_config.get("empty_backoff", {})
        self.backoff_max_consecutive_empty = backoff_config.get("max_consecutive_empty", 3)
        self.backoff_base_delay_hours = backoff_config.get("base_delay_hours", 2)
        self.backoff_max_delay_hours = backoff_config.get("max_delay_hours", 8)
        self.backoff_recover_after_nonempty = backoff_config.get("recover_after_nonempty", 1)

        # V2.5: OR-2 逻辑配置，从 "or_any_2" 解析出 2
        logic_mode = pool_config.get("logic", "or_any_2")
        if logic_mode.startswith("or_any_"):
            self._min_conditions = int(logic_mode.split("_")[-1])
        else:
            self._min_conditions = 2  # 默认回退

        # 候选池状态
        self.short_candidates: Set[str] = set()
        self.long_candidates: Set[str] = set()
        self._active_symbols: Set[str] = set()

        # V2.4: 落选币种缓存（通过流动性门槛但未满足候选池条件的币种，用于LV-RM扫描）
        self._eliminated_symbols: List[str] = []

        # V2.4: ticker 数据缓存（用于 LV-RM 低波动筛选）
        self._ticker_data: Dict[str, Dict[str, Any]] = {}

        # 新币策略冲突黑名单
        self._new_coin_conflict_blacklist: Dict[str, datetime] = {}

        # 新币做空策略当前开仓的币种（每日扫描时查询）
        self._new_coin_open_positions: Set[str] = set()

        # 上次扫描时间
        self._last_scan_time: Optional[datetime] = None

        # P0-5: 4h K线缓存引用（由 strategy 维护，避免重复合成）
        self._klines_4h_cache: Optional[Dict[str, List[Dict]]] = None

        # V2.5: 连续空池计数器
        self.consecutive_empty: int = 0

        logger.info(
            "候选池管理器初始化完成",
            short_conditions={
                "price_change": self.short_price_change,
                "funding_rate": self.short_funding_rate,
                "ema20_deviation": self.short_ema20_deviation,
                "oi_market_cap_min": self.short_oi_market_cap_min,
            },
            long_conditions={
                "price_change": self.long_price_change,
                "funding_rate": self.long_funding_rate,
                "ema20_deviation": self.long_ema20_deviation,
                "oi_market_cap_max": self.long_oi_market_cap_max,
            },
            empty_backoff={
                "max_consecutive_empty": self.backoff_max_consecutive_empty,
                "base_delay_hours": self.backoff_base_delay_hours,
                "max_delay_hours": self.backoff_max_delay_hours,
            },
        )

    def _should_exclude(self, symbol: str) -> bool:
        """
        检查是否应该排除该币种

        Args:
            symbol: 交易对

        Returns:
            是否应该排除
        """
        # 排除指定交易对（BTC/ETH）
        if symbol in self.exclude_symbols:
            return True

        # 提取基础资产
        base = symbol.replace("USDT", "").upper()

        # 排除稳定币
        if base in self.exclude_stablecoins:
            return True

        # 排除杠杆代币
        for keyword in self.exclude_leverage_keywords:
            if keyword in base:
                return True

        # 排除新币策略冲突
        if symbol in self._new_coin_conflict_blacklist:
            conflict_time = self._new_coin_conflict_blacklist[symbol]
            if (datetime.now(timezone.utc) - conflict_time).total_seconds() / 3600 < self.conflict_hours:
                return True
            # 过期后移除黑名单
            del self._new_coin_conflict_blacklist[symbol]

        # 排除新币做空策略当前开仓的币种
        if symbol in self._new_coin_open_positions:
            return True

        return False

    def _check_liquidity(self, ticker: Dict[str, Any]) -> bool:
        """
        检查流动性门槛

        Args:
            ticker: 24h行情数据

        Returns:
            是否满足流动性要求
        """
        volume = float(ticker.get("quoteVolume", 0))
        return volume >= self.min_volume_24h

    async def _load_new_coin_open_positions(self) -> None:
        """
        从数据库加载新币做空策略当前开仓的币种

        每日扫描前调用，查询 new_coin.short_positions 表中 status = 'open' 的记录，
        将这些币种加入排除列表，避免 HRS 对同一币种同时开仓造成冲突。

        由于每天仅执行一次，查询开销可忽略。
        """
        if self.db is None:
            logger.warning("数据库未提供，跳过加载新币做空持仓")
            return

        try:
            # 查询所有新币做空策略当前开仓的币种
            query = """
                SELECT DISTINCT symbol 
                FROM new_coin.short_positions 
                WHERE status = 'open'
            """
            rows = await self.db.fetch_all(query)

            # 查询成功后清空旧数据并填充新数据
            self._new_coin_open_positions.clear()
            for row in rows:
                symbol = row.get("symbol")
                if symbol:
                    self._new_coin_open_positions.add(symbol)

            if self._new_coin_open_positions:
                logger.info(
                    "加载新币做空开仓币种完成",
                    count=len(self._new_coin_open_positions),
                    symbols=list(self._new_coin_open_positions),
                )
            else:
                logger.debug("加载新币做空开仓币种完成，当前无开仓")

        except Exception as e:
            logger.warning("加载新币做空开仓币种失败", error=str(e))
            # 查询失败不中断扫描，继续使用缓存

    def _compute_percentile(self, values: List[float], percentile: float) -> float:
        """
        V2.3 计算分位数

        使用线性插值法计算，与 numpy.percentile 行为一致。

        Args:
            values: 已排序的数值列表
            percentile: 分位数（0.0 ~ 1.0）

        Returns:
            分位数值
        """
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]
        k = (len(values) - 1) * percentile
        f = int(k)
        c = k - f
        if f + 1 < len(values):
            return values[f] + c * (values[f + 1] - values[f])
        return values[f]

    async def _get_daily_klines(
        self, symbols: List[str], ticker_map: Dict[str, Dict[str, Any]]
    ) -> Dict[str, float]:
        """
        获取所有通过初筛币种的日K线涨跌幅

        使用 asyncio.gather 并行获取所有币种的日K线（1d, limit=1），
        计算 daily_change = (current_price - daily_open) / daily_open。
        使用 Semaphore(20) 控制并发，避免 API 过载。

        Args:
            symbols: 通过初筛的币种列表
            ticker_map: symbol -> ticker 映射

        Returns:
            {symbol: daily_change} 字典，获取失败的币种不包含在内
        """
        semaphore = asyncio.Semaphore(self.api_concurrency_limit)

        async def _get_single_kline(symbol: str) -> Optional[Tuple[str, float]]:
            """获取单个币种的日K线涨跌幅"""
            async with semaphore:
                try:
                    klines = await self.market_data.binance_api.get_klines(
                        symbol=symbol, interval="1d", limit=1
                    )
                    if not klines:
                        return None

                    daily_open = float(klines[-1].get("open", 0))
                    if daily_open <= 0:
                        return None

                    ticker = ticker_map.get(symbol, {})
                    current_price = float(ticker.get("lastPrice", 0))
                    if current_price <= 0:
                        return None

                    daily_change = (current_price - daily_open) / daily_open
                    return (symbol, daily_change)
                except Exception as e:
                    logger.debug("获取日K线失败", symbol=symbol, error=str(e))
                    return None

        # 并行获取所有币种的日K线
        tasks = [_get_single_kline(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks)

        # 过滤失败结果，构建返回字典
        daily_changes: Dict[str, float] = {}
        for result in results:
            if result is not None:
                symbol, change = result
                daily_changes[symbol] = change

        logger.info(
            "日K线涨跌幅获取完成",
            requested=len(symbols),
            success=len(daily_changes),
        )
        return daily_changes

    async def _compute_dynamic_thresholds(
        self, symbol_ticker_map: Dict[str, Dict[str, Any]]
    ) -> DynamicThresholds:
        """
        V2.3 计算全市场动态阈值

        遍历所有通过初筛的币种，收集资金费率、OI/市值比、EMA20偏离，
        按配置的分位数计算动态阈值。

        Args:
            symbol_ticker_map: 通过初筛的 symbol -> ticker 映射

        Returns:
            DynamicThresholds 实例
        """
        thresholds = DynamicThresholds()

        funding_rates: List[float] = []
        oi_market_caps: List[float] = []
        ema_deviations: List[float] = []

        for symbol, ticker in symbol_ticker_map.items():
            try:
                # 获取资金费率（年化百分比）
                funding_rate = await self.market_data.get_funding_rate(symbol)
                annual_rate = funding_rate * self.settlements_per_day * self.days_per_year * 100
                funding_rates.append(annual_rate)

                # 获取 OI/市值比
                oi_usd = await self.market_data.get_oi_usd(symbol)
                volume_24h = float(ticker.get("quoteVolume", 0))
                market_cap = await self.market_data.get_market_cap(symbol, oi_usd, volume_24h)
                if market_cap > 0 and oi_usd > 0:
                    oi_market_caps.append(oi_usd / market_cap)

                # 获取 EMA20 偏离
                try:
                    if self._klines_4h_cache and symbol in self._klines_4h_cache:
                        klines_4h = self._klines_4h_cache[symbol]
                        ema20_4h = self.market_data.get_ema20_from_4h_cache(symbol, klines_4h)
                    else:
                        klines_1h = await self.market_data.get_klines_1h(symbol, limit=self.keep_count)
                        if klines_1h:
                            ema20_4h = await self.market_data.get_ema20_4h(symbol, klines_1h)
                        else:
                            ema20_4h = 0.0
                    if ema20_4h > 0:
                        current_price = float(ticker.get("lastPrice", 0))
                        deviation = (current_price - ema20_4h) / ema20_4h
                        ema_deviations.append(deviation)
                except Exception:
                    pass
            except Exception as e:
                logger.debug("计算动态阈值时跳过币种", symbol=symbol, error=str(e))

        thresholds.sample_count = len(funding_rates)
        if thresholds.sample_count < self.dynamic_min_sample_size:
            logger.warning(
                "动态阈值样本不足",
                sample_count=thresholds.sample_count,
                min_required=self.dynamic_min_sample_size,
            )
            return thresholds  # is_valid() 返回 False，调用方回退固定阈值

        # 排序并计算分位数
        funding_rates.sort()
        oi_market_caps.sort()
        ema_deviations.sort()

        thresholds.funding_rate_short = self._compute_percentile(
            funding_rates, self.dynamic_rate_percentile_short
        )
        thresholds.funding_rate_long = self._compute_percentile(
            funding_rates, self.dynamic_rate_percentile_long
        )
        thresholds.oi_market_cap_short = self._compute_percentile(
            oi_market_caps, self.dynamic_oi_percentile_short
        )
        thresholds.oi_market_cap_long = self._compute_percentile(
            oi_market_caps, self.dynamic_oi_percentile_long
        )
        thresholds.ema20_short = self._compute_percentile(
            ema_deviations, self.dynamic_ema_percentile_short
        )
        thresholds.ema20_long = self._compute_percentile(
            ema_deviations, self.dynamic_ema_percentile_long
        )
        # EMM 分位数
        thresholds.funding_rate_emm_long = self._compute_percentile(
            funding_rates, self.dynamic_rate_emm_long
        )
        thresholds.funding_rate_emm_short = self._compute_percentile(
            funding_rates, self.dynamic_rate_emm_short
        )
        thresholds.oi_market_cap_emm = self._compute_percentile(
            oi_market_caps, self.dynamic_oi_emm
        )
        thresholds.computed_at = datetime.now(timezone.utc)

        logger.info(
            "动态阈值计算完成",
            sample_count=thresholds.sample_count,
            funding_rate_short=f"{thresholds.funding_rate_short:.2f}%",
            funding_rate_long=f"{thresholds.funding_rate_long:.2f}%",
            oi_market_cap_short=f"{thresholds.oi_market_cap_short:.4f}",
            oi_market_cap_long=f"{thresholds.oi_market_cap_long:.4f}",
            ema20_short=f"{thresholds.ema20_short:.4f}",
            ema20_long=f"{thresholds.ema20_long:.4f}",
        )

        return thresholds

    def get_dynamic_thresholds(self) -> Optional[DynamicThresholds]:
        """V2.3 获取当前动态阈值（供 scoring_engine 使用）"""
        if self._dynamic_thresholds.is_valid():
            return self._dynamic_thresholds
        return None

    def _check_empty_backoff(self) -> bool:
        """
        V2.5: 检查是否需要跳过扫描（空池退避）

        当连续空池次数达到阈值时，逐步指数延长扫描间隔。
        候选池非空后自动恢复。

        Returns:
            True: 应跳过本次扫描；False: 正常扫描
        """
        if self.consecutive_empty < self.backoff_max_consecutive_empty:
            return False

        if self._last_scan_time is None:
            return False

        # 计算退避延迟：指数增长，上限 max_delay_hours
        # 首次退避：excess_empty=1 → base_delay_hours * 2^0 = base_delay_hours
        # 第二次退避：excess_empty=2 → base_delay_hours * 2^1 = 2*base_delay_hours
        excess_empty = self.consecutive_empty - self.backoff_max_consecutive_empty + 1
        delay_hours = min(
            self.backoff_base_delay_hours * (2 ** (excess_empty - 1)),
            self.backoff_max_delay_hours,
        )

        elapsed = (datetime.now(timezone.utc) - self._last_scan_time).total_seconds() / 3600
        should_skip = elapsed < delay_hours

        if should_skip:
            logger.info(
                "空池退避生效，跳过本次扫描",
                consecutive_empty=self.consecutive_empty,
                delay_hours=delay_hours,
                elapsed_hours=round(elapsed, 2),
            )

        return should_skip

    async def scan_and_update(self) -> Dict[str, List[str]]:
        """
        每日扫描并更新候选池

        V2.5: OR-2 逻辑，满足任意 2 个条件即可入池。
        遍历所有流动性币种，对每个币种分别评估做空/做多 4 个维度。

        Returns:
            {"short": [symbols], "long": [symbols]}
        """
        # V2.5: 空池退避检查
        if self._check_empty_backoff():
            logger.info("空池退避中，跳过候选池扫描")
            return {"short": [], "long": []}

        logger.info("开始每日候选池扫描")

        try:
            # 每日扫描前加载新币做空当前开仓币种
            await self._load_new_coin_open_positions()

            tickers = await self.market_data.get_all_tickers()
            if not tickers:
                logger.error("获取行情数据失败，候选池扫描中止")
                return {"short": [], "long": []}

            # V2.5: OR-2 逻辑，不再按涨跌幅初筛
            # 构建 symbol -> ticker 映射，同时收集所有流动性币种
            symbol_ticker_map: Dict[str, Dict[str, Any]] = {}
            liquid_symbols: List[str] = []

            for ticker in tickers:
                symbol = ticker.get("symbol", "")
                if not symbol.endswith("USDT"):
                    continue
                if self._should_exclude(symbol):
                    continue

                # 流动性检查
                if not self._check_liquidity(ticker):
                    continue

                symbol_ticker_map[symbol] = ticker
                liquid_symbols.append(symbol)

            # V2.3 计算动态阈值（基于所有流动性币种）
            if self.dynamic_enabled:
                self._dynamic_thresholds = await self._compute_dynamic_thresholds(symbol_ticker_map)
                if self._dynamic_thresholds.is_valid():
                    logger.info(
                        "动态阈值已启用",
                        thresholds=self._dynamic_thresholds.to_dict(),
                    )
                else:
                    logger.warning("动态阈值计算失败，回退到固定阈值")

            # V2.5: OR-2 逻辑，遍历每个流动性币种评估 4 个维度
            short_candidates = []
            long_candidates = []

            for symbol in liquid_symbols:
                ticker = symbol_ticker_map.get(symbol, {})

                # 计算做空条件数
                short_count = await self._count_short_conditions(symbol, ticker)
                if short_count >= self._min_conditions:
                    short_candidates.append(symbol)

                # 计算做多条件数
                long_count = await self._count_long_conditions(symbol, ticker)
                if long_count >= self._min_conditions:
                    long_candidates.append(symbol)

            # 保存 ticker 数据（用于 LV-RM 低波动筛选）
            self._ticker_data = {}
            for symbol in liquid_symbols:
                ticker = symbol_ticker_map.get(symbol, {})
                price_change_pct = float(ticker.get("priceChangePercent", 0))
                self._ticker_data[symbol] = {
                    "price_change_percent": price_change_pct,
                    "last_price": float(ticker.get("lastPrice", 0)),
                    "volume_24h": float(ticker.get("quoteVolume", 0)),
                }

            # V2.4: 记录落选币种（通过流动性但未通过候选条件的币种，用于LV-RM扫描）
            final_candidates = set(short_candidates + long_candidates)
            self._eliminated_symbols = [
                s for s in liquid_symbols
                if s not in final_candidates
            ]

            # 更新候选池
            self.short_candidates = set(short_candidates)
            self.long_candidates = set(long_candidates)
            self._active_symbols = self.short_candidates | self.long_candidates
            self._last_scan_time = datetime.now(timezone.utc)

            # V2.5: 空池计数
            if not self._active_symbols:
                self.consecutive_empty += 1
                logger.info(
                    "候选池扫描完成，池为空",
                    consecutive_empty=self.consecutive_empty,
                )
            else:
                # 候选池非空，重置连续空池计数
                self.consecutive_empty = 0
                logger.info(
                    "候选池扫描完成",
                    short_candidates=len(short_candidates),
                    long_candidates=len(long_candidates),
                    total=len(self._active_symbols),
                )

            return {"short": short_candidates, "long": long_candidates}

        except Exception as e:
            logger.error("候选池扫描失败", error=str(e))
            return {"short": [], "long": []}

    async def _count_short_conditions(self, symbol: str, ticker: Dict[str, Any]) -> int:
        """
        V2.5: 计算做空条件达标数（0-4）

        评估 4 个维度：涨跌幅、资金费率、OI/市值比、EMA20 偏离。
        返回满足条件的数量，>= 2 表示可入池。

        Args:
            symbol: 交易对
            ticker: 24h行情数据

        Returns:
            条件达标数（0-4）
        """
        try:
            use_dynamic = self.dynamic_enabled and self._dynamic_thresholds.is_valid()
            dt = self._dynamic_thresholds if use_dynamic else None
            conditions_met = 0

            # 硬性前置检查：OI 必须 >= 最小要求
            oi_usd = await self.market_data.get_oi_usd(symbol)
            if oi_usd < self.min_oi_usd:
                return 0

            # 条件1: 涨跌幅（使用 ticker 的 priceChangePercent，已是百分比数值如 3.5 表示 3.5%）
            price_change = float(ticker.get("priceChangePercent", 0))
            if price_change >= self.short_price_change * 100:
                conditions_met += 1

            # 条件2: 资金费率
            funding_rate = await self.market_data.get_funding_rate(symbol)
            annual_rate = funding_rate * self.settlements_per_day * self.days_per_year * 100

            if use_dynamic:
                # V2.5: 动态阈值（50分位），费率 >= 中位数 且 > 0
                if annual_rate >= dt.funding_rate_short and annual_rate > 0:
                    conditions_met += 1
            else:
                if annual_rate >= self.short_funding_rate * 100:
                    conditions_met += 1

            # 条件3: OI/市值比
            volume_24h = float(ticker.get("quoteVolume", 0))
            market_cap = await self.market_data.get_market_cap(symbol, oi_usd, volume_24h)
            if market_cap > 0:
                oi_market_cap_ratio = oi_usd / market_cap
                if use_dynamic:
                    # V2.5: 动态阈值（50分位）
                    if oi_market_cap_ratio >= dt.oi_market_cap_short:
                        conditions_met += 1
                else:
                    if oi_market_cap_ratio >= self.short_oi_market_cap_min:
                        conditions_met += 1
            else:
                # P1-9: 市值获取失败时，跳过此条件（不计入达标也不计入未达标）
                logger.debug("市值获取失败，做空跳过OI/市值比条件", symbol=symbol)

            # 条件4: EMA20 偏离（做空：价格需向上偏离 EMA20(4h)）
            try:
                # P0-5: 优先使用 strategy 维护的统一4h缓存，避免重复合成
                if self._klines_4h_cache and symbol in self._klines_4h_cache:
                    klines_4h = self._klines_4h_cache[symbol]
                    ema20_4h = self.market_data.get_ema20_from_4h_cache(symbol, klines_4h)
                else:
                    klines_1h = await self.market_data.get_klines_1h(symbol, limit=self.keep_count)
                    if klines_1h:
                        ema20_4h = await self.market_data.get_ema20_4h(symbol, klines_1h)
                    else:
                        ema20_4h = 0.0

                if ema20_4h > 0:
                    current_price = float(ticker.get("lastPrice", 0))
                    deviation = (current_price - ema20_4h) / ema20_4h
                    if use_dynamic:
                        # V2.5: 动态阈值（50分位）
                        if deviation >= dt.ema20_short:
                            conditions_met += 1
                    else:
                        if deviation >= self.short_ema20_deviation:
                            conditions_met += 1
            except Exception:
                pass  # 获取失败时跳过 EMA20 条件

            return conditions_met
        except Exception as e:
            logger.warning("计算做空条件数失败", symbol=symbol, error=str(e))
            return 0

    async def _count_long_conditions(self, symbol: str, ticker: Dict[str, Any]) -> int:
        """
        V2.5: 计算做多条件达标数（0-4）

        与 _count_short_conditions 对称，做多方向。
        评估 4 个维度：涨跌幅、资金费率、OI/市值比、EMA20 偏离。

        Args:
            symbol: 交易对
            ticker: 24h行情数据

        Returns:
            条件达标数（0-4）
        """
        try:
            use_dynamic = self.dynamic_enabled and self._dynamic_thresholds.is_valid()
            dt = self._dynamic_thresholds if use_dynamic else None
            conditions_met = 0

            # 硬性前置检查：OI 必须 >= 最小要求
            oi_usd = await self.market_data.get_oi_usd(symbol)
            if oi_usd < self.min_oi_usd:
                return 0

            # 条件1: 涨跌幅（使用 ticker 的 priceChangePercent，已是百分比数值如 3.5 表示 3.5%）
            price_change = float(ticker.get("priceChangePercent", 0))
            if price_change <= self.long_price_change * 100:
                conditions_met += 1

            # 条件2: 资金费率
            funding_rate = await self.market_data.get_funding_rate(symbol)
            annual_rate = funding_rate * self.settlements_per_day * self.days_per_year * 100

            if use_dynamic:
                # V2.5: 动态阈值（50分位），费率 <= 中位数 且 < 0
                if annual_rate <= dt.funding_rate_long and annual_rate < 0:
                    conditions_met += 1
            else:
                if annual_rate <= self.long_funding_rate * 100:
                    conditions_met += 1

            # 条件3: OI/市值比
            volume_24h = float(ticker.get("quoteVolume", 0))
            market_cap = await self.market_data.get_market_cap(symbol, oi_usd, volume_24h)
            if market_cap > 0:
                oi_market_cap_ratio = oi_usd / market_cap
                if use_dynamic:
                    # V2.5: 动态阈值（50分位）
                    if oi_market_cap_ratio <= dt.oi_market_cap_long:
                        conditions_met += 1
                else:
                    if oi_market_cap_ratio <= self.long_oi_market_cap_max:
                        conditions_met += 1
            else:
                # P1-9: 市值获取失败时，跳过此条件
                logger.debug("市值获取失败，做多跳过OI/市值比条件", symbol=symbol)

            # 条件4: EMA20 偏离（做多：价格需向下偏离 EMA20(4h)）
            try:
                # P0-5: 优先使用 strategy 维护的统一4h缓存，避免重复合成
                if self._klines_4h_cache and symbol in self._klines_4h_cache:
                    klines_4h = self._klines_4h_cache[symbol]
                    ema20_4h = self.market_data.get_ema20_from_4h_cache(symbol, klines_4h)
                else:
                    klines_1h = await self.market_data.get_klines_1h(symbol, limit=self.keep_count)
                    if klines_1h:
                        ema20_4h = await self.market_data.get_ema20_4h(symbol, klines_1h)
                    else:
                        ema20_4h = 0.0

                if ema20_4h > 0:
                    current_price = float(ticker.get("lastPrice", 0))
                    deviation = (current_price - ema20_4h) / ema20_4h
                    if use_dynamic:
                        # V2.5: 动态阈值（50分位）
                        if deviation <= dt.ema20_long:
                            conditions_met += 1
                    else:
                        if deviation <= self.long_ema20_deviation:
                            conditions_met += 1
            except Exception:
                pass  # 获取失败时跳过 EMA20 条件

            return conditions_met
        except Exception as e:
            logger.warning("计算做多条件数失败", symbol=symbol, error=str(e))
            return 0

    def get_active_symbols(self) -> Set[str]:
        """获取所有活跃候选币种"""
        return self._active_symbols.copy()

    def get_short_candidates(self) -> Set[str]:
        """获取做空候选币种"""
        return self.short_candidates.copy()

    def get_long_candidates(self) -> Set[str]:
        """获取做多候选币种"""
        return self.long_candidates.copy()

    # ==================== V2.4: LV-RM 落选币种跟踪 ====================

    def get_eliminated_symbols(self) -> List[str]:
        """
        V2.4: 获取落选币种列表

        返回通过流动性门槛但未进入候选池的所有币种。
        """
        return self._eliminated_symbols.copy()

    async def get_lv_rm_scan_range(self) -> List[str]:
        """
        V2.5: 获取 LV-RM 独立扫描范围

        从全市场流动性币种中筛选 |涨跌幅| < max_price_change_24h 的币种，
        独立于候选池的入池逻辑，用于 LV-RM 模块独立扫描。

        Returns:
            低波动币种列表
        """
        lv_rm_config = self.config.get("lv_rm", {})
        scan_config = lv_rm_config.get("scan", {})
        max_price_change = scan_config.get("max_price_change_24h", 0.08)
        # priceChangePercent 返回的是百分比数值（如 3.5 表示 3.5%），需乘以 100 比较
        max_price_change_pct = max_price_change * 100

        try:
            tickers = await self.market_data.get_all_tickers()
            if not tickers:
                logger.error("获取行情数据失败，LV-RM 扫描范围获取中止")
                return []

            scan_range = []
            for ticker in tickers:
                symbol = ticker.get("symbol", "")
                if not symbol.endswith("USDT"):
                    continue
                if self._should_exclude(symbol):
                    continue
                if not self._check_liquidity(ticker):
                    continue

                price_change = float(ticker.get("priceChangePercent", 0))
                if abs(price_change) < max_price_change_pct:
                    scan_range.append(symbol)

            logger.info(
                "LV-RM 扫描范围获取完成",
                scan_range=len(scan_range),
                max_price_change_pct=max_price_change_pct,
            )
            return scan_range

        except Exception as e:
            logger.error("LV-RM 扫描范围获取失败", error=str(e))
            return []

    async def get_low_volatility_candidates(self) -> List[str]:
        """
        V2.5: 获取低波动候选币种（向后兼容）

        改为调用 get_lv_rm_scan_range() 获取全市场低波动币种列表，
        保持返回类型 List[str] 不变。

        Returns:
            低波动币种列表
        """
        return await self.get_lv_rm_scan_range()

    def has_candidates(self) -> bool:
        """
        检查候选池是否为空

        Returns:
            True: 候选池非空（有做空或做多候选）；False: 候选池为空
        """
        return len(self.short_candidates) > 0 or len(self.long_candidates) > 0

    def add_new_coin_conflict(self, symbol: str) -> None:
        """
        添加新币策略冲突黑名单

        Args:
            symbol: 交易对
        """
        self._new_coin_conflict_blacklist[symbol] = datetime.now(timezone.utc)
        logger.info("添加新币策略冲突黑名单", symbol=symbol)

    def is_healthy(self) -> bool:
        """
        检查候选池健康状态

        Returns:
            是否健康
        """
        if self._last_scan_time is None:
            return False
        # 从配置读取重新扫描间隔
        cycle_config = self.config.get("cycle", {})
        rescan_seconds = cycle_config.get("rescan_interval_seconds", 86400)
        return (datetime.now(timezone.utc) - self._last_scan_time).total_seconds() < rescan_seconds

    def get_last_scan_time(self) -> Optional[datetime]:
        """获取上次扫描时间"""
        return self._last_scan_time

    def set_klines_4h_cache(self, cache: Dict[str, List[Dict]]) -> None:
        """
        P0-5: 设置4h K线缓存引用

        由 strategy 在候选池更新后调用，传入 strategy 维护的统一 _klines_4h_cache。
        候选池验证时优先使用此缓存，避免每次重新合成4h K线。

        Args:
            cache: 4h K线缓存字典 {symbol: [klines_4h]}
        """
        self._klines_4h_cache = cache
        logger.debug("4h K线缓存已注入", symbols=len(cache))