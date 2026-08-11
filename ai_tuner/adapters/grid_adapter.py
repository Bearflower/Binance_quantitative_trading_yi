"""
网格交易策略适配器（方案C：混合模式）
采集网格策略的周度表现数据，并结合K线模拟推演不同参数组合的预期表现

网格策略特点：
- 半自动信号灯模式，非全自动交易
- 交易记录存储在 grid.grid_trades 表（按次填充记录）
- 没有传统"开仓-平仓"的交易对，而是连续填充的网格订单

方案C（混合模式）：
- 真实数据（grid_trades）：本周实际成交的利润、笔数、胜率
- 模拟推演（K线 + 参数场景）：用本周历史K线模拟不同参数组合下的预期表现
- AI 调优时同时参考真实表现和模拟对比，做出更合理的参数调整建议
"""
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import structlog

from ai_tuner.adapters.base_adapter import (
    BaseAdapter,
    DistributionMetrics,
    PerformanceMetrics,
    RiskMetrics,
    SimulationMetrics,
    StrategyMeta,
    StrategyReport,
)

logger = structlog.get_logger()

# 模拟推演置信度：从配置读取，提供默认值兜底
# 配置路径：ai_tuner/config.yaml -> simulation.fill_efficiency_factor


class GridAdapter(BaseAdapter):
    """网格交易策略数据适配器（方案C：混合模式）"""

    strategy_id = "grid"
    strategy_name = "网格交易策略"
    config_path = "strategies/grid/config.yaml"

    async def collect(self, week_offset: int = 0) -> StrategyReport:
        """
        采集网格策略表现数据

        数据来源：
        1. grid.grid_trades 表：真实成交记录
        2. K线服务：本周1h K线，用于模拟推演
        3. strategies/grid/config.yaml：策略配置
        支持 week_offset 参数，用于查询历史周数据（EffectTracker 回填使用）。

        Args:
            week_offset: 周偏移量
                - 0（默认）: 当前周
                - -1: 上一周（EffectTracker 回填使用）

        Returns:
            StrategyReport: 标准化策略周度体检报告（含 simulation 字段）
        """
        now = datetime.now()
        this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        # 周日定时调度时，本周已基本结束，取本周一（刚结束的周期）
        # 其他天（含周一手动触发）都取上周一
        base_week_start = this_monday if now.weekday() == 6 else this_monday - timedelta(days=7)
        # 应用 week_offset：偏移量 * 7 天
        week_start = base_week_start + timedelta(days=week_offset * 7)
        week_end = week_start + timedelta(days=7)

        report = StrategyReport()
        report.meta = StrategyMeta(
            strategy_id=self.strategy_id,
            strategy_name=self.strategy_name,
            week_start=week_start.strftime("%Y-%m-%d"),
            week_end=week_end.strftime("%Y-%m-%d"),
        )

        try:
            # 读取并缓存策略配置文件
            self._strategy_config_cache = self._read_config()
            config = self._strategy_config_cache
            report.meta.version = config.get("strategy", {}).get("version", "")

            # ========== 第一部分：真实成交数据 ==========
            trades = await self._query_weekly_trades(week_start, week_end)

            if trades:
                report.performance = self._calc_performance(trades)
                report.risk = self._calc_risk(trades)
                report.distribution = self._calc_distribution(trades)
                report.anomalies = self._detect_anomalies(trades, report)
            else:
                logger.info("网格策略本周无成交记录", strategy_id=self.strategy_id)

            # ========== 第二部分：模拟推演 ==========
            symbols = config.get("symbols", ["ETHUSDT"])
            sim_results = await self._simulate(week_start, week_end, symbols)
            report.simulation = sim_results

            # 如果模拟推演发现当前参数与市场状态不匹配，加入异常
            if sim_results:
                self._check_simulation_anomalies(report)

            logger.info(
                "网格策略数据采集完成",
                strategy_id=self.strategy_id,
                total_trades=report.performance.total_trades,
                total_pnl=round(report.performance.total_pnl, 2),
                sim_scenarios=len(sim_results),
            )

        except Exception as e:
            logger.error("网格策略数据采集异常", strategy_id=self.strategy_id, error=str(e))
            report.anomalies.append(f"数据采集异常: {str(e)}")

        return report

    # ============================================================
    # 真实成交数据查询
    # ============================================================

    async def _query_weekly_trades(
        self, week_start: datetime, week_end: datetime
    ) -> List[Dict[str, Any]]:
        """
        查询本周网格成交记录

        Args:
            week_start: 本周起始时间
            week_end: 本周结束时间

        Returns:
            网格成交记录列表
        """
        query = """
            SELECT t.*, c.symbol
            FROM grid.grid_trades t
            LEFT JOIN grid.grid_config c ON t.config_id = c.id
            WHERE t.executed_at >= $1
              AND t.executed_at < $2
              AND t.profit IS NOT NULL
            ORDER BY t.executed_at ASC
        """
        return await self.db_manager.fetch_all(query, week_start, week_end)

    # ============================================================
    # 真实绩效指标计算
    # ============================================================

    def _calc_performance(self, trades: List[Dict[str, Any]]) -> PerformanceMetrics:
        """计算网格策略绩效指标"""
        metrics = PerformanceMetrics()
        metrics.total_trades = len(trades)
        if metrics.total_trades == 0:
            return metrics

        profit_values = []
        for t in trades:
            profit = float(t.get("profit", 0) or 0)
            profit_values.append(profit)

            symbol = t.get("symbol", "") or ""
            if symbol:
                sym_dist = metrics.symbol_distribution
                sym_dist[symbol] = sym_dist.get(symbol, 0) + 1

        metrics.total_pnl = sum(profit_values)
        win_trades = [p for p in profit_values if p > 0]
        loss_trades = [p for p in profit_values if p < 0]
        metrics.win_count = len(win_trades)
        metrics.loss_count = len(loss_trades)
        metrics.win_rate = (
            metrics.win_count / metrics.total_trades
            if metrics.total_trades > 0
            else 0
        )
        metrics.avg_win = sum(win_trades) / len(win_trades) if win_trades else 0
        metrics.avg_loss = (
            abs(sum(loss_trades)) / len(loss_trades) if loss_trades else 0
        )
        metrics.profit_factor = (
            metrics.avg_win / metrics.avg_loss if metrics.avg_loss > 0 else 0
        )
        if len(profit_values) >= 2:
            avg_pnl = metrics.total_pnl / metrics.total_trades
            variance = (
                sum((p - avg_pnl) ** 2 for p in profit_values)
                / (len(profit_values) - 1)
            )
            std_dev = variance**0.5
            metrics.sharpe_approx = avg_pnl / std_dev if std_dev > 0 else 0

        return metrics

    def _calc_risk(self, trades: List[Dict[str, Any]]) -> RiskMetrics:
        """计算网格策略风险指标"""
        metrics = RiskMetrics()
        if not trades:
            return metrics

        current_streak = 0
        max_streak = 0
        for t in trades:
            profit = float(t.get("profit", 0) or 0)
            if profit < 0:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0
        metrics.max_consecutive_losses = max_streak

        cumulative_pnl = 0
        peak_pnl = 0
        max_drawdown = 0
        for t in trades:
            profit = float(t.get("profit", 0) or 0)
            cumulative_pnl += profit
            peak_pnl = max(peak_pnl, cumulative_pnl)
            drawdown = peak_pnl - cumulative_pnl
            max_drawdown = max(max_drawdown, drawdown)

        strategy_config = self._strategy_config_cache
        grid_margin = float(strategy_config.get("trading", {}).get("margin", 500))
        max_positions = int(strategy_config.get("trading", {}).get("max_positions", 2))
        total_capital = grid_margin * max_positions

        metrics.max_drawdown_pct = max_drawdown / total_capital if total_capital > 0 else 0
        metrics.current_drawdown_pct = (peak_pnl - cumulative_pnl) / total_capital if total_capital > 0 else 0

        max_drawdown_threshold = float(
            strategy_config.get("risk", {}).get("max_drawdown", 0.15)
        )
        if metrics.max_drawdown_pct >= max_drawdown_threshold:
            metrics.is_circuit_breaker_active = True

        return metrics

    def _calc_distribution(self, trades: List[Dict[str, Any]]) -> DistributionMetrics:
        """计算网格策略分布指标"""
        metrics = DistributionMetrics()
        if not trades:
            return metrics

        symbol_dist: Dict[str, int] = {}
        for t in trades:
            symbol = t.get("symbol", "") or ""
            if symbol:
                symbol_dist[symbol] = symbol_dist.get(symbol, 0) + 1
        metrics.symbol_distribution = symbol_dist

        side_dist: Dict[str, int] = {}
        for t in trades:
            side = t.get("side", "") or ""
            if side:
                side_dist[side] = side_dist.get(side, 0) + 1
        metrics.signal_distribution = side_dist

        return metrics

    def _detect_anomalies(
        self, trades: List[Dict[str, Any]], report: StrategyReport
    ) -> List[str]:
        """检测网格策略异常事件"""
        anomalies = []
        if not trades:
            return anomalies

        system_config = self._system_config
        anomaly_cfg = system_config.get("anomaly_detection", {})
        large_loss = anomaly_cfg.get("large_loss_threshold_grid", -30)
        max_consecutive = anomaly_cfg.get("max_consecutive_loss_threshold", 4)

        if report.performance.total_pnl < large_loss:
            anomalies.append(f"本周网格亏损较大: {report.performance.total_pnl:.2f} USDT")
        if report.risk.max_consecutive_losses >= max_consecutive:
            anomalies.append(f"本周网格连续亏损: {report.risk.max_consecutive_losses}次")

        return anomalies

    # ============================================================
    # 模拟推演（方案C核心）
    # ============================================================

    async def _simulate(
        self, week_start: datetime, week_end: datetime, symbols: List[str]
    ) -> List[SimulationMetrics]:
        """
        模拟推演不同参数组合下的网格预期表现

        流程：
        1. 获取本周 1h K线数据
        2. 计算市场统计指标（ATR、波动率、价格摆动总量）
        3. 构建 3 个参数场景（当前配置、更密集、更稀疏）
        4. 对每个场景计算网格参数和预期收益

        Args:
            week_start: 本周起始时间
            week_end: 本周结束时间
            symbols: 交易对列表

        Returns:
            SimulationMetrics 列表
        """
        results: List[SimulationMetrics] = []

        # 获取K线服务URL
        kline_url = self._get_kline_service_url()
        if not kline_url:
            logger.warning("K线服务未配置，跳过模拟推演", strategy_id=self.strategy_id)
            return results

        for symbol in symbols:
            try:
                # 获取本周1h K线
                klines = await self._fetch_klines(kline_url, symbol, week_start, week_end)
                if not klines or len(klines) < 24:
                    logger.warning(
                        "K线数据不足，跳过模拟推演",
                        symbol=symbol,
                        kline_count=len(klines) if klines else 0,
                    )
                    continue

                # 计算市场统计指标
                market_stats = self._calc_market_stats(klines)
                current_price = market_stats["current_price"]
                atr = market_stats["atr"]
                total_price_swing = market_stats["total_price_swing"]
                market_state = market_stats["market_state"]

                # 读取当前参数
                current_params = self._read_config()
                grid_cfg = current_params.get("grid", {})
                trading_cfg = current_params.get("trading", {})

                base_grid_count = int(grid_cfg.get("base_grid_count", 8))
                min_grid_count = int(grid_cfg.get("min_grid_count", 5))
                max_grid_count = int(grid_cfg.get("max_grid_count", 12))
                spacing_multiplier = float(
                    grid_cfg.get("grid_spacing_atr_multiplier", 2.0)
                )
                leverage = int(trading_cfg.get("leverage", 10))
                margin = float(trading_cfg.get("margin", 500))
                single_margin = float(trading_cfg.get("single_position_margin", 100))

                # 构建3个参数场景
                scenarios = [
                    {
                        "name": "当前配置",
                        "grid_count": base_grid_count,
                        "spacing_mult": spacing_multiplier,
                    },
                    {
                        "name": "更密集网格",
                        "grid_count": min(base_grid_count + 2, max_grid_count),
                        "spacing_mult": spacing_multiplier * 0.85,
                    },
                    {
                        "name": "更稀疏网格",
                        "grid_count": max(base_grid_count - 2, min_grid_count),
                        "spacing_mult": spacing_multiplier * 1.15,
                    },
                ]

                for scenario in scenarios:
                    sim = self._simulate_scenario(
                        scenario=scenario,
                        symbol=symbol,
                        market_state=market_state,
                        current_price=current_price,
                        atr=atr,
                        total_price_swing=total_price_swing,
                        leverage=leverage,
                        margin=margin,
                        single_margin=single_margin,
                    )
                    results.append(sim)

            except Exception as e:
                logger.warning(
                    "模拟推演出错",
                    symbol=symbol,
                    error=str(e),
                )
                continue

        return results

    def _get_kline_service_url(self) -> Optional[str]:
        """
        获取K线服务URL

        优先级：
        1. 系统配置 kline_service.url（支持 ${ENV_VAR} 语法自动解析环境变量）
        2. 环境变量 KLINE_SERVICE_URL

        Returns:
            K线服务URL，未配置时返回 None
        """
        # 从系统配置读取
        system_config = self._system_config
        kline_cfg = system_config.get("kline_service", {})
        url = kline_cfg.get("url", "")
        if url:
            # 解析 ${ENV_VAR} 语法
            resolved = self._resolve_env_var(url)
            if resolved:
                return resolved

        # 从环境变量读取
        url = os.getenv("KLINE_SERVICE_URL", "")
        if url:
            return url

        return None

    @staticmethod
    def _resolve_env_var(value: str) -> Optional[str]:
        """
        解析字符串中的 ${ENV_VAR} 占位符

        Args:
            value: 可能包含 ${ENV_VAR} 的字符串

        Returns:
            解析后的字符串，解析失败返回 None
        """
        import re
        pattern = r'\$\{([^}]+)\}'
        match = re.search(pattern, value)
        if not match:
            return value

        env_var = match.group(1)
        env_value = os.getenv(env_var)
        if env_value is None:
            logger.warning("环境变量未设置", var=env_var)
            return None

        return re.sub(pattern, env_value, value)

    async def _fetch_klines(
        self, base_url: str, symbol: str, week_start: datetime, week_end: datetime
    ) -> List[Dict[str, Any]]:
        """
        从K线服务获取本周1h K线数据

        Args:
            base_url: K线服务基础URL
            symbol: 交易对
            week_start: 起始时间
            week_end: 结束时间

        Returns:
            K线数据列表，每项包含 open/high/low/close 等字段
        """
        import aiohttp

        # 计算需要的小时数
        hours_needed = int((week_end - week_start).total_seconds() / 3600) + 24
        limit = min(hours_needed, 200)

        url = f"{base_url.rstrip('/')}/klines/latest"
        params = {
            "symbol": symbol.upper(),
            "interval": "1h",
            "limit": limit,
        }

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url, params=params) as response:
                data = await response.json()
                if response.status != 200 or data.get("code") != 0:
                    logger.warning(
                        "K线服务请求失败",
                        symbol=symbol,
                        status=response.status,
                        message=data.get("message", ""),
                    )
                    return []

                klines_data = data.get("data", [])
                if not isinstance(klines_data, list):
                    return []

                klines = []
                for k in klines_data:
                    klines.append({
                        "open_time": k.get("open_time"),
                        "open": float(k.get("open_price", 0)),
                        "high": float(k.get("high_price", 0)),
                        "low": float(k.get("low_price", 0)),
                        "close": float(k.get("close_price", 0)),
                        "volume": float(k.get("volume", 0)),
                    })

                return klines

    def _calc_market_stats(self, klines: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        从K线数据计算市场统计指标

        Args:
            klines: K线数据列表

        Returns:
            {
                "current_price": float,       # 最新价格
                "atr": float,                 # 14周期ATR
                "total_price_swing": float,   # 总价格摆动（sum|high-low|）
                "market_state": str,          # 市场状态估计
                "avg_volume": float,          # 平均成交量
            }
        """
        if not klines:
            return {
                "current_price": 0,
                "atr": 0,
                "total_price_swing": 0,
                "market_state": "unknown",
                "avg_volume": 0,
            }

        current_price = float(klines[-1]["close"])
        total_price_swing = sum(float(k["high"]) - float(k["low"]) for k in klines)
        avg_volume = sum(float(k.get("volume", 0)) for k in klines) / len(klines)

        # 简化ATR计算（不需要pandas）
        atr = self._calc_simple_atr(klines, period=14)

        # 市场状态估计
        market_state = self._estimate_market_state(klines, atr, current_price)

        return {
            "current_price": current_price,
            "atr": atr,
            "total_price_swing": total_price_swing,
            "market_state": market_state,
            "avg_volume": avg_volume,
        }

    @staticmethod
    def _calc_simple_atr(klines: List[Dict[str, Any]], period: int = 14) -> float:
        """
        简化ATR计算（无需pandas）

        计算最近 period 个K线的平均真实波幅。

        Args:
            klines: K线数据列表
            period: ATR周期

        Returns:
            ATR值
        """
        if len(klines) < period + 1:
            return 0.0

        tr_values = []
        for i in range(1, len(klines)):
            high = float(klines[i]["high"])
            low = float(klines[i]["low"])
            prev_close = float(klines[i - 1]["close"])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_values.append(tr)

        if len(tr_values) < period:
            return sum(tr_values) / len(tr_values) if tr_values else 0.0

        # Wilder's 平滑方法
        atr = sum(tr_values[:period]) / period
        for i in range(period, len(tr_values)):
            atr = (atr * (period - 1) + tr_values[i]) / period

        return atr

    @staticmethod
    def _estimate_market_state(
        klines: List[Dict[str, Any]], atr: float, current_price: float
    ) -> str:
        """
        简化市场状态估计

        基于最近24小时的价格走势一致性和波动率判断。

        Returns:
            市场状态："强趋势" / "弱趋势" / "高波动震荡" / "震荡市场"
        """
        recent = klines[-24:] if len(klines) >= 24 else klines
        if len(recent) < 6:
            return "震荡市场"

        # 计算连续同向收盘次数
        up_streak = 0
        down_streak = 0
        max_up = 0
        max_down = 0
        for i in range(1, len(recent)):
            diff = float(recent[i]["close"]) - float(recent[i - 1]["close"])
            if diff > 0:
                up_streak += 1
                down_streak = 0
                max_up = max(max_up, up_streak)
            else:
                down_streak += 1
                up_streak = 0
                max_down = max(max_down, down_streak)

        max_consistency = max(max_up, max_down)
        consistency_ratio = max_consistency / len(recent) if len(recent) > 0 else 0

        # 价格变化幅度
        first_close = float(recent[0]["close"])
        last_close = float(recent[-1]["close"])
        price_change_pct = (
            abs(last_close - first_close) / first_close if first_close > 0 else 0
        )

        # ATR占比
        atr_pct = atr / current_price if current_price > 0 else 0

        if consistency_ratio > 0.5 and price_change_pct > 0.05:
            return "强趋势"
        elif consistency_ratio > 0.35 and price_change_pct > 0.02:
            return "弱趋势"
        elif atr_pct > 0.03:
            return "高波动震荡"
        else:
            return "震荡市场"

    def _simulate_scenario(
        self,
        scenario: Dict[str, Any],
        symbol: str,
        market_state: str,
        current_price: float,
        atr: float,
        total_price_swing: float,
        leverage: int,
        margin: float,
        single_margin: float,
    ) -> SimulationMetrics:
        """
        模拟单个参数场景的网格表现

        核心逻辑：
        1. 网格间距 = ATR × spacing_multiplier
        2. 价格区间 = [当前价 - 间距×网格数/2, 当前价 + 间距×网格数/2]
        3. 预估填充数 = 总价格摆动 / 网格间距 × 填充效率因子
        4. 预估利润 = 填充数 × 间距 × 单格数量

        Args:
            scenario: 场景配置（name, grid_count, spacing_mult）
            symbol: 交易对
            market_state: 市场状态
            current_price: 当前价格
            atr: ATR值
            total_price_swing: 总价格摆动
            leverage: 杠杆
            margin: 总保证金
            single_margin: 单笔保证金

        Returns:
            SimulationMetrics 模拟结果
        """
        grid_count = scenario["grid_count"]
        spacing_mult = scenario["spacing_mult"]

        # 网格间距 = ATR × spacing_multiplier
        grid_spacing = atr * spacing_mult
        if grid_spacing <= 0:
            simulation_cfg = self._system_config.get("simulation", {})
            fallback_spacing_pct = simulation_cfg.get("fallback_spacing_pct", 0.005)
            grid_spacing = current_price * fallback_spacing_pct

        # 价格区间
        half_range = grid_spacing * grid_count / 2
        price_range_low = current_price - half_range
        price_range_high = current_price + half_range

        # 从配置读取模拟推演参数
        simulation_cfg = self._system_config.get("simulation", {})
        fill_efficiency = simulation_cfg.get("fill_efficiency_factor", 0.6)
        confidence_upper = simulation_cfg.get("confidence_upper_limit", 0.8)

        # 每格利润率（用于记录，暂未使用）
        # profit_rate = grid_spacing / current_price if current_price > 0 else 0

        # 每格名义价值（基于总保证金/网格数 × 杠杆）
        nominal_per_grid = (margin / grid_count) * leverage

        # 预估填充数：总价格摆动 / 网格间距 × 效率因子
        estimated_fills = int(total_price_swing / grid_spacing * fill_efficiency)
        if estimated_fills < 0:
            estimated_fills = 0

        # 预估利润：填充数 × 间距 × 单格数量
        # 单格数量 = nominal_per_grid / current_price
        qty_per_grid = nominal_per_grid / current_price if current_price > 0 else 0
        profit_per_fill = grid_spacing * qty_per_grid
        estimated_profit = estimated_fills * profit_per_fill

        # 置信度：数据越充分，置信度越高
        confidence = min(confidence_upper, fill_efficiency + 0.1)
        if total_price_swing <= 0:
            confidence = 0.1

        return SimulationMetrics(
            scenario_name=scenario["name"],
            symbol=symbol,
            market_state=market_state,
            grid_count=grid_count,
            grid_spacing=round(grid_spacing, 4),
            price_range_low=round(price_range_low, 2),
            price_range_high=round(price_range_high, 2),
            profit_rate_per_fill=round(profit_rate * 100, 2),  # 转百分比
            estimated_fills_weekly=estimated_fills,
            estimated_profit_weekly=round(estimated_profit, 2),
            confidence=round(confidence, 2),
        )

    def _check_simulation_anomalies(self, report: StrategyReport) -> None:
        """
        检查模拟推演是否发现异常

        对比真实成交 vs 模拟推演，发现以下异常：
        - 真实填充数远低于模拟预期 → 网格间距可能过大
        - 真实利润率为负 → 需要调整参数

        Args:
            report: 策略报告（已包含 simulation 数据）
        """
        if not report.simulation:
            return

        # 找到"当前配置"场景的模拟结果
        current_scenario = None
        for sim in report.simulation:
            if sim.scenario_name == "当前配置":
                current_scenario = sim
                break

        if not current_scenario:
            return

        # 如果有真实成交数据，对比真实 vs 模拟
        if report.performance.total_trades > 0:
            real_fills = report.performance.total_trades
            sim_fills = current_scenario.estimated_fills_weekly

            if sim_fills > 0 and real_fills < sim_fills * 0.3:
                report.anomalies.append(
                    f"真实填充({real_fills}次)远低于模拟预期({sim_fills}次)，"
                    f"可能网格间距过大或市场流动性不足"
                )

            if report.performance.total_pnl < 0 and sim_fills > 0:
                report.anomalies.append(
                    f"本周网格亏损({report.performance.total_pnl:.2f} USDT)，"
                    f"建议检查网格参数是否适应市场状态({current_scenario.market_state})"
                )

    # ============================================================
    # 参数管理
    # ============================================================

    def get_current_params(self) -> Dict[str, Any]:
        """从策略配置文件读取当前可调参数值"""
        config = self._read_config()
        result = {}
        for param_path in self.get_param_whitelist():
            value = self._get_nested_value(config, param_path)
            if value is not None:
                result[param_path] = value
        return result

    def validate_params(self, adjustments: Dict[str, Any]) -> Dict[str, Any]:
        """校验 AI 建议的参数调整是否合法"""
        errors = []
        validated = {}
        whitelist = self.get_param_whitelist()
        ranges = self._strategy_cfg.get("param_ranges", {})

        if not ranges:
            logger.warning(
                "网格策略配置缺少 param_ranges，参数校验将跳过范围检查",
                strategy_id=self.strategy_id,
            )

        for param_path, adjustment in adjustments.items():
            if param_path not in whitelist:
                errors.append(f"参数 {param_path} 不在白名单中，已拒绝")
                continue

            if isinstance(adjustment, dict):
                new_value = adjustment.get("to")
            else:
                new_value = adjustment

            if new_value is None:
                errors.append(f"参数 {param_path} 缺少目标值")
                continue

            if param_path in ranges:
                min_val, max_val = ranges[param_path]
                if new_value < min_val:
                    errors.append(
                        f"参数 {param_path} 值 {new_value} 低于最小值 {min_val}，已截断"
                    )
                    new_value = min_val
                elif new_value > max_val:
                    errors.append(
                        f"参数 {param_path} 值 {new_value} 高于最大值 {max_val}，已截断"
                    )
                    new_value = max_val

            validated[param_path] = new_value

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "validated": validated,
        }

    def _read_config(self) -> Dict[str, Any]:
        """
        读取策略配置（合并基础配置 + AI 调优覆盖层）

        通过 shared/config_loader.py 的 load_strategy_config() 加载，
        自动合并 config.yaml 基础配置和 tuning_overrides 覆盖层。
        """
        # 解析策略配置文件的绝对路径
        config_full_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            self.config_path,
        )
        if not os.path.exists(config_full_path):
            config_full_path = os.path.join(
                os.path.dirname(
                    os.path.dirname(
                        os.path.dirname(os.path.abspath(__file__))
                    )
                ),
                self.config_path,
            )

        # 使用统一配置加载器（合并基础配置 + AI 调优覆盖层）
        strategy_dir = os.path.dirname(config_full_path)
        from shared.config_loader import load_strategy_config
        return load_strategy_config(strategy_dir)

    @staticmethod
    def _get_nested_value(config: Dict[str, Any], key_path: str) -> Any:
        """按点分隔路径读取嵌套字典值"""
        keys = key_path.split(".")
        current = config
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
        return current