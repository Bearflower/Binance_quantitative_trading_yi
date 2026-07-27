"""
网格交易策略回测引擎 V2
======================
薄层回测引擎，只负责"时间推进 + 成交模拟"。
所有策略逻辑（网格计算、市场状态检测、仓位管理、风控）复用 strategies/grid/ 业务模块。

核心改进：
- 复用 GridCalculator 进行动态网格参数计算和层级生成
- 复用 MarketStateDetector 的核心算法判断市场状态
- 集成市场状态切换处理，解决孤儿仓 BUG
- 支持移动止盈模拟
- 支持 ATR 边界止损

作者：资深Python工程师
版本：V2.0.0
日期：2026-05-09
"""

from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from enum import Enum
import structlog
import pandas as pd
import yaml
import sys
import os

# 添加项目根目录到路径，确保可以导入 strategies 模块
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from backtest.grid.data_loader import DataLoader
from strategies.grid.grid_calculator import GridCalculator, GridLevel, DynamicGridParams, GridMode
from strategies.grid.market_state import MarketStateDetector, MarketState, MarketAnalysis
from strategies.grid.position_manager import PositionManager
from strategies.grid.risk_manager import RiskManager
from shared.indicators import TechnicalIndicators

logger = structlog.get_logger()


# ============================================================================
# 回测专用工具类
# ============================================================================

class TrailingStopSimulator:
    """
    回测移动止盈模拟器

    负责模拟移动止盈逻辑：
    - 当总盈利达到 profit_trigger（如15%）时启动追踪
    - 记录历史最高价
    - 从最高价回撤 trailing_percent（如5%）时触发止盈信号
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化移动止盈模拟器

        Args:
            config: 完整配置字典
        """
        trailing_config = config.get('strategy', {}).get('trailing_stop', {})
        self.enabled = trailing_config.get('enabled', True)
        self.profit_trigger = Decimal(str(trailing_config.get('profit_trigger', 0.15)))
        self.trailing_percent = Decimal(str(trailing_config.get('trailing_percent', 0.05)))

        # 运行时状态
        self.activated = False
        self.peak_price: Optional[Decimal] = None
        self.stop_price: Optional[Decimal] = None
        self.entry_price: Optional[Decimal] = None

        logger.info(
            "移动止盈模拟器初始化完成",
            enabled=self.enabled,
            profit_trigger=float(self.profit_trigger),
            trailing_percent=float(self.trailing_percent)
        )

    def set_entry(self, entry_price: Decimal) -> None:
        """
        设置入场价格，重置追踪状态

        Args:
            entry_price: 入场价格
        """
        self.entry_price = entry_price
        self.activated = False
        self.peak_price = None
        self.stop_price = None

    def reset(self) -> None:
        """重置追踪状态"""
        self.activated = False
        self.peak_price = None
        self.stop_price = None
        self.entry_price = None

    def check_and_update(self, current_price: Decimal) -> bool:
        """
        检查并更新移动止盈状态

        Args:
            current_price: 当前价格

        Returns:
            True 表示触发止盈，应平仓
        """
        if not self.enabled:
            return False
        if self.entry_price is None or self.entry_price <= 0:
            return False

        pnl_ratio = (current_price - self.entry_price) / self.entry_price

        if not self.activated:
            # 未启动：检查是否达到启动条件
            if pnl_ratio >= self.profit_trigger:
                self.activated = True
                self.peak_price = current_price
                self.stop_price = current_price * (Decimal('1') - self.trailing_percent)
                logger.info(
                    "移动止盈已启动",
                    entry_price=float(self.entry_price),
                    current_price=float(current_price),
                    pnl_ratio=float(pnl_ratio) * 100,
                    peak_price=float(self.peak_price),
                    stop_price=float(self.stop_price)
                )
            return False
        else:
            # 已启动：更新峰值和止盈价
            if current_price > self.peak_price:
                self.peak_price = current_price
                self.stop_price = current_price * (Decimal('1') - self.trailing_percent)
                logger.debug(
                    "更新移动止盈价",
                    peak_price=float(self.peak_price),
                    stop_price=float(self.stop_price)
                )

            # 检查是否触发
            if current_price <= self.stop_price:
                logger.warning(
                    "触发移动止盈",
                    current_price=float(current_price),
                    stop_price=float(self.stop_price),
                    peak_price=float(self.peak_price),
                    entry_price=float(self.entry_price)
                )
                return True

        return False


class BacktestExchangeSimulator:
    """
    回测交易所模拟器

    负责：
    - 加载和管理多时间框架 K 线数据
    - 按索引推进时间线
    - 提供当前和历史的 K 线数据查询
    - 支持不同时间框架之间的索引映射
    """

    def __init__(self, data_loader: DataLoader):
        """
        初始化交易所模拟器

        Args:
            data_loader: 数据加载器实例
        """
        self.data_loader = data_loader
        self.tf_data: Dict[str, List[Dict[str, Any]]] = {}
        self.klines_1h: List[Dict[str, Any]] = []
        self.klines_4h: List[Dict[str, Any]] = []
        self.klines_15m: List[Dict[str, Any]] = []
        self.current_index: int = 0

        # 加载数据
        self._load_data()

        # 预计算 1h 到 4h 的索引映射（用于市场状态检测）
        self._index_1h_to_4h: Dict[int, int] = self._build_tf_index_map('1h', '4h')

        logger.info(
            "交易所模拟器初始化完成",
            klines_1h=len(self.klines_1h),
            klines_4h=len(self.klines_4h),
            klines_15m=len(self.klines_15m)
        )

    def _load_data(self) -> None:
        """加载多时间框架数据"""
        self.tf_data = self.data_loader.load_multi_timeframe_data()
        self.klines_1h = self.tf_data.get('1h', [])
        self.klines_4h = self.tf_data.get('4h', [])
        self.klines_15m = self.tf_data.get('15m', [])

        if not self.klines_1h:
            raise ValueError("1小时K线数据为空，无法进行回测")

    def _build_tf_index_map(self, source_tf: str, target_tf: str) -> Dict[int, int]:
        """
        构建时间框架索引映射

        对于每个 source_tf 的索引 i，找到 target_tf 中时间戳 <= source_tf[i] 的最大索引

        Args:
            source_tf: 源时间框架
            target_tf: 目标时间框架

        Returns:
            索引映射字典
        """
        source_klines = self.tf_data.get(source_tf, [])
        target_klines = self.tf_data.get(target_tf, [])

        if not source_klines or not target_klines:
            return {}

        index_map = {}
        target_idx = 0

        for i, sk in enumerate(source_klines):
            src_ts = pd.to_datetime(sk['timestamp'])
            # 推进 target_idx 直到 target 时间戳 <= 源时间戳
            while target_idx + 1 < len(target_klines):
                next_ts = pd.to_datetime(target_klines[target_idx + 1]['timestamp'])
                if next_ts <= src_ts:
                    target_idx += 1
                else:
                    break
            index_map[i] = target_idx

        return index_map

    def advance_to(self, index: int) -> Dict[str, Any]:
        """
        推进到指定 K 线索引

        Args:
            index: 目标索引（1h 时间框架）

        Returns:
            当前 K 线数据
        """
        self.current_index = index
        return self.klines_1h[index]

    def get_current_kline(self, interval: str = '1h') -> Dict[str, Any]:
        """
        获取当前 K 线数据

        Args:
            interval: 时间框架

        Returns:
            当前 K 线数据
        """
        klines = self.tf_data.get(interval, [])
        mapped_idx = self._index_1h_to_4h.get(self.current_index, 0) if interval == '4h' else self.current_index
        if mapped_idx >= len(klines):
            mapped_idx = len(klines) - 1
        return klines[mapped_idx]

    def get_klines_up_to(self, interval: str = '1h') -> List[Dict[str, Any]]:
        """
        获取从开始到当前索引的 K 线数据

        Args:
            interval: 时间框架

        Returns:
            K 线数据列表
        """
        klines = self.tf_data.get(interval, [])
        end_idx = self._index_1h_to_4h.get(self.current_index, self.current_index) if interval == '4h' else self.current_index
        return klines[:end_idx + 1]

    def get_mapped_index(self, interval: str) -> int:
        """
        获取当前 1h 索引在目标时间框架中的映射索引

        Args:
            interval: 目标时间框架

        Returns:
            映射后的索引
        """
        if interval == '1h':
            return self.current_index
        return self._index_1h_to_4h.get(self.current_index, 0)


class BacktestMarketStateWrapper:
    """
    回测市场状态检测包装器

    复用 MarketStateDetector 的核心算法（_calculate_indicators, _determine_state,
    _calculate_trend_strength, _calculate_smooth_atr），
    提供同步的市场状态检测接口，适配回测场景的预加载数据。
    """

    def __init__(self, config: Dict[str, Any], exchange: BacktestExchangeSimulator):
        """
        初始化市场状态检测包装器

        Args:
            config: 完整配置字典
            exchange: 交易所模拟器实例
        """
        self.config = config
        self.exchange = exchange

        strategy_config = config.get('strategy', {})
        market_config = strategy_config.get('market', {})

        self.adx_oscillation = market_config.get('adx_oscillation', 20)
        self.adx_trend = market_config.get('adx_trend', 25)
        self.adx_strong = market_config.get('adx_strong', 40)
        self.ema_fast = market_config.get('ema_fast', 20)
        self.ema_slow = market_config.get('ema_slow', 50)
        self.atr_period = market_config.get('atr_period', 14)

        # 预计算指标，避免重复计算
        self._precompute_indicators()

        logger.info(
            "市场状态检测包装器初始化完成",
            adx_oscillation=self.adx_oscillation,
            adx_trend=self.adx_trend,
            adx_strong=self.adx_strong
        )

    def _precompute_indicators(self) -> None:
        """预计算所有技术指标序列"""
        # 1h 指标
        df_1h = pd.DataFrame(self.exchange.klines_1h)
        self._adx_1h_series = TechnicalIndicators.calculate_adx(df_1h, period=14)
        self._ema20_1h_series = TechnicalIndicators.calculate_ema(df_1h, period=self.ema_fast)
        self._ema50_1h_series = TechnicalIndicators.calculate_ema(df_1h, period=self.ema_slow)
        self._atr_1h_series = TechnicalIndicators.calculate_atr(df_1h, period=self.atr_period)

        # 平滑 ATR
        self._atr_smooth_series = self._atr_1h_series.ewm(span=self.atr_period, adjust=False).mean()

        # 4h 指标
        if self.exchange.klines_4h:
            df_4h = pd.DataFrame(self.exchange.klines_4h)
            self._adx_4h_series = TechnicalIndicators.calculate_adx(df_4h, period=14)
        else:
            self._adx_4h_series = pd.Series(dtype=float)

        logger.info(
            "技术指标预计算完成",
            adx_1h_points=len(self._adx_1h_series.dropna()),
            adx_4h_points=len(self._adx_4h_series.dropna()) if not self._adx_4h_series.empty else 0
        )

    def detect_at_index(self, index_1h: int) -> Optional[MarketAnalysis]:
        """
        在指定索引处同步检测市场状态

        Args:
            index_1h: 1h K 线的索引位置

        Returns:
            市场分析结果，指标不足时返回 None
        """
        # 检查指标是否有效（至少需要 50 根 K 线计算 ADX）
        if index_1h < 50:
            return None

        try:
            # 获取 1h 指标值
            adx_1h_val = self._adx_1h_series.iloc[index_1h]
            ema20_1h_val = self._ema20_1h_series.iloc[index_1h]
            ema50_1h_val = self._ema50_1h_series.iloc[index_1h]
            atr_smooth_val = self._atr_smooth_series.iloc[index_1h]
            current_price = Decimal(str(self.exchange.klines_1h[index_1h]['close']))

            if pd.isna(adx_1h_val) or pd.isna(ema20_1h_val) or pd.isna(ema50_1h_val):
                return None

            adx_1h = Decimal(str(adx_1h_val))
            ema20_1h = Decimal(str(ema20_1h_val))
            ema50_1h = Decimal(str(ema50_1h_val))
            atr_smooth = Decimal(str(atr_smooth_val)) if not pd.isna(atr_smooth_val) else Decimal('0')

            # 获取 4h ADX
            index_4h = self.exchange.get_mapped_index('4h')
            if not self._adx_4h_series.empty and index_4h < len(self._adx_4h_series):
                adx_4h_val = self._adx_4h_series.iloc[index_4h]
                adx_4h = Decimal(str(adx_4h_val)) if not pd.isna(adx_4h_val) else Decimal('0')
            else:
                adx_4h = Decimal('0')

            # 判断市场状态（复用 MarketStateDetector 的逻辑）
            state, confidence = self._determine_state(adx_1h, adx_4h, ema20_1h, ema50_1h, current_price)

            # 计算趋势强度
            trend_strength = self._calculate_trend_strength(adx_1h)

            analysis = MarketAnalysis(
                state=state,
                trend_strength=trend_strength,
                adx_1h=adx_1h,
                adx_4h=adx_4h,
                ema20_1h=ema20_1h,
                ema50_1h=ema50_1h,
                current_price=current_price,
                atr_smooth=atr_smooth,
                confidence=confidence
            )

            return analysis

        except Exception as e:
            logger.warning(
                f"市场状态检测异常: index={index_1h}",
                error=str(e)
            )
            return None

    def _determine_state(
        self,
        adx_1h: Decimal,
        adx_4h: Decimal,
        ema20_1h: Decimal,
        ema50_1h: Decimal,
        current_price: Decimal
    ) -> Tuple[MarketState, Decimal]:
        """
        判断市场状态

        与 MarketStateDetector._determine_state 保持一致的逻辑：
        1. 4h ADX >= 40 → 强趋势暂停
        2. 1h ADX < 20 → 震荡市场
        3. 1h ADX >= 25 → 趋势市场（方向由 EMA 决定，4h 需确认）
        4. 其他情况 → 默认震荡
        """
        # 强趋势暂停
        if adx_4h >= Decimal(str(self.adx_strong)):
            return MarketState.STRONG_TREND_PAUSE, Decimal('0.9')

        # 震荡市场
        if adx_1h < Decimal(str(self.adx_oscillation)):
            return MarketState.OSCILLATION, Decimal('0.8')

        # 趋势市场
        if adx_1h >= Decimal(str(self.adx_trend)):
            is_uptrend = ema20_1h > ema50_1h and current_price > ema20_1h
            is_downtrend = ema20_1h < ema50_1h and current_price < ema20_1h

            if is_uptrend:
                if adx_4h >= Decimal(str(self.adx_trend)):
                    return MarketState.UPTREND, Decimal('0.85')
                else:
                    return MarketState.OSCILLATION, Decimal('0.6')

            elif is_downtrend:
                if adx_4h >= Decimal(str(self.adx_trend)):
                    return MarketState.DOWNTREND, Decimal('0.85')
                else:
                    return MarketState.OSCILLATION, Decimal('0.6')

        # 默认震荡
        return MarketState.OSCILLATION, Decimal('0.5')

    def _calculate_trend_strength(self, adx_1h: Decimal) -> Decimal:
        """
        计算趋势强度系数 k

        公式：k = min(0.5, max(0, (ADX - 25) / 30))
        """
        if adx_1h < Decimal(str(self.adx_trend)):
            return Decimal('0')

        k = (adx_1h - Decimal(str(self.adx_trend))) / Decimal('30')
        k = max(Decimal('0'), min(Decimal('0.5'), k))
        return k


# ============================================================================
# 主引擎
# ============================================================================

class BacktestEngineV2:
    """
    回测引擎 V2

    薄层设计，只负责"时间推进 + 成交模拟"。
    所有策略逻辑复用 strategies/grid/ 业务模块。

    核心职责：
    - 按时间线推进 K 线
    - 模拟订单撮合（限价单成交判定）
    - 管理账户余额和持仓
    - 记录交易历史和资金曲线
    - 协调市场状态检测、网格初始化、风控检查
    """

    def __init__(self, config_path: str):
        """
        初始化回测引擎 V2

        Args:
            config_path: 配置文件路径

        Raises:
            ValueError: 配置验证失败
        """
        # 加载配置
        self.config = self._load_config(config_path)

        # 应用 ETHUSDT 专版默认值覆盖
        self._apply_ethusdt_defaults()

        # 回测参数
        backtest_config = self.config.get('backtest', {})
        self.initial_balance = Decimal(str(backtest_config.get('initial_balance', 10000)))
        self.commission_rate = Decimal(str(backtest_config.get('commission_rate', 0.0004)))
        self.slippage_rate = Decimal(str(backtest_config.get('slippage_rate', 0.0001)))
        self.start_date = backtest_config.get('start_date', '2025-11-09')
        self.end_date = backtest_config.get('end_date', '2026-05-09')

        # 策略参数
        strategy_config = self.config.get('strategy', {})
        trading_config = strategy_config.get('trading', {})
        self.leverage = trading_config.get('leverage', 10)
        self.margin = Decimal(str(trading_config.get('margin', 500)))
        self.min_quantity = trading_config.get('min_quantity', 1)

        # ---- 基础设施 ----
        self.data_loader = DataLoader(self.config)
        self.exchange = BacktestExchangeSimulator(self.data_loader)

        # ---- 复用策略业务模块 ----
        # GridCalculator：直接复用（只需 config 字典）
        self.grid_calculator = GridCalculator(strategy_config)

        # MarketStateDetector：通过包装器复用核心算法
        self.market_detector = BacktestMarketStateWrapper(self.config, self.exchange)

        # PositionManager：复用（使用空依赖，回测不需要币安API和数据库）
        self.position_manager = PositionManager(
            binance_client=None,   # 回测不需要真实API
            db=None,              # 回测不需要持久化
            config=strategy_config
        )

        # RiskManager：复用（使用空依赖）
        self.risk_manager = RiskManager(
            binance_client=None,
            db=None,
            notification_client=None,
            config=strategy_config
        )

        # 回测专用移动止盈模拟器
        self.trailing_stop = TrailingStopSimulator(self.config)

        # ---- 运行时状态 ----
        self.balance: Decimal = self.initial_balance
        self.position: Optional[Dict[str, Any]] = None
        self.trades: List[Dict[str, Any]] = []
        self.equity_curve: List[Dict[str, Any]] = []

        # 网格状态
        self.grid_state: Optional[Dict[str, Any]] = None
        self.grid_levels: List[GridLevel] = []
        self.active_orders: List[Dict[str, Any]] = []
        self.current_grid_params: Optional[DynamicGridParams] = None

        # 市场状态
        self.current_market_state: Optional[MarketState] = None
        market_config = strategy_config.get('market', {})
        self.market_check_interval: int = market_config.get('check_interval', 24)  # 每 N 根 1h K 线检查一次市场状态（从配置读取）
        self.last_market_check_index: int = -1

        # ATR 边界止损参数（从配置读取，避免硬编码）
        risk_config = strategy_config.get('risk', {})
        self.atr_boundary_buffer: Decimal = Decimal(
            str(risk_config.get('atr_boundary_buffer', 2))
        )  # 边界外 NxATR 触发止损

        # 统计计数器
        self._grid_rebuild_count: int = 0
        self._market_transition_count: int = 0
        self._trailing_stop_trigger_count: int = 0
        self._atr_stop_trigger_count: int = 0

        logger.info(
            "回测引擎 V2 初始化完成",
            initial_balance=float(self.initial_balance),
            commission_rate=float(self.commission_rate),
            slippage_rate=float(self.slippage_rate),
            leverage=self.leverage,
            margin=float(self.margin),
            start_date=self.start_date,
            end_date=self.end_date,
            market_check_interval=self.market_check_interval
        )

    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """
        加载配置文件

        Args:
            config_path: 配置文件路径

        Returns:
            配置字典

        Raises:
            FileNotFoundError: 配置文件不存在
            yaml.YAMLError: 配置格式错误
        """
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        logger.info(f"配置文件加载成功: {config_path}")
        return config

    def _apply_ethusdt_defaults(self) -> None:
        """
        应用 ETHUSDT 专版默认值

        确保策略配置使用 ETHUSDT 专版的参数值。
        如果配置文件中未显式设置，则使用以下默认值：
        - base_grid_count: 20
        - min_grid_count: 8
        - max_grid_count: 30
        - oscillation ATR 倍数: 4
        """
        strategy_config = self.config.setdefault('strategy', {})

        # 网格数量配置
        grid_config = strategy_config.setdefault('grid', {})
        if 'base_grid_count' not in grid_config:
            grid_config['base_grid_count'] = 20
        if 'min_grid_count' not in grid_config:
            grid_config['min_grid_count'] = 8
        if 'max_grid_count' not in grid_config:
            grid_config['max_grid_count'] = 30

        # ATR 倍数配置
        market_config = strategy_config.setdefault('market', {})
        atr_multipliers = market_config.setdefault('atr_multipliers', {})
        if 'oscillation' not in atr_multipliers:
            atr_multipliers['oscillation'] = 4

        logger.info(
            "ETHUSDT 专版默认值已应用",
            base_grid_count=grid_config['base_grid_count'],
            min_grid_count=grid_config['min_grid_count'],
            max_grid_count=grid_config['max_grid_count'],
            oscillation_atr_multiplier=atr_multipliers['oscillation']
        )

    # ========================================================================
    # 主流程
    # ========================================================================

    def run(self) -> Dict[str, Any]:
        """
        运行回测

        Returns:
            回测结果字典，包含统计信息、交易记录、资金曲线等

        Raises:
            RuntimeError: 回测执行失败
        """
        logger.info("=" * 60)
        logger.info("回测引擎 V2 开始运行")
        logger.info("=" * 60)

        try:
            # 1. 获取主周期 K 线数据
            klines = self.exchange.klines_1h
            if len(klines) < 50:
                raise RuntimeError(f"K线数据不足，至少需要50根，实际为 {len(klines)} 根")

            logger.info(f"K线总数: {len(klines)} 根")

            # 2. 主循环：遍历每根 K 线（从第 50 根开始，确保指标计算有效）
            for i in range(50, len(klines)):
                current_kline = klines[i]
                current_time = current_kline['timestamp']
                current_price = Decimal(str(current_kline['close']))

                # 时间范围检查
                if current_time < self.start_date:
                    continue
                if current_time > self.end_date:
                    logger.info(f"到达回测结束时间: {current_time}")
                    break

                # 推进交易所时间线
                self.exchange.advance_to(i)

                # 处理当前 K 线
                self._process_kline_v2(current_time, current_price, i)

                # 记录资金曲线
                self._record_equity(current_time, current_price)

                # 进度日志（从配置读取间隔）
                progress_interval = self.config.get('engine', {}).get('progress_log_interval', 500)
                if (i - 50) % progress_interval == 0 and i > 50:
                    progress = (i - 50) / (len(klines) - 50) * 100
                    logger.info(
                        f"回测进度: {progress:.1f}% ({i - 50}/{len(klines) - 50} 根K线)",
                        balance=float(self.balance),
                        trades=len(self.trades),
                        market_state=self.current_market_state.value if self.current_market_state else "未初始化"
                    )

            # 3. 回测结束：平仓所有持仓
            if self.position and self.position.get('quantity', Decimal('0')) > 0:
                last_kline = klines[-1]
                self._close_all_positions(
                    last_kline['timestamp'],
                    Decimal(str(last_kline['close'])),
                    "回测结束强制平仓"
                )

            # 4. 统计分析
            statistics = self._calculate_statistics()

            logger.info("=" * 60)
            logger.info(
                "回测引擎 V2 运行完成",
                final_balance=float(self.balance),
                total_trades=len(self.trades),
                total_return_pct=statistics.get('total_pnl_percent', 0),
                grid_rebuilds=self._grid_rebuild_count,
                market_transitions=self._market_transition_count,
                trailing_stop_triggers=self._trailing_stop_trigger_count,
                atr_stop_triggers=self._atr_stop_trigger_count
            )
            logger.info("=" * 60)

            return {
                'statistics': statistics,
                'trades': self.trades,
                'equity_curve': self.equity_curve,
                'config': self.config,
                'engine_info': {
                    'version': 'V2.0.0',
                    'grid_rebuild_count': self._grid_rebuild_count,
                    'market_transition_count': self._market_transition_count,
                    'trailing_stop_trigger_count': self._trailing_stop_trigger_count,
                    'atr_stop_trigger_count': self._atr_stop_trigger_count,
                }
            }

        except Exception as e:
            logger.error(f"回测执行失败: {e}", exc_info=True)
            raise RuntimeError(f"回测执行失败: {e}") from e

    def _process_kline_v2(
        self,
        current_time: str,
        current_price: Decimal,
        index: int
    ) -> None:
        """
        处理单根 K 线（V2 核心逻辑）

        处理流程：
        1. 定期检查市场状态（每 market_check_interval 根 K 线）
        2. 如果没有网格 → 尝试初始化
        3. 检查订单成交 → 更新持仓和余额
        4. 检查移动止盈 → 触发则平仓
        5. 检查 ATR 边界止损 → 突破则平仓

        Args:
            current_time: 当前时间
            current_price: 当前价格
            index: K 线索引
        """
        # 1. 定期检查市场状态
        if index - self.last_market_check_index >= self.market_check_interval:
            self._check_market_state(current_time, current_price, index)
            self.last_market_check_index = index

        # 2. 如果没有网格 → 尝试初始化
        if not self.grid_state:
            self._try_initialize_grid(current_time, current_price, index)
            return

        # 3. 检查订单成交 → 更新持仓和余额
        self._check_order_fills(current_time, current_price, index)

        # 4. 检查移动止盈 → 触发则平仓
        if self.position and self.position.get('quantity', Decimal('0')) > 0:
            if self.trailing_stop.check_and_update(current_price):
                self._close_all_positions(current_time, current_price, "移动止盈触发")
                self._trailing_stop_trigger_count += 1
                return

        # 5. 检查 ATR 边界止损 → 价格突破边界外 ±2xATR 则平仓
        if self.position and self.position.get('quantity', Decimal('0')) > 0:
            self._check_atr_boundary_stop(current_time, current_price)

    # ========================================================================
    # 市场状态检测与切换
    # ========================================================================

    def _check_market_state(
        self,
        current_time: str,
        current_price: Decimal,
        index: int
    ) -> None:
        """
        检查市场状态并处理状态切换

        Args:
            current_time: 当前时间
            current_price: 当前价格
            index: K 线索引
        """
        analysis = self.market_detector.detect_at_index(index)
        if analysis is None:
            return

        new_state = analysis.state
        old_state = self.current_market_state

        # 首次检测，直接记录状态
        if old_state is None:
            self.current_market_state = new_state
            logger.info(
                f"初始市场状态: {new_state.value}",
                time=current_time,
                price=float(current_price),
                adx_1h=float(analysis.adx_1h),
                adx_4h=float(analysis.adx_4h),
                confidence=float(analysis.confidence)
            )
            return

        # 状态未变化，跳过
        if old_state == new_state:
            return

        # 状态发生变化，处理切换
        logger.warning(
            f"市场状态切换: {old_state.value} → {new_state.value}",
            time=current_time,
            price=float(current_price),
            adx_1h=float(analysis.adx_1h),
            adx_4h=float(analysis.adx_4h)
        )

        self._handle_market_state_transition(current_time, new_state, analysis, old_state)
        self._market_transition_count += 1

    def _handle_market_state_transition(
        self,
        current_time: str,
        new_state: MarketState,
        analysis: MarketAnalysis,
        old_state: MarketState
    ) -> None:
        """
        处理市场状态切换（解决孤儿仓 BUG 的核心逻辑）

        四种切换场景：
        - Case1: 进入强趋势暂停 → 撤单 + 平仓
        - Case2: 震荡 → 趋势 → 撤旧单 + 保留持仓 + 新参数重建网格
        - Case3: 趋势 → 震荡 → 撤旧单 + 保留持仓 + 新参数重建网格
        - Case4: 上升 ↔ 下降 → 撤单 + 平仓（方向反转）

        Args:
            current_time: 当前时间
            new_state: 新市场状态
            analysis: 市场分析结果
            old_state: 旧市场状态
        """
        current_price = analysis.current_price

        # Case1: 进入强趋势暂停 → 撤单 + 平仓
        if new_state == MarketState.STRONG_TREND_PAUSE:
            logger.warning(
                f"【Case1】进入强趋势暂停，撤单并平仓",
                time=current_time,
                price=float(current_price),
                adx_4h=float(analysis.adx_4h)
            )
            self._cancel_all_orders(current_time)
            self._close_all_positions(current_time, current_price, "强趋势暂停")
            self.grid_state = None
            self.grid_levels = []
            self.current_grid_params = None
            self.current_market_state = new_state
            return

        # Case2: 震荡 → 趋势 → 撤旧单 + 保留持仓 + 新参数重建网格
        if old_state == MarketState.OSCILLATION and new_state in (MarketState.UPTREND, MarketState.DOWNTREND):
            logger.info(
                f"【Case2】震荡→趋势，保留持仓并重建网格",
                time=current_time,
                old_state=old_state.value,
                new_state=new_state.value
            )
            self._cancel_all_orders(current_time)
            self._rebuild_grid_with_new_params(current_time, analysis, "震荡→趋势，网格参数重建")
            self.current_market_state = new_state
            return

        # Case3: 趋势 → 震荡 → 撤旧单 + 保留持仓 + 新参数重建网格
        if old_state in (MarketState.UPTREND, MarketState.DOWNTREND) and new_state == MarketState.OSCILLATION:
            logger.info(
                f"【Case3】趋势→震荡，保留持仓并重建网格",
                time=current_time,
                old_state=old_state.value,
                new_state=new_state.value
            )
            self._cancel_all_orders(current_time)
            self._rebuild_grid_with_new_params(current_time, analysis, "趋势→震荡，网格参数重建")
            self.current_market_state = new_state
            return

        # Case4: 上升 ↔ 下降 → 撤单 + 平仓（方向反转）
        if (old_state == MarketState.UPTREND and new_state == MarketState.DOWNTREND) or \
           (old_state == MarketState.DOWNTREND and new_state == MarketState.UPTREND):
            logger.warning(
                f"【Case4】趋势方向反转，撤单并平仓",
                time=current_time,
                old_state=old_state.value,
                new_state=new_state.value,
                price=float(current_price)
            )
            self._cancel_all_orders(current_time)
            self._close_all_positions(current_time, current_price, f"趋势方向反转 ({old_state.value}→{new_state.value})")
            self.grid_state = None
            self.grid_levels = []
            self.current_grid_params = None
            self.current_market_state = new_state
            return

        # 其他情况：安全处理
        logger.warning(
            f"未预期的市场状态切换: {old_state.value} → {new_state.value}",
            time=current_time
        )
        self.current_market_state = new_state

    # ========================================================================
    # 网格初始化与重建
    # ========================================================================

    def _try_initialize_grid(
        self,
        current_time: str,
        current_price: Decimal,
        index: int
    ) -> None:
        """
        尝试初始化网格

        仅在市场状态不是 STRONG_TREND_PAUSE 时初始化

        Args:
            current_time: 当前时间
            current_price: 当前价格
            index: K 线索引
        """
        if self.grid_state is not None:
            return

        # 获取市场分析
        analysis = self.market_detector.detect_at_index(index)
        if analysis is None:
            return

        # 强趋势暂停时不初始化网格
        if analysis.state == MarketState.STRONG_TREND_PAUSE:
            logger.debug(
                f"强趋势暂停，跳过网格初始化",
                time=current_time,
                price=float(current_price)
            )
            self.current_market_state = MarketState.STRONG_TREND_PAUSE
            return

        self.current_market_state = analysis.state

        # 检查数据是否足够计算基准 ATR
        klines_1h = self.exchange.get_klines_up_to('1h')
        atr_baseline_period = (
            self.config.get('strategy', {}).get('market', {}).get('atr_baseline_period', 90)
        )
        if len(klines_1h) < atr_baseline_period:
            logger.debug(
                f"数据不足，无法计算基准ATR（需要{atr_baseline_period}根，当前{len(klines_1h)}根），跳过网格初始化",
                time=current_time
            )
            return

        try:
            # 计算基准 ATR
            atr_baseline = self.grid_calculator.calculate_baseline_atr(klines_1h)

            # 计算动态网格参数
            grid_params = self.grid_calculator.calculate_dynamic_grid_params(
                current_price=analysis.current_price,
                atr_smooth=analysis.atr_smooth,
                atr_baseline=atr_baseline,
                market_state=analysis.state.value,
                trend_strength=analysis.trend_strength
            )

            # 将动态参数同步到 GridCalculator，确保 calculate_grid_levels 使用动态计算的间距
            self.grid_calculator.grid_spacing = grid_params.grid_spacing
            self.grid_calculator.grid_count = grid_params.grid_count

            # 计算网格层级
            grid_levels = self.grid_calculator.calculate_grid_levels(
                current_price=current_price,
                volatility=None  # 动态网格间距已通过 grid_spacing 传入
            )

            if not grid_levels:
                logger.warning("网格层级为空，跳过初始化")
                return

            # 保存网格状态
            self.grid_state = {
                'initialized_at': current_time,
                'initial_price': current_price,
                'market_state': analysis.state.value,
                'params': grid_params
            }
            self.grid_levels = grid_levels
            self.current_grid_params = grid_params

            # 创建初始订单
            self._create_grid_orders(current_time, grid_levels)

            # 设置移动止盈入场价
            self.trailing_stop.set_entry(current_price)

            logger.info(
                f"网格初始化成功",
                time=current_time,
                market_state=analysis.state.value,
                grid_count=grid_params.grid_count,
                grid_mode=grid_params.grid_mode.value,
                price_range=f"[{float(grid_params.lower_boundary):.2f}, {float(grid_params.upper_boundary):.2f}]",
                atr_smooth=float(analysis.atr_smooth),
                orders_created=len(self.active_orders)
            )

        except Exception as e:
            logger.error(
                f"网格初始化失败",
                time=current_time,
                error=str(e),
                exc_info=True
            )

    def _rebuild_grid_with_new_params(
        self,
        current_time: str,
        analysis: MarketAnalysis,
        reason: str
    ) -> None:
        """
        使用新的市场参数重建网格（保留持仓）

        在状态切换 Case2/Case3 时调用，保留现有持仓只重建订单

        Args:
            current_time: 当前时间
            analysis: 市场分析结果
            reason: 重建原因
        """
        current_price = analysis.current_price

        try:
            # 计算基准 ATR
            klines_1h = self.exchange.get_klines_up_to('1h')
            atr_baseline = self.grid_calculator.calculate_baseline_atr(klines_1h)

            # 计算新的动态网格参数
            grid_params = self.grid_calculator.calculate_dynamic_grid_params(
                current_price=current_price,
                atr_smooth=analysis.atr_smooth,
                atr_baseline=atr_baseline,
                market_state=analysis.state.value,
                trend_strength=analysis.trend_strength
            )

            # 将动态参数同步到 GridCalculator，确保 calculate_grid_levels 使用动态计算的间距
            self.grid_calculator.grid_spacing = grid_params.grid_spacing
            self.grid_calculator.grid_count = grid_params.grid_count

            # 计算新的网格层级
            grid_levels = self.grid_calculator.calculate_grid_levels(
                current_price=current_price,
                volatility=None  # 动态网格间距已通过 grid_spacing 传入
            )

            if not grid_levels:
                logger.warning("网格层级为空，跳过重建")
                return

            # 更新网格状态
            self.grid_state = {
                'initialized_at': current_time,
                'initial_price': current_price,
                'market_state': analysis.state.value,
                'params': grid_params
            }
            self.grid_levels = grid_levels
            self.current_grid_params = grid_params

            # 创建新订单
            self._create_grid_orders(current_time, grid_levels)

            self._grid_rebuild_count += 1

            logger.info(
                f"网格重建成功: {reason}",
                time=current_time,
                new_state=analysis.state.value,
                grid_count=grid_params.grid_count,
                grid_mode=grid_params.grid_mode.value,
                price_range=f"[{float(grid_params.lower_boundary):.2f}, {float(grid_params.upper_boundary):.2f}]",
                position_quantity=float(self.position.get('quantity', 0)) if self.position else 0
            )

        except Exception as e:
            logger.error(
                f"网格重建失败: {reason}",
                time=current_time,
                error=str(e),
                exc_info=True
            )

    # ========================================================================
    # 订单管理
    # ========================================================================

    def _create_grid_orders(
        self,
        current_time: str,
        grid_levels: List[GridLevel]
    ) -> None:
        """
        为网格层级创建初始订单

        Args:
            current_time: 当前时间
            grid_levels: 网格层级列表
        """
        self.active_orders = []

        for level in grid_levels:
            if level.side == 'HOLD':
                continue

            order = {
                'order_id': f"order_{len(self.active_orders)}",
                'time': current_time,
                'price': level.price,
                'side': level.side,
                'quantity': level.quantity,
                'status': 'PENDING',
                'level': level.level
            }
            self.active_orders.append(order)

        logger.debug(f"创建网格订单: {len(self.active_orders)} 个")

    def _cancel_all_orders(self, current_time: str) -> None:
        """
        撤销所有活跃订单

        Args:
            current_time: 当前时间
        """
        cancelled = len(self.active_orders)
        self.active_orders = []
        if cancelled > 0:
            logger.info(f"撤销所有订单: {cancelled} 个", time=current_time)

    def _check_order_fills(
        self,
        current_time: str,
        current_price: Decimal,
        index: int
    ) -> None:
        """
        检查订单成交（使用当前 K 线的高低价格区间）

        买单成交条件：最低价 <= 订单价格
        卖单成交条件：最高价 >= 订单价格
        这样可以更真实地模拟订单在 K 线范围内的成交

        Args:
            current_time: 当前时间
            current_price: 当前收盘价
            index: K 线索引
        """
        kline = self.exchange.get_current_kline('1h')
        high_price = Decimal(str(kline['high']))
        low_price = Decimal(str(kline['low']))

        filled_orders = []

        for order in self.active_orders:
            if order['status'] != 'PENDING':
                continue

            order_price = order['price']

            if order['side'] == 'BUY' and low_price <= order_price:
                # 买单成交（使用最低价判断，确保 K 线范围内触及过订单价）
                filled_orders.append(order)
            elif order['side'] == 'SELL' and high_price >= order_price:
                # 卖单成交（使用最高价判断）
                filled_orders.append(order)

        # 处理成交订单
        for order in filled_orders:
            self._execute_order(order, current_time, current_price)

    def _execute_order(
        self,
        order: Dict[str, Any],
        current_time: str,
        current_price: Decimal
    ) -> None:
        """
        执行订单成交

        Args:
            order: 订单信息
            current_time: 当前时间
            current_price: 当前收盘价（用于计算盈亏参考）
        """
        order_price = order['price']
        order_side = order['side']
        order_quantity = order['quantity']

        # 计算滑点后的执行价格
        if order_side == 'BUY':
            execution_price = order_price * (Decimal('1') + self.slippage_rate)
        else:
            execution_price = order_price * (Decimal('1') - self.slippage_rate)

        # 计算手续费
        commission = execution_price * order_quantity * self.commission_rate

        # 初始化持仓（如果为空）
        if not self.position:
            self.position = {
                'quantity': Decimal('0'),
                'entry_price': Decimal('0'),
                'entry_time': None,
                'total_cost': Decimal('0')
            }

        pos = self.position
        pnl = Decimal('0')

        if order_side == 'BUY':
            # ---- 买入 ----
            cost = execution_price * order_quantity
            self.balance -= (cost + commission)

            old_quantity = pos['quantity']

            if old_quantity == Decimal('0'):
                # 新开仓
                pos['entry_price'] = execution_price
                pos['entry_time'] = current_time
                pos['total_cost'] = cost
                pos['quantity'] = order_quantity

                # 设置移动止盈入场价
                self.trailing_stop.set_entry(execution_price)
            else:
                # 加仓：更新平均成本
                new_quantity = old_quantity + order_quantity
                pos['total_cost'] += cost
                pos['entry_price'] = pos['total_cost'] / new_quantity
                pos['quantity'] = new_quantity

                # 更新移动止盈入场价（使用加权平均）
                self.trailing_stop.set_entry(pos['entry_price'])

        else:
            # ---- 卖出 ----
            revenue = execution_price * order_quantity
            self.balance += (revenue - commission)

            old_quantity = pos['quantity']

            if old_quantity > Decimal('0'):
                # 计算已实现盈亏
                pnl = (execution_price - pos['entry_price']) * min(order_quantity, old_quantity)

            pos['quantity'] -= order_quantity

            if pos['quantity'] <= Decimal('0'):
                # 完全平仓
                pos['quantity'] = Decimal('0')
                pos['entry_price'] = Decimal('0')
                pos['entry_time'] = None
                pos['total_cost'] = Decimal('0')

                # 重置移动止盈
                self.trailing_stop.reset()

        # 标记订单为已成交
        order['status'] = 'FILLED'
        order['execution_price'] = execution_price
        order['execution_time'] = current_time

        # 确定交易原因
        reason = "网格交易"

        # 记录交易
        trade = {
            'time': current_time,
            'side': order_side,
            'price': float(execution_price),
            'quantity': float(order_quantity),
            'pnl': float(pnl),
            'commission': float(commission),
            'reason': reason,
            'balance': float(self.balance)
        }
        self.trades.append(trade)

        # 更新 PositionManager（复用策略模块）
        try:
            symbol = self.config.get('symbol', 'ETHUSDT')
            self.position_manager.update_position(
                symbol=symbol,
                side=order_side,
                quantity=order_quantity,
                price=execution_price
            )
        except Exception as e:
            logger.debug(f"更新 PositionManager 失败（不影响回测）: {e}")

        # 更新风控日盈亏
        try:
            self.risk_manager.update_daily_pnl(pnl)
        except Exception as e:
            logger.debug(f"更新 RiskManager 失败（不影响回测）: {e}")

        # 创建反向订单
        self._create_reverse_order(order, current_time)

        logger.debug(
            f"订单成交: {order_side}",
            time=current_time,
            price=float(execution_price),
            quantity=float(order_quantity),
            pnl=float(pnl),
            balance=float(self.balance),
            position=float(self.position.get('quantity', 0)) if self.position else 0
        )

    def _create_reverse_order(
        self,
        filled_order: Dict[str, Any],
        current_time: str
    ) -> None:
        """
        创建反向订单（网格交易核心逻辑：成交后自动反向挂单）

        Args:
            filled_order: 已成交订单
            current_time: 当前时间
        """
        if not self.grid_state or not self.current_grid_params:
            return

        grid_params = self.current_grid_params
        order_price = filled_order['price']
        order_side = filled_order['side']

        # 根据网格模式计算反向价格
        if grid_params.grid_mode == GridMode.ARITHMETIC:
            spacing = grid_params.grid_spacing
            if order_side == 'BUY':
                reverse_price = order_price + spacing
                reverse_side = 'SELL'
            else:
                reverse_price = order_price - spacing
                reverse_side = 'BUY'
        else:
            # 等比网格
            if grid_params.grid_spacing > 0 and order_price > 0:
                ratio = Decimal('1') + grid_params.grid_spacing / order_price
                if order_side == 'BUY':
                    reverse_price = order_price * ratio
                    reverse_side = 'SELL'
                else:
                    reverse_price = order_price / ratio
                    reverse_side = 'BUY'
            else:
                logger.warning("无效的网格间距，跳过反向订单创建")
                return

        # 确保价格大于 0
        if reverse_price <= 0:
            logger.warning(f"反向价格无效: {reverse_price}，跳过反向订单创建")
            return

        # 创建反向订单
        reverse_order = {
            'order_id': f"order_{len(self.active_orders)}",
            'time': current_time,
            'price': reverse_price,
            'side': reverse_side,
            'quantity': filled_order['quantity'],
            'status': 'PENDING',
            'level': filled_order.get('level', 0)
        }
        self.active_orders.append(reverse_order)

    # ========================================================================
    # 平仓操作
    # ========================================================================

    def _close_all_positions(
        self,
        current_time: str,
        current_price: Decimal,
        reason: str
    ) -> None:
        """
        平仓所有持仓

        Args:
            current_time: 当前时间
            current_price: 当前平仓价格
            reason: 平仓原因
        """
        if not self.position or self.position.get('quantity', Decimal('0')) <= 0:
            return

        pos = self.position
        quantity = pos['quantity']
        entry_price = pos.get('entry_price', Decimal('0'))

        # 计算盈亏
        pnl = Decimal('0')
        if entry_price > 0:
            pnl = (current_price - entry_price) * quantity

        # 计算手续费
        commission = current_price * quantity * self.commission_rate

        # 更新余额
        revenue = current_price * quantity
        self.balance += (revenue - commission)

        # 记录交易
        trade = {
            'time': current_time,
            'side': 'SELL',
            'price': float(current_price),
            'quantity': float(quantity),
            'pnl': float(pnl),
            'commission': float(commission),
            'reason': reason,
            'balance': float(self.balance)
        }
        self.trades.append(trade)

        logger.warning(
            f"平仓: {reason}",
            time=current_time,
            entry_price=float(entry_price) if entry_price > 0 else 0,
            exit_price=float(current_price),
            quantity=float(quantity),
            pnl=float(pnl),
            balance=float(self.balance)
        )

        # 清空持仓
        self.position = None

        # 撤销所有订单
        self._cancel_all_orders(current_time)

        # 重置移动止盈
        self.trailing_stop.reset()

        # 更新风控日盈亏
        try:
            self.risk_manager.update_daily_pnl(pnl)
        except Exception:
            pass

    # ========================================================================
    # ATR 边界止损
    # ========================================================================

    def _check_atr_boundary_stop(
        self,
        current_time: str,
        current_price: Decimal
    ) -> None:
        """
        检查 ATR 边界止损

        当价格突破网格边界 ± 2xATR 时触发止损平仓

        Args:
            current_time: 当前时间
            current_price: 当前价格
        """
        if not self.current_grid_params:
            return

        params = self.current_grid_params

        # 获取当前 ATR（从市场检测器的最新分析中获取）
        # 使用当前的 atr_smooth 值
        analysis = self.market_detector.detect_at_index(self.exchange.current_index)
        if analysis is None:
            return

        atr = analysis.atr_smooth
        if atr <= 0:
            return

        buffer = self.atr_boundary_buffer * atr

        # 超出上边界
        if current_price > params.upper_boundary + buffer:
            logger.warning(
                f"价格突破上边界+2xATR，触发止损",
                current_price=float(current_price),
                upper_boundary=float(params.upper_boundary),
                atr=float(atr),
                buffer=float(buffer)
            )
            self._close_all_positions(current_time, current_price, f"ATR上边界止损")
            self._atr_stop_trigger_count += 1
            self.grid_state = None
            self.grid_levels = []
            self.current_grid_params = None

        # 跌破下边界
        elif current_price < params.lower_boundary - buffer:
            logger.warning(
                f"价格跌破下边界-2xATR，触发止损",
                current_price=float(current_price),
                lower_boundary=float(params.lower_boundary),
                atr=float(atr),
                buffer=float(buffer)
            )
            self._close_all_positions(current_time, current_price, f"ATR下边界止损")
            self._atr_stop_trigger_count += 1
            self.grid_state = None
            self.grid_levels = []
            self.current_grid_params = None

    # ========================================================================
    # 资金曲线记录
    # ========================================================================

    def _record_equity(self, current_time: str, current_price: Decimal) -> None:
        """
        记录资金曲线数据点

        Args:
            current_time: 当前时间
            current_price: 当前价格
        """
        # 计算持仓价值
        position_value = Decimal('0')
        if self.position and self.position.get('quantity', Decimal('0')) > 0:
            position_value = self.position['quantity'] * current_price

        # 总权益 = 余额 + 持仓价值
        total_equity = self.balance + position_value

        self.equity_curve.append({
            'time': current_time,
            'balance': float(self.balance),
            'equity': float(total_equity),
            'price': float(current_price),
            'position_value': float(position_value),
            'has_position': self.position is not None and self.position.get('quantity', Decimal('0')) > 0
        })

    # ========================================================================
    # 统计分析
    # ========================================================================

    def _calculate_statistics(self) -> Dict[str, Any]:
        """
        计算回测统计指标

        Returns:
            统计指标字典
        """
        if not self.trades:
            return {
                'total_trades': 0,
                'total_pnl': 0,
                'total_pnl_percent': 0,
                'win_rate': 0,
                'max_drawdown': 0,
                'sharpe_ratio': 0,
                'initial_balance': float(self.initial_balance),
                'final_balance': float(self.balance)
            }

        # 总盈亏
        total_pnl = self.balance - self.initial_balance
        total_pnl_percent = float(total_pnl / self.initial_balance * 100)

        # 统计卖出交易（平仓操作）的盈亏
        sell_trades = [t for t in self.trades if t.get('side') == 'SELL' and t.get('pnl', 0) != 0]
        winning_trades = [t for t in sell_trades if t.get('pnl', 0) > 0]
        losing_trades = [t for t in sell_trades if t.get('pnl', 0) < 0]

        win_rate = len(winning_trades) / len(sell_trades) * 100 if sell_trades else 0

        # 最大回撤（基于权益曲线）
        max_drawdown = Decimal('0')
        peak = Decimal(str(float(self.initial_balance)))

        for point in self.equity_curve:
            equity = Decimal(str(point.get('equity', point.get('balance', 0))))
            if equity > peak:
                peak = equity
            if peak > 0:
                drawdown = (peak - equity) / peak
                if drawdown > max_drawdown:
                    max_drawdown = drawdown

        # 交易天数
        trading_days = 0
        if self.trades:
            try:
                first_time = pd.to_datetime(self.trades[0]['time'])
                last_time = pd.to_datetime(self.trades[-1]['time'])
                trading_days = max(1, (last_time - first_time).days)
            except Exception:
                trading_days = 0

        # 年化收益率
        annualized_return = 0
        if trading_days > 0 and total_pnl_percent != 0:
            annualized_return = total_pnl_percent * (365 / trading_days)

        # 夏普比率（简化计算）
        sharpe_ratio = Decimal('0')
        if self.equity_curve and len(self.equity_curve) > 1:
            df = pd.DataFrame(self.equity_curve)
            equity_col = df.get('equity', df.get('balance', pd.Series([0])))
            if len(equity_col) > 1:
                daily_returns = [float(equity_col.iloc[i] / equity_col.iloc[i - 1] - 1)
                                 for i in range(1, len(equity_col))]
                if daily_returns:
                    avg_return = sum(daily_returns) / len(daily_returns)
                    std_return = (sum((r - avg_return) ** 2 for r in daily_returns) / len(daily_returns)) ** 0.5
                    if std_return > 0:
                        sharpe_ratio = Decimal(str(avg_return / std_return * (252 ** 0.5)))

        return {
            'total_trades': len(self.trades),
            'total_pnl': float(total_pnl),
            'total_pnl_percent': round(total_pnl_percent, 2),
            'win_rate': round(win_rate, 2),
            'max_drawdown': round(float(max_drawdown * 100), 2),
            'sharpe_ratio': round(float(sharpe_ratio), 2),
            'annualized_return': round(annualized_return, 2),
            'trading_days': trading_days,
            'initial_balance': float(self.initial_balance),
            'final_balance': float(self.balance),
            'grid_rebuild_count': self._grid_rebuild_count,
            'market_transition_count': self._market_transition_count,
            'trailing_stop_trigger_count': self._trailing_stop_trigger_count,
            'atr_stop_trigger_count': self._atr_stop_trigger_count,
        }


# ============================================================================
# 向后兼容别名
# ============================================================================

# 保持 BacktestEngine 别名，确保 run_backtest.py 等调用方兼容
BacktestEngine = BacktestEngineV2