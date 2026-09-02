"""
网格回测引擎核心模块

基于历史K线逐根模拟网格策略的挂单、成交、持仓管理。
纯Python实现，不依赖pandas、数据库或网络，保证确定性输出。

核心算法：
1. 初始化网格层级：根据当前价格和网格参数计算各层挂单价
2. 逐根K线遍历：检查价格是否触及挂单价，模拟成交
3. 手续费和滑点：maker 0.04% / taker 0.06% / 滑点 0.01%
4. 止盈止损检查
5. 回测结束后计算统计指标
"""

from datetime import datetime
from typing import Any, Dict, List

import structlog

from ai_tuner.backtest.market_segment import MarketSegment, MarketSegmenter
from ai_tuner.backtest.metrics import MetricsCalculator
from ai_tuner.backtest.models import (
    BacktestDataError,
    BacktestState,
    BacktestTimeoutError,
    FillRecord,
    GridParams,
)

logger = structlog.get_logger()


# ============================================================
# 回测引擎
# ============================================================

class GridBacktestEngine:
    """网格策略逐笔回测引擎

    纯Python实现，不依赖pandas、数据库或网络。
    相同输入保证相同输出，无随机性。
    """

    def __init__(self, config: Dict[str, Any]):
        """
        从 ai_tuner/config.yaml 的 backtest 段初始化配置

        Args:
            config: 完整系统配置字典（ai_tuner/config.yaml）
        """
        self._load_config(config)

        # 初始化子模块
        self._metrics_calc = MetricsCalculator(
            return_weight=self._return_weight,
            drawdown_weight=self._drawdown_weight,
            sharpe_weight=self._sharpe_weight,
            return_score_scale=self._return_score_scale,
            drawdown_cap=self._drawdown_cap,
            sharpe_score_scale=self._sharpe_score_scale,
        )
        self._segmenter = MarketSegmenter(
            trend_threshold=self._trend_threshold,
            min_segment_length=self._min_segment_length,
        )

        logger.info(
            "回测引擎初始化完成",
            maker_fee=self._maker_fee,
            taker_fee=self._taker_fee,
            slippage=self._slippage,
            min_kline_count=self._min_kline_count,
        )

    def _load_config(self, config: Dict[str, Any]) -> None:
        """从配置字典加载所有回测参数"""
        backtest_cfg = config.get("backtest", {})

        # 费率配置
        fee_cfg = backtest_cfg.get("fee", {})
        self._maker_fee = float(fee_cfg.get("maker", 0.0004))
        self._taker_fee = float(fee_cfg.get("taker", 0.0006))
        self._slippage = float(fee_cfg.get("slippage", 0.0001))

        # 评分权重配置
        scoring_cfg = backtest_cfg.get("scoring", {})
        self._return_weight = float(scoring_cfg.get("return_weight", 0.40))
        self._drawdown_weight = float(scoring_cfg.get("drawdown_weight", 0.30))
        self._sharpe_weight = float(scoring_cfg.get("sharpe_weight", 0.30))

        # 评分计算参数
        self._return_score_scale = float(backtest_cfg.get("return_score_scale", 10))
        self._drawdown_cap = float(backtest_cfg.get("drawdown_cap", 0.20))
        self._sharpe_score_scale = float(backtest_cfg.get("sharpe_score_scale", 50))

        # 性能限制
        self._max_execution_time = int(backtest_cfg.get("max_execution_time", 10))
        self._min_kline_count = int(backtest_cfg.get("min_kline_count", 24))

        # 市况分段参数
        regime_cfg = backtest_cfg.get("market_regime", {})
        self._trend_threshold = float(regime_cfg.get("trend_threshold", 0.02))
        self._min_segment_length = int(regime_cfg.get("min_segment_length", 8))

        # 网格间距兜底参数
        self._fallback_spacing_pct = float(backtest_cfg.get("fallback_spacing_pct", 0.005))

    def run(
        self,
        klines: List[Dict[str, Any]],
        params: GridParams,
    ) -> Dict[str, Any]:
        """
        执行回测

        Args:
            klines: K线数据列表，每项包含 open_time/open/high/low/close/volume
            params: 网格参数

        Returns:
            回测结果字典，包含所有指标字段

        Raises:
            BacktestDataError: K线数据不足
        """
        if not klines or len(klines) < self._min_kline_count:
            kline_count = len(klines) if klines else 0
            logger.warning("K线数据不足，无法执行回测", kline_count=kline_count)
            raise BacktestDataError(
                f"K线数据不足：当前 {kline_count} 根，至少需要 {self._min_kline_count} 根"
            )

        # 0. 缓存参数（供内部方法使用）
        self._cache_params(params)

        # 1. 初始化状态
        state = self._init_state(params)

        # 2. 计算网格层级
        grid_levels = self._calc_grid_levels(params)

        # 3. 初始化挂单
        self._init_orders(state, grid_levels, params)

        # 4. 分段市场状态
        segments = self._segmenter.segment(klines)
        regime_map = self._build_regime_map(segments, len(klines))

        # 5. 逐根K线遍历
        for i, kline in enumerate(klines):
            self._process_kline(state, kline, i, regime_map)

        # 6. 计算最终权益
        final_price = float(klines[-1]["close"])
        final_equity = state.cash + state.position * final_price

        # 7. 计算各项指标
        initial_capital = params.margin * params.leverage
        result = self._metrics_calc.calculate(
            fills=state.fills,
            equity_curve=state.equity_curve,
            final_state=state,
            kline_count=len(klines),
            initial_capital=initial_capital,
            final_equity=final_equity,
        )

        logger.info(
            "回测完成",
            kline_count=len(klines),
            fill_count=result["fill_count"],
            total_return_pct=round(result["total_return_pct"], 2),
            composite_score=round(result["composite_score"], 1),
        )

        return result

    # ============================================================
    # 参数缓存
    # ============================================================

    def _cache_params(self, params: GridParams) -> None:
        """缓存回测参数，供内部方法使用"""
        self._cached_params = params
        self._cached_grid_spacing = params.atr * params.grid_spacing_atr_multiplier
        if self._cached_grid_spacing <= 0:
            self._cached_grid_spacing = params.current_price * self._fallback_spacing_pct
        self._cached_stop_loss_pct = params.stop_loss_percent
        self._cached_hard_stop_loss = abs(params.hard_stop_loss)

    # ============================================================
    # 初始化方法
    # ============================================================

    def _init_state(self, params: GridParams) -> BacktestState:
        """初始化回测状态"""
        initial_capital = params.margin * params.leverage
        return BacktestState(
            cash=initial_capital,
            position=0.0,
            entry_price=0.0,
            last_price=params.current_price,
            equity_curve=[initial_capital],
            peak_equity=initial_capital,
        )

    def _calc_grid_levels(self, params: GridParams) -> List[float]:
        """
        计算等差网格层级

        网格间距 = ATR × grid_spacing_atr_multiplier
        价格区间 = [当前价 - 间距×网格数/2, 当前价 + 间距×网格数/2]

        Returns:
            网格档位价格列表，从低到高排列
        """
        grid_spacing = self._cached_grid_spacing
        half_range = grid_spacing * params.base_grid_count / 2
        price_low = params.current_price - half_range

        levels = []
        for i in range(params.base_grid_count + 1):
            level = price_low + i * grid_spacing
            levels.append(level)

        return levels

    def _init_orders(
        self, state: BacktestState, grid_levels: List[float], params: GridParams
    ) -> None:
        """
        初始化挂单

        规则：
        - 档位 < 当前价格：挂买单（等待价格下跌到该档位时买入）
        - 档位 > 当前价格：挂卖单（等待价格上涨到该档位时卖出）
        - 档位 == 当前价格：不挂单（避免立即成交）

        Args:
            state: 回测状态
            grid_levels: 网格层级价格列表
            params: 网格参数
        """
        current_price = params.current_price
        # 每格名义价值 = 单格保证金 × 杠杆
        notional_per_grid = params.single_position_margin * params.leverage

        for level in grid_levels:
            if level <= 0:
                continue
            qty = notional_per_grid / level
            if level < current_price:
                # 低于当前价格：挂买单
                state.buy_orders[level] = qty
            elif level > current_price:
                # 高于当前价格：挂卖单
                state.sell_orders[level] = qty

    # ============================================================
    # K线处理
    # ============================================================

    def _process_kline(
        self,
        state: BacktestState,
        kline: Dict[str, Any],
        kline_index: int,
        regime_map: Dict[int, str],
    ) -> None:
        """
        处理单根K线：检查挂单成交、更新状态

        Args:
            state: 当前回测状态
            kline: K线数据
            kline_index: K线序号
            regime_map: K线序号到市况的映射
        """
        high = float(kline["high"])
        low = float(kline["low"])
        close = float(kline["close"])
        open_time = kline.get("open_time")
        current_regime = regime_map.get(kline_index, "横盘")

        # 检查买单成交
        filled_buy_prices = []
        for buy_price in list(state.buy_orders.keys()):
            if low <= buy_price <= high:
                qty = state.buy_orders[buy_price]
                self._execute_buy(state, buy_price, qty, kline_index, open_time, current_regime)
                filled_buy_prices.append(buy_price)

        # 删除已成交的买单
        for p in filled_buy_prices:
            del state.buy_orders[p]

        # 检查卖单成交
        filled_sell_prices = []
        for sell_price in list(state.sell_orders.keys()):
            if low <= sell_price <= high:
                qty = state.sell_orders[sell_price]
                self._execute_sell(state, sell_price, qty, kline_index, open_time, current_regime)
                filled_sell_prices.append(sell_price)

        # 删除已成交的卖单
        for p in filled_sell_prices:
            del state.sell_orders[p]

        # 更新状态
        state.last_price = close
        equity = state.cash + state.position * close
        state.equity_curve.append(equity)
        state.peak_equity = max(state.peak_equity, equity)

        # 检查止损（如果持仓不为0）
        if state.position != 0 and state.entry_price > 0:
            self._check_stop_loss(state, close, kline_index, open_time, current_regime)

    def _execute_buy(
        self,
        state: BacktestState,
        price: float,
        qty: float,
        kline_index: int,
        open_time: Any,
        regime: str,
    ) -> None:
        """
        执行买入成交

        Args:
            state: 回测状态
            price: 成交价格
            qty: 成交数量
            kline_index: K线序号
            open_time: K线开盘时间
            regime: 市况
        """
        # 计算滑点影响
        slippage_price = price * (1 + self._slippage)
        notional = slippage_price * qty
        fee = notional * self._maker_fee

        # 扣除资金和手续费
        state.cash -= notional + fee
        state.total_fee += fee

        # 更新持仓和开仓均价
        if state.position == 0:
            state.position = qty
            state.entry_price = slippage_price
        else:
            # 加仓：更新开仓均价
            total_value = state.position * state.entry_price + qty * slippage_price
            state.position += qty
            state.entry_price = total_value / state.position if state.position > 0 else 0

        # 记录成交
        self._record_fill(state, "buy", slippage_price, qty, fee, "maker", notional, kline_index, open_time, regime)

        # 在买入价 + 网格间距处挂卖单（止盈挂单）
        grid_spacing = self._get_grid_spacing()
        take_profit_price = price + grid_spacing
        state.sell_orders[take_profit_price] = state.sell_orders.get(take_profit_price, 0) + qty

    def _execute_sell(
        self,
        state: BacktestState,
        price: float,
        qty: float,
        kline_index: int,
        open_time: Any,
        regime: str,
    ) -> None:
        """
        执行卖出成交

        Args:
            state: 回测状态
            price: 成交价格
            qty: 成交数量
            kline_index: K线序号
            open_time: K线开盘时间
            regime: 市况
        """
        # 计算滑点影响
        slippage_price = price * (1 - self._slippage)
        notional = slippage_price * qty
        fee = notional * self._maker_fee

        # 增加资金
        state.cash += notional - fee
        state.total_fee += fee

        # 更新持仓
        state.position -= qty

        # 记录成交
        self._record_fill(state, "sell", slippage_price, qty, fee, "maker", notional, kline_index, open_time, regime)

        # 在卖出价 - 网格间距处挂买单（重新挂单）
        grid_spacing = self._get_grid_spacing()
        re_enter_price = price - grid_spacing
        state.buy_orders[re_enter_price] = state.buy_orders.get(re_enter_price, 0) + qty

    def _check_stop_loss(
        self,
        state: BacktestState,
        close: float,
        kline_index: int,
        open_time: Any,
        regime: str,
    ) -> None:
        """
        检查止损条件

        止损触发条件：持仓亏损超过 stop_loss_percent 或 hard_stop_loss

        Args:
            state: 回测状态
            close: 当前收盘价
            kline_index: K线序号
            open_time: K线开盘时间
            regime: 市况
        """
        # 计算盈亏比例
        if state.entry_price <= 0:
            return

        pnl_pct = (close - state.entry_price) / state.entry_price

        # 硬止损
        hard_stop = self._cached_hard_stop_loss
        if pnl_pct <= -hard_stop:
            self._execute_stop(state, close, kline_index, open_time, regime, "hard_stop")
            return

        # 普通止损
        stop_pct = self._cached_stop_loss_pct
        if pnl_pct <= -stop_pct:
            self._execute_stop(state, close, kline_index, open_time, regime, "stop_loss")

    def _execute_stop(
        self,
        state: BacktestState,
        price: float,
        kline_index: int,
        open_time: Any,
        regime: str,
        stop_type: str,
    ) -> None:
        """
        执行止损平仓

        Args:
            state: 回测状态
            price: 平仓价格
            kline_index: K线序号
            open_time: K线开盘时间
            regime: 市况
            stop_type: 止损类型（"stop_loss" / "hard_stop"）
        """
        if state.position == 0:
            return

        qty = abs(state.position)
        notional = price * qty
        fee = notional * self._taker_fee

        if state.position > 0:
            # 多头平仓：卖出
            state.cash += notional - fee
        else:
            # 空头平仓：买入
            state.cash -= notional + fee

        state.total_fee += fee

        # 记录成交
        self._record_fill(
            state,
            "sell" if state.position > 0 else "buy",
            price, qty, fee, "taker", notional, kline_index, open_time, regime,
        )

        state.position = 0
        state.entry_price = 0

        logger.debug(
            "止损触发",
            stop_type=stop_type,
            price=price,
            qty=qty,
            fee=fee,
        )

    # ============================================================
    # 成交记录辅助方法
    # ============================================================

    @staticmethod
    def _record_fill(
        state: BacktestState,
        direction: str,
        price: float,
        qty: float,
        fee: float,
        fee_type: str,
        notional: float,
        kline_index: int,
        open_time: Any,
        regime: str,
    ) -> None:
        """创建成交记录并追加到状态中"""
        fill = FillRecord(
            kline_index=kline_index,
            open_time=open_time if isinstance(open_time, datetime) else None,
            direction=direction,
            price=price,
            quantity=qty,
            fee=fee,
            fee_type=fee_type,
            notional=notional,
            regime=regime,
        )
        state.fills.append(fill)

    # ============================================================
    # 辅助方法
    # ============================================================

    def _get_grid_spacing(self) -> float:
        """获取缓存的网格间距"""
        return getattr(self, "_cached_grid_spacing", 0.0)

    @staticmethod
    def _build_regime_map(
        segments: List[MarketSegment], kline_count: int
    ) -> Dict[int, str]:
        """
        构建K线序号到市况的映射

        Args:
            segments: 市况分段列表
            kline_count: K线总数

        Returns:
            {kline_index: regime} 映射字典
        """
        regime_map: Dict[int, str] = {}
        for seg in segments:
            for i in range(seg.start_index, seg.end_index + 1):
                regime_map[i] = seg.regime
        # 未覆盖的K线默认为横盘
        for i in range(kline_count):
            if i not in regime_map:
                regime_map[i] = "横盘"
        return regime_map