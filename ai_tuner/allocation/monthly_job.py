"""
月度资金分配主流程

每月末自动触发，编排完整的月度资金分配流程：

流程：
1. 幂等性检查：查询当月是否已存在分配记录
2. 计算时间范围：当月起止时间
3. 盈亏采集：从数据库查询各策略当月已实现盈亏
4. 分配计算：按收益率排名或首月默认比例计算分配方案
5. 写入存储：数据库 + 配置文件
6. 飞书通知：推送月度分配报告卡片
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import structlog

from ai_tuner.allocation.allocation_calculator import (
    AllocationCalculator,
    AllocationResult,
)
from ai_tuner.allocation.config_updater import AllocationConfigUpdater
from ai_tuner.allocation.pnl_collector import PnLCollector

logger = structlog.get_logger()

# 中国标准时间时区 (UTC+8)
CST = timezone(timedelta(hours=8))


class MonthlyAllocationJob:
    """
    月度资金分配作业

    编排完整的月度分配流程，包含幂等性保护。
    """

    # 幂等性检查：查询当月是否已有分配记录
    _IDEMPOTENCY_CHECK_QUERY = """
        SELECT month, status FROM public.capital_allocation
        WHERE month = $1
        LIMIT 1
    """

    # 检查是否为首月：查询是否有任何历史记录
    _FIRST_MONTH_CHECK_QUERY = """
        SELECT COUNT(*) as cnt FROM public.capital_allocation
    """

    def __init__(
        self,
        config: Dict[str, Any],
        db_manager,
        notification_client,
        messenger,
        config_operator,
        rollback_manager,
    ):
        """
        初始化月度分配任务

        Args:
            config: 完整系统配置字典
            db_manager: DatabaseManager 实例
            notification_client: NotificationClient 实例
            messenger: Messenger 实例
            config_operator: ConfigOperator 实例
            rollback_manager: RollbackManager 实例
        """
        self.config = config
        self.db_manager = db_manager
        self.notification_client = notification_client
        self.messenger = messenger
        self.config_operator = config_operator
        self.rollback_manager = rollback_manager

        # 从配置中读取资金分配参数
        self.allocation_cfg = config.get("capital_allocation", {})
        self.participating_strategies = self.allocation_cfg.get("participating_strategies", [])

        # 构建参与策略配置列表（从完整策略列表中筛选）
        self._strategy_configs = self._build_participating_configs()

        # 初始化子模块
        self.pnl_collector = PnLCollector(
            db_manager=db_manager,
            strategies=self._strategy_configs,
        )
        self.calculator = AllocationCalculator()
        self.config_updater = AllocationConfigUpdater()

    def _build_participating_configs(self) -> List[Dict[str, Any]]:
        """
        从完整策略配置列表中筛选出参与资金分配的策略

        Returns:
            参与策略的配置列表
        """
        all_strategies = self.config.get("strategies", [])
        participating = []
        for s in all_strategies:
            if s.get("strategy_id", "") in self.participating_strategies:
                participating.append(s)
        return participating

    async def run_monthly_allocation(self) -> Optional[Dict[str, Any]]:
        """
        执行月度资金分配主流程

        流程：
        1. 幂等性检查
        2. 计算时间范围
        3. 盈亏采集
        4. 分配计算
        5. 写入存储
        6. 飞书通知

        Returns:
            分配结果字典，如果跳过或失败返回 None
        """
        logger.info("月度资金分配开始")

        try:
            # 检查是否启用
            if not self.allocation_cfg.get("enabled", False):
                logger.info("月度资金分配未启用，跳过")
                return None

            # 1. 计算当前月份标识
            now = datetime.now(CST)
            current_month = now.strftime("%Y-%m")

            # 2. 幂等性检查
            if await self._check_idempotency(current_month):
                logger.info("当月已存在分配记录，跳过", month=current_month)
                return None

            # 3. 计算时间范围（当月 1 日 00:00:00 到下月第一天 00:00:00，左闭右开）
            month_start, month_end = self._calculate_month_range(now)

            # 4. 判断是否为首月
            is_first_month = await self._check_is_first_month()

            # 5. 盈亏采集
            pnl_data = await self.pnl_collector.collect_all_realized_pnl(
                month_start=month_start,
                month_end=month_end,
            )

            if not pnl_data:
                logger.warning("没有采集到任何策略盈亏数据，跳过分配")
                return None

            # 6. 分配计算
            total_capital = float(self.allocation_cfg["total_capital"])
            reserve_ratio = float(self.allocation_cfg["reserve_ratio"])
            rank_ratios = self.allocation_cfg["rank_ratios"]
            fallback_ratios = self.allocation_cfg.get("fallback", {}).get("ratios", {})
            fallback_capitals = self.allocation_cfg.get("fallback", {}).get("capitals", {})

            # 构建策略名称映射
            strategy_names = {
                s.get("strategy_id", ""): s.get("name", s.get("strategy_id", ""))
                for s in self._strategy_configs
            }

            result = self.calculator.calculate(
                total_capital=total_capital,
                pnl_data=pnl_data,
                is_first_month=is_first_month,
                fallback_ratios=fallback_ratios,
                fallback_capitals=fallback_capitals,
                rank_ratios=rank_ratios,
                reserve_ratio=reserve_ratio,
                strategy_names=strategy_names,
                month=current_month,
            )

            # 7. 写入存储
            update_success = await self.config_updater.update_all(
                result=result,
                config=self.config,
                db_manager=self.db_manager,
                config_operator=self.config_operator,
                rollback_manager=self.rollback_manager,
            )

            if not update_success:
                logger.error("配置更新失败，但分配结果已计算")
                # 即使写入失败，也尝试发送通知
                await self._send_notification(result, failed=True)
                return None

            # 8. 飞书通知
            await self._send_notification(result)

            logger.info(
                "月度资金分配完成",
                month=current_month,
                total_capital=total_capital,
                strategy_count=len(result.entries),
                is_first_month=is_first_month,
            )

            return {
                "month": current_month,
                "total_capital": total_capital,
                "entries": [
                    {
                        "strategy_id": e.strategy_id,
                        "allocated_amount": e.allocated_amount,
                        "allocated_ratio": e.allocated_ratio,
                    }
                    for e in result.entries
                ],
                "is_first_month": is_first_month,
            }

        except Exception as e:
            logger.error("月度资金分配异常", error=str(e), exc_info=True)
            return None

    async def _check_idempotency(self, month: str) -> bool:
        """
        幂等性检查：查询当月是否已有分配记录

        Args:
            month: 月份标识，格式 "YYYY-MM"

        Returns:
            True 表示已存在记录，应跳过
        """
        try:
            row = await self.db_manager.fetch_one(
                self._IDEMPOTENCY_CHECK_QUERY,
                month,
            )
            if row:
                logger.info(
                    "当月已存在分配记录",
                    month=month,
                    status=row.get("status"),
                )
                return True
            return False
        except Exception as e:
            logger.error("幂等性检查异常", error=str(e))
            # 检查异常时假设不存在，继续执行（避免因检查失败而跳过）
            return False

    async def _check_is_first_month(self) -> bool:
        """
        判断是否为首月分配

        查询 public.capital_allocation 表，如果完全没有记录，就是首月。

        Returns:
            True 表示首月
        """
        try:
            row = await self.db_manager.fetch_one(self._FIRST_MONTH_CHECK_QUERY)
            if row and row.get("cnt", 0) > 0:
                logger.info("存在历史分配记录，非首月")
                return False
            logger.info("无历史分配记录，判定为首月")
            return True
        except Exception as e:
            logger.error("首月判断异常，默认非首月", error=str(e))
            return False

    def _calculate_month_range(self, now: datetime) -> Tuple[datetime, datetime]:
        """
        计算当月时间范围

        Args:
            now: 当前时间

        Returns:
            (month_start, month_end) 元组，均为 datetime 对象
        """
        # 当月第一天 00:00:00
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # 下月第一天 00:00:00（即当月结束，不包含）
        if now.month == 12:
            month_end = now.replace(year=now.year + 1, month=1, day=1,
                                    hour=0, minute=0, second=0, microsecond=0)
        else:
            month_end = now.replace(month=now.month + 1, day=1,
                                    hour=0, minute=0, second=0, microsecond=0)

        return month_start, month_end

    async def _send_notification(
        self,
        result: AllocationResult,
        failed: bool = False,
    ) -> None:
        """
        发送月度分配结果通知

        Args:
            result: AllocationResult 对象
            failed: 是否配置更新失败（仅通知已计算的结果）
        """
        try:
            if failed:
                # 配置更新失败通知
                error_msg = (
                    f"月度资金分配计算完成，但配置更新失败，请检查日志。\n"
                    f"月份：{result.month}\n"
                    f"请手动检查并修复配置。"
                )
                await self.messenger.send_error_notification(
                    strategy_name="月度资金分配",
                    strategy_id="monthly_allocation",
                    error_message=error_msg,
                )
            else:
                await self.messenger.send_allocation_card(result)
        except Exception as e:
            logger.error("发送分配通知异常", error=str(e))