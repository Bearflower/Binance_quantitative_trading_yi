"""
混合反转策略 HRS 主类
继承 BaseStrategy，实现做空和做多双向反转交易逻辑
"""
import asyncio
import json
import os

from typing import Dict, Any, Optional, List, Set, Tuple
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import structlog


class DecimalEncoder(json.JSONEncoder):
    """自定义 JSON 编码器，处理 Decimal 类型"""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)

from shared.base_strategy import BaseStrategy
from shared.notification import NotificationClient
from shared.database import DatabaseManager
from shared.strategy_state import save_strategy_state
from shared.capital_manager import CapitalManager
from shared.trade_logger import TradeLogger

from .candidate_pool import CandidatePool
from .scoring_engine import ScoringEngine, ScoringResult
from .pattern import PatternRecognizer
from .market_data import MarketDataProvider
from .executor import TradingExecutor
from .position_manager import PositionManager
from .risk_manager import RiskManager


logger = structlog.get_logger()

# K线间隔到毫秒的映射
KLINE_INTERVAL_MS = {
    "1h": 3600000,
    "4h": 14400000,
    "1d": 86400000,
}


class HRSStrategy(BaseStrategy):
    """混合反转策略 (HRS)

    核心逻辑：
    1. 每日 8:05 扫描候选池（做空/做多双向）
    2. 每小时K线收盘后对候选币种进行评分
    3. 满足开仓条件时执行市价开仓
    4. 监控持仓：分批止盈、移动止盈、时间止损
    5. 严格风控：连续亏损暂停、回撤熔断、黑名单
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化策略

        Args:
            config: 策略配置字典
        """
        super().__init__(config)

        self.strategy_name = config.get("strategy", {}).get("name", "hrs")

        # 各模块（延迟初始化）
        self.market_data: Optional[MarketDataProvider] = None
        self.candidate_pool: Optional[CandidatePool] = None
        self.scoring_engine: Optional[ScoringEngine] = None
        self.pattern_recognizer: Optional[PatternRecognizer] = None
        self.trading_executor: Optional[TradingExecutor] = None
        self.position_manager: Optional[PositionManager] = None
        self.risk_manager: Optional[RiskManager] = None

        # K线缓存（用于1h K线和4h合成）
        self._klines_cache: Dict[str, List[Dict]] = {}
        # 4h K线缓存（由1h K线合成，用于EMA20计算和状态持久化）
        self._klines_4h_cache: Dict[str, List[Dict]] = {}

        # 已注册到K线服务的币种
        self._registered_symbols: Set[str] = set()

        # 累计盈亏（用于回撤熔断检查）
        self._total_pnl: float = 0.0

        # K线配置
        kline_config = config.get("kline", {})
        self.kline_interval = kline_config.get("interval", "1h")
        self.min_klines = kline_config.get("min_klines_for_analysis", 24)
        self.keep_count = kline_config.get("keep_count", 168)
        self.max_api_limit = kline_config.get("max_api_limit", 100)

        # 检查间隔（从配置读取）
        cycle_config = config.get("cycle", {})
        self.check_interval = cycle_config.get("check_interval_seconds", 3600)
        self.align_minute = cycle_config.get("align_minute", 21)
        self.retry_delay = cycle_config.get("retry_delay_seconds", 60)
        # 每日扫描间隔（秒），默认24小时
        self.rescan_interval_seconds = cycle_config.get("rescan_interval_seconds", 86400)

        # K线预热等待配置（P0-2）
        warmup_config = kline_config.get("warmup", {})
        self.warmup_min_bars = warmup_config.get("min_bars", 24)
        self.warmup_max_wait_seconds = warmup_config.get("max_wait_seconds", 300)
        self.warmup_check_interval_seconds = warmup_config.get("check_interval_seconds", 30)

        # 形态失效放弃配置（P0-3）
        abandon_config = config.get("trading", {}).get("abandon", {})
        self.abandon_extreme_lookback = abandon_config.get("extreme_lookback", 20)

        # P1-1: 信号超时放弃配置
        self.abandon_timeout_bars = abandon_config.get("timeout_bars", 6)
        # P1-1: 信号首次触发时间跟踪 {symbol: {direction: first_trigger_time}}
        self._signal_timestamps: Dict[str, Dict[str, str]] = {}

        # P1-3: 资金费率逆转阈值
        self.abandon_funding_rate_reversal_threshold = abandon_config.get(
            "funding_rate_reversal_threshold", 0
        )

        # P1-10: 候选池扫描定时器
        self._candidate_scan_task: Optional[asyncio.Task] = None  # 候选池扫描定时器任务

        # P2-3: 加仓配置
        add_pos_config = config.get("trading", {}).get("add_position", {})
        self.add_position_enabled = add_pos_config.get("enabled", True)
        self.add_position_profit_threshold = add_pos_config.get("profit_threshold", 0.05)
        self.add_position_size_ratio = add_pos_config.get("size_ratio", 0.5)
        self.add_position_max_times = add_pos_config.get("max_times", 1)

        # P2-3: 加仓计数 {symbol: {direction: count}}
        self._add_position_count: Dict[str, Dict[str, int]] = {}

        # P2-4: 通知事件配置
        notif_config = config.get("notification", {})
        self._notif_events = notif_config.get("events", {})
        self._notif_enabled = notif_config.get("enabled", True)

        # P2-5: 策略启停控制
        self._paused: bool = False

        # 资金分配管理器（读取 capital_limits.monthly_limit 限制仓位）
        config_dir = os.path.dirname(os.path.abspath(__file__))
        self.capital_mgr = CapitalManager(os.path.join(config_dir, "config.yaml"))

        # 候选池为空时休眠配置
        pool_config = config.get("candidate_pool", {})
        sleep_config = pool_config.get("sleep_on_empty", {})
        self._sleep_on_empty_enabled = sleep_config.get("enabled", True)

        # 定期清理残留订单跟踪（Fix #2）
        self._last_orphan_cleanup: Optional[datetime] = None
        # 孤儿订单清理间隔（秒），从配置读取，默认3600（1小时）
        self._orphan_cleanup_interval_seconds = config.get("cycle", {}).get("orphan_cleanup_interval_seconds", 3600)

        # K线缓存清理跟踪（Fix #7）
        self._last_klines_trim: Optional[datetime] = None
        # K线缓存清理间隔（秒），默认3600（1小时）
        self._klines_trim_interval_seconds = config.get("cycle", {}).get("klines_trim_interval_seconds", 3600)

        logger.info(
            "HRS策略初始化",
            strategy_name=self.strategy_name,
            check_interval=self.check_interval,
        )

    async def initialize(self) -> None:
        """初始化策略资源"""
        logger.info("开始初始化HRS策略资源")

        if not self.binance_client:
            raise ValueError("币安客户端未设置")
        if not self.kline_service:
            raise ValueError("K线服务未设置")
        if not self.notification_client:
            raise ValueError("通知客户端未设置")
        if not self.db:
            raise ValueError("数据库管理器未设置")

        # 初始化各模块
        self.market_data = MarketDataProvider(self.binance_client, self.kline_service, self.config)
        self.candidate_pool = CandidatePool(self.config, self.market_data, self.db)
        self.scoring_engine = ScoringEngine(self.config)
        self.pattern_recognizer = PatternRecognizer(self.config)
        self.position_manager = PositionManager(self.config, self.binance_client, db=self.db)
        self.trading_executor = TradingExecutor(
            self.config, self.binance_client, self.db, self.notification_client,
            self.position_manager,
            should_notify_callback=self._should_notify,
        )
        # P1-8: 传入通知回调，使风控模块能在暂停/熔断时发送通知
        self.risk_manager = RiskManager(
            self.config,
            notification_callback=self._send_risk_notification,
            should_notify_callback=self._should_notify,
        )

        # 初始化数据库表
        await self._ensure_db_schema()

        # 恢复状态
        await self._restore_state()

        # P1-4: 启动时恢复未触发订单
        await self._restore_orders()

        # 启动后补单：为所有持仓补充缺失的止损止盈条件单
        await self._replenish_all_positions()

        # P1-10: 启动候选池扫描定时器（asyncio 版本）
        self._start_candidate_scan_timer()

        logger.info("HRS策略资源初始化完成")

    async def analyze(self, symbol: str) -> Dict[str, Any]:
        """
        分析单个币种的做空和做多机会

        Args:
            symbol: 交易对

        Returns:
            分析结果
        """
        result = {"symbol": symbol, "short": None, "long": None}

        try:
            # 获取K线数据
            klines = self._klines_cache.get(symbol, [])
            if len(klines) < self.min_klines:
                # 尝试预热K线
                klines = await self._warmup_klines(symbol)
                if len(klines) < self.min_klines:
                    result["skip"] = True
                    result["reason"] = f"K线数据不足 ({len(klines)} < {self.min_klines})"
                    return result
                self._klines_cache[symbol] = klines

            current_close = float(klines[-1].get("close", 0))
            # P0-4: 资金费率时间对齐，使用K线收盘时间（open_time + 1h）而非开盘时间
            current_open_time = klines[-1].get("open_time", 0)
            current_close_time = current_open_time + KLINE_INTERVAL_MS.get(self.kline_interval, 3600000)  # K线收盘时间（毫秒）

            # 获取OI和资金费率
            oi_usd = await self.market_data.get_oi_usd(symbol)
            # P0-4: 传入K线收盘时间，确保与资金费率结算周期对齐
            funding_rate = await self.market_data.get_funding_rate(symbol, at_time=current_close_time)
            volume_24h = await self.market_data.get_24h_volume(symbol)

            # 获取市值
            market_cap = await self.market_data.get_market_cap(symbol, oi_usd, volume_24h)
            has_market_cap = market_cap > 0
            oi_market_cap_ratio = oi_usd / market_cap if has_market_cap else 0.0

            # 检查做空候选
            if symbol in self.candidate_pool.get_short_candidates():
                # P0-3: 进场前形态失效检查
                if self._check_pattern_expiry(symbol, "short", current_close, klines):
                    result["short"] = {
                        "score_result": {},
                        "patterns": {},
                        "current_price": current_close,
                        "oi_usd": oi_usd,
                        "funding_rate": funding_rate,
                        "market_cap": market_cap,
                        "should_entry": False,
                        "veto_reason": "形态失效：价格突破反向极值",
                    }
                else:
                    short_patterns = self.pattern_recognizer.detect_short_patterns(klines)
                    short_score = self.scoring_engine.score(
                        symbol=symbol,
                        direction="short",
                        oi_market_cap_ratio=oi_market_cap_ratio,
                        patterns=short_patterns,
                        funding_rate=funding_rate,
                        has_market_cap=has_market_cap,
                    )
                    result["short"] = {
                        "score_result": short_score.to_dict(),
                        "patterns": short_patterns,
                        "current_price": current_close,
                        "oi_usd": oi_usd,
                        "funding_rate": funding_rate,
                        "market_cap": market_cap,
                        "should_entry": self.scoring_engine.should_entry(short_score),
                    }

            # 检查做多候选
            if symbol in self.candidate_pool.get_long_candidates():
                # P0-3: 进场前形态失效检查
                if self._check_pattern_expiry(symbol, "long", current_close, klines):
                    result["long"] = {
                        "score_result": {},
                        "patterns": {},
                        "current_price": current_close,
                        "oi_usd": oi_usd,
                        "funding_rate": funding_rate,
                        "market_cap": market_cap,
                        "should_entry": False,
                        "veto_reason": "形态失效：价格突破反向极值",
                    }
                else:
                    long_patterns = self.pattern_recognizer.detect_long_patterns(klines)
                    long_score = self.scoring_engine.score(
                        symbol=symbol,
                        direction="long",
                        oi_market_cap_ratio=oi_market_cap_ratio,
                        patterns=long_patterns,
                        funding_rate=funding_rate,
                        has_market_cap=has_market_cap,
                    )
                    result["long"] = {
                        "score_result": long_score.to_dict(),
                        "patterns": long_patterns,
                        "current_price": current_close,
                        "oi_usd": oi_usd,
                        "funding_rate": funding_rate,
                        "market_cap": market_cap,
                        "should_entry": self.scoring_engine.should_entry(long_score),
                    }

            return result

        except Exception as e:
            logger.error("分析失败", symbol=symbol, error=str(e))
            result["error"] = str(e)
            return result

    async def execute_signal(self, signal: Dict[str, Any]) -> bool:
        """
        执行交易信号

        在进场前执行以下检查：
        - P1-2: 流动性降级检查（OI二次确认）
        - P1-3: 资金费率逆转检查

        Args:
            signal: 交易信号

        Returns:
            是否执行成功
        """
        try:
            symbol = signal.get("symbol")
            direction = signal.get("direction")
            score = signal.get("score", 0)
            current_price = signal.get("current_price", 0)
            klines = signal.get("klines", [])
            # V2.4: 获取入场模式
            entry_mode = signal.get("entry_mode", "standard")

            # 刷新当前价格：使用实时 ticker 价格替代 K线收盘价，避免限价单因价格过时无法成交
            try:
                ticker = await self.binance_client.get_ticker(symbol)
                live_price = float(ticker.get("lastPrice", 0))
                if live_price > 0:
                    current_price = live_price
            except Exception as e:
                logger.warning("获取实时价格失败，使用K线收盘价", symbol=symbol, error=str(e))

            if not symbol or not direction:
                return False

            # 风控检查
            if not self.risk_manager.can_open_position(direction):
                return False

            if self.risk_manager.is_blacklisted(symbol):
                return False

            # 检查是否已有同币种持仓
            if self.position_manager.has_position(symbol):
                # P2-3: 已有持仓，检查是否满足加仓条件
                if self.add_position_enabled:
                    add_result = await self._handle_add_position(
                        symbol, direction, score, current_price, klines
                    )
                    if add_result:
                        return True
                logger.info("该币种已有持仓，跳过", symbol=symbol)
                return False

            # P1-2: 进场前OI二次确认（流动性降级放弃）
            if not await self._check_oi_before_entry(symbol, direction):
                return False

            # P1-3: 进场前资金费率方向检查（费率逆转放弃）
            if not await self._check_funding_rate_before_entry(symbol, direction):
                return False

            # 计算ATR
            atr = await self.trading_executor.calculate_atr(symbol, klines)

            # 获取账户余额
            account_info = await self.binance_client.get_account_info()
            balance = float(account_info.get("totalMarginBalance", 0))

            # 计算止损幅度（委托给 executor 统一计算）
            tick_size, _ = await self.trading_executor._get_symbol_precision(symbol)
            if direction == "short":
                stop_loss_price = self.trading_executor.calculate_short_stop_loss(
                    current_price, atr, tick_size
                )
                stop_loss_percent = (stop_loss_price - current_price) / current_price
            else:
                stop_loss_price = self.trading_executor.calculate_long_stop_loss(
                    current_price, atr, tick_size
                )
                stop_loss_percent = (current_price - stop_loss_price) / current_price

            # 计算仓位
            quantity = self.risk_manager.calculate_position_size(
                balance, stop_loss_percent, self.trading_executor.leverage, current_price
            )

            # 开仓前检查：总仓位不超过分配上限
            current_positions_value = 0.0
            for _, pos_data in self.position_manager.get_all_positions().items():
                pos_qty = pos_data.get("entry_quantity", 0) or pos_data.get("quantity", 0)
                pos_price = pos_data.get("entry_price", 0)
                current_positions_value += float(pos_qty) * float(pos_price)
            new_position_value = float(quantity) * float(current_price)
            if not self.capital_mgr.can_open_position(current_positions_value, new_position_value):
                logger.warning(
                    "总仓位超限，跳过开仓",
                    symbol=symbol,
                    current=current_positions_value,
                    new=new_position_value,
                    limit=self.capital_mgr.get_allocated_capital(),
                )
                return False

            # 执行开仓
            order = None
            if direction == "short":
                order = await self.trading_executor.execute_short(
                    symbol=symbol,
                    entry_price=current_price,
                    atr=atr,
                    quantity=Decimal(str(quantity)),
                    score=score,
                    entry_mode=entry_mode,
                )
            else:
                order = await self.trading_executor.execute_long(
                    symbol=symbol,
                    entry_price=current_price,
                    atr=atr,
                    quantity=Decimal(str(quantity)),
                    score=score,
                    entry_mode=entry_mode,
                )

            if order:
                # 记录持仓
                self.position_manager.add_position(
                    symbol=symbol,
                    direction=direction,
                    entry_price=current_price,
                    quantity=quantity,
                    atr=atr,
                )

                # 记录风控
                self.risk_manager.record_open(direction)

                # 保存状态
                await self._save_state()

                logger.info("开仓成功", symbol=symbol, direction=direction)
                return True

            return False

        except Exception as e:
            logger.error("执行信号失败", error=str(e))
            return False

    async def run(self) -> None:
        """运行策略主循环"""
        logger.info("HRS策略开始运行")
        self._running = True

        try:
            # 对齐到整点
            await self._align_to_hour()

            while self._running:
                try:
                    # P2-5: 暂停时只维护K线和持仓监控，不评分
                    if self._paused:
                        logger.debug("策略暂停中，仅维护K线数据和持仓监控")
                        await self._monitor_positions()
                        await asyncio.sleep(self.check_interval)
                        continue

                    # V2.5: 候选池为空时，仅跳过标准模式/EMM，LV-RM 继续运行
                    if self._is_sleep_on_empty():
                        logger.debug("候选池为空，仅执行 LV-RM 检查")
                        await self._run_lv_rm_only_cycle()
                        await self._monitor_positions()
                        await asyncio.sleep(self.check_interval)
                        continue

                    await self._execute_cycle()
                    await asyncio.sleep(self.check_interval)
                except Exception as e:
                    logger.error("执行周期异常", error=str(e))
                    await asyncio.sleep(self.retry_delay)

        except Exception as e:
            logger.error("策略运行异常", error=str(e))
            raise

    async def stop(self) -> None:
        """停止策略"""
        logger.info("停止HRS策略")
        self._running = False

        # P1-10: 取消候选池扫描定时器
        if self._candidate_scan_task:
            self._candidate_scan_task.cancel()
            self._candidate_scan_task = None

        await self._save_state()

        if self.market_data:
            await self.market_data.close()

        await self.cleanup()
        logger.info("HRS策略已停止")

    # ==================== P2-5: 策略启停控制 ====================

    def pause(self) -> None:
        """
        P2-5: 暂停策略

        暂停评分循环，但保持K线数据接收和持仓监控。
        可在外部通过 HTTP API 或信号文件调用。
        """
        if self._paused:
            logger.info("策略已在暂停状态，忽略重复暂停请求")
            return
        self._paused = True
        logger.warning("策略已暂停，评分循环停止，K线数据和持仓监控继续运行")

    def resume(self) -> None:
        """
        P2-5: 恢复策略

        重新开始评分循环。
        """
        if not self._paused:
            logger.info("策略未在暂停状态，忽略恢复请求")
            return
        self._paused = False
        logger.info("策略已恢复，评分循环重新开始")

    def is_paused(self) -> bool:
        """
        P2-5: 查询策略是否暂停

        Returns:
            True: 暂停中；False: 运行中
        """
        return self._paused

    def _is_sleep_on_empty(self) -> bool:
        """
        检查是否应进入候选池空休眠状态

        当 sleep_on_empty 启用且候选池为空时，策略进入休眠状态。
        休眠期间不评分，仅维护持仓监控。
        每次醒来检查候选池是否已更新，若已更新则恢复正常执行。

        Returns:
            True: 应进入休眠；False: 正常运行
        """
        if not self._sleep_on_empty_enabled:
            return False
        if self.candidate_pool is None:
            return False
        # 候选池非空时不休眠
        if self.candidate_pool.has_candidates():
            return False
        # 候选池从未扫描过（尚未初始化），也不休眠
        if self.candidate_pool.get_last_scan_time() is None:
            return False
        return True

    def _check_pattern_expiry(self, symbol: str, direction: str, current_price: float, klines: List[Dict]) -> bool:
        """
        P0-3: 检查形态是否已失效

        进场前检查反向突破极值：
        - 做空方向：当前价是否 > 最近 N 根 1h K 线最高价 → 突破则放弃信号
        - 做多方向：当前价是否 < 最近 N 根 1h K 线最低价 → 突破则放弃信号

        Args:
            symbol: 交易对
            direction: 方向 ('short' 或 'long')
            current_price: 当前价格
            klines: 1h K线数据列表

        Returns:
            True: 形态已失效，应放弃信号
        """
        if not klines or len(klines) < self.abandon_extreme_lookback:
            return False

        lookback_klines = klines[-self.abandon_extreme_lookback:]

        if direction == "short":
            # 做空：当前价突破最近N根K线最高价 → 放弃
            extreme_price = max(float(k.get("high", 0)) for k in lookback_klines)
            if current_price > extreme_price:
                logger.info(
                    "形态失效：做空方向价格突破反向极值",
                    symbol=symbol,
                    current_price=current_price,
                    extreme_high=extreme_price,
                    lookback=self.abandon_extreme_lookback,
                )
                return True
        else:
            # 做多：当前价跌破最近N根K线最低价 → 放弃
            extreme_price = min(float(k.get("low", 0)) for k in lookback_klines)
            if current_price < extreme_price:
                logger.info(
                    "形态失效：做多方向价格突破反向极值",
                    symbol=symbol,
                    current_price=current_price,
                    extreme_low=extreme_price,
                    lookback=self.abandon_extreme_lookback,
                )
                return True

        return False

    async def _execute_cycle(self) -> None:
        """
        执行一个周期

        流程：
        1. 检查是否需要更新候选池
        2. 对候选币种评分
        3. 处理开仓信号
        4. 监控持仓
        5. 定期清理残留订单（Fix #2）
        6. 定期裁剪K线缓存（Fix #7）
        """
        now = datetime.now(timezone.utc)

        # Fix #8: 资金费率结算时间安全检查
        if self._is_settlement_skip_window():
            logger.info("资金费率结算窗口，跳过本周期评分")
            return

        # 检查回撤熔断
        try:
            account_info = await self.binance_client.get_account_info()
            balance = float(account_info.get("totalMarginBalance", 0))
            if balance > 0 and not await self.risk_manager.check_drawdown(self._total_pnl, balance):
                logger.warning("回撤熔断触发，跳过本周期")
                return
        except Exception as e:
            logger.warning("回撤检查失败，继续执行", error=str(e))

        # P1-8: 检查暂停状态并输出日志
        if self.risk_manager.is_paused():
            now_utc = datetime.now(timezone.utc)
            pause_info = self._get_pause_info()
            if pause_info:
                remaining = (pause_info["until"] - now_utc).total_seconds() / 3600
                logger.info(
                    "策略暂停中，跳过本周期",
                    reason=pause_info["reason"],
                    remaining_hours=round(remaining, 1),
                )
            else:
                logger.info("策略暂停中，跳过本周期")
            return

        # 检查是否需要在8:05更新候选池
        await self._check_candidate_update(now)

        # 获取活跃候选币种
        active_symbols = self.candidate_pool.get_active_symbols()
        if not active_symbols:
            logger.debug("无活跃候选币种，仅执行 LV-RM 检查")
            await self._run_lv_rm_only_cycle()
            await self._monitor_positions()
            return

        logger.info("开始评分周期", symbols=list(active_symbols)[:10])

        # 评分并处理
        short_signals = []
        long_signals = []

        for symbol in list(active_symbols):
            try:
                # P2-6: 检查极端行情熔断
                if self.risk_manager.is_circuit_breaker_active(symbol):
                    cb_info = self.risk_manager.get_circuit_breaker_info(symbol)
                    logger.debug(
                        "熔断中，跳过评分",
                        symbol=symbol,
                        remaining_minutes=round(cb_info["remaining_minutes"], 1) if cb_info else 0,
                    )
                    continue

                # P2-6: 计算1小时价格变化并检查熔断
                klines = self._klines_cache.get(symbol, [])
                if len(klines) >= 2:
                    price_1h_ago = float(klines[-2].get("close", 0))
                    current_price_for_cb = float(klines[-1].get("close", 0))
                    if price_1h_ago > 0:
                        price_change_1h = (current_price_for_cb - price_1h_ago) / price_1h_ago
                        if not self.risk_manager.check_price_change(symbol, price_change_1h):
                            # 触发熔断，跳过评分
                            continue

                analysis = await self.analyze(symbol)
                if analysis.get("skip") or analysis.get("error"):
                    continue

                # 处理做空信号
                short_data = analysis.get("short")
                if short_data and short_data.get("should_entry"):
                    # P2-6: 已有持仓时不产生新信号（不加仓）
                    if self.position_manager.has_position(symbol):
                        logger.debug("该币种已有持仓，跳过做空信号", symbol=symbol)
                    else:
                        # P1-1: 记录信号首次触发时间
                        self._record_signal_timestamp(symbol, "short")
                        short_signals.append({
                            "symbol": symbol,
                            "direction": "short",
                            "score": short_data["score_result"]["total_score"],
                            "technical_score": short_data["score_result"].get("technical_score", 0),
                            "sentiment_score": short_data["score_result"].get("sentiment_score", 0),
                            "current_price": short_data["current_price"],
                            "klines": self._klines_cache.get(symbol, []),
                        })
                else:
                    # 信号消失，清理时间戳
                    self._clear_signal_timestamp(symbol, "short")

                # 处理做多信号
                long_data = analysis.get("long")
                if long_data and long_data.get("should_entry"):
                    # P2-6: 已有持仓时不产生新信号（不加仓）
                    if self.position_manager.has_position(symbol):
                        logger.debug("该币种已有持仓，跳过做多信号", symbol=symbol)
                    else:
                        # P1-1: 记录信号首次触发时间
                        self._record_signal_timestamp(symbol, "long")
                        long_signals.append({
                            "symbol": symbol,
                            "direction": "long",
                            "score": long_data["score_result"]["total_score"],
                            "technical_score": long_data["score_result"].get("technical_score", 0),
                            "sentiment_score": long_data["score_result"].get("sentiment_score", 0),
                            "current_price": long_data["current_price"],
                            "klines": self._klines_cache.get(symbol, []),
                        })
                else:
                    # 信号消失，清理时间戳
                    self._clear_signal_timestamp(symbol, "long")

            except Exception as e:
                logger.error("分析失败", symbol=symbol, error=str(e))

        # P1-1: 过滤超时信号
        short_signals = await self._filter_timeout_signals(short_signals, "short")
        long_signals = await self._filter_timeout_signals(long_signals, "long")

        # P2-1: 记录信号日志到独立表
        for sig in short_signals + long_signals:
            await self._log_signal_to_db(sig["symbol"], sig["direction"], sig)

        # ===== V2.4: LV-RM 低波动反转模块检查 =====
        lv_rm_config = self.config.get("lv_rm", {})
        if lv_rm_config.get("enabled", True):
            try:
                # 获取低波动候选币种
                low_vol_symbols = await self.candidate_pool.get_low_volatility_candidates()
                if low_vol_symbols:
                    await self._check_lv_rm_entries(low_vol_symbols, short_signals, long_signals)
            except Exception as e:
                logger.error("LV-RM检查失败", error=str(e))

        # 处理双向冲突
        await self._resolve_conflicts(short_signals, long_signals)

        # 发送周期汇总通知
        await self._send_cycle_summary(
            short_signals, long_signals, list(active_symbols),
            list(self.candidate_pool.get_short_candidates()),
            list(self.candidate_pool.get_long_candidates()),
        )

        # Fix #7: 定期裁剪K线缓存
        self._trim_klines_cache()

        # Fix #2: 定期清理残留订单
        await self._cleanup_orphan_orders()

        # 监控持仓
        await self._monitor_positions()

        # 保存策略状态到 strategy_states（用于 orphan_cleanup 统一检测）
        positions = {}
        for symbol, pos in self.position_manager.get_all_positions().items():
            positions[symbol] = {
                "direction": pos.get("direction"),
                "entry_price": pos.get("entry_price"),
                "quantity": pos.get("entry_quantity"),
                "remaining_quantity": pos.get("remaining_quantity"),
                "entry_time": str(pos.get("entry_time", "")),
                "algo_ids": pos.get("algo_ids", {}),
            }
        await save_strategy_state(self.db, "hrs", positions)

    async def _check_candidate_update(self, now: datetime) -> None:
        """
        P1-10: 检查是否需要更新候选池

        使用双重保障机制：
        - 主定时器：asyncio 定时器在指定时间精确触发
        - 主循环兜底：每日扫描时间（UTC+8）后第一次检查时，若今天还没扫描过则触发
          解决旧版 threading.Timer 回调静默失败导致候选池永不更新的问题
        支持多个扫描时间（scan_times 列表）
        """
        pool_config = self.config.get("candidate_pool", {})
        # 优先使用 scan_times 列表，向后兼容单值 scan_time
        scan_times_str = pool_config.get("scan_times", [pool_config.get("scan_time", "08:05")])
        if isinstance(scan_times_str, str):
            scan_times_str = [scan_times_str]

        last_scan = self.candidate_pool.get_last_scan_time()

        for scan_time_str in scan_times_str:
            scan_hour, scan_minute = map(int, scan_time_str.split(":"))
            # UTC+8 转 UTC
            utc_scan_hour = (scan_hour - 8) % 24
            utc_scan_minute = scan_minute

            # 构建今天的扫描目标时间（UTC）
            today_target = now.replace(hour=utc_scan_hour, minute=utc_scan_minute, second=0, microsecond=0)

            # 如果当前时间还没到今天的扫描时间，跳过
            if now < today_target:
                continue

            # 检查是否今天已经扫描过该时间点：last_scan >= today_target 说明已扫描
            if last_scan is not None and last_scan >= today_target:
                continue

            # 找到第一个需要扫描的时间点
            await self._perform_candidate_scan(scan_time_str)
            return

    async def _perform_candidate_scan(self, scan_time_str: str) -> None:
        """
        执行候选池扫描的内部逻辑

        Args:
            scan_time_str: 扫描时间字符串（用于日志）
        """
        logger.info("触发每日候选池更新", scan_time=scan_time_str)
        result = await self.candidate_pool.scan_and_update()

        # P0-5: 候选池更新后，注入 strategy 维护的统一4h K线缓存
        self.candidate_pool.set_klines_4h_cache(self._klines_4h_cache)

        # V2.3：候选池扫描完成后，将动态阈值注入评分引擎
        dt = self.candidate_pool.get_dynamic_thresholds()
        self.scoring_engine.set_dynamic_thresholds(dt)

        # 为新候选币种注册K线并预热
        for direction in ["short", "long"]:
            for symbol in result.get(direction, []):
                if symbol not in self._registered_symbols:
                    await self._register_and_warmup(symbol)

        # 候选池更新后，注销不再需要的K线服务
        await self._unregister_symbols()

    def _start_candidate_scan_timer(self) -> None:
        """
        P1-10: 启动候选池扫描定时器（asyncio 版本）

        使用 asyncio.create_task 实现可靠定时器，在每日 08:05 (UTC+8) 触发扫描。
        替代旧版 threading.Timer，解决线程回调静默失败问题。
        """
        self._candidate_scan_task = asyncio.create_task(self._candidate_scan_timer_loop())
        logger.info("候选池扫描定时器已启动（asyncio 版本）")

    def _get_next_scan_time(self) -> Tuple[Optional[datetime], Optional[str]]:
        """
        获取下一次候选池扫描时间

        从配置读取 scan_times 列表，计算距离当前时间最近的下一次扫描时间。
        作为公共方法，供 _candidate_scan_timer_loop 和 _calculate_sleep_until_next_scan 复用。

        Returns:
            (next_scan_datetime, next_scan_time_str): 下一次扫描时间及对应的时间字符串
            next_scan_time_str 为 UTC+8 时间字符串（如 "08:05"），用于日志
        """
        pool_config = self.config.get("candidate_pool", {})
        # 优先使用 scan_times 列表，向后兼容单值 scan_time
        scan_times_str = pool_config.get("scan_times", [pool_config.get("scan_time", "08:05")])
        if isinstance(scan_times_str, str):
            scan_times_str = [scan_times_str]

        # 解析所有扫描时间（UTC+8 转 UTC）
        scan_times_utc: List[Tuple[int, int]] = []
        for time_str in scan_times_str:
            hour, minute = map(int, time_str.split(":"))
            utc_hour = (hour - 8) % 24
            scan_times_utc.append((utc_hour, minute))

        now = datetime.now(timezone.utc)

        # 计算所有扫描时间中，未来最近的一个
        next_scan: Optional[datetime] = None
        next_scan_time_str: Optional[str] = None

        for i, (utc_hour, utc_minute) in enumerate(scan_times_utc):
            candidate = now.replace(hour=utc_hour, minute=utc_minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)

            if next_scan is None or candidate < next_scan:
                next_scan = candidate
                next_scan_time_str = scan_times_str[i]

        return next_scan, next_scan_time_str

    async def _candidate_scan_timer_loop(self) -> None:
        """
        候选池扫描定时器循环（asyncio 版本）

        支持多个扫描时间（scan_times 列表），按时间顺序依次触发。
        每次计算相对于当前时间最近的下一次扫描时间，精确等待后触发。
        """
        while True:
            try:
                next_scan, next_scan_time_str = self._get_next_scan_time()

                if next_scan is None or next_scan_time_str is None:
                    logger.error("无法计算下一次扫描时间，使用默认重试")
                    await asyncio.sleep(self.retry_delay)
                    continue

                now = datetime.now(timezone.utc)
                delay_seconds = (next_scan - now).total_seconds()
                logger.info(
                    "候选池扫描定时器已设置",
                    next_scan=next_scan.isoformat(),
                    next_scan_time=f"{next_scan_time_str} UTC+8",
                    delay_seconds=delay_seconds,
                )

                await asyncio.sleep(delay_seconds)
                await self._perform_candidate_scan(next_scan_time_str)
            except asyncio.CancelledError:
                logger.info("候选池扫描定时器已取消")
                break
            except Exception as e:
                logger.error("候选池扫描定时器异常", error=str(e))
                # 异常后等待重试延迟再重试，避免频繁错误
                await asyncio.sleep(self.retry_delay)

    async def _calculate_sleep_until_next_scan(self) -> float:
        """
        计算距离下一次候选池扫描时间的剩余秒数

        复用 _get_next_scan_time 方法获取下一次扫描时间，
        用于候选池为空时确定休眠到下一次扫描时间点的精确秒数。
        确保候选池空休眠醒来后正好赶上扫描时间，候选池可能已更新。

        Returns:
            距离下一次扫描时间的剩余秒数（浮点数，始终 >= 0）
        """
        next_scan, _ = self._get_next_scan_time()

        if next_scan is None:
            # 兜底：使用默认重试延迟
            logger.warning("无法计算下一次扫描时间，使用默认重试延迟作为休眠时间")
            return float(self.retry_delay)

        now = datetime.now(timezone.utc)
        delay_seconds = (next_scan - now).total_seconds()
        logger.debug(
            "计算到下次扫描的休眠时间",
            next_scan=next_scan.isoformat(),
            delay_seconds=delay_seconds,
        )
        return max(delay_seconds, 0.0)

    async def _register_and_warmup(self, symbol: str) -> None:
        """
        注册币种到K线服务并预热K线

        拉取168根1h K线并合成4h K线，缓存两者。

        Args:
            symbol: 交易对
        """
        try:
            # 注册到K线服务
            registered = await self.kline_service.register_symbol(symbol, intervals=[self.kline_interval])
            if registered:
                self._registered_symbols.add(symbol)
                logger.info("K线服务注册成功", symbol=symbol)

            # 预热1h K线（拉取168根）
            klines = await self._warmup_klines(symbol)
            if klines:
                self._klines_cache[symbol] = klines

                # 合成4h K线并缓存
                await self._synthesize_4h_klines(symbol, klines)
        except Exception as e:
            logger.warning("注册预热失败", symbol=symbol, error=str(e))

    async def _warmup_klines(self, symbol: str) -> List[Dict]:
        """
        预热K线数据

        拉取 keep_count（168根，约7天）根1h K线，用于后续分析和4h合成。
        尝试从K线服务获取，失败则从币安API获取。

        Args:
            symbol: 交易对

        Returns:
            K线数据列表
        """
        try:
            # 从K线服务获取
            klines = await self.kline_service.get_klines(
                symbol=symbol,
                interval=self.kline_interval,
                limit=self.keep_count,
            )
            if klines and len(klines) >= self.min_klines:
                return klines
        except Exception:
            logger.debug("K线服务获取失败，回退到币安API", symbol=symbol)

        # 回退到币安API（单次最多 max_api_limit 根）
        try:
            klines = await self.binance_client.get_klines(
                symbol=symbol,
                interval=self.kline_interval,
                limit=min(self.keep_count, self.max_api_limit),
            )
            return klines
        except Exception as e:
            logger.warning("币安API拉取K线失败", symbol=symbol, error=str(e))
            return []

    async def _synthesize_4h_klines(self, symbol: str, klines_1h: List[Dict]) -> None:
        """
        从1h K线合成4h K线并缓存

        Args:
            symbol: 交易对
            klines_1h: 1h K线数据列表
        """
        try:
            if not self.market_data:
                logger.warning("市场数据提供者未初始化，无法合成4h K线")
                return

            klines_4h = await self.market_data.synthesize_4h_klines(klines_1h)
            if klines_4h:
                # 只保留最近 synthetic_4h_count 根4h K线
                kline_config = self.config.get("kline", {})
                synthetic_count = kline_config.get("synthetic_4h_count", 50)
                self._klines_4h_cache[symbol] = klines_4h[-synthetic_count:]
                logger.debug("4h K线合成完成", symbol=symbol, count=len(klines_4h))
        except Exception as e:
            logger.warning("4h K线合成失败", symbol=symbol, error=str(e))

    def _should_unregister(self, symbol: str) -> bool:
        """
        检查币种是否应该注销K线服务

        条件：不在候选池 + 无持仓 + 不在黑名单
        特例：暂停/熔断期间保留所有K线监控，以便恢复后能立即分析

        P0-6: 增加暂停/熔断状态判断，暂停期间不注销K线服务

        Args:
            symbol: 交易对

        Returns:
            是否应该注销
        """
        # P0-6: 暂停/熔断期间保留K线监控，不注销
        if self.risk_manager.is_paused():
            logger.debug("暂停/熔断中，保留K线监控", symbol=symbol)
            return False

        # 检查是否在候选池中
        in_short = symbol in self.candidate_pool.get_short_candidates()
        in_long = symbol in self.candidate_pool.get_long_candidates()
        if in_short or in_long:
            return False

        # 检查是否有持仓
        if self.position_manager.has_position(symbol):
            return False

        # 检查是否在黑名单中
        if self.risk_manager.is_blacklisted(symbol):
            return False

        return True

    async def _unregister_symbols(self) -> None:
        """
        每日候选池更新后，注销不再需要的K线服务

        对比新旧候选池，对满足注销条件的币种执行注销操作。
        注销条件：不在候选池 + 无持仓 + 不在黑名单
        """
        to_unregister = []
        for symbol in list(self._registered_symbols):
            if self._should_unregister(symbol):
                to_unregister.append(symbol)

        for symbol in to_unregister:
            await self._unregister_single_symbol(symbol)

        if to_unregister:
            logger.info("K线服务批量注销完成", count=len(to_unregister))

    async def _unregister_single_symbol(self, symbol: str) -> None:
        """
        注销单个币种的K线服务

        Args:
            symbol: 交易对
        """
        try:
            await self.kline_service.unregister_symbol(symbol)
            self._registered_symbols.discard(symbol)
            self._klines_cache.pop(symbol, None)
            self._klines_4h_cache.pop(symbol, None)
            logger.info("K线服务已注销（平仓触发）", symbol=symbol)
        except Exception as e:
            logger.warning("K线服务注销失败", symbol=symbol, error=str(e))

    async def _resolve_conflicts(
        self,
        short_signals: List[Dict],
        long_signals: List[Dict],
    ) -> None:
        """
        解析双向信号冲突

        若同一币种同时满足做空和做多，优先选择总分较高的一方。

        Args:
            short_signals: 做空信号列表
            long_signals: 做多信号列表
        """
        short_by_symbol = {s["symbol"]: s for s in short_signals}
        long_by_symbol = {s["symbol"]: s for s in long_signals}

        # 收集冲突币种
        conflict_symbols = set(short_by_symbol.keys()) & set(long_by_symbol.keys())

        for symbol in conflict_symbols:
            short_s = short_by_symbol[symbol]
            long_s = long_by_symbol[symbol]

            if short_s["score"] > long_s["score"]:
                # 选择做空，做多标记为冲突
                del long_by_symbol[symbol]
                await self._update_signal_resolution(symbol, "long", "conflict")
            elif long_s["score"] > short_s["score"]:
                # 选择做多，做空标记为冲突
                del short_by_symbol[symbol]
                await self._update_signal_resolution(symbol, "short", "conflict")
            else:
                # 分数相同，比较技术面
                short_tech = short_s.get("technical_score", 0)
                long_tech = long_s.get("technical_score", 0)
                if short_tech > long_tech:
                    del long_by_symbol[symbol]
                    await self._update_signal_resolution(symbol, "long", "conflict")
                elif long_tech > short_tech:
                    del short_by_symbol[symbol]
                    await self._update_signal_resolution(symbol, "short", "conflict")
                else:
                    # 技术分也相同，比较情绪得分
                    short_sentiment = short_s.get("sentiment_score", 0)
                    long_sentiment = long_s.get("sentiment_score", 0)
                    if short_sentiment > long_sentiment:
                        del long_by_symbol[symbol]
                        await self._update_signal_resolution(symbol, "long", "conflict")
                    elif long_sentiment > short_sentiment:
                        del short_by_symbol[symbol]
                        await self._update_signal_resolution(symbol, "short", "conflict")
                    else:
                        # 都相同，放弃两个
                        del short_by_symbol[symbol]
                        del long_by_symbol[symbol]
                        await self._update_signal_resolution(symbol, "short", "conflict")
                        await self._update_signal_resolution(symbol, "long", "conflict")
                        logger.info("双向信号冲突且无法区分，放弃", symbol=symbol)

        # 执行无冲突的信号
        for signal in list(short_by_symbol.values()) + list(long_by_symbol.values()):
            success = await self.execute_signal(signal)
            # P2-1: 更新信号结果
            if success:
                await self._update_signal_resolution(signal["symbol"], signal["direction"], "executed")

    async def _writeback_pnl(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        quantity: float,
        close_order: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        回写平仓盈亏到 trade_records

        计算平仓盈亏并调用 TradeLogger.update_realized_pnl 写入数据库。
        写入失败不影响主流程。

        Args:
            symbol: 交易对
            direction: 方向 ('short'/'long')
            entry_price: 入场价格
            exit_price: 出场价格（用于 PnL 计算）
            quantity: 平仓数量
            close_order: 平仓订单结果（含 orderId），用于精确匹配
        """
        try:
            if entry_price <= 0 or exit_price <= 0 or quantity <= 0:
                logger.warning("PnL回写参数无效，跳过", symbol=symbol, entry_price=entry_price, exit_price=exit_price, quantity=quantity)
                return

            # 计算 PnL
            direction_upper = "LONG" if direction == "long" else "SHORT"
            close_side = "SELL" if direction == "long" else "BUY"

            pnl = TradeLogger.calculate_pnl(
                direction=direction_upper,
                entry_price=Decimal(str(entry_price)),
                exit_price=Decimal(str(exit_price)),
                quantity=Decimal(str(quantity)),
            )

            # 获取 trade_logger 实例
            trade_logger = getattr(self.binance_client, 'trade_logger', None)
            if not trade_logger:
                logger.warning("trade_logger 未设置，跳过PnL回写", symbol=symbol)
                return

            order_id = str(close_order.get("orderId", "")) if close_order else ""

            await trade_logger.update_realized_pnl(
                order_id=order_id,
                realized_pnl=pnl,
                side=close_side,
                symbol=symbol,
                executed_at=datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None),
            )

            logger.info(
                "PnL回写成功",
                symbol=symbol,
                direction=direction,
                pnl=float(pnl),
                quantity=quantity,
                order_id=order_id or "fallback",
            )

        except Exception as e:
            logger.warning("PnL回写失败，不影响主流程", symbol=symbol, error=str(e))

    async def _get_actual_pnl_from_binance(
        self,
        symbol: str,
        entry_time: datetime,
    ) -> Optional[Decimal]:
        """
        从币安API获取该持仓的实际已实现盈亏

        查询该 symbol 从入场时间到当前时间的全部 REALIZED_PNL 记录，
        汇总得到实际 PnL。

        Args:
            symbol: 交易对
            entry_time: 入场时间（UTC datetime）

        Returns:
            实际 PnL 总和（USDT），API 失败或未找到记录返回 None
        """
        try:
            # 将 entry_time 转换为毫秒时间戳
            start_ms = int(entry_time.timestamp() * 1000)
            end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

            income_records = await self.binance_client.get_income_history(
                start_time=start_ms,
                end_time=end_ms,
                income_type="REALIZED_PNL",
                limit=1000,
            )

            # 过滤出该 symbol 的收入记录并求和
            total_pnl = Decimal("0")
            match_count = 0
            for record in income_records:
                if record.get("symbol") == symbol:
                    income_str = record.get("income", "0")
                    total_pnl += Decimal(str(income_str))
                    match_count += 1

            if match_count == 0:
                logger.warning("未找到该symbol的实际PnL记录", symbol=symbol)
                return None

            logger.info(
                "从币安获取实际PnL成功",
                symbol=symbol,
                total_pnl=float(total_pnl),
                match_count=match_count,
            )
            return total_pnl

        except Exception as e:
            logger.warning(
                "从币安获取实际PnL失败，将降级到理论计算",
                symbol=symbol,
                error=str(e),
            )
            return None

    async def _calculate_theoretical_total_pnl(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        entry_quantity: float,
        atr: float,
        current_price: float,
        pos: Dict[str, Any],
    ) -> Optional[Decimal]:
        """
        计算理论总 PnL（TP1、TP2、剩余部分合计）

        当无法从币安API获取实际 PnL 时，使用理论计算作为降级方案。
        合并 TP1、TP2、剩余部分为 1 个总值，只写入 1 次。

        Args:
            symbol: 交易对
            direction: 方向 ('short'/'long')
            entry_price: 入场价格
            entry_quantity: 初始入场数量
            atr: ATR 值
            current_price: 当前价格（用于剩余部分的出场价估算）
            pos: 持仓数据字典

        Returns:
            理论总 PnL（USDT），计算失败返回 None
        """
        try:
            if entry_price <= 0 or entry_quantity <= 0:
                return None

            target1_reached = pos.get("target1_reached", False)
            target2_reached = pos.get("target2_reached", False)
            direction_upper = "LONG" if direction == "long" else "SHORT"

            # 从 position_manager 获取 target 配置（已在初始化时从 config.yaml 读取）
            t1_pct = self.position_manager.target1_close_percent
            t2_pct = self.position_manager.target2_close_percent
            t1_mult = self.position_manager.target1_atr_multiplier
            t2_mult = self.position_manager.target2_atr_multiplier

            total_pnl = Decimal("0")
            remaining_qty = entry_quantity

            # TP1 部分
            if target1_reached:
                tp1_qty = entry_quantity * t1_pct
                if direction == "short":
                    tp1_price = entry_price - atr * t1_mult
                else:
                    tp1_price = entry_price + atr * t1_mult
                if tp1_price > 0 and tp1_qty > 0:
                    pnl = TradeLogger.calculate_pnl(
                        direction=direction_upper,
                        entry_price=Decimal(str(entry_price)),
                        exit_price=Decimal(str(tp1_price)),
                        quantity=Decimal(str(tp1_qty)),
                    )
                    total_pnl += pnl
                    remaining_qty -= tp1_qty

            # TP2 部分
            if target2_reached:
                tp2_qty = entry_quantity * t2_pct
                if direction == "short":
                    tp2_price = entry_price - atr * t2_mult
                else:
                    tp2_price = entry_price + atr * t2_mult
                if tp2_price > 0 and tp2_qty > 0:
                    pnl = TradeLogger.calculate_pnl(
                        direction=direction_upper,
                        entry_price=Decimal(str(entry_price)),
                        exit_price=Decimal(str(tp2_price)),
                        quantity=Decimal(str(tp2_qty)),
                    )
                    total_pnl += pnl
                    remaining_qty -= tp2_qty

            # 剩余部分（止损或手动平仓）
            qty_tolerance = getattr(self.position_manager, 'qty_tolerance_absolute', 0.0001)
            if remaining_qty > qty_tolerance and current_price > 0:
                pnl = TradeLogger.calculate_pnl(
                    direction=direction_upper,
                    entry_price=Decimal(str(entry_price)),
                    exit_price=Decimal(str(current_price)),
                    quantity=Decimal(str(remaining_qty)),
                )
                total_pnl += pnl

            logger.info(
                "理论PnL计算完成",
                symbol=symbol,
                direction=direction,
                total_pnl=float(total_pnl),
                target1_reached=target1_reached,
                target2_reached=target2_reached,
                remaining_qty=remaining_qty,
            )
            return total_pnl

        except Exception as e:
            logger.warning("理论PnL计算失败", symbol=symbol, error=str(e))
            return None

    async def _writeback_pnl_for_full_close(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        entry_quantity: float,
        atr: float,
        current_price: float,
        pos: Dict[str, Any],
    ) -> None:
        """
        全部平仓时回写完整 PnL（优先从币安API获取实际值，降级到理论计算）

        当 detect_take_profit_fills 检测到全部平仓（tp_result == 0）时，
        优先从币安API获取该持仓的实际已实现盈亏，写入 trade_records。
        API 失败时降级到理论计算（合并 TP1+TP2+剩余为 1 个总值，只写入 1 次）。

        Args:
            symbol: 交易对
            direction: 方向 ('short'/'long')
            entry_price: 入场价格
            entry_quantity: 初始入场数量
            atr: ATR 值
            current_price: 当前价格（用于剩余部分的出场价估算）
            pos: 持仓数据字典
        """
        try:
            if entry_price <= 0 or entry_quantity <= 0:
                return

            trade_logger = getattr(self.binance_client, 'trade_logger', None)
            if not trade_logger:
                return

            pnl_value: Optional[Decimal] = None
            pnl_source = "theoretical"

            # 1. 优先从币安 API 获取实际 PnL
            entry_time = pos.get("entry_time")
            if isinstance(entry_time, datetime):
                pnl_value = await self._get_actual_pnl_from_binance(symbol, entry_time)
                if pnl_value is not None:
                    pnl_source = "binance_api"

            # 2. 实际 PnL 获取失败，降级到理论计算
            if pnl_value is None:
                pnl_value = await self._calculate_theoretical_total_pnl(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    entry_quantity=entry_quantity,
                    atr=atr,
                    current_price=current_price,
                    pos=pos,
                )

            if pnl_value is None:
                logger.warning("PnL计算失败，无法回写", symbol=symbol)
                return

            # 3. 直接插入一条 PnL 汇总记录（条件单成交无 trade_records 可 UPDATE）
            close_side = "SELL" if direction == "long" else "BUY"
            success = await trade_logger.insert_pnl_summary(
                realized_pnl=pnl_value,
                symbol=symbol,
                side=close_side,
                executed_at=datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None),
            )

            if success:
                logger.info(
                    "全部平仓PnL回写成功",
                    symbol=symbol,
                    direction=direction,
                    pnl=float(pnl_value),
                    source=pnl_source,
                )
            else:
                logger.warning(
                    "全部平仓PnL回写失败",
                    symbol=symbol,
                    direction=direction,
                    pnl=float(pnl_value),
                    source=pnl_source,
                )

        except Exception as e:
            logger.warning("全部平仓PnL回写失败，不影响主流程", symbol=symbol, error=str(e))

    async def _monitor_positions(self) -> None:
        """监控持仓：移动止盈、时间止损"""
        positions = self.position_manager.get_all_positions()
        if not positions:
            return

        for symbol, pos in list(positions.items()):
            try:
                direction = pos.get("direction")
                entry_price = pos.get("entry_price", 0)
                entry_quantity = pos.get("entry_quantity", 0)
                atr = pos.get("atr", 0)

                # 获取当前价格（使用公开API）
                ticker = await self.binance_client.get_ticker(symbol)
                current_price = float(ticker.get("lastPrice", 0))

                # 更新最佳价格
                self.position_manager.update_best_price(symbol, current_price)

                # P1-6: 使用 detect_take_profit_fills() 检测止盈单成交
                try:
                    positions_data = await self.binance_client.get_position(symbol)
                    pos_amt = 0.0
                    for pos_data in positions_data:
                        pos_amt = abs(float(pos_data.get("positionAmt", 0)))
                        if pos_amt > 0:
                            break

                    # 调用 position_manager 的止盈检测方法
                    tp_result = self.position_manager.detect_take_profit_fills(symbol, pos_amt)

                    if tp_result == 0:
                        # 全部平仓——计算 TP1、TP2 和剩余部分的 PnL 并回写
                        await self._writeback_pnl_for_full_close(
                            symbol=symbol,
                            direction=direction,
                            entry_price=entry_price,
                            entry_quantity=entry_quantity,
                            atr=atr,
                            current_price=current_price,
                            pos=pos,
                        )
                        await self.position_manager.cancel_all_orders(symbol)
                        self.position_manager.remove_position(symbol)
                        self.risk_manager.record_profit()
                        await self._save_state()
                        if self._should_unregister(symbol):
                            await self._unregister_single_symbol(symbol)
                        continue  # 跳过后续检查
                    elif tp_result == 1:
                        # P1-6: TP1成交后，补充TP2订单（重启后可能丢失）
                        await self._replenish_single_position(symbol)
                except Exception as e:
                    logger.debug("获取持仓变化失败", symbol=symbol, error=str(e))

                # 检查时间止损
                if self.position_manager.check_time_stop(symbol):
                    order_result = await self.trading_executor.close_position(
                        symbol=symbol,
                        direction=direction,
                        close_percent=1.0,
                        reason="时间止损",
                    )
                    # 回写平仓盈亏
                    close_qty = entry_quantity  # 全仓平仓
                    await self._writeback_pnl(
                        symbol=symbol,
                        direction=direction,
                        entry_price=entry_price,
                        exit_price=current_price,
                        quantity=close_qty,
                        close_order=order_result,
                    )
                    # 取消条件单
                    await self.position_manager.cancel_all_orders(symbol)
                    self.position_manager.remove_position(symbol)
                    await self.risk_manager.record_loss(symbol, entry_price, current_price)
                    # 使用 calculate_pnl 统一计算盈亏（支持做多/做空方向）
                    pnl = TradeLogger.calculate_pnl(
                        direction="LONG" if direction == "long" else "SHORT",
                        entry_price=Decimal(str(entry_price)),
                        exit_price=Decimal(str(current_price)),
                        quantity=Decimal(str(entry_quantity)),
                    )
                    self._total_pnl += float(pnl)
                    await self._save_state()

                    # P2-4: 发送止损通知
                    await self._send_position_close_notification(
                        symbol, direction, "时间止损", current_price, entry_price
                    )

                    # 平仓后检查是否需要注销K线服务
                    if self._should_unregister(symbol):
                        await self._unregister_single_symbol(symbol)
                    continue

                # 检查移动止盈
                if self.position_manager.check_trailing_stop(symbol, current_price):
                    remaining_qty = pos.get("remaining_quantity", 0)
                    if remaining_qty > 0:
                        order_result = await self.trading_executor.close_position(
                            symbol=symbol,
                            direction=direction,
                            close_percent=1.0,
                            reason="移动止盈",
                        )
                        # 回写平仓盈亏（仅剩余部分）
                        await self._writeback_pnl(
                            symbol=symbol,
                            direction=direction,
                            entry_price=entry_price,
                            exit_price=current_price,
                            quantity=remaining_qty,
                            close_order=order_result,
                        )
                        await self.position_manager.cancel_all_orders(symbol)
                        self.position_manager.remove_position(symbol)
                        self.risk_manager.record_profit()
                        self._total_pnl += 0  # 盈利暂不累计到回撤计算
                        await self._save_state()

                        # P2-4: 发送止盈通知
                        await self._send_position_close_notification(
                            symbol, direction, "移动止盈", current_price, entry_price
                        )

                        # 平仓后检查是否需要注销K线服务
                        if self._should_unregister(symbol):
                            await self._unregister_single_symbol(symbol)
                        continue

                # 检查黑名单监控
                self.risk_manager.check_blacklist_monitor(symbol, current_price)

            except Exception as e:
                logger.error("监控持仓失败", symbol=symbol, error=str(e))

    async def _send_cycle_summary(
        self,
        short_signals: List[Dict],
        long_signals: List[Dict],
        active_symbols: List[str],
        short_candidates: List[str] = None,
        long_candidates: List[str] = None,
    ) -> None:
        """发送周期评分汇总通知"""
        # P2-4: 检查通知开关
        if not self._should_notify("cycle_summary"):
            return

        try:
            lines = ["【HRS策略周期汇总】"]
            lines.append(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC")
            lines.append(f"活跃候选币种: {len(active_symbols)}")
            if short_candidates:
                lines.append(f"做空候选 ({len(short_candidates)}): {', '.join(short_candidates)}")
            if long_candidates:
                lines.append(f"做多候选 ({len(long_candidates)}): {', '.join(long_candidates)}")
            lines.append(f"做空信号: {len(short_signals)}，做多信号: {len(long_signals)}")
            lines.append("")

            if short_signals:
                lines.append("--- 做空信号 ---")
                for s in short_signals:
                    lines.append(f"  {s['symbol']}: 评分 {s['score']:.1f}")

            if long_signals:
                lines.append("--- 做多信号 ---")
                for s in long_signals:
                    lines.append(f"  {s['symbol']}: 评分 {s['score']:.1f}")

            if not short_signals and not long_signals:
                lines.append("本轮无开仓信号")

            await self.notification_client.send(
                message="\n".join(lines),
                level="info",
                project="hrs",
            )
        except Exception as e:
            logger.warning("发送周期汇总失败", error=str(e))

    # ==================== V2.4: LV-RM 低波动反转模块 ====================

    # ==================== V2.5: LV-RM 独立运行 ====================

    async def _run_lv_rm_only_cycle(self) -> None:
        """
        V2.5: 候选池为空时的轻量级循环

        仅执行 LV-RM 扫描，跳过标准模式/EMM 评分。
        确保候选池为空时 LV-RM 仍然可以正常工作。
        """
        logger.info("候选池为空，运行 LV-RM 独立检查")
        lv_rm_config = self.config.get("lv_rm", {})
        if not lv_rm_config.get("enabled", True):
            logger.debug("LV-RM 未启用，跳过")
            return

        try:
            low_vol_symbols = await self.candidate_pool.get_lv_rm_scan_range()
            if low_vol_symbols:
                await self._check_lv_rm_entries(low_vol_symbols, [], [])
            else:
                logger.debug("LV-RM 无低波动候选币种")
        except Exception as e:
            logger.error("LV-RM 独立检查失败", error=str(e))

    async def _check_lv_rm_entries(
        self,
        low_vol_symbols: List[str],
        short_signals: List[Dict],
        long_signals: List[Dict],
    ) -> None:
        """
        V2.4: 检查LV-RM低波动反转信号

        对低波动候选币种执行LV-RM评分，检查入场条件。

        Args:
            low_vol_symbols: 低波动币种列表
            short_signals: 做空信号列表（标准模式，将被追加）
            long_signals: 做多信号列表（标准模式，将被追加）
        """
        lv_rm_config = self.config.get("lv_rm", {})
        scan_config = lv_rm_config.get("scan", {})
        concurrency_limit = scan_config.get("api_concurrency_limit", 10)

        logger.info(
            "开始LV-RM低波动反转检查",
            symbol_count=len(low_vol_symbols),
            concurrency_limit=concurrency_limit,
        )

        # 并发检查所有低波动币种
        semaphore = asyncio.Semaphore(concurrency_limit)

        async def check_symbol(symbol: str):
            async with semaphore:
                try:
                    # 已在标准模式中检查过的币种跳过
                    if symbol in self.candidate_pool.get_short_candidates() or \
                       symbol in self.candidate_pool.get_long_candidates():
                        return

                    # 检查熔断
                    if self.risk_manager.is_circuit_breaker_active(symbol):
                        return

                    # 获取K线
                    klines = self._klines_cache.get(symbol, [])
                    if len(klines) < self.min_klines:
                        klines = await self._warmup_klines(symbol)
                        if len(klines) < self.min_klines:
                            return
                        self._klines_cache[symbol] = klines

                    # 合成4h K线
                    klines_4h = self._klines_4h_cache.get(symbol, [])

                    current_close = float(klines[-1].get("close", 0))
                    current_open_time = klines[-1].get("open_time", 0)
                    current_close_time = current_open_time + KLINE_INTERVAL_MS.get(self.kline_interval, 3600000)

                    # 获取OI和资金费率
                    oi_usd = await self.market_data.get_oi_usd(symbol)
                    funding_rate = await self.market_data.get_funding_rate(symbol, at_time=current_close_time)
                    volume_24h = await self.market_data.get_24h_volume(symbol)
                    market_cap = await self.market_data.get_market_cap(symbol, oi_usd, volume_24h)
                    has_market_cap = market_cap > 0
                    oi_market_cap_ratio = oi_usd / market_cap if has_market_cap else 0.0

                    # LV-RM 评分（做空）
                    score = await self.scoring_engine.score_lv_rm(
                        symbol=symbol,
                        direction="short",
                        oi_market_cap_ratio=oi_market_cap_ratio,
                        funding_rate=funding_rate,
                        has_market_cap=has_market_cap,
                        klines_1h=klines,
                        klines_4h=klines_4h,
                    )
                    should_entry_short = self.scoring_engine.should_entry(score)

                    # LV-RM 评分（做多）
                    score_long = await self.scoring_engine.score_lv_rm(
                        symbol=symbol,
                        direction="long",
                        oi_market_cap_ratio=oi_market_cap_ratio,
                        funding_rate=funding_rate,
                        has_market_cap=has_market_cap,
                        klines_1h=klines,
                        klines_4h=klines_4h,
                    )
                    should_entry_long = self.scoring_engine.should_entry(score_long)

                    if should_entry_short:
                        # P2-6: 已有持仓时不产生新信号（不加仓）
                        if self.position_manager.has_position(symbol):
                            logger.debug("LV-RM: 该币种已有持仓，跳过做空信号", symbol=symbol)
                        else:
                            self._record_signal_timestamp(symbol, "short")
                            short_signals.append({
                                "symbol": symbol,
                                "direction": "short",
                                "score": score.total_score,
                                "technical_score": score.technical_score,
                                "sentiment_score": score.sentiment_score,
                                "current_price": current_close,
                                "klines": klines,
                                "entry_mode": "lv_rm",
                                "bb_position": score.bb_position,
                                "rsi_value": score.rsi_value,
                            })
                    else:
                        self._clear_signal_timestamp(symbol, "short")

                    if should_entry_long:
                        # P2-6: 已有持仓时不产生新信号（不加仓）
                        if self.position_manager.has_position(symbol):
                            logger.debug("LV-RM: 该币种已有持仓，跳过做多信号", symbol=symbol)
                        else:
                            self._record_signal_timestamp(symbol, "long")
                            long_signals.append({
                                "symbol": symbol,
                                "direction": "long",
                                "score": score_long.total_score,
                                "technical_score": score_long.technical_score,
                                "sentiment_score": score_long.sentiment_score,
                                "current_price": current_close,
                                "klines": klines,
                                "entry_mode": "lv_rm",
                                "bb_position": score_long.bb_position,
                                "rsi_value": score_long.rsi_value,
                            })
                    else:
                        self._clear_signal_timestamp(symbol, "long")

                except Exception as e:
                    logger.error("LV-RM分析失败", symbol=symbol, error=str(e))

        await asyncio.gather(*[check_symbol(s) for s in low_vol_symbols])

    async def _align_to_hour(self) -> None:
        """对齐到每小时第N分钟（从配置读取）"""
        now = datetime.now(timezone.utc)
        target_minute = self.align_minute
        next_run = now.replace(minute=target_minute, second=0, microsecond=0)
        if now.minute >= target_minute:
            next_run += timedelta(hours=1)
        wait_seconds = (next_run - now).total_seconds()
        if wait_seconds > 0:
            logger.info(f"对齐到每小时第{target_minute}分钟，等待 {wait_seconds:.0f} 秒")
            await asyncio.sleep(wait_seconds)

    async def _ensure_db_schema(self) -> None:
        """确保数据库schema和表结构存在"""
        try:
            await self.db.execute_ddl("CREATE SCHEMA IF NOT EXISTS hrs")

            await self.db.execute_ddl("""
                CREATE TABLE IF NOT EXISTS hrs.hrs_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at BIGINT
                )
            """)

            await self.db.execute_ddl("""
                CREATE TABLE IF NOT EXISTS hrs.hrs_positions (
                    symbol TEXT,
                    direction TEXT,
                    entry_price DOUBLE PRECISION,
                    quantity DOUBLE PRECISION,
                    entry_time BIGINT,
                    PRIMARY KEY (symbol, direction)
                )
            """)

            await self.db.execute_ddl("""
                CREATE TABLE IF NOT EXISTS hrs.hrs_orders (
                    order_id BIGINT PRIMARY KEY,
                    symbol TEXT,
                    type TEXT,
                    trigger_price DOUBLE PRECISION,
                    quantity DOUBLE PRECISION,
                    placed_time BIGINT
                )
            """)

            await self.db.execute_ddl("""
                CREATE TABLE IF NOT EXISTS hrs.hrs_blacklist (
                    symbol TEXT PRIMARY KEY,
                    reason TEXT,
                    blocked_at BIGINT
                )
            """)

            await self.db.execute_ddl("""
                CREATE TABLE IF NOT EXISTS hrs.hrs_state (
                    state_key TEXT PRIMARY KEY,
                    state_data JSONB,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # V1.9 新增：1h K线缓存表
            await self.db.execute_ddl("""
                CREATE TABLE IF NOT EXISTS hrs.hrs_klines_1h (
                    symbol TEXT NOT NULL,
                    open_time BIGINT NOT NULL,
                    open DOUBLE PRECISION,
                    high DOUBLE PRECISION,
                    low DOUBLE PRECISION,
                    close DOUBLE PRECISION,
                    volume DOUBLE PRECISION,
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (symbol, open_time)
                )
            """)

            # V1.9 新增：4h K线缓存表（由1h合成）
            await self.db.execute_ddl("""
                CREATE TABLE IF NOT EXISTS hrs.hrs_klines_4h (
                    symbol TEXT NOT NULL,
                    open_time BIGINT NOT NULL,
                    open DOUBLE PRECISION,
                    high DOUBLE PRECISION,
                    low DOUBLE PRECISION,
                    close DOUBLE PRECISION,
                    volume DOUBLE PRECISION,
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (symbol, open_time)
                )
            """)

            # V1.9 新增：活跃币种注册表
            await self.db.execute_ddl("""
                CREATE TABLE IF NOT EXISTS hrs.hrs_active_symbols (
                    symbol TEXT PRIMARY KEY,
                    registered_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # V2.0 新增：信号日志表（P2-1 独立表持久化）
            await self.db.execute_ddl("""
                CREATE TABLE IF NOT EXISTS hrs.hrs_signals (
                    id BIGSERIAL PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    total_score DOUBLE PRECISION,
                    technical_score DOUBLE PRECISION,
                    sentiment_score DOUBLE PRECISION,
                    contract_score DOUBLE PRECISION,
                    current_price DOUBLE PRECISION,
                    triggered_at TIMESTAMPTZ DEFAULT NOW(),
                    status TEXT DEFAULT 'triggered',
                    resolution TEXT,
                    resolved_at TIMESTAMPTZ
                )
            """)
            # 信号日志索引
            await self.db.execute_ddl("""
                CREATE INDEX IF NOT EXISTS idx_hrs_signals_symbol_time
                ON hrs.hrs_signals (symbol, triggered_at DESC)
            """)
            await self.db.execute_ddl("""
                CREATE INDEX IF NOT EXISTS idx_hrs_signals_status
                ON hrs.hrs_signals (status)
            """)

            # V2.4: LV-RM 字段
            await self.db.execute_ddl("""
                ALTER TABLE hrs.hrs_signals
                ADD COLUMN IF NOT EXISTS bb_position DOUBLE PRECISION
            """)
            await self.db.execute_ddl("""
                ALTER TABLE hrs.hrs_signals
                ADD COLUMN IF NOT EXISTS rsi_value DOUBLE PRECISION
            """)

            logger.info("数据库Schema初始化完成")
        except Exception as e:
            logger.error("数据库Schema初始化失败", error=str(e))

    async def _restore_state(self) -> None:
        """
        从数据库恢复策略状态（P2-1: 优先从独立表恢复，JSONB 作为兜底）

        恢复顺序：
        1. 从 hrs_positions 恢复持仓
        2. 从 hrs_blacklist 恢复黑名单
        3. 从 hrs_active_symbols 恢复活跃币种
        4. 从 hrs_klines_1h / hrs_klines_4h 恢复 K 线缓存
        5. 从 hrs_meta 恢复风控状态
        6. 以上任一独立表为空时，回退到 hrs_state JSONB 兜底
        """
        try:
            restored_from_independent = False

            # 尝试从独立表恢复
            positions_rows = await self.db.fetch_all(
                "SELECT symbol, direction, entry_price, quantity, entry_time FROM hrs.hrs_positions"
            )
            blacklist_rows = await self.db.fetch_all(
                "SELECT symbol, reason FROM hrs.hrs_blacklist"
            )
            active_symbols_rows = await self.db.fetch_all(
                "SELECT symbol FROM hrs.hrs_active_symbols"
            )

            if positions_rows or blacklist_rows or active_symbols_rows:
                restored_from_independent = True
                logger.info("从独立表恢复状态")

                # 恢复持仓
                for row in positions_rows:
                    self.position_manager.add_position(
                        symbol=row["symbol"],
                        direction=row["direction"],
                        entry_price=float(row["entry_price"]),
                        quantity=float(row["quantity"]),
                        atr=0.0,  # ATR 后续分析时更新
                    )
                    # 覆盖 entry_time
                    pos = self.position_manager.get_position(row["symbol"])
                    if pos and row.get("entry_time"):
                        et = row["entry_time"]
                        if isinstance(et, int) and et > 0:
                            pos["entry_time"] = datetime.fromtimestamp(et / 1000, tz=timezone.utc)

                # 恢复黑名单
                blacklist_symbols = set()
                for row in blacklist_rows:
                    blacklist_symbols.add(row["symbol"])
                self.risk_manager.blacklist = blacklist_symbols

                # 恢复活跃币种
                self._registered_symbols = set()
                for row in active_symbols_rows:
                    self._registered_symbols.add(row["symbol"])

                # 恢复风控元数据
                meta_rows = await self.db.fetch_all(
                    "SELECT key, value FROM hrs.hrs_meta WHERE key LIKE $1",
                    "risk.%",
                )
                risk_data = {}
                for row in meta_rows:
                    key = row["key"].replace("risk.", "")
                    risk_data[key] = row["value"]
                if risk_data:
                    # 将元数据转换为 risk_manager 可识别的格式
                    rm_data = {
                        "consecutive_losses": int(risk_data.get("consecutive_losses", 0)),
                        "pause_until": risk_data.get("pause_until") or None,
                        "drawdown_pause_until": risk_data.get("drawdown_pause_until") or None,
                        "blacklist": list(blacklist_symbols),
                        "daily_open_count": json.loads(risk_data.get("daily_open_count", '{"short": 0, "long": 0}')),
                        "stop_loss_monitor": json.loads(risk_data.get("stop_loss_monitor", "{}")),
                        # P2-6: 恢复熔断状态
                        "circuit_breakers": json.loads(risk_data.get("circuit_breakers", "{}")),
                    }
                    self.risk_manager.from_dict(rm_data)

                    # P2-3: 恢复加仓计数
                    try:
                        self._add_position_count = json.loads(risk_data.get("add_position_count", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        self._add_position_count = {}

                    # P2-5: 恢复暂停状态
                    paused_str = risk_data.get("paused", "False")
                    self._paused = paused_str.lower() == "true"

                # 恢复 K 线缓存
                await self._restore_klines_from_tables()

                logger.info(
                    "独立表状态恢复完成",
                    positions=len(positions_rows),
                    blacklist=len(blacklist_symbols),
                    registered_symbols=len(self._registered_symbols),
                )

            # 独立表数据不足，回退到 JSONB 兜底
            if not restored_from_independent:
                state = await self.db.fetch_one(
                    "SELECT state_data FROM hrs.hrs_state WHERE state_key = $1",
                    "main",
                )
                if state:
                    data = state.get("state_data", {})
                    if isinstance(data, str):
                        data = json.loads(data)

                    self.risk_manager.from_dict(data.get("risk", {}))
                    self.position_manager.from_dict(data.get("positions", {}))
                    self._registered_symbols = set(data.get("registered_symbols", []))
                    self._klines_cache = data.get("klines_cache", {})
                    self._klines_4h_cache = data.get("klines_4h_cache", {})
                    # P2-3: 恢复加仓计数
                    self._add_position_count = data.get("add_position_count", {})
                    # P2-5: 恢复暂停状态
                    self._paused = data.get("paused", False)

                    logger.info(
                        "JSONB状态恢复完成（兜底）",
                        positions=len(self.position_manager.get_all_positions()),
                        blacklist=len(self.risk_manager.blacklist),
                        registered_symbols=len(self._registered_symbols),
                    )

            # 恢复后重新注册K线服务
            if self._registered_symbols:
                await self._reregister_klines_services()

            # P0-1: 启动时与交易所持仓对账
            await self._reconcile_positions()

        except Exception as e:
            logger.error("状态恢复失败", error=str(e))
            # 最终兜底：尝试 JSONB
            await self._restore_state_from_jsonb_fallback()

    async def _restore_klines_from_tables(self) -> None:
        """
        P2-1: 从独立表恢复 K 线缓存

        从 hrs_klines_1h 和 hrs_klines_4h 恢复缓存的 K 线数据。
        """
        try:
            kline_config = self.config.get("kline", {})
            keep_count = kline_config.get("keep_count", 168)
            synthetic_count = kline_config.get("synthetic_4h_count", 50)

            # 恢复 1h K 线
            klines_1h_rows = await self.db.fetch_all(
                "SELECT symbol, open_time, open, high, low, close, volume "
                "FROM hrs.hrs_klines_1h ORDER BY symbol, open_time ASC"
            )
            self._klines_cache = {}
            for row in klines_1h_rows:
                symbol = row["symbol"]
                if symbol not in self._klines_cache:
                    self._klines_cache[symbol] = []
                self._klines_cache[symbol].append({
                    "open_time": row["open_time"],
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                })
            # 裁剪到 keep_count
            for symbol in self._klines_cache:
                self._klines_cache[symbol] = self._klines_cache[symbol][-keep_count:]

            # 恢复 4h K 线
            klines_4h_rows = await self.db.fetch_all(
                "SELECT symbol, open_time, open, high, low, close, volume "
                "FROM hrs.hrs_klines_4h ORDER BY symbol, open_time ASC"
            )
            self._klines_4h_cache = {}
            for row in klines_4h_rows:
                symbol = row["symbol"]
                if symbol not in self._klines_4h_cache:
                    self._klines_4h_cache[symbol] = []
                self._klines_4h_cache[symbol].append({
                    "open_time": row["open_time"],
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                })
            # 裁剪到 synthetic_count
            for symbol in self._klines_4h_cache:
                self._klines_4h_cache[symbol] = self._klines_4h_cache[symbol][-synthetic_count:]

            if self._klines_cache:
                logger.info(
                    "K线缓存从独立表恢复完成",
                    symbols_1h=len(self._klines_cache),
                    symbols_4h=len(self._klines_4h_cache),
                )
        except Exception as e:
            logger.warning("从独立表恢复K线缓存失败，将使用JSONB兜底", error=str(e))

    async def _restore_state_from_jsonb_fallback(self) -> None:
        """
        P2-1: JSONB 最终兜底恢复

        在所有独立表恢复失败后尝试从 JSONB 恢复。
        """
        try:
            state = await self.db.fetch_one(
                "SELECT state_data FROM hrs.hrs_state WHERE state_key = $1",
                "main",
            )
            if state:
                data = state.get("state_data", {})
                if isinstance(data, str):
                    data = json.loads(data)

                self.risk_manager.from_dict(data.get("risk", {}))
                self.position_manager.from_dict(data.get("positions", {}))
                self._registered_symbols = set(data.get("registered_symbols", []))
                self._klines_cache = data.get("klines_cache", {})
                self._klines_4h_cache = data.get("klines_4h_cache", {})
                # P2-3: 恢复加仓计数
                self._add_position_count = data.get("add_position_count", {})
                # P2-5: 恢复暂停状态
                self._paused = data.get("paused", False)
                logger.info("JSONB兜底恢复完成")
        except Exception as e:
            logger.warning("JSONB兜底恢复也失败，从头开始", error=str(e))

    async def _reconcile_positions(self) -> None:
        """
        P0-1: 启动时与交易所持仓对账

        对比交易所实际持仓与本地恢复的持仓状态：
        - 以交易所为准，交易所有持仓而本地无 → 重建本地记录
        - 本地有而交易所无 → 清理本地记录
        - 差异记录告警日志
        """
        try:
            # 获取交易所所有持仓
            exchange_positions = await self.binance_client.get_position()
            if not exchange_positions:
                logger.info("交易所无持仓，跳过对账")
                return

            # 构建交易所持仓映射：{symbol: {"direction": str, "positionAmt": float, "entryPrice": float}}
            exchange_pos_map: Dict[str, Dict[str, Any]] = {}
            for pos in exchange_positions:
                pos_amt = abs(float(pos.get("positionAmt", 0)))
                if pos_amt < 0.0001:
                    continue  # 忽略零持仓
                symbol = pos.get("symbol", "")
                amt = float(pos.get("positionAmt", 0))
                direction = "short" if amt < 0 else "long"
                exchange_pos_map[symbol] = {
                    "direction": direction,
                    "position_amt": pos_amt,
                    "entry_price": float(pos.get("entryPrice", 0)),
                }

            # 获取本地持仓
            local_positions = self.position_manager.get_all_positions()

            # 交易所有而本地无 → 仅记录告警，不自动接管
            for symbol, exch_pos in exchange_pos_map.items():
                if symbol not in local_positions:
                    logger.warning(
                        "发现非本策略持仓，不自动接管（请手动确认后处理）",
                        symbol=symbol,
                        direction=exch_pos["direction"],
                        position_amt=exch_pos["position_amt"],
                        entry_price=exch_pos["entry_price"],
                        hint="如需接管，请重启策略或手动添加持仓记录",
                    )
                    # 不再调用 add_position() 和 _register_and_warmup()

            # 本地有而交易所无 → 清理本地记录
            for symbol in list(local_positions.keys()):
                if symbol not in exchange_pos_map:
                    logger.warning(
                        "本地持仓交易所已不存在，清理记录",
                        symbol=symbol,
                        direction=local_positions[symbol].get("direction"),
                    )
                    self.position_manager.remove_position(symbol)
                    # 清理K线服务
                    if self._should_unregister(symbol):
                        await self._unregister_single_symbol(symbol)

            # 差异汇总
            local_only = set(local_positions.keys()) - set(exchange_pos_map.keys())
            exchange_only = set(exchange_pos_map.keys()) - set(local_positions.keys())
            if local_only or exchange_only:
                logger.info(
                    "持仓对账完成",
                    local_positions=len(local_positions),
                    exchange_positions=len(exchange_pos_map),
                    local_only=list(local_only),
                    exchange_only=list(exchange_only),
                )
            else:
                logger.info(
                    "持仓对账完成，无差异",
                    positions=len(local_positions),
                )

        except Exception as e:
            logger.error("持仓对账失败", error=str(e))

    async def _reregister_klines_services(self) -> None:
        """
        恢复状态后重新注册K线服务并等待K线预热

        遍历已恢复的 registered_symbols，重新注册到K线服务。
        注册后检查每个币种的1h K线数量是否达到 min_bars，
        不足则等待直到超时。

        P0-2: 启动时K线预热等待，确保分析时有足够数据。
        """
        if not self._registered_symbols:
            return

        logger.info("开始重新注册K线服务", count=len(self._registered_symbols))
        reregistered = 0
        for symbol in list(self._registered_symbols):
            try:
                registered = await self.kline_service.register_symbol(
                    symbol, intervals=[self.kline_interval]
                )
                if registered:
                    reregistered += 1
                    logger.debug("K线服务重新注册成功", symbol=symbol)
            except Exception as e:
                logger.warning("K线服务重新注册失败", symbol=symbol, error=str(e))

        logger.info("K线服务重新注册完成", success=reregistered, total=len(self._registered_symbols))

        # P0-2: 等待K线预热
        await self._wait_klines_warmup()

    async def _wait_klines_warmup(self) -> None:
        """
        P0-2: 启动时K线预热等待

        检查每个已注册交易对的1h K线数量是否 >= warmup_min_bars，
        不足则等待，每 warmup_check_interval_seconds 秒检查一次，
        最多等待 warmup_max_wait_seconds 秒。
        超时则告警但仍启动策略。
        """
        if not self._registered_symbols:
            return

        warmup_start = datetime.now(timezone.utc)
        logger.info(
            "开始K线预热等待",
            symbols=len(self._registered_symbols),
            min_bars=self.warmup_min_bars,
            max_wait=self.warmup_max_wait_seconds,
        )

        while True:
            all_ready = True
            for symbol in list(self._registered_symbols):
                klines = self._klines_cache.get(symbol, [])
                if len(klines) < self.warmup_min_bars:
                    # 尝试从K线服务获取最新数据
                    try:
                        fresh_klines = await self.kline_service.get_klines(
                            symbol=symbol,
                            interval=self.kline_interval,
                            limit=self.keep_count,
                        )
                        if fresh_klines:
                            self._klines_cache[symbol] = fresh_klines
                            klines = fresh_klines
                    except Exception as e:
                        logger.debug("K线预热获取失败", symbol=symbol, error=str(e))

                    if len(klines) < self.warmup_min_bars:
                        all_ready = False
                        logger.debug(
                            "K线预热中",
                            symbol=symbol,
                            current=len(klines),
                            required=self.warmup_min_bars,
                        )

            if all_ready:
                elapsed = (datetime.now(timezone.utc) - warmup_start).total_seconds()
                logger.info("K线预热完成", elapsed_seconds=elapsed)
                return

            # 检查是否超时
            elapsed = (datetime.now(timezone.utc) - warmup_start).total_seconds()
            if elapsed >= self.warmup_max_wait_seconds:
                # 统计未达标的币种
                insufficient = []
                for symbol in list(self._registered_symbols):
                    klines = self._klines_cache.get(symbol, [])
                    if len(klines) < self.warmup_min_bars:
                        insufficient.append(symbol)

                logger.warning(
                    "K线预热超时，继续启动策略",
                    elapsed_seconds=elapsed,
                    max_wait=self.warmup_max_wait_seconds,
                    insufficient_symbols=insufficient,
                )
                return

            await asyncio.sleep(self.warmup_check_interval_seconds)

    async def _save_state(self) -> None:
        """
        保存策略状态到数据库（P2-1: 独立表持久化）

        将数据分别写入独立表（hrs_meta、hrs_positions、hrs_blacklist、
        hrs_active_symbols、hrs_klines_1h、hrs_klines_4h），
        同时保留 hrs_state JSONB 作为向后兼容备份。
        """
        try:
            now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)

            # 1. 保存风控状态到 hrs_meta
            risk_data = self.risk_manager.to_dict()
            risk_meta = {
                "consecutive_losses": str(risk_data.get("consecutive_losses", 0)),
                "pause_until": risk_data.get("pause_until") or "",
                "drawdown_pause_until": risk_data.get("drawdown_pause_until") or "",
                "daily_open_count": json.dumps(risk_data.get("daily_open_count", {})),
                "stop_loss_monitor": json.dumps(risk_data.get("stop_loss_monitor", {})),
                # P2-3: 持久化加仓计数
                "add_position_count": json.dumps(self._add_position_count),
                # P2-5: 持久化暂停状态
                "paused": str(self._paused),
                # P2-6: 持久化熔断状态
                "circuit_breakers": json.dumps(risk_data.get("circuit_breakers", {})),
            }
            for key, value in risk_meta.items():
                await self.db.execute(
                    """
                    INSERT INTO hrs.hrs_meta (key, value, updated_at)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = $3
                    """,
                    f"risk.{key}", value, now_ts,
                )

            # 2. 保存黑名单到 hrs_blacklist
            await self.db.execute("DELETE FROM hrs.hrs_blacklist")
            for symbol in self.risk_manager.blacklist:
                await self.db.execute(
                    """
                    INSERT INTO hrs.hrs_blacklist (symbol, reason, blocked_at)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (symbol) DO UPDATE SET reason = $2, blocked_at = $3
                    """,
                    symbol, "策略运行时恢复", now_ts,
                )

            # 3. 保存持仓到 hrs_positions
            await self.db.execute("DELETE FROM hrs.hrs_positions")
            for symbol, pos in self.position_manager.get_all_positions().items():
                entry_time = pos.get("entry_time")
                if isinstance(entry_time, datetime):
                    entry_time_ts = int(entry_time.timestamp() * 1000)
                elif isinstance(entry_time, str):
                    entry_time_ts = int(datetime.fromisoformat(entry_time).timestamp() * 1000)
                else:
                    entry_time_ts = now_ts
                await self.db.execute(
                    """
                    INSERT INTO hrs.hrs_positions (symbol, direction, entry_price, quantity, entry_time)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (symbol, direction) DO UPDATE
                    SET entry_price = $3, quantity = $4, entry_time = $5
                    """,
                    symbol,
                    pos.get("direction", ""),
                    pos.get("entry_price", 0),
                    pos.get("entry_quantity", 0),
                    entry_time_ts,
                )

            # 4. 保存活跃币种到 hrs_active_symbols
            await self.db.execute("DELETE FROM hrs.hrs_active_symbols")
            for symbol in self._registered_symbols:
                await self.db.execute(
                    """
                    INSERT INTO hrs.hrs_active_symbols (symbol, registered_at, updated_at)
                    VALUES ($1, NOW(), NOW())
                    ON CONFLICT (symbol) DO UPDATE SET updated_at = NOW()
                    """,
                    symbol,
                )

            # 5. 保存1h K线到 hrs_klines_1h（批量 UPSERT）
            kline_config = self.config.get("kline", {})
            for symbol, klines in self._klines_cache.items():
                trimmed = klines[-self.keep_count:]
                for k in trimmed:
                    await self.db.execute(
                        """
                        INSERT INTO hrs.hrs_klines_1h (symbol, open_time, open, high, low, close, volume, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                        ON CONFLICT (symbol, open_time) DO UPDATE
                        SET open = $3, high = $4, low = $5, close = $6, volume = $7, updated_at = NOW()
                        """,
                        symbol,
                        k.get("open_time", 0),
                        k.get("open", 0),
                        k.get("high", 0),
                        k.get("low", 0),
                        k.get("close", 0),
                        k.get("volume", 0),
                    )

            # 6. 保存4h K线到 hrs_klines_4h（批量 UPSERT）
            synthetic_count = kline_config.get("synthetic_4h_count", 50)
            for symbol, klines_4h in self._klines_4h_cache.items():
                trimmed_4h = klines_4h[-synthetic_count:]
                for k in trimmed_4h:
                    await self.db.execute(
                        """
                        INSERT INTO hrs.hrs_klines_4h (symbol, open_time, open, high, low, close, volume, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                        ON CONFLICT (symbol, open_time) DO UPDATE
                        SET open = $3, high = $4, low = $5, close = $6, volume = $7, updated_at = NOW()
                        """,
                        symbol,
                        k.get("open_time", 0),
                        k.get("open", 0),
                        k.get("high", 0),
                        k.get("low", 0),
                        k.get("close", 0),
                        k.get("volume", 0),
                    )

            # 7. 保留 JSONB 备份（向后兼容）
            trimmed_klines = {}
            for symbol, klines in self._klines_cache.items():
                trimmed_klines[symbol] = klines[-self.keep_count:]
            trimmed_klines_4h = {}
            for symbol, klines_4h in self._klines_4h_cache.items():
                trimmed_klines_4h[symbol] = klines_4h[-synthetic_count:]

            state_data = {
                "risk": self.risk_manager.to_dict(),
                "positions": self.position_manager.to_dict(),
                "registered_symbols": list(self._registered_symbols),
                "klines_cache": trimmed_klines,
                "klines_4h_cache": trimmed_klines_4h,
                "active_symbols": list(self._registered_symbols),
                "last_update": datetime.now(timezone.utc).isoformat(),
                # P2-3: 加仓计数
                "add_position_count": self._add_position_count,
                # P2-5: 暂停状态
                "paused": self._paused,
            }

            await self.db.execute(
                """
                INSERT INTO hrs.hrs_state (state_key, state_data, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (state_key)
                DO UPDATE SET state_data = $2, updated_at = NOW()
                """,
                "main",
                json.dumps(state_data, cls=DecimalEncoder),
            )

            logger.debug("状态已保存（独立表+JSONB备份）")
        except Exception as e:
            logger.error("状态保存失败", error=str(e))

    async def _log_signal_to_db(
        self,
        symbol: str,
        direction: str,
        signal: Dict[str, Any],
        resolution: Optional[str] = None,
    ) -> None:
        """
        P2-1: 将信号记录持久化到 hrs_signals 表

        Args:
            symbol: 交易对
            direction: 方向 ('short' 或 'long')
            signal: 信号字典，包含 score, technical_score, sentiment_score, current_price 等
            resolution: 结果状态，如 'executed', 'abandoned', 'timeout' 等
        """
        try:
            total_score = signal.get("score", 0)
            technical_score = signal.get("technical_score", 0)
            sentiment_score = signal.get("sentiment_score", 0)
            current_price = signal.get("current_price", 0)
            # V2.4: LV-RM 字段
            bb_position = signal.get("bb_position")
            rsi_value = signal.get("rsi_value")

            # 合约数据评分从 total_score 反推（近似）
            scoring_config = self.config.get("scoring", {})
            weights = scoring_config.get("weights", {})
            contract_weight = weights.get("contract", 0.25)
            technical_weight = weights.get("technical", 0.45)
            sentiment_weight = weights.get("sentiment", 0.30)

            # 近似计算合约数据评分
            if technical_weight + sentiment_weight > 0:
                contract_score = (
                    total_score - technical_score * technical_weight - sentiment_score * sentiment_weight
                ) / contract_weight if contract_weight > 0 else 0
            else:
                contract_score = 0

            await self.db.execute(
                """
                INSERT INTO hrs.hrs_signals
                    (symbol, direction, total_score, technical_score, sentiment_score,
                     contract_score, current_price, status, resolution,
                     bb_position, rsi_value)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'triggered', $8, $9, $10)
                """,
                symbol,
                direction,
                round(total_score, 2),
                round(technical_score, 2),
                round(sentiment_score, 2),
                round(contract_score, 2),
                round(current_price, 6),
                resolution,
                bb_position,
                rsi_value,
            )
            logger.debug("信号已记录到数据库", symbol=symbol, direction=direction, score=total_score)
        except Exception as e:
            logger.warning("信号日志持久化失败", symbol=symbol, error=str(e))

    async def _update_signal_resolution(
        self,
        symbol: str,
        direction: str,
        resolution: str,
    ) -> None:
        """
        P2-1: 更新信号的处理结果

        Args:
            symbol: 交易对
            direction: 方向
            resolution: 结果状态 ('executed', 'abandoned', 'timeout', 'conflict')
        """
        try:
            await self.db.execute(
                """
                UPDATE hrs.hrs_signals
                SET status = 'resolved', resolution = $3, resolved_at = NOW()
                WHERE symbol = $1 AND direction = $2 AND status = 'triggered'
                """,
                symbol, direction, resolution,
            )
        except Exception as e:
            logger.warning("信号结果更新失败", symbol=symbol, error=str(e))

    # ==================== P1-1: 信号超时跟踪 ====================

    def _record_signal_timestamp(self, symbol: str, direction: str) -> None:
        """
        P1-1: 记录信号首次触发时间

        只在首次触发时记录，后续重复触发不更新。

        Args:
            symbol: 交易对
            direction: 方向 ('short' 或 'long')
        """
        if symbol not in self._signal_timestamps:
            self._signal_timestamps[symbol] = {}
        if direction not in self._signal_timestamps[symbol]:
            self._signal_timestamps[symbol][direction] = datetime.now(timezone.utc).isoformat()
            logger.debug("信号首次触发", symbol=symbol, direction=direction)

    def _clear_signal_timestamp(self, symbol: str, direction: str) -> None:
        """
        P1-1: 清理信号时间戳（信号消失时调用）

        Args:
            symbol: 交易对
            direction: 方向 ('short' 或 'long')
        """
        if symbol in self._signal_timestamps:
            removed = self._signal_timestamps[symbol].pop(direction, None)
            if removed:
                logger.debug("信号消失，清理时间戳", symbol=symbol, direction=direction)
            if not self._signal_timestamps[symbol]:
                del self._signal_timestamps[symbol]

    async def _filter_timeout_signals(
        self,
        signals: List[Dict],
        direction: str,
    ) -> List[Dict]:
        """
        P1-1: 过滤超时信号

        检查信号是否超过 timeout_bars 根K线未入场：
        - 若超时，标记信号为过期并放弃
        - 未超时的信号保留

        Args:
            signals: 信号列表
            direction: 方向 ('short' 或 'long')

        Returns:
            过滤后的信号列表
        """
        filtered = []
        now = datetime.now(timezone.utc)
        for signal in signals:
            symbol = signal["symbol"]
            ts_data = self._signal_timestamps.get(symbol, {}).get(direction)
            if ts_data:
                try:
                    first_trigger = datetime.fromisoformat(ts_data)
                    elapsed_hours = (now - first_trigger).total_seconds() / 3600
                    if elapsed_hours >= self.abandon_timeout_bars:
                        logger.info(
                            "信号超时放弃",
                            symbol=symbol,
                            direction=direction,
                            elapsed_hours=round(elapsed_hours, 1),
                            timeout_bars=self.abandon_timeout_bars,
                        )
                        # P2-1: 更新信号结果为 timeout
                        await self._update_signal_resolution(symbol, direction, "timeout")
                        self._clear_signal_timestamp(symbol, direction)
                        continue
                except (ValueError, TypeError):
                    pass
            filtered.append(signal)
        return filtered

    # ==================== P1-2: 流动性降级放弃 ====================

    async def _check_oi_before_entry(self, symbol: str, direction: str) -> bool:
        """
        P1-2: 进场前OI二次确认

        获取最新OI，若低于流动性门槛，放弃进场。

        Args:
            symbol: 交易对
            direction: 方向 ('short' 或 'long')

        Returns:
            True: 检查通过，可以进场；False: 放弃进场
        """
        try:
            oi_usd = await self.market_data.get_oi_usd(symbol)
            pool_config = self.config.get("candidate_pool", {})
            liquidity_config = pool_config.get("liquidity", {})
            min_oi = liquidity_config.get("min_oi_usd", 10000000)

            if oi_usd < min_oi:
                logger.warning(
                    "流动性降级，OI低于门槛，放弃进场",
                    symbol=symbol,
                    direction=direction,
                    oi_usd=oi_usd,
                    min_oi=min_oi,
                )
                return False

            logger.debug("OI二次确认通过", symbol=symbol, oi_usd=oi_usd)
            return True
        except Exception as e:
            logger.warning("OI二次确认失败，允许进场", symbol=symbol, error=str(e))
            return True  # 获取失败时允许进场，避免因网络问题错过机会

    # ==================== P1-3: 资金费率逆转放弃 ====================

    async def _check_funding_rate_before_entry(self, symbol: str, direction: str) -> bool:
        """
        P1-3: 进场前资金费率方向检查

        - 做空信号：若最新费率为负（多头拥挤消退）→ 放弃
        - 做多信号：若最新费率为正（空头拥挤消退）→ 放弃

        Args:
            symbol: 交易对
            direction: 方向 ('short' 或 'long')

        Returns:
            True: 检查通过，可以进场；False: 放弃进场
        """
        try:
            # 获取最新资金费率
            funding_rate = await self.market_data.get_funding_rate(symbol)
            threshold = self.abandon_funding_rate_reversal_threshold

            if direction == "short" and funding_rate <= threshold:
                logger.warning(
                    "资金费率逆转，做空信号放弃",
                    symbol=symbol,
                    funding_rate=funding_rate,
                    threshold=threshold,
                )
                return False
            elif direction == "long" and funding_rate >= threshold:
                logger.warning(
                    "资金费率逆转，做多信号放弃",
                    symbol=symbol,
                    funding_rate=funding_rate,
                    threshold=threshold,
                )
                return False

            logger.debug("资金费率方向检查通过", symbol=symbol, direction=direction, funding_rate=funding_rate)
            return True
        except Exception as e:
            logger.warning("资金费率检查失败，允许进场", symbol=symbol, error=str(e))
            return True  # 获取失败时允许进场

    # ==================== P1-4: 启动时恢复未触发订单 ====================

    async def _restore_orders(self) -> None:
        """
        P1-4: 启动时恢复未触发订单（已废弃API）

        条件单查询API（/papi/v1/um/algo/openOrders）已废弃，
        不再通过交易所查询未触发条件单。
        订单恢复由 _replenish_all_positions 在启动后通过本地
        algoId 跟踪重新为持仓设置止损止盈条件单。
        """
        logger.info("条件单查询API已废弃，跳过启动恢复，由_replenish_all_positions重新下单")

    async def _replenish_all_positions(self) -> None:
        """启动后为所有持仓补充缺失的止损止盈条件单"""
        positions = self.position_manager.get_all_positions()
        if not positions:
            logger.info("无持仓需要补单")
            return

        logger.info("开始补充持仓保护订单", count=len(positions))
        for symbol, pos in positions.items():
            await self._replenish_single_position(symbol)
        logger.info("持仓保护订单补充完成")

    async def _replenish_single_position(self, symbol: str) -> None:
        """为单个持仓补充缺失的止损止盈条件单"""
        try:
            pos = self.position_manager.get_position(symbol)
            if not pos:
                return

            direction = pos.get("direction", "")
            entry_price = pos.get("entry_price", 0)
            entry_quantity = pos.get("entry_quantity", 0)
            atr = pos.get("atr", 0)
            target1_reached = pos.get("target1_reached", False)
            target2_reached = pos.get("target2_reached", False)

            if entry_price <= 0 or entry_quantity <= 0:
                logger.warning("持仓数据无效，跳过补单", symbol=symbol)
                return

            # 补单前检查交易所实际持仓量，更新 target1_reached 状态
            # 避免重启后 TP1 已成交但数据库状态未同步
            try:
                positions_data = await self.binance_client.get_position(symbol)
                for pos_data in positions_data:
                    exchange_qty = abs(float(pos_data.get("positionAmt", 0)))
                    if exchange_qty > 0:
                        # 如果交易所持仓量明显小于初始开仓量(>10%差异)，说明 TP1 已部分成交
                        if exchange_qty < entry_quantity * 0.9:
                            target1_reached = True
                            pos["target1_reached"] = True
                            logger.info(
                                "检测到TP1已成交，更新状态",
                                symbol=symbol,
                                entry_quantity=entry_quantity,
                                exchange_qty=exchange_qty,
                            )
                        break
            except Exception as e:
                logger.debug("获取交易所持仓量失败", symbol=symbol, error=str(e))

            # ATR为0时尝试重新计算
            if atr <= 0:
                klines = self._klines_cache.get(symbol, [])
                if len(klines) >= 15:
                    atr = await self.trading_executor.calculate_atr(symbol, klines)
                    if atr > 0:
                        pos["atr"] = atr

            # 在补单前，先取消该币种所有已跟踪的条件单，避免重复累积
            try:
                await self.position_manager.cancel_all_orders(symbol)
            except Exception as e:
                logger.warning("取消旧条件单失败", symbol=symbol, error=str(e))

            result = await self.trading_executor.replenish_position_orders(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                entry_quantity=entry_quantity,
                atr=atr,
                target1_reached=target1_reached,
                target2_reached=target2_reached,
            )

            # 价格已过TP2目标，标记target2_reached以激活移动止盈
            if result == "price_past_tp2":
                pos["target2_reached"] = True
                logger.info("价格已过TP2目标，已标记target2_reached，激活移动止盈", symbol=symbol)

        except Exception as e:
            logger.warning("补充持仓保护订单失败", symbol=symbol, error=str(e))

    # ==================== P2-3: 加仓逻辑 ====================

    async def _handle_add_position(
        self,
        symbol: str,
        direction: str,
        score: float,
        current_price: float,
        klines: List[Dict],
    ) -> bool:
        """
        P2-3: 处理加仓

        检查加仓条件：
        1. 持仓盈利超过加仓阈值
        2. 加仓次数未达上限
        3. 加仓数量不超过初始仓位的 size_ratio

        Args:
            symbol: 交易对
            direction: 方向
            score: 评分
            current_price: 当前价格
            klines: K线数据

        Returns:
            True: 加仓成功；False: 不满足条件或失败
        """
        pos = self.position_manager.get_position(symbol)
        if not pos:
            return False

        # 检查方向是否一致
        if pos.get("direction") != direction:
            logger.info("持仓方向与信号方向不一致，跳过加仓", symbol=symbol)
            return False

        # 检查加仓次数
        if symbol not in self._add_position_count:
            self._add_position_count[symbol] = {}
        current_count = self._add_position_count[symbol].get(direction, 0)
        if current_count >= self.add_position_max_times:
            logger.info("加仓次数已达上限，跳过", symbol=symbol, count=current_count, max=self.add_position_max_times)
            return False

        # 计算持仓盈亏
        entry_price = pos.get("entry_price", 0)
        if entry_price <= 0:
            return False

        if direction == "short":
            profit_pct = (entry_price - current_price) / entry_price
        else:
            profit_pct = (current_price - entry_price) / entry_price

        if profit_pct < self.add_position_profit_threshold:
            logger.info(
                "持仓盈利不足，不满足加仓条件",
                symbol=symbol,
                profit_pct=f"{profit_pct:.2%}",
                threshold=f"{self.add_position_profit_threshold:.2%}",
            )
            return False

        # 计算加仓数量
        initial_qty = pos.get("entry_quantity", 0)
        add_qty = initial_qty * self.add_position_size_ratio

        # 计算ATR
        atr = await self.trading_executor.calculate_atr(symbol, klines)
        if atr <= 0:
            atr = pos.get("atr", 0)  # 回退到持仓记录的ATR

        # 执行加仓
        order = await self.trading_executor.add_to_position(
            symbol=symbol,
            direction=direction,
            entry_price=current_price,
            atr=atr,
            quantity=Decimal(str(add_qty)),
            score=score,
        )

        if order:
            # 更新加仓计数
            self._add_position_count[symbol][direction] = current_count + 1
            # 更新持仓（更新入场均价和ATR）
            total_qty = initial_qty + add_qty
            avg_entry = (entry_price * initial_qty + current_price * add_qty) / total_qty
            pos["entry_price"] = avg_entry
            pos["entry_quantity"] = total_qty
            pos["atr"] = atr
            pos["best_price"] = current_price  # 重置最佳价格

            # 保存状态
            await self._save_state()

            logger.info(
                "加仓成功",
                symbol=symbol,
                direction=direction,
                add_qty=add_qty,
                total_qty=total_qty,
                avg_entry=avg_entry,
                add_count=self._add_position_count[symbol][direction],
            )
            return True

        return False

    # ==================== P2-4: 通知事件 ====================

    def _should_notify(self, event: str) -> bool:
        """
        P2-4: 检查是否应发送指定类型的通知

        Args:
            event: 通知事件名称，如 'open_position', 'close_position', 'stop_loss', 'take_profit'

        Returns:
            True: 应发送通知；False: 不发送
        """
        if not self._notif_enabled:
            return False
        if not self._notif_events:
            return True  # 未配置事件列表时默认全部发送
        return self._notif_events.get(event, True)

    async def _send_position_close_notification(
        self,
        symbol: str,
        direction: str,
        reason: str,
        current_price: float,
        entry_price: float,
    ) -> None:
        """
        P2-4: 发送平仓/止损/止盈通知

        Args:
            symbol: 交易对
            direction: 方向
            reason: 平仓原因（时间止损/移动止盈/止盈成交等）
            current_price: 平仓价格
            entry_price: 入场价格
        """
        # 根据原因判断事件类型
        if "止损" in reason:
            event = "stop_loss"
        elif "止盈" in reason:
            event = "take_profit"
        else:
            event = "close_position"

        if not self._should_notify(event):
            return

        try:
            if direction == "short":
                pnl_pct = (entry_price - current_price) / entry_price
            else:
                pnl_pct = (current_price - entry_price) / entry_price

            pnl_label = "盈利" if pnl_pct > 0 else "亏损"

            message = (
                f"【HRS策略平仓通知】\n"
                f"交易对: {symbol}\n"
                f"方向: {direction}\n"
                f"平仓原因: {reason}\n"
                f"入场价: {entry_price}\n"
                f"平仓价: {current_price}\n"
                f"{pnl_label}: {pnl_pct:.2%}\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC"
            )
            level = "warning" if pnl_pct < 0 else "info"
            await self.notification_client.send(message=message, level=level, project="hrs")
        except Exception as e:
            logger.warning("发送平仓通知失败", symbol=symbol, error=str(e))

    # ==================== P1-8: 连续亏损通知相关 ====================

    async def _send_risk_notification(self, message: str, level: str, project: str) -> None:
        """
        P1-8: 风控通知回调

        由 risk_manager 在触发暂停/熔断时调用。

        Args:
            message: 通知消息
            level: 通知级别
            project: 项目名称
        """
        try:
            if self.notification_client:
                await self.notification_client.send(
                    message=message,
                    level=level,
                    project=project,
                )
        except Exception as e:
            logger.warning("发送风控通知失败", error=str(e))

    def _get_pause_info(self) -> Optional[Dict[str, Any]]:
        """
        P1-8: 获取暂停状态信息

        Returns:
            暂停信息字典，包含 reason 和 until，无暂停返回 None
        """
        now = datetime.now(timezone.utc)
        # 通过 risk_manager 的内部状态获取暂停信息
        rm = self.risk_manager

        if rm.pause_until and now < rm.pause_until:
            return {
                "reason": "连续亏损暂停",
                "until": rm.pause_until,
            }
        if rm.drawdown_pause_until and now < rm.drawdown_pause_until:
            return {
                "reason": "回撤熔断",
                "until": rm.drawdown_pause_until,
            }
        return None

    # ==================== Fix #2: 定期清理残留订单 ====================

    async def _cleanup_orphan_orders(self) -> None:
        """
        定期清理残留订单（Fix #2，需求 9.5 节，已废弃API）

        条件单查询API（/papi/v1/um/algo/openOrders）已废弃，
        不再通过交易所查询僵尸订单。
        取消条件单改由以下方式处理：
        - 平仓时通过 position_manager.cancel_all_orders() 使用本地记录的 algoId 取消
        - 补单时通过 executor._replenish_position_orders() 取消旧条件单后重新设置
        """
        logger.debug("条件单查询API已废弃，跳过定期清理残留订单")

    # ==================== Fix #7: K线缓存运行期定期清理 ====================

    def _trim_klines_cache(self) -> None:
        """
        K线缓存运行期定期清理（Fix #7，需求 11.1 节末尾）

        定期裁剪缓存的K线数据，确保1h K线不超过 keep_count 根，
        4h K线不超过 synthetic_4h_count 根，避免内存持续增长。
        """
        now = datetime.now(timezone.utc)
        if (
            self._last_klines_trim is not None
            and (now - self._last_klines_trim).total_seconds() < self._klines_trim_interval_seconds
        ):
            return

        self._last_klines_trim = now
        kline_config = self.config.get("kline", {})
        keep_count = kline_config.get("keep_count", 168)
        synthetic_count = kline_config.get("synthetic_4h_count", 50)

        trimmed_1h = 0
        trimmed_4h = 0

        # 裁剪1h K线缓存
        for symbol in list(self._klines_cache.keys()):
            klines = self._klines_cache[symbol]
            if len(klines) > keep_count:
                self._klines_cache[symbol] = klines[-keep_count:]
                trimmed_1h += 1

        # 裁剪4h K线缓存
        for symbol in list(self._klines_4h_cache.keys()):
            klines = self._klines_4h_cache[symbol]
            if len(klines) > synthetic_count:
                self._klines_4h_cache[symbol] = klines[-synthetic_count:]
                trimmed_4h += 1

        if trimmed_1h > 0 or trimmed_4h > 0:
            logger.debug("K线缓存已裁剪", trimmed_1h=trimmed_1h, trimmed_4h=trimmed_4h)

    # ==================== Fix #8: 资金费率结算时间安全检查 ====================

    def _is_settlement_skip_window(self) -> bool:
        """
        资金费率结算时间安全检查（Fix #8，需求 4.2 节/9.2 节）

        检查当前时间是否在资金费率结算后的5分钟跳过窗口内。
        虽然代码已实现历史费率查询，但此方法作为容错兜底：
        在结算后5分钟内跳过评分，避免因费率跳变导致的信号误判。

        Returns:
            True: 当前处于跳过窗口，应跳过评分
        """
        funding_config = self.config.get("funding_rate", {})
        # 如果已启用历史费率，则不需要跳过
        if funding_config.get("use_historical", True):
            return False

        # 备选方案：结算后5分钟内跳过评分
        skip_minutes = funding_config.get("skip_minutes_after_settlement", 5)
        settlement_times = funding_config.get("settlement_times", ["00:00", "08:00", "16:00"])

        # 转换为 UTC+8 时间
        now_utc8 = datetime.now(timezone.utc) + timedelta(hours=8)
        current_hour = now_utc8.hour
        current_minute = now_utc8.minute

        for st in settlement_times:
            try:
                st_hour, st_minute = map(int, st.split(":"))
                # 检查是否在结算后 skip_minutes 分钟内
                if st_hour == current_hour:
                    if st_minute <= current_minute < st_minute + skip_minutes:
                        logger.debug("资金费率结算窗口，跳过评分", settlement_time=st, current_time=f"{current_hour:02d}:{current_minute:02d}")
                        return True
            except (ValueError, IndexError):
                continue

        return False