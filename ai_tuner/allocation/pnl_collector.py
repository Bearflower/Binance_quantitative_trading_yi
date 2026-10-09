"""
盈亏与资金数据采集器

负责从各策略数据表查询当月已实现盈亏和月初实际占用保证金。

数据来源：
    - trading.trade_records: 各策略的已实现盈亏记录
    - public.strategy_position_snapshot_history: 各策略月初实际占用保证金
      （open_margin，作为月收益率的分母，替代名义分配额）
"""

import structlog
from datetime import datetime
from typing import Any, Dict, List

# trade_records.strategy 存的是英文短名（strategy_id），直接用 strategy_id 列表查询即可
# 例如 "btc_eth"、"btc_eth_aggressive"、"new_coin"、"hrs"、"grid"

logger = structlog.get_logger()


def _to_naive(dt: datetime) -> datetime:
    """转换为 naive datetime（数据库 TIMESTAMP 字段无时区）"""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


class PnLCollector:
    """
    盈亏与资金数据采集器

    从数据库查询各参与策略的当月已实现盈亏和月初实际占用保证金。
    """

    # 查询当月已实现盈亏的 SQL 模板（按落库中文名列表匹配）
    _PNL_QUERY_TEMPLATE = """
        SELECT COALESCE(SUM(realized_pnl), 0) AS total_pnl
        FROM trading.trade_records
        WHERE strategy = ANY($1::text[])
          AND executed_at >= $2
          AND executed_at < $3
    """

    # 持仓占用对账的历史明细表（小时级快照，月度资金分配读取其月初实际占用保证金）
    _SNAPSHOT_HISTORY_TABLE = "public.strategy_position_snapshot_history"

    # 查询本月（snapshot_hour >= 月初）最早一条快照的持仓保证金
    _MONTH_START_MARGIN_QUERY = f"""
        SELECT open_margin
        FROM {_SNAPSHOT_HISTORY_TABLE}
        WHERE strategy_id = $1
          AND snapshot_hour >= $2
        ORDER BY snapshot_hour ASC
        LIMIT 1
    """

    # 回退：本月无快照时，取早于本月最晚一条快照的持仓保证金
    _PREV_PERIOD_MARGIN_QUERY = f"""
        SELECT open_margin
        FROM {_SNAPSHOT_HISTORY_TABLE}
        WHERE strategy_id = $1
          AND snapshot_hour < $2
        ORDER BY snapshot_hour DESC
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
        采集所有参与策略的当月已实现盈亏和月初实际占用保证金

        Args:
            month_start: 当月起始时间（包含）
            month_end: 当月结束时间（不包含）

        Returns:
            {strategy_id: {"pnl": float, "capital": float}} 格式的字典
            - pnl: 当月已实现盈亏（USDT）
            - capital: 月初实际占用保证金（USDT，作收益率分母），无历史快照时为 0.0
        """
        result: Dict[str, Dict[str, float]] = {}

        for strategy_cfg in self.strategies:
            strategy_id = strategy_cfg.get("strategy_id", "")
            if not strategy_id:
                logger.warning("策略配置缺少 strategy_id，跳过")
                continue

            try:
                # 解析该策略在 trade_records 中的落库中文名列表
                db_names = self._resolve_db_names(strategy_cfg)

                # 查询当月已实现盈亏
                pnl = await self._query_strategy_pnl(
                    db_names=db_names,
                    month_start=month_start,
                    month_end=month_end,
                )

                # 查询月初实际占用保证金（作收益率分母）
                capital = await self._query_month_start_margin(
                    strategy_id,
                    month_start,
                )

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

    def _resolve_db_names(self, strategy_cfg: Dict[str, Any]) -> list:
        """
        解析某策略在 trade_records 中的落库 strategy_id 列表

        trade_records.strategy 存的是英文短名（strategy_id），直接返回单元素列表即可。

        Args:
            strategy_cfg: 策略配置字典（含 strategy_id）

        Returns:
            strategy_id 列表
        """
        strategy_id = strategy_cfg.get("strategy_id", "")
        if strategy_id:
            return [strategy_id]

        logger.warning(
            "策略配置缺少 strategy_id，PnL 采集按 0 处理",
            strategy=strategy_cfg.get("name", ""),
        )
        return []

    async def _query_strategy_pnl(
        self,
        db_names: list,
        month_start: datetime,
        month_end: datetime,
    ) -> float:
        """
        查询指定策略当月的已实现盈亏

        Args:
            db_names: 该策略在 trade_records 中的落库中文名列表（为空时直接返回 0.0）
            month_start: 当月起始时间（包含）
            month_end: 当月结束时间（不包含）

        Returns:
            已实现盈亏总额（USDT）
        """
        if not db_names:
            return 0.0

        # 数据库 TIMESTAMP 字段无时区，需转为 naive datetime
        naive_start = _to_naive(month_start)
        naive_end = _to_naive(month_end)

        row = await self.db_manager.fetch_one(
            self._PNL_QUERY_TEMPLATE,
            db_names,
            naive_start,
            naive_end,
        )
        if row and row.get("total_pnl") is not None:
            return float(row["total_pnl"])
        return 0.0

    async def _query_month_start_margin(self, strategy_id: str, month_start: datetime) -> float:
        """
        查询指定策略月初实际占用的持仓保证金（open_margin），作为收益率分母

        从 public.strategy_position_snapshot_history 中取该策略本月最早一条快照的持仓保证金；
        若本月无历史快照（历史表尚未积累月初数据），回退取早于本月的最晚一条快照。

        Args:
            strategy_id: 策略唯一标识
            month_start: 当月起始时间（可带时区，可按需转为 naive）

        Returns:
            月初实际占用保证金（USDT）；无任何历史快照时返回 0.0
        """
        naive_start = _to_naive(month_start)

        # 优先取本月最早一条快照的持仓保证金
        row = await self.db_manager.fetch_one(
            self._MONTH_START_MARGIN_QUERY,
            strategy_id,
            naive_start,
        )
        if row is not None and row.get("open_margin") is not None:
            return float(row["open_margin"])

        # 本月无快照，回退到早于本月的最晚一条快照
        row = await self.db_manager.fetch_one(
            self._PREV_PERIOD_MARGIN_QUERY,
            strategy_id,
            naive_start,
        )
        if row is not None and row.get("open_margin") is not None:
            return float(row["open_margin"])

        logger.warning("策略无历史持仓快照，月初占用按 0 处理", strategy_id=strategy_id)
        return 0.0