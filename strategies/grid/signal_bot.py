"""
网格信号灯模块
半自动信号灯系统，自动分析市场状态并推送网格参数
"""
import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, Optional
import structlog

from shared.config_loader import load_strategy_config_with_metadata
from shared.kline_service import KLineService
from shared.notification import NotificationClient
from shared.binance_api import BinanceClient
from .heartbeat import HeartbeatThread
from .market_state import MarketStateDetector, MarketState, MarketAnalysis
from .grid_calculator import (
    GridCalculator, DynamicGridParams, CapitalFeasibility,
    BINDING_STRUCT, BINDING_PROFIT, BINDING_CAPITAL,
)
from .margin_advisor import MarginAdvisor, MarginAdvice, DANGEROUS_STATES
from .realtime.reference_store import (ExportStore, SessionState, check_schema,
                                        connect, make_reference_id)
from .realtime.rules import ReferenceSnapshot


logger = structlog.get_logger()


@dataclass
class GridSignal:
    """
    网格信号数据类

    Attributes:
        symbol: 交易对
        market_analysis: 市场分析结果
        grid_params: 动态网格参数
        timestamp: 时间戳
        message: 推送消息
        position_valid: 仓位是否可行
        position_message: 仓位提示信息
    """
    symbol: str
    market_analysis: MarketAnalysis
    grid_params: Optional[DynamicGridParams]
    timestamp: datetime
    message: str
    position_valid: Optional[bool]
    position_message: str
    # V2.5.5 方案B：资本可行性结果与本次实际使用的保证金口径（非网格状态为 None）
    capital_feasibility: Optional[CapitalFeasibility] = None
    margin_used: Optional[Decimal] = None


class GridSignalBot:
    """
    网格信号灯机器人

    半自动信号灯系统，自动分析市场状态并推送网格参数到飞书。

    主要功能：
    - 定时巡检市场状态
    - 自动计算最优网格参数
    - 自动检查仓位可行性
    - 通过飞书推送可执行的操作指令
    - 为后续全自动交易打下基础
    """

    def __init__(
        self,
        binance_client: BinanceClient,
        kline_service: KLineService,
        notification_client: NotificationClient,
        grid_calculator: GridCalculator,
        config: Dict
    ):
        """
        初始化网格信号灯机器人

        Args:
            binance_client: 币安客户端
            kline_service: K线服务
            notification_client: 通知客户端
            grid_calculator: 网格计算器
            config: 配置字典

        Raises:
            ValueError: 参数验证失败
        """
        if not binance_client:
            raise ValueError("币安客户端不能为空")

        if not kline_service:
            raise ValueError("K线服务不能为空")

        if not notification_client:
            raise ValueError("通知客户端不能为空")

        if not grid_calculator:
            raise ValueError("网格计算器不能为空")

        if not config:
            raise ValueError("配置不能为空")

        self.binance_client = binance_client
        self.kline_service = kline_service
        self.notification_client = notification_client
        self.grid_calculator = grid_calculator
        self.config = config

        # 市场状态检测器构造参数（ETH 与 BTC 共享同一份，保证检测口径一致）
        market_cfg = config.get('market', {})
        confidence_cfg = market_cfg.get('confidence', {})
        detector_kwargs = {
            'adx_extreme_strong': market_cfg.get('adx_extreme_strong', 40),
            'adx_extreme_strong_4h': market_cfg.get('adx_extreme_strong_4h', 30),
            'adx_normal_strong': market_cfg.get('adx_normal_strong', 30),
            'adx_normal_strong_4h': market_cfg.get('adx_normal_strong_4h', 25),
            'weak_trend_adx_lower': market_cfg.get('weak_trend_adx_lower', 25),
            'weak_trend_adx_upper': market_cfg.get('weak_trend_adx_upper', 30),
            'volatility_ratio_threshold': Decimal(str(market_cfg.get('volatility_ratio_threshold', 1.2))),
            'volatility_consecutive_count': market_cfg.get('volatility_consecutive_count', 2),
            'volatility_recovery_ratio': Decimal(str(market_cfg.get('volatility_recovery_ratio', 1.2))),
            'recovery_adx_strong_1h': market_cfg.get('recovery_adx_strong_1h', 30),
            'recovery_adx_strong_4h': market_cfg.get('recovery_adx_strong_4h', 30),
            'recovery_adx_weak_1h': market_cfg.get('recovery_adx_weak_1h', 25),
            'recovery_adx_weak_4h': market_cfg.get('recovery_adx_weak_4h', 25),
            'trend_strength_divisor': market_cfg.get('trend_strength_divisor', 30),
            'atr_history_size': market_cfg.get('atr_history_size', 5),
            'ema_fast_period': market_cfg.get('ema_fast', 20),
            'ema_slow_period': market_cfg.get('ema_slow', 50),
            'atr_period': market_cfg.get('atr_period', 14),
            # V2.3 新增参数
            'emergency_adx_threshold': market_cfg.get('emergency_adx_threshold', 55),
            'trend_acceleration_threshold': market_cfg.get('trend_acceleration_threshold', 8),
            'adx_history_size': market_cfg.get('adx_history_size', 3),
            # V2.4 三层预警架构新增参数
            'adx_period': market_cfg.get('adx_period', 10),
            'price_emergency_1h': Decimal(str(market_cfg.get('price_emergency_1h', 0.03))),
            'price_emergency_15m': Decimal(str(market_cfg.get('price_emergency_15m', 0.015))),
            'adx_early_warning_15m': market_cfg.get('adx_early_warning_15m', 50),
            'price_early_warning_1h': Decimal(str(market_cfg.get('price_early_warning_1h', 0.01))),
            # 置信度参数（V2.3从配置读取）
            'confidence_emergency': Decimal(str(confidence_cfg.get('emergency_extreme_trend', 0.99))),
            'confidence_trend_accelerating': Decimal(str(confidence_cfg.get('trend_accelerating', 0.9))),
            'confidence_extreme_strong': Decimal(str(confidence_cfg.get('extreme_strong_trend', 0.95))),
            'confidence_volatility_abnormal': Decimal(str(confidence_cfg.get('volatility_abnormal', 0.85))),
            'confidence_normal_strong': Decimal(str(confidence_cfg.get('normal_strong_trend', 0.8))),
            'confidence_weak_trend': Decimal(str(confidence_cfg.get('weak_trend', 0.7))),
            'confidence_oscillation': Decimal(str(confidence_cfg.get('oscillation', 0.5))),
            # V2.4 新增置信度
            'confidence_price_emergency': Decimal(str(confidence_cfg.get('price_emergency', 1.0))),
            'confidence_early_warning_15m': Decimal(str(confidence_cfg.get('early_warning_15m', 0.92))),
            'confidence_trend_confirmed_1h': Decimal(str(confidence_cfg.get('trend_confirmed_1h', 0.95)))
        }

        # 初始化市场状态检测器（ETH 主检测）
        self.market_detector = MarketStateDetector(kline_service=kline_service, **detector_kwargs)

        # V2.5 保证金引导：BTC 独立检测器（与 ETH 同参数，实例状态独立不污染）
        self.btc_market_detector = MarketStateDetector(kline_service=kline_service, **detector_kwargs)
        self.margin_advisor = MarginAdvisor(
            config=config,
            binance_client=binance_client,
            kline_service=kline_service,
            btc_detector=self.btc_market_detector,
            grid_calculator=grid_calculator
        )

        # 交易对配置
        self.symbols = config.get('symbols', [])
        if not self.symbols:
            raise ValueError("交易对列表不能为空")

        # 杠杆和保证金配置
        self.default_leverage = config.get('trading', {}).get('leverage', 10)
        self.default_margin = Decimal(str(config.get('trading', {}).get('margin', 500)))

        # V2.5.5 方案B：每格最小下单量 q_min（ETH，FR-06，启用原幽灵配置 trading.min_quantity）
        self.min_quantity = Decimal(str(config.get('trading', {}).get('min_quantity', 0.01)))
        # 方案B 总开关；false 时整体回退 V2.5 已知行为（含 FR-01 口径，见设计 4.4）
        self.capital_constraint_enabled = bool(
            config.get('grid', {}).get('capital_constraint', {}).get('enabled', True)
        )

        # K线数量（基准 ATR 取数）
        self._kline_limit = int(config.get('kline', {}).get('limit', 100))

        # 网格数量上下限（用于仓位建议）
        self.min_grid_count = config.get('grid', {}).get('min_grid_count', 5)

        # 巡检配置（从信号灯专用配置读取，单位：分钟）
        self.check_interval_minutes = config.get('signal_bot', {}).get('check_interval_minutes', 60)
        self.run_at_minute = config.get('signal_bot', {}).get('run_at_minute', 5)  # 固定在每小时的第几分钟执行

        # 推送冷却时间（V2.3三档冷却，从配置文件读取）
        self.push_cooldown_hours_alert = config.get('signal_bot', {}).get('push_cooldown_hours_alert', 1)  # 紧急/趋势加速/极端强趋势
        self.push_cooldown_hours_normal = config.get('signal_bot', {}).get('push_cooldown_hours_normal', 6)  # 普通强趋势/波动率异常
        self.push_cooldown_hours_tradable = config.get('signal_bot', {}).get('push_cooldown_hours_tradable', 2)  # 弱趋势/震荡

        # 利润率低阈值（从配置文件读取，预留后续使用）
        trigger_cfg = config.get('signal_bot', {}).get('trigger_thresholds', {})
        self.profit_rate_low_threshold = Decimal(str(trigger_cfg.get('profit_rate_low', 0.012)))  # TODO: V2.2 利润率恶化提醒

        # 保守方案网格减少步长（从配置文件读取）
        self.conservative_grid_reduce = config.get('signal_bot', {}).get('conservative_grid_reduce', 10)

        # 历史状态记录（用于检测切换）
        self.last_signals: Dict[str, GridSignal] = {}

        # V2.5.4 实时预警交接：默认全部 None（enabled=false 时零行为变化，AC-01）
        self.export_store: Optional[ExportStore] = None
        self._export_conn: Optional[sqlite3.Connection] = None
        self._heartbeat: Optional[HeartbeatThread] = None
        self._state: Optional[SessionState] = None
        self._metadata: Dict = {}
        self._init_export()

        logger.info(
            "网格信号灯机器人初始化完成",
            symbols=self.symbols,
            leverage=self.default_leverage,
            margin=float(self.default_margin),
            check_interval_minutes=self.check_interval_minutes,
            push_cooldown_hours_alert=self.push_cooldown_hours_alert,
            push_cooldown_hours_normal=self.push_cooldown_hours_normal,
            push_cooldown_hours_tradable=self.push_cooldown_hours_tradable
        )

    async def run_once(self, symbol: str) -> GridSignal:
        """
        执行一次信号检测（V2.4：按10种市场状态分发，三层预警架构）

        Args:
            symbol: 交易对

        Returns:
            网格信号

        Raises:
            ValueError: 参数验证失败
            Exception: 检测失败
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")

        logger.info(f"开始执行信号检测: {symbol}")

        try:
            # 1. 检测市场状态
            market_analysis = await self.market_detector.detect_market_state(symbol)

            # 2. V2.5 保证金智能引导（所有状态均计算；失败不阻断主流程）
            advice = None
            try:
                advice = await self.margin_advisor.compute_advice(symbol, market_analysis)
            except Exception as e:
                logger.warning(
                    f"{symbol} 保证金引导计算失败，本次跳过",
                    error=str(e),
                    exc_info=True
                )

            # 3. 根据市场状态分发
            grid_params = None
            position_valid = None  # 非网格状态为 None，表示不适用
            position_message = ""
            capital_feasibility = None  # V2.5.5 方案B：资本可行性结果
            margin_used = None          # V2.5.5 方案B：本次可行性口径实际使用的保证金

            state = market_analysis.state

            # V2.4: 检测是否从危险状态恢复到可交易状态
            dangerous_states = DANGEROUS_STATES
            tradable_states = {MarketState.WEAK_TREND, MarketState.OSCILLATION}

            is_recovery = False
            if symbol in self.last_signals:
                prev_state = self.last_signals[symbol].market_analysis.state
                if prev_state in dangerous_states and state in tradable_states:
                    is_recovery = True
                    logger.info(
                        f"{symbol} 从危险状态恢复到可交易状态",
                        prev_state=prev_state.value,
                        new_state=state.value
                    )

            # 恢复通知：从危险状态恢复到可交易状态时推送
            if is_recovery:
                message = self._generate_recovery_message(symbol, market_analysis)

            # 价格行为紧急触发（第1层，V2.4新增，0延迟）
            if state == MarketState.PRICE_EMERGENCY:
                message = self._generate_price_emergency_message(symbol, market_analysis)

            # 15m ADX 早期预警（第2层，V2.4新增，比1h快4倍）
            elif state == MarketState.EARLY_WARNING_15M:
                message = self._generate_early_warning_15m_message(symbol, market_analysis)

            # 1h ADX(10) 趋势确认（第3层，V2.4新增，ADX周期从14缩短为10）
            elif state == MarketState.TREND_CONFIRMED_1H:
                message = self._generate_trend_confirmed_1h_message(symbol, market_analysis, advice=advice)

            # 趋势急剧增强：不计算网格参数，推送"暂停或单向挂单"（V2.3新增）
            elif state == MarketState.TREND_ACCELERATING:
                message = self._generate_trend_accelerating_message(symbol, market_analysis, advice=advice)

            # 极端强趋势：不计算网格参数，推送"必须立即终止"
            elif state == MarketState.EXTREME_STRONG_TREND:
                message = self._generate_extreme_strong_message(symbol, market_analysis, advice=advice)

            # 波动率异常：不计算网格参数，推送"暂停挂单"
            elif state == MarketState.VOLATILITY_ABNORMAL:
                message = self._generate_volatility_abnormal_message(symbol, market_analysis)

            # 普通强趋势：不计算网格参数，推送"建议终止"
            elif state == MarketState.NORMAL_STRONG_TREND:
                message = self._generate_normal_strong_message(symbol, market_analysis, advice=advice)

            # 弱趋势或震荡：计算网格参数，推送网格建议
            elif state in [MarketState.WEAK_TREND, MarketState.OSCILLATION]:
                # V2.5.5 FR-01：可行性保证金口径 = 建议保证金；开关关闭时整体回退固定配置值
                margin_used = self._select_margin_for_feasibility(advice)
                grid_params, capital_feasibility = await self._calculate_grid_params(
                    symbol,
                    market_analysis,
                    margin=margin_used,
                    atr_baseline=self.margin_advisor.last_baseline_atr
                )
                position_valid = capital_feasibility.feasible
                message = self._generate_signal_message(
                    symbol=symbol,
                    market_analysis=market_analysis,
                    grid_params=grid_params,
                    capital_feasibility=capital_feasibility,
                    margin_used=margin_used,
                    advice=advice
                )

            else:
                # 未知状态，默认震荡处理
                logger.warning(f"{symbol} 未知市场状态: {state}，默认按震荡处理")
                margin_used = self._select_margin_for_feasibility(advice)
                grid_params, capital_feasibility = await self._calculate_grid_params(
                    symbol,
                    market_analysis,
                    margin=margin_used,
                    atr_baseline=self.margin_advisor.last_baseline_atr
                )
                position_valid = capital_feasibility.feasible
                message = self._generate_signal_message(
                    symbol=symbol,
                    market_analysis=market_analysis,
                    grid_params=grid_params,
                    capital_feasibility=capital_feasibility,
                    margin_used=margin_used,
                    advice=advice
                )

            # 4. 构建信号
            signal = GridSignal(
                symbol=symbol,
                market_analysis=market_analysis,
                grid_params=grid_params,
                timestamp=datetime.now(),
                message=message,
                position_valid=position_valid,
                position_message=position_message,
                capital_feasibility=capital_feasibility,
                margin_used=margin_used
            )

            logger.info(
                f"{symbol} 信号检测完成",
                state=market_analysis.state.value,
                has_grid_params=grid_params is not None
            )

            return signal

        except Exception as e:
            logger.error(
                f"信号检测失败: {symbol}",
                error=str(e),
                exc_info=True
            )
            raise

    async def _wait_until_next_run(self) -> None:
        """
        等待到下一个固定的执行分钟节点

        例如 run_at_minute=5，则等待到每小时 :05 执行。
        如果当前时间已经过了 :05，则等待到下一个小时的 :05。
        """
        now = datetime.now()
        current_minute = now.minute
        current_second = now.second
        current_microsecond = now.microsecond

        if current_minute < self.run_at_minute:
            # 当前小时还没到目标分钟，等本小时的 :05
            wait_seconds = (self.run_at_minute - current_minute) * 60 - current_second - current_microsecond / 1_000_000
        else:
            # 已过目标分钟，等下一个小时的 :05
            wait_seconds = (60 - current_minute + self.run_at_minute) * 60 - current_second - current_microsecond / 1_000_000

        if wait_seconds > 0:
            logger.info(
                "等待到下一个执行节点",
                run_at_minute=self.run_at_minute,
                current_time=f"{now.hour:02d}:{now.minute:02d}:{now.second:02d}",
                wait_seconds=int(wait_seconds)
            )
            await asyncio.sleep(wait_seconds)

    async def run_loop(self, interval_minutes: int = None) -> None:
        """
        持续运行信号检测循环

        每次执行固定在每小时第 run_at_minute 分钟（如 :05），
        与 K 线收盘时间对齐，确保数据完整。

        Args:
            interval_minutes: 巡检间隔（分钟），默认从配置读取

        Raises:
            Exception: 运行失败
        """
        if interval_minutes is None:
            interval_minutes = self.check_interval_minutes

        logger.info(
            "开始运行信号检测循环",
            interval_minutes=interval_minutes,
            run_at_minute=self.run_at_minute
        )

        self._start_export()
        try:
            # 首次执行：等待到下一个固定分钟节点
            await self._wait_until_next_run()

            while True:
                try:
                    # 对每个交易对执行检测
                    for symbol in self.symbols:
                        try:
                            signal = await self.run_once(symbol)

                            # 检查是否需要推送
                            if self._should_notify(signal):
                                await self._send_with_export(signal)
                                self.last_signals[symbol] = signal

                        except Exception as e:
                            logger.error(
                                f"信号检测失败: {symbol}",
                                error=str(e),
                                exc_info=True
                            )

                    # 等待到下一个固定分钟节点（保持对齐）
                    await self._wait_until_next_run()

                except Exception as e:
                    logger.error(
                        "信号检测循环失败",
                        error=str(e),
                        exc_info=True
                    )
                    await asyncio.sleep(60)
        finally:
            self._shutdown_export()

    def _select_margin_for_feasibility(self, advice: Optional[MarginAdvice]) -> Decimal:
        """
        选择资金可行性检查使用的保证金口径（FR-01）

        - 方案B 启用：advice.suggested_margin>0 时用建议保证金，否则回退 trading.margin
        - 方案B 关闭（整体回退）：恒用固定 trading.margin（V2.5 已知行为）
        """
        if not self.capital_constraint_enabled:
            return self.default_margin
        if advice is not None and advice.suggested_margin > 0:
            return advice.suggested_margin
        return self.default_margin

    def _state_grid_bounds(self, state: MarketState) -> tuple:
        """按市场状态取网格数 (N_min, N_max)：震荡读 min/max_grid_count，弱趋势读 weak_trend_*"""
        grid_cfg = self.config.get('grid', {})
        if state == MarketState.WEAK_TREND:
            return (
                grid_cfg.get('weak_trend_min_grid_count', 4),
                grid_cfg.get('weak_trend_max_grid_count', 10)
            )
        return (
            grid_cfg.get('min_grid_count', 5),
            grid_cfg.get('max_grid_count', 12)
        )

    def _resolve_profit_cap(self, params: DynamicGridParams, n_min: int) -> int:
        """
        C2 利润率约束上限（精确口径，沿用 validate_profit_rate 的等差/等比逐试）

        Returns:
            满足每格利润率下限的最大网格数；
            任何 >= n_min 的格数都不满足时返回 n_min-1（交由约束链判不可行）
        """
        profit_valid, suggested_count = self.grid_calculator.validate_profit_rate(
            params, min_grid_count=n_min
        )
        if profit_valid:
            return params.grid_count
        if suggested_count is not None:
            return suggested_count
        return n_min - 1

    async def _calculate_grid_params(
        self,
        symbol: str,
        market_analysis: MarketAnalysis,
        margin: Optional[Decimal] = None,
        atr_baseline: Optional[Decimal] = None
    ) -> tuple:
        """
        计算动态网格参数并解析资本约束链（V2.5.5 方案B）

        方案B 启用：N_final = min(N_struct, N_profit, N_cap)，
        可行时 profit_rate/grid_spacing 按 N_final 重算（修正旧实现降网格后
        间距/利润率不与格数同步的怪癖）；不可行时保留 N_struct 仅供参考。
        方案B 关闭：整体回退 V2.5 行为（仅 C2 降网格、C3 不参与决策）。

        Args:
            symbol: 交易对
            market_analysis: 市场分析结果
            margin: 可行性口径保证金（FR-01，由调用方按建议保证金传入）
            atr_baseline: 基准ATR（V2.5 由保证金引导复用传入，避免重复拉取 K 线）

        Returns:
            (DynamicGridParams, CapitalFeasibility)

        Raises:
            ValueError: 参数验证失败
        """
        # V2.5：未传入时获取历史K线数据（用于计算基准ATR）
        if atr_baseline is None:
            klines = await self.kline_service.get_klines(
                symbol=symbol,
                interval='1d',
                limit=self._kline_limit
            )
            atr_baseline = self.grid_calculator.calculate_baseline_atr(klines)

        # 计算动态网格参数（N_struct，C1 结构约束已在计算器内按状态夹逼）
        params = self.grid_calculator.calculate_dynamic_grid_params(
            current_price=market_analysis.current_price,
            atr_smooth=market_analysis.atr_smooth,
            atr_baseline=atr_baseline,
            market_state=market_analysis.state.value,
            trend_strength=market_analysis.trend_strength
        )
        n_min, _ = self._state_grid_bounds(market_analysis.state)

        if not self.capital_constraint_enabled:
            return self._legacy_grid_params(
                params, market_analysis, atr_baseline, n_min
            )

        if margin is None:
            margin = self.default_margin

        # C2 利润率约束（精确口径）
        n_profit_max = self._resolve_profit_cap(params, n_min)

        # C3 资本约束 + 约束链合并
        feasibility = self.grid_calculator.resolve_capital_feasibility(
            price=market_analysis.current_price,
            leverage=self.default_leverage,
            margin=margin,
            n_struct=params.grid_count,
            n_profit_max=n_profit_max,
            n_min=n_min,
            min_quantity=self.min_quantity
        )

        # 可行：按 N_final 重算利润率与间距，保证三者一致（设计 2.2 澄清）
        if feasibility.feasible and feasibility.grid_count != params.grid_count:
            params.grid_count = feasibility.grid_count
            params.profit_rate, params.grid_spacing = \
                self.grid_calculator._calculate_profit_rate(
                    lower_boundary=params.lower_boundary,
                    upper_boundary=params.upper_boundary,
                    grid_count=params.grid_count,
                    grid_mode=params.grid_mode,
                    current_price=market_analysis.current_price
                )

        return params, feasibility

    def _legacy_grid_params(
        self,
        params: DynamicGridParams,
        market_analysis: MarketAnalysis,
        atr_baseline: Decimal,
        n_min: int
    ) -> tuple:
        """
        方案B 关闭时的 V2.5 已知行为（整体回退，含 FR-01 口径）

        - 仅 C2 利润率降网格（保留 V2.5 降网格后不重算 spacing/profit_rate 的行为）
        - C3 资本不参与网格数决策；保证金口径固定为 trading.margin
        """
        profit_valid, suggested_count = self.grid_calculator.validate_profit_rate(params)

        if not profit_valid and suggested_count:
            # 保留 V2.5 行为：重新计算动态参数后仅覆写 grid_count
            params = self.grid_calculator.calculate_dynamic_grid_params(
                current_price=market_analysis.current_price,
                atr_smooth=market_analysis.atr_smooth,
                atr_baseline=atr_baseline,
                market_state=market_analysis.state.value,
                trend_strength=market_analysis.trend_strength
            )
            params.grid_count = suggested_count

        feasibility = self.grid_calculator.resolve_capital_feasibility(
            price=market_analysis.current_price,
            leverage=self.default_leverage,
            margin=self.default_margin,
            n_struct=params.grid_count,
            n_profit_max=params.grid_count,
            n_min=n_min,
            min_quantity=self.min_quantity,
            enforce_capital=False
        )
        return params, feasibility

    def _should_notify(self, signal: GridSignal) -> bool:
        """
        判断是否需要推送通知（V2.3 三档冷却逻辑）

        触发条件：
        - 首次运行：一定推送
        - 市场状态变化：立即推送
        - 同状态：检查冷却时间，超过冷却时间才推送

        冷却时间三档：
        - alert（1小时）：紧急极端趋势/趋势急剧增强/极端强趋势
        - normal（6小时）：普通强趋势/波动率异常
        - tradable（2小时）：弱趋势/震荡

        Args:
            signal: 当前信号

        Returns:
            是否需要推送
        """
        symbol = signal.symbol
        state = signal.market_analysis.state

        # 首次运行：一定推送
        if symbol not in self.last_signals:
            logger.info(f"{symbol} 首次运行，需推送", state=state.value)
            return True

        last = self.last_signals[symbol]
        old_state = last.market_analysis.state

        # 状态变化：立即推送
        if old_state != state:
            logger.info(f"{symbol} 市场状态变化，需推送",
                        old_state=old_state.value, new_state=state.value)
            return True

        # 同状态：根据市场状态选择冷却时间（V2.3三档冷却）
        if state in [MarketState.PRICE_EMERGENCY, MarketState.EARLY_WARNING_15M, MarketState.TREND_CONFIRMED_1H,
             MarketState.TREND_ACCELERATING, MarketState.EXTREME_STRONG_TREND]:
            cooldown_hours = self.push_cooldown_hours_alert  # 1小时
        elif state in [MarketState.NORMAL_STRONG_TREND, MarketState.VOLATILITY_ABNORMAL]:
            cooldown_hours = self.push_cooldown_hours_normal  # 6小时
        else:
            cooldown_hours = self.push_cooldown_hours_tradable  # 2小时（弱趋势/震荡）

        if hasattr(last, 'timestamp') and last.timestamp:
            hours_since = (datetime.now() - last.timestamp).total_seconds() / 3600
            if hours_since < cooldown_hours - 0.01:  # 0.01小时≈36秒宽容度，避免浮点临界值问题
                logger.info(f"{symbol} 冷却中，跳过推送",
                            state=state.value,
                            hours_since=round(hours_since, 1),
                            cooldown=cooldown_hours)
                return False
            # 超过冷却时间：推送
            logger.info(f"{symbol} 冷却期满，推送", state=state.value, hours_since=round(hours_since, 1))
        else:
            logger.info(f"{symbol} 无上次推送记录，推送", state=state.value)
        return True

    async def _send_notification(self, signal: GridSignal) -> bool:
        """
        发送通知到飞书

        Args:
            signal: 网格信号

        Returns:
            是否发送成功
        """
        try:
            success = await self.notification_client.send(
                message=signal.message,
                level="info",
                project="grid"
            )

            if success:
                logger.info(
                    f"{signal.symbol} 通知发送成功",
                    state=signal.market_analysis.state.value
                )
            else:
                logger.error(f"{signal.symbol} 通知发送失败")

            return success

        except Exception as e:
            logger.error(
                f"{signal.symbol} 发送通知失败",
                error=str(e),
                exc_info=True
            )
            return False

    # ─────────── V2.5.4 快照导出与持久化交接 ───────────

    def _init_export(self) -> None:
        """enabled 时打开持久连接并校验 schema（失败不阻止构造，AC-01）。"""
        rt_cfg = self.config.get("realtime_alert", {})
        if not rt_cfg.get("enabled"):
            return
        _, self._metadata = load_strategy_config_with_metadata(
            os.path.dirname(__file__))
        try:
            conn = connect(rt_cfg["storage"]["path"],
                           rt_cfg["storage"]["busy_timeout_ms"])
            check_schema(conn)
        except (KeyError, sqlite3.Error, RuntimeError) as exc:
            logger.warning("实时交接持久化不可用，出口将只推送不落库",
                           error=str(exc))
            return
        self._export_conn = conn
        self.export_store = ExportStore(conn)

    def _start_export(self) -> None:
        """run_loop 起点：开始会话 + 心跳线程。"""
        if self.export_store is None or self._state is not None:
            return
        rt_cfg = self.config["realtime_alert"]
        sid = self.export_store.begin_session(_now_ms())
        self._state = SessionState(sid, 0, None, None)
        self._heartbeat = HeartbeatThread(
            lambda: self._state, rt_cfg["storage"]["path"],
            rt_cfg["reference_sync"]["heartbeat_seconds"],
            rt_cfg["storage"]["busy_timeout_ms"], _now_ms)
        self._heartbeat.start()
        logger.info("实时交接会话已开始", session_id=sid)

    def _shutdown_export(self) -> None:
        """run_loop 终点（含取消）：停心跳、关会话与连接。"""
        if self._heartbeat is not None:
            self._heartbeat.stop()
            self._heartbeat = None
        if self.export_store is not None and self._state is not None:
            try:
                self.export_store.close_session(_now_ms())
            except sqlite3.Error as exc:
                logger.warning("关闭出口会话失败", error=str(exc))
        self._state = None
        if self._export_conn is not None:
            self._export_conn.close()
            self._export_conn = None
        self.export_store = None

    async def _send_with_export(self, signal: GridSignal) -> bool:
        """发送并走 PREPARED→SENDING→SENT/FAILED 交接；持久化故障不阻止推送。"""
        if not self._export_ready(signal):
            return await self._send_notification(signal)
        try:
            return await self._send_tracked(signal)
        except sqlite3.Error as exc:
            self._abort_export(exc)
            return await self._send_notification(signal)

    def _export_ready(self, signal: GridSignal) -> bool:
        """交接可用条件：store/会话就绪且本信号含边界（无边界不创建更新）。"""
        return (self.export_store is not None and self._state is not None
                and signal.grid_params is not None)

    async def _send_tracked(self, signal: GridSignal) -> bool:
        """持久化可用时的完整发送链路。"""
        gp = signal.grid_params
        calculated_ms = _datetime_ms(signal.timestamp)
        reference_id = make_reference_id(
            calculated_ms, gp.lower_boundary, gp.upper_boundary,
            gp.stop_loss_low, gp.stop_loss_high)
        snapshot = self._build_snapshot(signal, reference_id, calculated_ms)
        self.export_store.prepare(_now_ms(), self._state, snapshot)
        self._state = SessionState(
            self._state.session_id, self._state.current_seq,
            self._state.current_reference_id,
            self._state.current_seq + 1)
        self.export_store.mark_sending(reference_id)
        sent = await self._send_notification(signal)
        self._finish_send(reference_id, sent)
        return sent

    def _finish_send(self, reference_id: str, sent: bool) -> None:
        """按发送结果落 SENT（序号前移）或 FAILED（清未决）。"""
        assert self.export_store is not None and self._state is not None
        now = _now_ms()
        if sent:
            self.export_store.mark_sent(reference_id, now, self._state, now)
            seq = self._state.pending_seq
            self._state = SessionState(
                self._state.session_id, seq, reference_id, None)
        else:
            self.export_store.mark_failed(reference_id, self._state, now)
            self._state = SessionState(
                self._state.session_id, self._state.current_seq,
                self._state.current_reference_id, None)

    def _abort_export(self, exc: Exception) -> None:
        """持久化故障：尽力置 SYNC_UNCERTAIN 并断开交接（§4.2.4）。"""
        logger.warning("实时交接持久化故障，暂停操作建议", error=str(exc))
        try:
            if self.export_store is not None and self._state is not None:
                self.export_store.set_sync_uncertain(self._state, _now_ms())
        except sqlite3.Error:
            pass
        self._shutdown_export()

    def _build_snapshot(self, signal: GridSignal, reference_id: str,
                        calculated_ms: int) -> ReferenceSnapshot:
        """从 signal 计算对象构造快照（不解析消息文本、不重算边界）。"""
        gp, ma = signal.grid_params, signal.market_analysis
        return ReferenceSnapshot(
            reference_id=reference_id, symbol=signal.symbol,
            calculated_at_ms=calculated_ms, effective_at_ms=calculated_ms,
            grid_lower=gp.lower_boundary, grid_upper=gp.upper_boundary,
            stop_lower=gp.stop_loss_low, stop_upper=gp.stop_loss_high,
            stop_move_up_price=gp.stop_move_up_price,
            stop_move_down_price=gp.stop_move_down_price,
            market_state=ma.state.value, atr=ma.atr_smooth,
            adx_1h=ma.adx_1h, adx_4h=ma.adx_4h,
            config_version=str(self.config.get("strategy", {}).get("version")),
            overrides_version=self._metadata.get("applied_overrides_version"),
            config_hash=self._metadata.get("config_hash"), source="signal_bot")

    # 绑定约束文案（FR-10，并列 tie-break 见 grid_calculator.resolve_capital_feasibility）
    _BINDING_LABELS = {
        BINDING_STRUCT: '结构',
        BINDING_PROFIT: '利润率',
        BINDING_CAPITAL: '资本',
    }

    def _build_funding_text(
        self,
        market_analysis: MarketAnalysis,
        grid_params: DynamicGridParams,
        feasibility: CapitalFeasibility,
        margin_used: Decimal
    ) -> str:
        """
        生成资金板块文案（V2.5.5 方案B，设计 4.5）

        可行：资金配置三行（杠杆/保证金/每格下单量 ETH+N_final+绑定约束）；
        不可行：可达性提醒 + 方案1（加保证金至 M_required）/方案2（加杠杆至 L_required），
        不再输出 V2.5「减少至5格」这类不可达建议（修正 D-1）。
        O-1（A-review 裁定）：binding=PROFIT 不可行时资本非绑定，抑制「可达上限」行与
        方案1/2，仅保留区间过窄/波动率过低归因（避免「加资金无法解决」与「提高保证金」自相矛盾）。
        """
        q_min = self.min_quantity
        if feasibility.feasible:
            binding_label = self._BINDING_LABELS.get(feasibility.binding, '结构')
            return f"""💰 资金配置
- 建议杠杆: {self.default_leverage}x
- 建议保证金: {float(margin_used):.0f} USDT
- 每格 {float(feasibility.qty_per_grid):.2f} ETH（≥{q_min}，满足），网格数量 {feasibility.grid_count} 格（受{binding_label}约束）"""

        n_min, _ = self._state_grid_bounds(market_analysis.state)
        lines = [
            "💰 资金可行性提醒",
            f"当前建议保证金 {float(margin_used):.0f} USDT 无法支撑最少 {n_min} 格"
            f"（每格仅 {float(feasibility.qty_per_grid):.3f} ETH，需 ≥{q_min} ETH）。"
        ]
        # 利润率绑定（区间过窄/波动率过低）：与 D-4 间距提示分开表述（设计 4.6）
        # O-1：资本非绑定时不展示资本口径的「可达上限」与加保证金/杠杆方案
        if feasibility.binding == BINDING_PROFIT:
            lines.append(
                "区间过窄/波动率过低：即使最少网格也无法满足每格利润率下限，"
                "增加资金无法解决，建议暂不创建网格。"
            )
            return "\n".join(lines)
        # 资本可达上限（仅在至少 1 格时展示）
        n_cap = self.grid_calculator.max_grids_by_capital(
            price=market_analysis.current_price,
            leverage=self.default_leverage,
            margin=margin_used,
            min_quantity=q_min
        )
        if n_cap >= 1:
            lines.append(f"可达上限：{n_cap} 格。")
        lines.append("建议：")
        lines.append(
            f"- 方案1：将保证金提高至 {feasibility.required_margin} USDT"
            f"（保持 {self.default_leverage}x）"
        )
        lines.append(
            f"- 方案2：将杠杆提高至 {feasibility.required_leverage}x"
            f"（保持 {float(margin_used):.0f} USDT，风险较高）"
        )
        lines.append("- 若两者均不接受，建议暂不创建网格")
        return "\n".join(lines)

    def _build_legacy_funding_text(
        self,
        market_analysis: MarketAnalysis,
        grid_params: DynamicGridParams,
        feasibility: CapitalFeasibility
    ) -> str:
        """
        方案B 关闭时的 V2.5 资金板块文案（整体回退，设计 4.4）

        复用 resolve_capital_feasibility(enforce_capital=False) 的每格下单量，
        但有效/无效判定与文案模板完全保持 V2.5 已知行为。
        ETH 口径下数量可为小数，不输出「取整后N张」段（A-review 9.3 修改点②）。
        """
        margin = self.default_margin
        qty_per_grid = feasibility.qty_per_grid
        if qty_per_grid >= self.min_quantity:
            return f"""💰 资金配置
- 建议杠杆: {self.default_leverage}x
- 建议保证金: {float(margin):.0f} USDT
- 每格{float(qty_per_grid):.2f} ETH"""

        price = market_analysis.current_price
        min_margin = (
            self.min_quantity * price * Decimal(str(grid_params.grid_count))
            / Decimal(str(self.default_leverage))
        )
        position_message = (
            f"每格仅{float(qty_per_grid):.2f} ETH，不足{self.min_quantity} ETH。"
            f"请将保证金增至{float(min_margin):.0f} USDT，"
            f"或减少网格数量至"
            f"{max(self.min_grid_count, int(float(margin * Decimal(str(self.default_leverage)) / price)))}格"
        )
        return f"""💰 资金可行性提醒
{position_message}

建议：
- 方案1（保守）：减少网格数量至 {max(self.min_grid_count, grid_params.grid_count - self.conservative_grid_reduce)} 格
- 方案2（激进）：增加保证金或提高杠杆（风险较高）
- 请根据您的资金情况在币安创建界面调整参数"""

    def _generate_signal_message(
        self,
        symbol: str,
        market_analysis: MarketAnalysis,
        grid_params: DynamicGridParams,
        capital_feasibility: CapitalFeasibility,
        margin_used: Decimal,
        advice: Optional[MarginAdvice] = None
    ) -> str:
        """
        生成网格信号推送消息

        Args:
            symbol: 交易对
            market_analysis: 市场分析结果
            grid_params: 动态网格参数
            capital_feasibility: 资本可行性结果（V2.5.5 方案B）
            margin_used: 本次可行性口径实际使用的保证金
            advice: 保证金引导建议（V2.5，skipped 时不拼装板块）

        Returns:
            推送消息
        """
        # 标题
        title = f"【网格信号灯】{market_analysis.state.value}"

        # 市场数据
        market_data = f"""
📊 当前市场数据
- 价格: {float(market_analysis.current_price):.2f} USDT
- ATR(14): {float(market_analysis.atr_smooth):.2f}
- ADX(1h): {float(market_analysis.adx_1h):.2f}
- ADX(4h): {float(market_analysis.adx_4h):.2f}
- 每格利润率: {float(grid_params.profit_rate) * 100:.2f}%
"""

        # 网格参数
        grid_params_text = f"""
📐 建议网格参数
- 网格模式: {grid_params.grid_mode.value}
- 价格区间: {float(grid_params.lower_boundary):.2f} - {float(grid_params.upper_boundary):.2f} USDT
- 网格数量: {grid_params.grid_count} 格
- 网格间距: {float(grid_params.grid_spacing):.2f} USDT
"""

        # 止盈止损
        stop_loss_text = f"""
🎯 止盈止损
- 终止最低价: {float(grid_params.stop_loss_low):.2f} USDT
- 终止最高价: {float(grid_params.stop_loss_high):.2f} USDT
"""

        # 上移/下移功能
        move_text = ""
        if grid_params.stop_move_up_price:
            move_text += f"""
📈 上移功能（启用）
- 停止上移价格: {float(grid_params.stop_move_up_price):.2f} USDT
"""
        if grid_params.stop_move_down_price:
            move_text += f"""
📉 下移功能（启用）
- 停止下移价格: {float(grid_params.stop_move_down_price):.2f} USDT
"""

        # 资金可行性板块（V2.5.5 方案B；开关关闭时整体回退 V2.5 文案）
        if self.capital_constraint_enabled:
            funding_text = self._build_funding_text(
                market_analysis, grid_params, capital_feasibility, margin_used
            )
        else:
            funding_text = self._build_legacy_funding_text(
                market_analysis, grid_params, capital_feasibility
            )

        # 操作指令
        operation_text = f"""
💡 操作指令：
1. 登录币安APP → 永续合约 → 策略交易 → 运行中，终止当前 {symbol} 网格（如有）。
2. 点击"创建网格" → 合约网格。
3. 填入以上价格区间、网格数量、网格模式。
4. 设置杠杆（建议{self.default_leverage}x）、总投入金额（根据您的资金能力）。
5. 高级设置中，启用"上移/下移"并填入停止价格（如适用），设置止盈止损价格。
6. 确认创建前请检查每格下单数量≥{self.min_quantity} ETH。
"""

        # V2.5 保证金引导板块（插入在资金配置与操作指令之间；skipped 时不拼装）
        # V2.5.5：尾部「每格张数」注意行由 signal_bot 注入可行性结果（设计 4.5），
        # 保持 MarginAdvisor 不反向依赖网格可行性；开关关闭时保持 V2.5 原文案
        margin_section = ""
        if advice is not None and not advice.skipped:
            qty_note = None
            if self.capital_constraint_enabled:
                qty_note = (
                    f"按建议保证金 {float(margin_used):.0f} USDT 计算，"
                    f"每格 {float(capital_feasibility.qty_per_grid):.2f} ETH"
                    f"（需≥{self.min_quantity} ETH）"
                )
            margin_section = self.margin_advisor.format_section(advice, qty_note=qty_note)

        # 组合消息（保证金板块仅在非空时插入）
        parts = [
            title, market_data, grid_params_text, stop_loss_text,
            move_text, funding_text, margin_section, operation_text
        ]
        message = "\n\n".join(part.strip() for part in parts if part.strip())

        return message.strip()

    @staticmethod
    def _append_divergence(message: str, advice: Optional[MarginAdvice]) -> str:
        """
        追加 BTC 背离提示行到警报末尾（V2.5，仅非空时追加）

        Args:
            message: 原始警报消息
            advice: 保证金引导建议（可为 None）

        Returns:
            追加后的消息
        """
        if advice is not None and advice.divergence_line:
            return f"{message}\n\n⚠️ 背离: {advice.divergence_line}"
        return message

    def _generate_trend_accelerating_message(
        self,
        symbol: str,
        market_analysis: MarketAnalysis,
        advice: Optional[MarginAdvice] = None
    ) -> str:
        """
        生成趋势急剧增强警报消息（V2.3新增）

        当2h内1h ADX上升超过trend_acceleration_threshold时触发。

        Args:
            symbol: 交易对
            market_analysis: 市场分析结果
            advice: 保证金引导建议（V2.5，末尾追加 BTC 背离提示）

        Returns:
            推送消息
        """
        adx_current = float(market_analysis.adx_1h)
        adx_prev = float(market_analysis.adx_prev_1h)
        acceleration = adx_current - adx_prev if adx_prev > 0 else 0
        message = f"""
⚠️ 【网格信号灯】趋势急剧增强

📊 ADX 在 2 小时内从 {adx_prev:.1f} 升至 {adx_current:.1f} (+{acceleration:.1f})

⚠️ 风险提示
趋势正在加速，即使未达到极端阈值，也建议暂停网格或启用只做单向挂单。

💡 操作：考虑终止网格或取消所有逆势挂单。
""".strip()
        return self._append_divergence(message, advice)

    def _generate_extreme_strong_message(
        self,
        symbol: str,
        market_analysis: MarketAnalysis,
        advice: Optional[MarginAdvice] = None
    ) -> str:
        """
        生成极端强趋势警报消息

        Args:
            symbol: 交易对
            market_analysis: 市场分析结果
            advice: 保证金引导建议（V2.5，末尾追加 BTC 背离提示）

        Returns:
            推送消息
        """
        direction = "上升" if market_analysis.ema20_1h > market_analysis.ema50_1h else "下降"
        message = f"""
🚨 【网格信号灯】极端强趋势警报 - 必须立即终止

📊 市场状态
- 交易对：{symbol}
- 1h ADX：{float(market_analysis.adx_1h):.1f}（极端强趋势）
- 4h ADX：{float(market_analysis.adx_4h):.1f}
- 价格：{float(market_analysis.current_price):.2f} USDT
- 方向：{direction}

⚠️ 风险提示
ADX 超过 {self.market_detector.adx_extreme_strong}，市场处于极端单边行情。任何逆势网格都会快速亏损。

💡 操作指令：
请立即终止当前 {symbol} 网格，不要犹豫。
等待 1h ADX 回落到 {self.market_detector.recovery_adx_strong_1h} 以下，且 4h ADX < {self.market_detector.recovery_adx_strong_4h} 时再考虑重建。
""".strip()
        return self._append_divergence(message, advice)

    def _generate_normal_strong_message(
        self,
        symbol: str,
        market_analysis: MarketAnalysis,
        advice: Optional[MarginAdvice] = None
    ) -> str:
        """
        生成普通强趋势警报消息

        Args:
            symbol: 交易对
            market_analysis: 市场分析结果
            advice: 保证金引导建议（V2.5，末尾追加 BTC 背离提示）

        Returns:
            推送消息
        """
        direction = "上升" if market_analysis.ema20_1h > market_analysis.ema50_1h else "下降"
        message = f"""
⚠️ 【网格信号灯】强趋势警报 - 建议终止网格

📊 市场状态
- 交易对：{symbol}
- 1h ADX：{float(market_analysis.adx_1h):.1f}（强趋势）
- 4h ADX：{float(market_analysis.adx_4h):.1f}（确认）
- 价格：{float(market_analysis.current_price):.2f} USDT
- 方向：{direction}（1h/4h EMA 同向确认）

⚠️ 风险提示
当前处于确认的强趋势，中性网格大概率逆势亏损。

💡 操作指令：
建议立即终止当前 {symbol} 网格。等待后续 ADX 回落到 {self.market_detector.recovery_adx_strong_1h} 以下再重建。
""".strip()
        return self._append_divergence(message, advice)

    def _generate_volatility_abnormal_message(self, symbol: str, market_analysis: MarketAnalysis) -> str:
        """
        生成波动率异常警报消息

        Args:
            symbol: 交易对
            market_analysis: 市场分析结果

        Returns:
            推送消息
        """
        atr_current = float(market_analysis.atr_smooth)
        atr_2h_ago = float(market_analysis.atr_2h_ago)
        change_pct = ((atr_current / atr_2h_ago) - 1) * 100 if atr_2h_ago > 0 else 0
        return f"""
🌊 【网格信号灯】波动率异常警报 - 暂停挂单

📊 波动率数据
- 交易对：{symbol}
- 当前 ATR(14)：{atr_current:.2f}
- 2小时前 ATR：{atr_2h_ago:.2f}
- 变化率：+{change_pct:.1f}%
- 价格：{float(market_analysis.current_price):.2f} USDT

⚠️ 风险提示
ATR 在 2 小时内飙升 {change_pct:.1f}%，市场可能出现剧烈单边行情。

💡 操作指令：
1. 立即取消当前网格的所有挂单（但不平仓），暂停网格运行。
2. 等待 2 小时后系统重新巡检，若 ATR 回落则推送恢复通知。
3. 若价格已大幅偏离，建议直接终止网格止损。
""".strip()

    def _generate_price_emergency_message(self, symbol: str, market_analysis: MarketAnalysis) -> str:
        """
        生成价格行为紧急触发消息（V2.4新增，第1层预警）

        当1h变动>=3%或15m变动>=1.5%时触发，0延迟。

        Args:
            symbol: 交易对
            market_analysis: 市场分析结果

        Returns:
            推送消息
        """
        pct_1h = float(market_analysis.price_change_1h) * 100
        pct_15m = float(market_analysis.price_change_15m) * 100
        trigger_reason = ""
        if abs(pct_1h) >= 3:
            trigger_reason = f"1h 价格变动 {pct_1h:+.2f}%（超过+/-3%阈值）"
        if abs(pct_15m) >= 1.5:
            trigger_reason += f"；15m 价格变动 {pct_15m:+.2f}%（超过+/-1.5%阈值）"

        return f"""
🚨🚨 【网格信号灯】价格行为紧急触发 - 必须立即终止网格 🚨🚨

📊 触发原因
{trigger_reason}

📊 市场数据
- 价格: {float(market_analysis.current_price):.2f} USDT
- 1h ADX: {float(market_analysis.adx_1h):.1f}
- 15m ADX: {float(market_analysis.adx_15m):.1f}
- 4h ADX: {float(market_analysis.adx_4h):.1f}

⚠️ 风险提示
价格行为直接触发紧急预警（第1层），0延迟响应。市场可能出现极端单边行情，任何网格都会快速亏损。
请 **立即终止** 当前所有网格，不要犹豫。

💡 恢复条件：等待价格变动率回落到正常范围，且ADX指标恢复正常后，系统会推送恢复通知。
""".strip()

    def _generate_early_warning_15m_message(self, symbol: str, market_analysis: MarketAnalysis) -> str:
        """
        生成15m ADX早期预警消息（V2.4新增，第2层预警）

        当15m ADX>=50且1h变动>=1%时触发，比1h ADX快4倍。

        Args:
            symbol: 交易对
            market_analysis: 市场分析结果

        Returns:
            推送消息
        """
        pct_1h = float(market_analysis.price_change_1h) * 100
        return f"""
⚠️⚠️ 【网格信号灯】15m ADX 早期预警 - 建议暂停网格 ⚠️⚠️

📊 预警信号
- 15m ADX: {float(market_analysis.adx_15m):.1f}（超过{self.market_detector.adx_early_warning_15m}阈值，趋势加速中）
- 1h 价格变动: {pct_1h:+.2f}%（超过+/-1%阈值）
- 1h ADX: {float(market_analysis.adx_1h):.1f}
- 价格: {float(market_analysis.current_price):.2f} USDT

⚠️ 风险提示
15分钟级别ADX已触发早期预警（第2层），比1h ADX快4倍。趋势可能正在加速形成。
建议立即暂停网格挂单，等待市场方向明确后再操作。

💡 操作建议：
1. 取消所有逆势挂单
2. 可保留顺势挂单
3. 密切关注后续1h ADX是否确认趋势
""".strip()

    def _generate_trend_confirmed_1h_message(
        self,
        symbol: str,
        market_analysis: MarketAnalysis,
        advice: Optional[MarginAdvice] = None
    ) -> str:
        """
        生成1h ADX(10)趋势确认消息（V2.4新增，第3层预警）

        当1h ADX(10)>=55时触发，ADX计算周期从14缩短为10。

        Args:
            symbol: 交易对
            market_analysis: 市场分析结果
            advice: 保证金引导建议（V2.5，末尾追加 BTC 背离提示）

        Returns:
            推送消息
        """
        direction = "上升" if market_analysis.ema20_1h > market_analysis.ema50_1h else "下降"
        message = f"""
🚨 【网格信号灯】1h ADX(10) 趋势确认 - 必须立即终止网格 🚨

📊 市场数据
- 1h ADX(10): {float(market_analysis.adx_1h):.1f}（超过{self.market_detector.emergency_adx_threshold}阈值，趋势已确认）
- 4h ADX: {float(market_analysis.adx_4h):.1f}
- 15m ADX: {float(market_analysis.adx_15m):.1f}
- 价格: {float(market_analysis.current_price):.2f} USDT
- 方向: {direction}
- 1h 价格变动: {float(market_analysis.price_change_1h) * 100:+.2f}%

⚠️ 风险提示
1h ADX(10)已确认强趋势（第3层），ADX周期从14缩短为10，反应速度提升约40%。
市场处于单边行情，任何逆势网格都会快速亏损。

💡 操作指令：
请立即终止当前所有网格。

🔄 恢复条件：
等待 1h ADX 回落到 {self.market_detector.recovery_adx_strong_1h} 以下再考虑重建。""".strip()
        return self._append_divergence(message, advice)

    def _generate_recovery_message(self, symbol: str, market_analysis: MarketAnalysis) -> str:
        """
        生成趋势恢复消息

        Args:
            symbol: 交易对
            market_analysis: 市场分析结果

        Returns:
            推送消息
        """
        return f"""
✅ 【网格信号灯】趋势减弱 - 可重新创建网格

📊 市场状态
- 交易对：{symbol}
- 1h ADX：{float(market_analysis.adx_1h):.1f}
- 4h ADX：{float(market_analysis.adx_4h):.1f}
- 价格：{float(market_analysis.current_price):.2f} USDT

市场已从强趋势/波动率异常恢复，可以重新创建网格或恢复挂单。
""".strip()


def _now_ms() -> int:
    """当前墙钟毫秒（出口状态时间戳）。"""
    return int(time.time() * 1000)


def _datetime_ms(value: datetime) -> int:
    """naive/带时区 datetime 转墙钟毫秒（同 signal.timestamp 口径）。"""
    return int(value.timestamp() * 1000)
