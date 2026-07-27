"""
新币做空策略适配器
采集新币做空策略的周度表现数据

数据来源：
- trading.trade_records 表（strategy="新币做空策略"）
- strategies/new_coin/config.yaml 配置文件
"""

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List

import structlog
import yaml

from ai_tuner.adapters.base_adapter import (
    BaseAdapter,
    DistributionMetrics,
    PerformanceMetrics,
    RiskMetrics,
    StrategyMeta,
    StrategyReport,
)

logger = structlog.get_logger()


class NewCoinAdapter(BaseAdapter):
    """新币做空策略数据适配器"""

    strategy_id = "new_coin"
    strategy_name = "新币做空策略"
    config_path = "strategies/new_coin/config.yaml"

    async def collect(self) -> StrategyReport:
        """
        采集本周新币做空策略表现数据

        从数据库查询本周交易记录，计算各项指标，生成标准化报告。

        Returns:
            StrategyReport: 标准化策略周度体检报告
        """
        # 计算上一个完整周的时间范围（周一~周日）
        now = datetime.now()
        this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        # 周日定时调度时，本周已基本结束，取本周一（刚结束的周期）
        # 其他天（含周一手动触发）都取上周一
        week_start = this_monday if now.weekday() == 6 else this_monday - timedelta(days=7)
        week_end = week_start + timedelta(days=7)

        report = StrategyReport()
        report.meta = StrategyMeta(
            strategy_id=self.strategy_id,
            strategy_name=self.strategy_name,
            week_start=week_start.strftime("%Y-%m-%d"),
            week_end=week_end.strftime("%Y-%m-%d"),
        )

        try:
            # 读取并缓存策略配置文件（后续 _calc_risk 等方法复用）
            self._strategy_config_cache = self._read_config()
            config = self._strategy_config_cache
            report.meta.version = config.get("strategy", {}).get("version", "")

            # 查询本周交易记录
            trades = await self._query_weekly_trades(week_start, week_end)

            if not trades:
                logger.info("新币做空策略本周无交易记录", strategy_id=self.strategy_id)
                return report

            # 计算绩效指标
            report.performance = self._calc_performance(trades)
            # 计算风险指标
            report.risk = self._calc_risk(trades)
            # 计算分布指标
            report.distribution = self._calc_distribution(trades)
            # 检测异常
            report.anomalies = self._detect_anomalies(trades, report)

            logger.info(
                "新币做空策略数据采集完成",
                strategy_id=self.strategy_id,
                total_trades=report.performance.total_trades,
                win_rate=round(report.performance.win_rate, 2),
                total_pnl=round(report.performance.total_pnl, 2),
            )

        except Exception as e:
            logger.error("新币做空策略数据采集异常", strategy_id=self.strategy_id, error=str(e))
            report.anomalies.append(f"数据采集异常: {str(e)}")

        return report

    async def _query_weekly_trades(self, week_start: datetime, week_end: datetime) -> List[Dict[str, Any]]:
        """
        查询本周交易记录

        Args:
            week_start: 本周起始时间
            week_end: 本周结束时间

        Returns:
            交易记录列表
        """
        query = """
            SELECT *
            FROM trading.trade_records
            WHERE strategy = $1
              AND executed_at >= $2
              AND executed_at < $3
            ORDER BY executed_at ASC
        """
        return await self.db_manager.fetch_all(query, self.strategy_name, week_start, week_end)

    def _calc_performance(self, trades: List[Dict[str, Any]]) -> PerformanceMetrics:
        """
        计算绩效指标

        Args:
            trades: 交易记录列表

        Returns:
            PerformanceMetrics: 绩效指标
        """
        metrics = PerformanceMetrics()
        metrics.total_trades = len(trades)

        if metrics.total_trades == 0:
            return metrics

        pnl_values = []
        for t in trades:
            pnl = t.get("realized_pnl", 0) or 0
            pnl_values.append(pnl)

        metrics.total_pnl = sum(pnl_values)
        win_trades = [p for p in pnl_values if p > 0]
        loss_trades = [p for p in pnl_values if p < 0]

        metrics.win_count = len(win_trades)
        metrics.loss_count = len(loss_trades)
        metrics.win_rate = metrics.win_count / metrics.total_trades if metrics.total_trades > 0 else 0

        metrics.avg_win = sum(win_trades) / len(win_trades) if win_trades else 0
        metrics.avg_loss = abs(sum(loss_trades) / len(loss_trades)) if loss_trades else 0

        metrics.profit_factor = metrics.avg_win / metrics.avg_loss if metrics.avg_loss > 0 else 0

        # 近似夏普比率
        if len(pnl_values) >= 2:
            avg_pnl = metrics.total_pnl / metrics.total_trades
            variance = sum((p - avg_pnl) ** 2 for p in pnl_values) / (len(pnl_values) - 1)
            std_dev = variance ** 0.5
            metrics.sharpe_approx = avg_pnl / std_dev if std_dev > 0 else 0

        return metrics

    def _calc_risk(self, trades: List[Dict[str, Any]]) -> RiskMetrics:
        """
        计算风险指标

        Args:
            trades: 交易记录列表

        Returns:
            RiskMetrics: 风险指标
        """
        metrics = RiskMetrics()

        if not trades:
            return metrics

        # 计算最大连续亏损
        current_streak = 0
        max_streak = 0
        for t in trades:
            pnl = t.get("realized_pnl", 0) or 0
            if pnl < 0:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0
        metrics.max_consecutive_losses = max_streak

        # 如果当前连续亏损达到熔断阈值，标记熔断
        config = self._strategy_config_cache
        max_consecutive = config.get("trading", {}).get("consecutive_loss", {}).get("max_consecutive_losses", 3)
        if current_streak >= max_consecutive:
            metrics.is_circuit_breaker_active = True

        # 计算累计回撤
        cumulative_pnl = 0
        peak_pnl = 0
        max_drawdown = 0
        for t in trades:
            pnl = t.get("realized_pnl", 0) or 0
            cumulative_pnl += pnl
            peak_pnl = max(peak_pnl, cumulative_pnl)
            drawdown = peak_pnl - cumulative_pnl
            max_drawdown = max(max_drawdown, drawdown)

        # 回撤百分比（从缓存策略配置读取保证金和持仓数，计算初始资金）
        strategy_config = self._strategy_config_cache
        single_position_margin = strategy_config.get("trading", {}).get("single_position_margin")
        max_positions = strategy_config.get("trading", {}).get("max_positions")
        if not single_position_margin or not max_positions:
            # 策略配置缺少关键参数，从系统配置读取默认值
            system_config = self._system_config
            initial_capital = float(
                system_config.get("anomaly_detection", {}).get("default_initial_capital_new_coin", 150.0)
            )
            logger.warning("策略配置缺少 trading.single_position_margin 或 trading.max_positions，"
                           "使用系统默认值", default=initial_capital)
        else:
            initial_capital = float(single_position_margin) * float(max_positions)
        metrics.max_drawdown_pct = max_drawdown / initial_capital if initial_capital > 0 else 0
        metrics.current_drawdown_pct = (peak_pnl - cumulative_pnl) / initial_capital if initial_capital > 0 else 0

        return metrics

    def _calc_distribution(self, trades: List[Dict[str, Any]]) -> DistributionMetrics:
        """
        计算分布指标

        Args:
            trades: 交易记录列表

        Returns:
            DistributionMetrics: 分布指标
        """
        metrics = DistributionMetrics()

        if not trades:
            return metrics

        # 交易对分布
        symbol_dist: Dict[str, int] = {}
        for t in trades:
            symbol = t.get("symbol", "") or ""
            if symbol:
                symbol_dist[symbol] = symbol_dist.get(symbol, 0) + 1
        metrics.symbol_distribution = symbol_dist

        # 信号分布（新币做空策略使用总分作为信号等级）
        # 信号等级阈值从系统配置读取
        system_config = self._system_config
        signal_thresholds = system_config.get("anomaly_detection", {}).get("signal_distribution", {})
        high_threshold = signal_thresholds.get("high_score", 8)
        medium_threshold = signal_thresholds.get("medium_score", 6)

        signal_dist: Dict[str, int] = {}
        for t in trades:
            score = t.get("score", 0)
            if isinstance(score, (int, float)):
                if score >= high_threshold:
                    signal_dist["高"] = signal_dist.get("高", 0) + 1
                elif score >= medium_threshold:
                    signal_dist["中"] = signal_dist.get("中", 0) + 1
                else:
                    signal_dist["低"] = signal_dist.get("低", 0) + 1
        metrics.signal_distribution = signal_dist

        # 平均持仓时长（小时）
        holding_hours = []
        for t in trades:
            entry_time = t.get("entry_time")
            exit_time = t.get("exit_time")
            if entry_time and exit_time and isinstance(entry_time, datetime) and isinstance(exit_time, datetime):
                hours = (exit_time - entry_time).total_seconds() / 3600
                holding_hours.append(hours)
        metrics.avg_holding_hours = sum(holding_hours) / len(holding_hours) if holding_hours else 0

        return metrics

    def _detect_anomalies(
        self, trades: List[Dict[str, Any]], report: StrategyReport
    ) -> List[str]:
        """
        检测异常事件

        Args:
            trades: 交易记录列表
            report: 策略报告

        Returns:
            异常事件列表
        """
        anomalies = []

        if not trades:
            return anomalies

        # 从系统配置读取异常检测阈值（禁止硬编码）
        system_config = self._system_config
        anomaly_cfg = system_config.get("anomaly_detection", {})
        stop_loss_ratio = anomaly_cfg.get("stop_loss_ratio_threshold", 0.5)
        low_win_rate = anomaly_cfg.get("low_win_rate_threshold", 0.3)
        min_trades = anomaly_cfg.get("min_trades_for_anomaly", 5)
        large_loss = anomaly_cfg.get("large_loss_threshold_new_coin", -30)

        # 检测紧急止损触发
        emergency_stop_count = 0
        for t in trades:
            remarks = (t.get("remarks", "") or "").lower()
            if "紧急止损" in remarks or "emergency_stop" in remarks:
                emergency_stop_count += 1
        if emergency_stop_count > 0:
            anomalies.append(f"本周触发紧急止损: {emergency_stop_count}次")

        # 检测止损比例过高
        stop_loss_count = 0
        for t in trades:
            remarks = (t.get("remarks", "") or "").lower()
            if "止损" in remarks or "stop_loss" in remarks:
                stop_loss_count += 1
        if stop_loss_count > report.performance.total_trades * stop_loss_ratio:
            anomalies.append(f"本周止损触发比例过高: {stop_loss_count}/{report.performance.total_trades}")

        # 检测胜率过低
        if report.performance.total_trades >= min_trades and report.performance.win_rate < low_win_rate:
            anomalies.append(f"本周胜率过低: {report.performance.win_rate:.1%}")

        # 检测熔断激活
        if report.risk.is_circuit_breaker_active:
            anomalies.append("连续亏损熔断已激活，策略暂停交易中")

        # 检测大额亏损
        if report.performance.total_pnl < large_loss:
            anomalies.append(f"本周总亏损较大: {report.performance.total_pnl:.2f} USDT")

        return anomalies

    def get_current_params(self) -> Dict[str, Any]:
        """
        从策略配置文件中读取当前可调参数值

        Returns:
            字典，key 为参数路径（如 "scoring.entry_threshold"），value 为当前值
        """
        config = self._read_config()
        result = {}
        for param_path in self.get_param_whitelist():
            value = self._get_nested_value(config, param_path)
            if value is not None:
                result[param_path] = value
        return result

    def validate_params(self, adjustments: Dict[str, Any]) -> Dict[str, Any]:
        """
        校验 AI 建议的参数调整是否合法

        Args:
            adjustments: AI 建议的参数调整，格式为 {param_path: {"from": old, "to": new}}

        Returns:
            校验结果 {"valid": bool, "errors": list, "validated": dict}
        """
        errors = []
        validated = {}
        whitelist = self.get_param_whitelist()

        # 从系统配置（ai_tuner/config.yaml）读取参数有效范围，禁止硬编码
        ranges = self._strategy_cfg.get("param_ranges", {})
        if not ranges:
            logger.warning("策略配置缺少 param_ranges，参数校验将跳过范围检查",
                           strategy_id=self.strategy_id)

        for param_path, adjustment in adjustments.items():
            # 检查是否在白名单中
            if param_path not in whitelist:
                errors.append(f"参数 {param_path} 不在白名单中，已拒绝")
                continue

            # 提取新值
            if isinstance(adjustment, dict):
                new_value = adjustment.get("to")
            else:
                new_value = adjustment

            if new_value is None:
                errors.append(f"参数 {param_path} 缺少目标值")
                continue

            # 范围校验
            if param_path in ranges:
                min_val, max_val = ranges[param_path]
                if new_value < min_val:
                    errors.append(f"参数 {param_path} 值 {new_value} 低于最小值 {min_val}，已截断")
                    new_value = min_val
                elif new_value > max_val:
                    errors.append(f"参数 {param_path} 值 {new_value} 高于最大值 {max_val}，已截断")
                    new_value = max_val

            validated[param_path] = new_value

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "validated": validated,
        }

    def _read_config(self) -> Dict[str, Any]:
        """
        读取策略配置文件

        Returns:
            配置字典
        """
        config_full_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            self.config_path,
        )
        # 如果相对路径不存在，尝试从项目根目录读取
        if not os.path.exists(config_full_path):
            config_full_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                self.config_path,
            )

        with open(config_full_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def _get_nested_value(config: Dict[str, Any], key_path: str) -> Any:
        """
        按点分隔路径读取嵌套字典值

        Args:
            config: 配置字典
            key_path: 点分隔的键路径

        Returns:
            配置值，如果路径不存在返回 None
        """
        keys = key_path.split(".")
        current = config
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
        return current