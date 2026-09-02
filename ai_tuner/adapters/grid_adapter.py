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
from shared.utils import resolve_env_var

logger = structlog.get_logger()

# 模拟推演置信度：从配置读取，提供默认值兜底
# 配置路径：ai_tuner/config.yaml -> simulation.fill_efficiency_factor


class GridAdapter(BaseAdapter):
    """网格交易策略数据适配器（方案D：真实回测引擎）"""

    strategy_id = "grid"
    strategy_name = "网格交易策略"
    config_path = "strategies/grid/config.yaml"

    # 方案D：网格策略即使无真实成交也执行调优（基于回测数据）
    allow_tuning_without_trades = True

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
    # 回测模拟（方案D核心）
    # ============================================================

    async def _simulate(
        self, week_start: datetime, week_end: datetime, symbols: List[str]
    ) -> List[SimulationMetrics]:
        """
        使用回测引擎模拟网格策略表现（方案D）

        流程：
        1. 获取K线数据（PG优先 + API降级）
        2. 计算ATR和市场统计
        3. 提取当前网格参数
        4. 构建多场景参数组合
        5. 对每个场景执行回测
        6. 转换为 SimulationMetrics 列表

        Args:
            week_start: 本周起始时间
            week_end: 本周结束时间
            symbols: 交易对列表

        Returns:
            SimulationMetrics 列表
        """
        results: List[SimulationMetrics] = []

        try:
            # 1. 获取K线数据
            klines = await self._fetch_klines_for_backtest(week_start, week_end, symbols)
            if not klines:
                logger.warning("无法获取K线数据，回测跳过", strategy_id=self.strategy_id)
                return results

            # 2. 计算市场统计指标
            market_stats = self._calc_market_stats(klines)

            # 3. 提取当前网格参数
            current_params = self._extract_grid_params(market_stats)

            # 4. 构建多场景参数组合
            scenarios = self._build_scenarios(current_params)

            # 5. 初始化回测引擎
            from ai_tuner.backtest.grid_backtest import GridBacktestEngine
            engine = GridBacktestEngine(self._system_config)

            # 6. 对每个场景执行回测
            for scenario in scenarios:
                try:
                    result = engine.run(klines, scenario["params"])
                    sim = self._backtest_result_to_simulation(
                        result, scenario["name"], market_stats
                    )
                    results.append(sim)
                except Exception as e:
                    logger.warning(
                        "场景回测失败",
                        scenario=scenario["name"],
                        error=str(e),
                    )
                    continue

            logger.info(
                "回测模拟完成",
                strategy_id=self.strategy_id,
                kline_count=len(klines),
                scenario_count=len(results),
            )

        except Exception as e:
            logger.warning(
                "回测模拟异常",
                strategy_id=self.strategy_id,
                error=str(e),
            )

        return results

    # ============================================================
    # 方案D新增方法
    # ============================================================

    def _extract_grid_params(self, market_stats: Dict[str, Any]) -> "GridParams":
        """
        从策略配置中提取回测所需的网格参数

        Args:
            market_stats: 市场统计指标（含 ATR、当前价格）

        Returns:
            GridParams 实例
        """
        from ai_tuner.backtest.models import GridParams

        config = self._read_config()
        grid_cfg = config.get("grid", {})
        trading_cfg = config.get("trading", {})
        risk_cfg = config.get("risk", {})

        return GridParams(
            base_grid_count=int(grid_cfg.get("base_grid_count", 6)),
            grid_spacing_atr_multiplier=float(
                grid_cfg.get("grid_spacing_atr_multiplier", 2.5)
            ),
            stop_loss_buffer=int(grid_cfg.get("stop_loss_buffer", 2)),
            leverage=int(trading_cfg.get("leverage", 10)),
            margin=float(trading_cfg.get("margin", 500)),
            single_position_margin=float(trading_cfg.get("single_position_margin", 100)),
            stop_loss_percent=float(risk_cfg.get("stop_loss_percent", 0.10)),
            hard_stop_loss=float(risk_cfg.get("hard_stop_loss", -0.15)),
            atr=market_stats.get("atr", 0.0),
            current_price=market_stats.get("current_price", 0.0),
        )

    def _build_scenarios(
        self, current_params: "GridParams"
    ) -> List[Dict[str, Any]]:
        """
        构建多场景参数组合

        生成3个场景：
        1. 当前配置：使用现有参数
        2. 更密集网格：grid_count + 2，spacing × 0.85
        3. 更稀疏网格：grid_count - 2，spacing × 1.15

        Args:
            current_params: 当前网格参数

        Returns:
            [{"name": "场景名", "params": GridParams}, ...]
        """
        from ai_tuner.backtest.models import GridParams
        from copy import deepcopy

        scenarios = []

        # 场景1：当前配置
        scenarios.append({
            "name": "当前配置",
            "params": deepcopy(current_params),
        })

        # 场景2：更密集网格
        dense_params = deepcopy(current_params)
        dense_params.base_grid_count = min(
            current_params.base_grid_count + 2,
            self._get_max_grid_count(current_params.base_grid_count),
        )
        dense_params.grid_spacing_atr_multiplier *= 0.85
        scenarios.append({
            "name": "更密集网格",
            "params": dense_params,
        })

        # 场景3：更稀疏网格
        sparse_params = deepcopy(current_params)
        sparse_params.base_grid_count = max(
            current_params.base_grid_count - 2,
            self._get_min_grid_count(current_params.base_grid_count),
        )
        sparse_params.grid_spacing_atr_multiplier *= 1.15
        scenarios.append({
            "name": "更稀疏网格",
            "params": sparse_params,
        })

        return scenarios

    @staticmethod
    def _get_min_grid_count(base: int) -> int:
        """获取最小网格数"""
        return max(base - 2, 3)

    @staticmethod
    def _get_max_grid_count(base: int) -> int:
        """获取最大网格数"""
        return min(base + 2, 15)

    def _backtest_result_to_simulation(
        self,
        result: Dict[str, Any],
        scenario_name: str,
        market_stats: Dict[str, Any],
    ) -> SimulationMetrics:
        """
        将回测结果字典转换为 SimulationMetrics（保持接口兼容）

        Args:
            result: 回测引擎返回的结果字典
            scenario_name: 场景名称
            market_stats: 市场统计指标

        Returns:
            SimulationMetrics 实例
        """
        config = self._read_config()
        grid_cfg = config.get("grid", {})
        trading_cfg = config.get("trading", {})

        base_grid_count = int(grid_cfg.get("base_grid_count", 6))
        spacing_multiplier = float(grid_cfg.get("grid_spacing_atr_multiplier", 2.5))
        atr = market_stats.get("atr", 0.0)
        current_price = market_stats.get("current_price", 0.0)
        margin = float(trading_cfg.get("margin", 500))

        grid_spacing = atr * spacing_multiplier
        half_range = grid_spacing * base_grid_count / 2
        price_range_low = current_price - half_range
        price_range_high = current_price + half_range

        profit_rate = grid_spacing / current_price if current_price > 0 else 0

        return SimulationMetrics(
            scenario_name=scenario_name,
            symbol=result.get("symbol", "ETHUSDT"),
            market_state=market_stats.get("market_state", ""),
            grid_count=base_grid_count,
            grid_spacing=round(grid_spacing, 4),
            price_range_low=round(price_range_low, 2),
            price_range_high=round(price_range_high, 2),
            profit_rate_per_fill=round(profit_rate * 100, 2),
            estimated_fills_weekly=result.get("fill_count", 0),
            estimated_profit_weekly=round(result.get("total_pnl", 0.0), 2),
            confidence=0.85,  # 回测模式置信度较高
        )

    # ============================================================
    # K线数据获取（方案D：PG优先 + API降级）
    # ============================================================

    async def _fetch_klines_for_backtest(
        self, week_start: datetime, week_end: datetime, symbols: List[str]
    ) -> List[Dict[str, Any]]:
        """
        获取回测所需的K线数据（PG优先 + API降级）

        优先级：
        1. PostgreSQL 直查 kline_ethusdt_1h 表
        2. K线服务 HTTP API 降级
        3. 都失败则返回空列表

        Args:
            week_start: 本周起始时间
            week_end: 本周结束时间
            symbols: 交易对列表

        Returns:
            K线数据列表，长度应在 24-168 之间
        """
        # 尝试从 PostgreSQL 读取
        klines = await self._fetch_klines_from_pg(week_start, week_end)
        min_kline_count = self._system_config.get("backtest", {}).get("min_kline_count", 24)
        if klines and len(klines) >= min_kline_count:
            logger.info(
                "从PostgreSQL获取K线数据",
                count=len(klines),
                strategy_id=self.strategy_id,
            )
            return klines

        # 降级到 K线服务 API
        logger.warning(
            "PostgreSQL K线数据不足，降级到K线服务API",
            pg_count=len(klines) if klines else 0,
            strategy_id=self.strategy_id,
        )

        kline_url = self._get_kline_service_url()
        if not kline_url:
            logger.error(
                "K线数据源不可用：PostgreSQL和K线服务均不可用",
                strategy_id=self.strategy_id,
            )
            return []

        for symbol in symbols:
            try:
                api_klines = await self._fetch_klines(kline_url, symbol, week_start, week_end)
                if api_klines and len(api_klines) >= min_kline_count:
                    logger.info(
                        "从K线服务API获取K线数据",
                        symbol=symbol,
                        count=len(api_klines),
                    )
                    return api_klines
            except Exception as e:
                logger.warning(
                    "K线服务API请求失败",
                    symbol=symbol,
                    error=str(e),
                )

        logger.error("所有K线数据源均不可用，无法执行回测")
        return []

    async def _fetch_klines_from_pg(
        self, week_start: datetime, week_end: datetime
    ) -> List[Dict[str, Any]]:
        """
        从 PostgreSQL 直接查询 K线数据

        使用已有的 db_manager 连接，不从零创建连接。

        Args:
            week_start: 起始时间
            week_end: 结束时间

        Returns:
            K线数据列表
        """
        try:
            query = """
                SELECT open_time, open_price, high_price, low_price, close_price, volume
                FROM kline_ethusdt_1h
                WHERE open_time >= $1
                  AND open_time < $2
                ORDER BY open_time ASC
            """
            rows = await self.db_manager.fetch_all(query, week_start, week_end)
            if not rows:
                logger.warning(
                    "PostgreSQL K线表无数据",
                    table="kline_ethusdt_1h",
                    week_start=week_start,
                    week_end=week_end,
                )
                return []

            return self._convert_kline_rows(rows)

        except Exception as e:
            logger.warning(
                "PostgreSQL K线查询失败",
                error=str(e),
                strategy_id=self.strategy_id,
            )
            return []

    async def _validate_new_params(
        self,
        old_params: "GridParams",
        new_params: "GridParams",
        klines: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        验证新参数是否优于旧参数（Step 6：参数验证）

        Args:
            old_params: 当前参数
            new_params: LLM建议的新参数
            klines: 上周K线数据

        Returns:
            {
                "passed": bool,
                "reason": str,
                "old_score": float,
                "new_score": float,
            }
        """
        from ai_tuner.backtest.grid_backtest import GridBacktestEngine

        engine = GridBacktestEngine(self._system_config)

        try:
            old_result = engine.run(klines, old_params)
            new_result = engine.run(klines, new_params)

            old_score = old_result.get("composite_score", 0)
            new_score = new_result.get("composite_score", 0)

            if new_score > old_score:
                return {
                    "passed": True,
                    "reason": (
                        f"新参数综合评分({new_score}) > 旧参数({old_score})，采纳建议"
                    ),
                    "old_score": old_score,
                    "new_score": new_score,
                }
            else:
                return {
                    "passed": False,
                    "reason": (
                        f"新参数综合评分({new_score}) <= 旧参数({old_score})，拒绝采纳"
                    ),
                    "old_score": old_score,
                    "new_score": new_score,
                }
        except Exception as e:
            logger.error("参数验证回测异常", error=str(e))
            return {
                "passed": False,
                "reason": f"参数验证回测异常: {str(e)}",
                "old_score": 0,
                "new_score": 0,
            }

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
    # 工具方法（方案C保留，方案D也使用）
    # ============================================================

    def _get_kline_service_url(self) -> Optional[str]:
        """
        获取K线服务URL

        优先级：
        1. 系统配置 kline_service.url（支持 ${ENV_VAR} 语法自动解析环境变量）
        2. 环境变量 KLINE_SERVICE_URL

        Returns:
            K线服务URL，未配置时返回 None
        """
        system_config = self._system_config
        kline_cfg = system_config.get("kline_service", {})
        url = kline_cfg.get("url", "")
        if url:
            resolved = resolve_env_var(url)
            if resolved:
                return resolved

        url = os.getenv("KLINE_SERVICE_URL", "")
        if url:
            return url

        return None

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

                return self._convert_kline_rows(klines_data)

    @staticmethod
    def _convert_kline_rows(rows: List[Any]) -> List[Dict[str, Any]]:
        """将数据库行或API响应转换为统一的K线字典格式"""
        klines = []
        for row in rows:
            klines.append({
                "open_time": row.get("open_time") if isinstance(row, dict) else getattr(row, "open_time", None),
                "open": float(row.get("open_price", 0) if isinstance(row, dict) else getattr(row, "open_price", 0)),
                "high": float(row.get("high_price", 0) if isinstance(row, dict) else getattr(row, "high_price", 0)),
                "low": float(row.get("low_price", 0) if isinstance(row, dict) else getattr(row, "low_price", 0)),
                "close": float(row.get("close_price", 0) if isinstance(row, dict) else getattr(row, "close_price", 0)),
                "volume": float(row.get("volume", 0) if isinstance(row, dict) else getattr(row, "volume", 0)),
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
                "total_price_swing": float,   # 总价格摆动
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

        atr = self._calc_simple_atr(klines, period=14)
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

        Returns:
            "强趋势" / "弱趋势" / "高波动震荡" / "震荡市场"
        """
        recent = klines[-24:] if len(klines) >= 24 else klines
        if len(recent) < 6:
            return "震荡市场"

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

        first_close = float(recent[0]["close"])
        last_close = float(recent[-1]["close"])
        price_change_pct = (
            abs(last_close - first_close) / first_close if first_close > 0 else 0
        )

        atr_pct = atr / current_price if current_price > 0 else 0

        if consistency_ratio > 0.5 and price_change_pct > 0.05:
            return "强趋势"
        elif consistency_ratio > 0.35 and price_change_pct > 0.02:
            return "弱趋势"
        elif atr_pct > 0.03:
            return "高波动震荡"
        else:
            return "震荡市场"

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

    