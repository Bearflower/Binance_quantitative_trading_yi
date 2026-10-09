"""
月度资金分配主流程

每月末自动触发，编排完整的月度资金分配流程：

月份口径（需求 §14.1，v1.4 修复）：
- 盈亏归属月（pnl_month）：刚结束的当月，PnL/成交行数采集范围
- 生效月（effective_month）：次月，作为幂等键、写库 month、配置
  allocation_month、通知卡片月份与返回字典月份，与消费方
  （capital_manager / daily_refresher / dashboard 按当前月查询）对齐

流程：
1. 幂等性检查：查询生效月是否已存在分配记录
2. 计算时间范围：盈亏归属月起止时间
3. 盈亏采集：从数据库查询各策略盈亏归属月已实现盈亏
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
from ai_tuner.allocation.balance_provider import get_actual_balance
from ai_tuner.allocation.config_updater import AllocationConfigUpdater
from ai_tuner.allocation.pnl_collector import PnLCollector

logger = structlog.get_logger()

# 中国标准时间时区 (UTC+8)
CST = timezone(timedelta(hours=8))


def _next_month(year: int, month: int) -> Tuple[int, int]:
    """
    计算指定月份的下一个月

    Args:
        year: 年份
        month: 月份（1~12）

    Returns:
        (下一年份, 下一月份)；12 月跨年到次年 1 月
    """
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _resolve_months(now: datetime) -> Tuple[str, str]:
    """
    依据运行时刻推导（盈亏归属月, 生效月）

    月度任务在月末运行：盈亏采集归属刚结束的当月，分配记录对次月生效。

    Args:
        now: 运行时刻

    Returns:
        (pnl_month, effective_month)，格式均为 "YYYY-MM"
    """
    pnl_month = now.strftime("%Y-%m")
    next_year, next_month = _next_month(now.year, now.month)
    effective_month = f"{next_year:04d}-{next_month:02d}"
    return pnl_month, effective_month


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

    # 建表 DDL（幂等，确保表存在）
    _CAPITAL_ALLOCATION_DDL = """
        CREATE TABLE IF NOT EXISTS public.capital_allocation (
            id              SERIAL PRIMARY KEY,
            month           DATE NOT NULL UNIQUE,
            total_capital   DECIMAL(20, 8) NOT NULL,
            strategy_count  INTEGER NOT NULL,
            is_first_month  BOOLEAN NOT NULL DEFAULT FALSE,
            entries         JSONB NOT NULL,
            status          VARCHAR(20) NOT NULL DEFAULT 'active',
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """

    def __init__(
        self,
        config: Dict[str, Any],
        db_manager,
        notification_client,
        messenger,
        config_operator,
        rollback_manager,
        binance_client=None,
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
            binance_client: BinanceClient 实例（可选），用于获取合约账户实际可用余额
        """
        self.config = config
        self.db_manager = db_manager
        self.notification_client = notification_client
        self.messenger = messenger
        self.config_operator = config_operator
        self.rollback_manager = rollback_manager
        self.binance_client = binance_client

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

    async def _ensure_table(self) -> None:
        """确保 public.capital_allocation 表存在（幂等）"""
        try:
            await self.db_manager.execute_ddl(self._CAPITAL_ALLOCATION_DDL)
        except Exception as e:
            logger.warning("capital_allocation 建表异常（可能已存在）", error=str(e))

    async def _get_actual_balance(self) -> Optional[float]:
        """
        从交易所获取合约账户净资产（USDT）

        委托公共模块 balance_provider.get_actual_balance 实现，保持对外
        方法签名不变，行为等价。有 binance_client 且查询成功返回净资产；
        查询失败或没有 binance_client 返回 None（使用配置值兜底）。

        Returns:
            净资产（USDT），失败返回 None
        """
        return await get_actual_balance(self.binance_client)

    async def run_monthly_allocation(
        self,
        pnl_month: Optional[str] = None,
        effective_month: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        执行月度资金分配主流程

        流程：
        1. 确保表存在
        2. 幂等性检查（按生效月）
        3. 计算盈亏归属月时间范围
        4. 盈亏采集
        5. 获取实际余额（有 binance_client 时从交易所查询）
        6. 分配计算（结果月份为生效月）
        7. 写入存储
        8. 飞书通知

        Args:
            pnl_month: 盈亏归属月 "YYYY-MM"（缺省按运行时刻取当月）
            effective_month: 生效月 "YYYY-MM"（缺省取次月）；
                补生成历史月份时由手动脚本显式提供，两者必须同时给出

        Returns:
            分配结果字典（month 为生效月），如果跳过或失败返回 None
        """
        logger.info("月度资金分配开始")

        try:
            # 0. 确保表存在（幂等）
            await self._ensure_table()

            # 检查是否启用
            if not self.allocation_cfg.get("enabled", False):
                logger.info("月度资金分配未启用，跳过")
                return None

            # 1. 解析月份口径：盈亏归属月（采集范围）与生效月（幂等键/写库/通知）
            now = datetime.now(CST)
            pnl_month, effective_month = self._resolve_month_inputs(
                now, pnl_month, effective_month
            )

            # 2. 幂等性检查（生效月）
            if await self._check_idempotency(effective_month):
                logger.info("生效月已存在分配记录，跳过", month=effective_month)
                return None

            # 3. 计算盈亏归属月时间范围（月初 00:00:00 到次月 1 日 00:00:00，左闭右开）
            month_start, month_end = self._calculate_month_range(pnl_month)

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

            # 6. 获取实际可用余额（优先从交易所查询，兜底使用配置值）
            actual_balance = await self._get_actual_balance()
            if actual_balance is not None and actual_balance > 0:
                total_capital = actual_balance
                logger.info(
                    "使用交易所实际可用余额",
                    total_capital=total_capital,
                    config_value=self.allocation_cfg.get("total_capital"),
                )
            else:
                total_capital = float(self.allocation_cfg["total_capital"])
                logger.info(
                    "使用配置值作为总资金",
                    total_capital=total_capital,
                )
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
                month=effective_month,
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
                pnl_month=pnl_month,
                month=effective_month,
                total_capital=total_capital,
                strategy_count=len(result.entries),
                is_first_month=is_first_month,
            )

            return {
                "month": effective_month,
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

    def _resolve_month_inputs(
        self,
        now: datetime,
        pnl_month: Optional[str],
        effective_month: Optional[str],
    ) -> Tuple[str, str]:
        """
        解析月份入参

        - 两者均缺省：按运行时刻推导（盈亏归属月=当月，生效月=次月）
        - 两者均显式提供：直接使用（手动补生成历史月份场景）
        - 仅提供其一：拒绝执行，避免盈亏范围与记录月份错配（R3 资金安全）

        Args:
            now: 运行时刻
            pnl_month: 显式盈亏归属月（可缺省）
            effective_month: 显式生效月（可缺省）

        Returns:
            (pnl_month, effective_month)
        """
        if pnl_month is None and effective_month is None:
            return _resolve_months(now)
        if pnl_month is None or effective_month is None:
            raise ValueError("pnl_month 与 effective_month 必须同时提供或同时缺省")
        return pnl_month, effective_month

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

    def _calculate_month_range(self, pnl_month: str) -> Tuple[datetime, datetime]:
        """
        计算盈亏归属月的采集窗口（左闭右开）

        Args:
            pnl_month: 盈亏归属月标识，格式 "YYYY-MM"

        Returns:
            (month_start, month_end)：该月 1 日 00:00:00（含）至次月
            1 日 00:00:00（不含），CST 墙钟时间；跨年由 _next_month 处理
        """
        # 月初第一天 00:00:00（strptime 同时校验月份格式）
        anchor = datetime.strptime(pnl_month, "%Y-%m").replace(tzinfo=CST)
        month_start = anchor

        # 次月第一天 00:00:00（即归属月结束，不包含）
        next_year, next_month = _next_month(anchor.year, anchor.month)
        month_end = datetime(next_year, next_month, 1, tzinfo=CST)

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