"""
盈亏与资金数据采集器

负责从各策略数据表查询当月已实现盈亏和月初分配资金。

数据来源：
    - trading.trade_records: 各策略的已实现盈亏记录
    - public.capital_allocation: 上月分配记录（用于获取月初分配资金）
"""

import json
import structlog
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

# 中国标准时间时区 (UTC+8)
CST = timezone(timedelta(hours=8))

logger = structlog.get_logger()


class PnLCollector:
    """
    盈亏与资金数据采集器

    从数据库查询各参与策略的当月已实现盈亏和月初分配资金。
    """

    # 查询当月已实现盈亏的 SQL 模板
    _PNL_QUERY_TEMPLATE = """
        SELECT COALESCE(SUM(realized_pnl), 0) AS total_pnl
        FROM trading.trade_records
        WHERE strategy = $1
          AND close_time >= $2
          AND close_time < $3
          AND status = 'CLOSED'
    """

    # 查询上月分配记录的 SQL 模板
    _PREV_ALLOCATION_QUERY = """
        SELECT entries
        FROM public.capital_allocation
        WHERE month = $1
        LIMIT 1
    """

    def __init__(
        self,
        db_manager,
        strategies: List[Dict[str, Any]],
    ):
        """
        初始化盈亏采集器

        Args:
            db_manager: DatabaseManager 实例
            strategies: 参与资金分配的策略配置列表，每个元素包含 strategy_id、name 等字段
        """
        self.db_manager = db_manager
        self.strategies = strategies

    async def collect_all_realized_pnl(
        self,
        month_start: datetime,
        month_end: datetime,
    ) -> Dict[str, Dict[str, float]]:
        """
        采集所有参与策略的当月已实现盈亏和月初分配资金

        Args:
            month_start: 当月起始时间（包含）
            month_end: 当月结束时间（不包含）

        Returns:
            {strategy_id: {"pnl": float, "capital": float}} 格式的字典
            - pnl: 当月已实现盈亏（USDT）
            - capital: 月初分配资金（USDT），若无上月记录则为 0.0
        """
        result: Dict[str, Dict[str, float]] = {}

        for strategy_cfg in self.strategies:
            strategy_id = strategy_cfg.get("strategy_id", "")
            if not strategy_id:
                logger.warning("策略配置缺少 strategy_id，跳过")
                continue

            try:
                # 查询当月已实现盈亏
                pnl = await self._query_strategy_pnl(
                    strategy_id=strategy_id,
                    month_start=month_start,
                    month_end=month_end,
                )

                # 查询月初分配资金
                capital = await self._query_prev_month_capital(strategy_id)

                result[strategy_id] = {
                    "pnl": pnl,
                    "capital": capital,
                }

                logger.info(
                    "策略盈亏采集完成",
                    strategy_id=strategy_id,
                    pnl=round(pnl, 2),
                    capital=round(capital, 2),
                )

            except Exception as e:
                logger.error(
                    "策略盈亏采集异常",
                    strategy_id=strategy_id,
                    error=str(e),
                )
                # 异常时使用默认值，不中断整体流程
                result[strategy_id] = {
                    "pnl": 0.0,
                    "capital": 0.0,
                }

        return result

    async def _query_strategy_pnl(
        self,
        strategy_id: str,
        month_start: datetime,
        month_end: datetime,
    ) -> float:
        """
        查询指定策略当月的已实现盈亏

        Args:
            strategy_id: 策略唯一标识
            month_start: 当月起始时间（包含）
            month_end: 当月结束时间（不包含）

        Returns:
            已实现盈亏总额（USDT）
        """
        row = await self.db_manager.fetch_one(
            self._PNL_QUERY_TEMPLATE,
            strategy_id,
            month_start,
            month_end,
        )
        if row and row.get("total_pnl") is not None:
            return float(row["total_pnl"])
        return 0.0

    async def _query_prev_month_capital(self, strategy_id: str) -> float:
        """
        查询指定策略上月的分配资金

        从上月 public.capital_allocation 表中查找该策略的 allocated_amount。

        Args:
            strategy_id: 策略唯一标识

        Returns:
            上月分配资金（USDT），若无记录返回 0.0
        """
        # 计算上月月份标识
        now = datetime.now(CST)
        if now.month == 1:
            prev_month = f"{now.year - 1}-12"
        else:
            prev_month = f"{now.year}-{now.month - 1:02d}"

        row = await self.db_manager.fetch_one(
            self._PREV_ALLOCATION_QUERY,
            prev_month,
        )

        if row and row.get("entries"):
            entries = row["entries"]
            # entries 可能是 JSON 字符串或已解析的列表
            if isinstance(entries, str):
                entries = json.loads(entries)
            if isinstance(entries, list):
                for entry in entries:
                    if entry.get("strategy_id") == strategy_id:
                        return float(entry.get("allocated_amount", 0.0))

        return 0.0