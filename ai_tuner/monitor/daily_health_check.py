"""
每日健康检查模块
在两次周度调优之间，检查各策略是否出现异常
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List

import structlog

logger = structlog.get_logger()


class DailyHealthCheck:
    """
    每日健康检查

    检查项：
    1. 总亏损超过阈值（large_loss）
    2. 连续亏损超过阈值（max_consecutive_loss）
    3. 胜率过低（low_win_rate）
    """

    def __init__(self, config: Dict[str, Any], db_manager, messenger):
        """
        初始化每日健康检查

        Args:
            config: 系统配置字典
            db_manager: 数据库管理器实例
            messenger: 消息发送器实例（用于推送告警）
        """
        self.config = config
        self.db_manager = db_manager
        self.messenger = messenger
        self.anomaly_cfg = config.get("anomaly_detection", {})

    async def run_check(self) -> None:
        """执行所有策略的健康检查"""
        strategies = self.config.get("strategies", [])
        for strategy in strategies:
            strategy_id = strategy.get("strategy_id", "")
            if not strategy.get("enabled", True):
                continue

            try:
                # 获取策略名称（数据库 trade_records 表中使用策略名称查询）
                strategy_name = strategy.get("name", strategy_id)
                report = await self._collect_recent_performance(strategy_name)
                anomalies = self._detect_anomalies(strategy_id, report)
                if anomalies:
                    await self._notify_anomalies(strategy_id, anomalies)
                else:
                    logger.info("健康检查通过", strategy_id=strategy_id)
            except Exception as e:
                logger.error("健康检查异常", strategy_id=strategy_id, error=str(e))

    async def _collect_recent_performance(self, strategy_name: str) -> Dict[str, Any]:
        """
        从数据库查询最近24小时的交易表现

        Args:
            strategy_name: 策略名称（如 "MTPCS策略"）

        Returns:
            包含最近24小时交易表现的字典
        """
        since = datetime.utcnow() - timedelta(hours=24)

        # 查询 trade_records 汇总统计
        query = """
            SELECT
                COUNT(*) as total_trades,
                COUNT(*) FILTER (WHERE realized_pnl > 0) as win_count,
                COUNT(*) FILTER (WHERE realized_pnl <= 0) as loss_count,
                COALESCE(SUM(realized_pnl), 0) as total_pnl
            FROM trading.trade_records
            WHERE strategy = $1 AND executed_at >= $2
        """
        row = await self.db_manager.fetch_one(query, strategy_name, since)
        row = row or {}

        # 查询连续亏损（使用窗口函数计算最近连续亏损笔数）
        query_consecutive = """
            SELECT COUNT(*) as consecutive_losses
            FROM (
                SELECT realized_pnl,
                       executed_at,
                       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END)
                           OVER (ORDER BY executed_at DESC
                                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) as grp
                FROM trading.trade_records
                WHERE strategy = $1 AND executed_at >= $2
                  AND realized_pnl IS NOT NULL
                ORDER BY executed_at DESC
            ) sub
            WHERE realized_pnl <= 0
            GROUP BY grp
            ORDER BY MAX(executed_at) DESC
            LIMIT 1
        """
        row_consecutive = await self.db_manager.fetch_one(query_consecutive, strategy_name, since)
        row_consecutive = row_consecutive or {}

        return {
            "total_trades": row.get("total_trades", 0) or 0,
            "win_count": row.get("win_count", 0) or 0,
            "loss_count": row.get("loss_count", 0) or 0,
            "total_pnl": float(row.get("total_pnl", 0) or 0),
            "consecutive_losses": row_consecutive.get("consecutive_losses", 0) or 0,
        }

    def _detect_anomalies(self, strategy_id: str, report: Dict[str, Any]) -> List[str]:
        """
        检测异常

        根据配置中的阈值检测各项异常，交易数据不足时不触发告警。

        Args:
            strategy_id: 策略唯一标识
            report: 24小时交易表现报告

        Returns:
            异常描述列表，无异常时返回空列表
        """
        anomalies = []
        min_trades = self.anomaly_cfg.get("min_trades_for_anomaly", 5)

        if report["total_trades"] < min_trades:
            return anomalies  # 交易数据不足，不触发异常检测

        # 1. 检查总亏损
        # 优先查找策略专用阈值（如 large_loss_threshold_mtpcs）
        large_loss_key = f"large_loss_threshold_{strategy_id}"
        threshold = self.anomaly_cfg.get(large_loss_key)
        if threshold is None:
            # 从配置读取全局默认值
            threshold = self.anomaly_cfg.get("default_large_loss_threshold", -50)

        if report["total_pnl"] < threshold:
            anomalies.append(
                f"24h总亏损 {report['total_pnl']:.1f} USDT，低于告警阈值 {threshold} USDT"
            )

        # 2. 检查连续亏损
        max_consecutive = self.anomaly_cfg.get("max_consecutive_loss_threshold", 4)
        if report["consecutive_losses"] >= max_consecutive:
            anomalies.append(
                f"连续亏损 {report['consecutive_losses']} 笔，超过告警阈值 {max_consecutive} 笔"
            )

        # 3. 检查胜率
        if report["total_trades"] >= min_trades:
            win_rate = report["win_count"] / report["total_trades"] if report["total_trades"] > 0 else 0
            low_win_rate = self.anomaly_cfg.get("low_win_rate_threshold", 0.3)
            if win_rate < low_win_rate:
                anomalies.append(
                    f"24h胜率 {win_rate:.1%}，低于告警阈值 {low_win_rate:.0%}"
                )

        return anomalies

    async def _notify_anomalies(self, strategy_id: str, anomalies: List[str]) -> None:
        """
        推送飞书告警

        Args:
            strategy_id: 策略唯一标识
            anomalies: 异常描述列表
        """
        title = f"策略异常告警：{strategy_id}"
        content_lines = [
            f"策略: {strategy_id}",
            f"检测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "异常项:",
            "",
        ]
        for i, anomaly in enumerate(anomalies, 1):
            content_lines.append(f"{i}. {anomaly}")

        content_lines.append("")
        content_lines.append("请及时检查策略运行状态，必要时手动干预。")

        try:
            await self.messenger.send_alert(
                title=title,
                content="\n".join(content_lines),
                level="error",
            )
            logger.info("异常告警已推送", strategy_id=strategy_id, anomaly_count=len(anomalies))
        except Exception as e:
            logger.error("推送异常告警失败", strategy_id=strategy_id, error=str(e))