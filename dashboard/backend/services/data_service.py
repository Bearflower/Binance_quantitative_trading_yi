"""
Dashboard 数据服务
封装采集器调用逻辑,提供格式化的数据
"""
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sys
from pathlib import Path

import structlog

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from dashboard.backend.collectors.collector import DailyReportCollector, StrategyStats
from dashboard.backend.collectors.weekly_collector import WeeklyReportCollector, WeeklyStrategyStats
from dashboard.backend.models.schemas import (
    StrategySummary,
    StrategyDetail,
    SymbolDetail,
    TrendDataPoint
)


logger = structlog.get_logger()

BEIJING_TZ = timezone(timedelta(hours=8))


class DataService:
    """
    数据服务：调用采集器获取数据并格式化

    复用现有的日报/周报采集器，避免重复开发。
    """

    def __init__(
        self,
        daily_collector: DailyReportCollector,
        weekly_collector: WeeklyReportCollector
    ):
        """
        初始化数据服务

        Args:
            daily_collector: 日报采集器实例
            weekly_collector: 周报采集器实例
        """
        self.daily_collector = daily_collector
        self.weekly_collector = weekly_collector

        logger.info("数据服务初始化完成")

    async def get_daily_stats(self) -> Dict[str, StrategyStats]:
        """
        获取日报数据

        Returns:
            策略统计数据字典 {strategy_key: StrategyStats}
        """
        logger.info("开始采集日报数据")
        try:
            stats = await self.daily_collector.collect_all()
            logger.info("日报数据采集完成", strategy_count=len(stats))
            return stats
        except Exception as e:
            logger.error("日报数据采集失败", error=str(e), exc_info=True)
            raise

    async def get_weekly_stats(self) -> Dict[str, WeeklyStrategyStats]:
        """
        获取周报数据

        Returns:
            策略统计数据字典 {strategy_key: WeeklyStrategyStats}
        """
        logger.info("开始采集周报数据")
        try:
            stats = await self.weekly_collector.collect_all()
            logger.info("周报数据采集完成", strategy_count=len(stats))
            return stats
        except Exception as e:
            logger.error("周报数据采集失败", error=str(e), exc_info=True)
            raise

    @staticmethod
    def format_strategy_summary(
        strategy_key: str,
        stats: Any,
        strategy_config: Dict[str, Any]
    ) -> StrategySummary:
        """
        格式化策略摘要

        Args:
            strategy_key: 策略ID
            stats: 采集器返回的策略统计数据
            strategy_config: 策略配置

        Returns:
            策略摘要模型
        """
        config = strategy_config.get(strategy_key, {})

        return StrategySummary(
            id=strategy_key,
            name=config.get("name", stats.name),
            emoji=config.get("emoji", ""),
            order_count=stats.order_count,
            fill_count=stats.fill_count,
            closed_count=stats.closed_count,
            win_count=stats.win_count,
            loss_count=stats.loss_count,
            total_pnl=str(stats.total_pnl),
            win_rate=stats.win_rate,
            error=getattr(stats, "error", None)
        )

    @staticmethod
    def format_strategy_detail(
        strategy_key: str,
        stats: WeeklyStrategyStats,
        strategy_config: Dict[str, Any]
    ) -> StrategyDetail:
        """
        格式化策略详情

        Args:
            strategy_key: 策略ID
            stats: 采集器返回的周报统计数据
            strategy_config: 策略配置

        Returns:
            策略详情模型
        """
        config = strategy_config.get(strategy_key, {})

        # 格式化币种明细
        symbols = []
        for symbol, sym_stats in stats.symbols.items():
            symbols.append(SymbolDetail(
                symbol=symbol,
                order_count=sym_stats.order_count,
                fill_count=sym_stats.fill_count,
                wins=sym_stats.wins,
                losses=sym_stats.losses,
                total_pnl=str(sym_stats.total_pnl),
                win_rate=sym_stats.win_rate,
                data_quality=getattr(sym_stats, "data_quality", "ok"),
                quality_note=getattr(sym_stats, "quality_note", "")
            ))

        return StrategyDetail(
            id=strategy_key,
            name=config.get("name", stats.name),
            emoji=config.get("emoji", stats.emoji),
            order_count=stats.order_count,
            fill_count=stats.fill_count,
            closed_count=stats.closed_count,
            win_count=stats.wins,
            loss_count=stats.losses,
            total_pnl=str(stats.total_pnl),
            win_rate=stats.win_rate,
            avg_daily_orders=stats.avg_daily_orders,
            symbols=symbols,
            daily_counts=stats.daily_counts,
            data_source=stats.data_source,
            validation_warnings=stats.validation_warnings,
            error=stats.error
        )

    @staticmethod
    def get_report_date(report_type: str) -> str:
        """
        获取报告日期

        Args:
            report_type: 报告类型（daily/weekly）

        Returns:
            报告日期字符串
        """
        now = datetime.now(BEIJING_TZ)

        if report_type == "daily":
            # 日报：昨日日期
            yesterday = now.date() - timedelta(days=1)
            return yesterday.strftime("%Y-%m-%d")
        else:
            # 周报：上周日日期
            today = now.date()
            today_weekday = today.weekday()
            days_to_last_sunday = 0 if today_weekday == 6 else (today_weekday + 1)
            last_sunday = today - timedelta(days=days_to_last_sunday)
            return last_sunday.strftime("%Y-%m-%d")

    async def get_trend_data(
        self,
        trend_type: str,
        days: int
    ) -> Dict[str, List[TrendDataPoint]]:
        """
        获取趋势数据（用于图表）

        Args:
            trend_type: 趋势类型（daily/weekly）
            days: 天数

        Returns:
            各策略的趋势数据 {strategy_key: [TrendDataPoint]}
        """
        logger.info(
            "开始获取趋势数据",
            trend_type=trend_type,
            days=days
        )

        # TODO: 实现趋势数据采集
        # 这里需要根据日期范围逐日/逐周采集数据
        # 当前版本返回空数据，后续可扩展

        return {}
