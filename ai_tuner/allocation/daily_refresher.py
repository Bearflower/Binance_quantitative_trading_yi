"""
每日刷新月度资金分配金额

每天北京时间 00:15 自动执行，根据当日实时净资产重新计算各策略的
分配金额（allocated_amount）并覆盖到数据库，实现"月初定比例、
每日按净值浮动金额"的资金管理机制。

设计约束（与月度分配一致）：
1. 月初算出的 allocated_ratio 是权威值，整月不变。
2. 每日仅刷新 allocated_amount = 当日净资产 × allocated_ratio。
3. 只 UPDATE 数据库，绝不修改策略 config，不生成 config 备份。
4. 失败容错：币安净值查询失败或异常时跳过本次、保留上一日旧值。
5. reserve（风险备用金）不在 entries 内，天然不参与刷新。
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import structlog
from apscheduler.triggers.cron import CronTrigger

from ai_tuner.allocation.balance_provider import get_actual_balance

logger = structlog.get_logger()

# 分配金额保留的小数位数（与月度分配保留 2 位小数保持一致）
_ROUND_DECIMALS = 2

# 默认每日刷新执行时刻（北京时间，cron 5 字段：分 时 日 月 周）
_DEFAULT_REFRESH_CRON = "15 0 * * *"


class DailyAllocationRefresher:
    """
    每日刷新月度资金分配金额

    读取当月 active 分配记录，用当日净资产重算各策略 allocated_amount，
    幂等覆盖到数据库，绝不改动策略 config。
    """

    # 查询当月 active 分配记录
    _FETCH_ACTIVE_QUERY = """
        SELECT entries
        FROM public.capital_allocation
        WHERE month = $1 AND status = 'active'
        ORDER BY created_at DESC
        LIMIT 1
    """

    # 幂等更新：仅覆盖当月 active 记录的 entries 与 total_capital
    _UPDATE_QUERY = """
        UPDATE public.capital_allocation
        SET entries = $1, total_capital = $2
        WHERE month = $3 AND status = 'active'
    """

    def __init__(
        self,
        config: Dict[str, Any],
        db_manager,
        binance_client=None,
    ):
        """
        初始化每日刷新任务

        Args:
            config: 完整系统配置字典
            db_manager: DatabaseManager 实例
            binance_client: 币安客户端（可选），用于获取当日实时净资产
        """
        self.config = config
        self.db_manager = db_manager
        self.binance_client = binance_client

        # 从配置读取资金分配参数与调度时区，避免硬编码
        self.allocation_cfg = config.get("capital_allocation", {})
        timezone_name = config.get("scheduler", {}).get("timezone", "Asia/Shanghai")
        self.timezone = ZoneInfo(timezone_name)

    def get_cron_trigger(self) -> CronTrigger:
        """
        返回每日刷新任务的 cron 触发器

        执行时刻从配置 capital_allocation.daily_refresh_cron 读取，
        时区取 scheduler.timezone，避免硬编码魔法值。

        Returns:
            CronTrigger 实例
        """
        cron_expr = self.allocation_cfg.get(
            "daily_refresh_cron", _DEFAULT_REFRESH_CRON
        )
        return CronTrigger.from_crontab(cron_expr, timezone=self.timezone)

    async def run_daily_refresh(self) -> Optional[Dict[str, Any]]:
        """
        执行每日刷新流程

        读取当月 active 分配记录，用当日净资产重算各策略 allocated_amount
        并幂等覆盖数据库；任一环节失败（无记录/净值失败/entries 非法）时
        跳过本次并保留旧值。绝不改动策略 config。

        Returns: 刷新结果字典，跳过或失败返回 None
        """
        try:
            # 未启用资金分配时直接跳过，与月度分配（monthly_job）的 enabled 门控对齐
            if not self.allocation_cfg.get("enabled", False):
                logger.info("月度资金分配未启用，跳过每日刷新")
                return None

            month = datetime.now(self.timezone).strftime("%Y-%m")

            row = await self.db_manager.fetch_one(self._FETCH_ACTIVE_QUERY, month)
            if not row:
                logger.warning("当月无 active 分配记录，跳过每日刷新", month=month)
                return None

            total_capital = await self._get_actual_balance()
            # 净资产为 None 或 <=0 均视为"获取失败/无可分配资金"，保留上一日旧值，
            # 口径与月度分配的 actual_balance > 0 校验保持一致
            if total_capital is None or total_capital <= 0:
                logger.warning(
                    "获取当日净资产失败或非正数，保留上一日的分配金额", month=month
                )
                return None

            entries = self._recalculate_entries(row.get("entries"), total_capital)
            if entries is None:
                logger.warning("entries 非法或为空，跳过每日刷新", month=month)
                return None

            await self.db_manager.execute(
                self._UPDATE_QUERY,
                json.dumps(entries, ensure_ascii=False),
                round(total_capital, _ROUND_DECIMALS),
                month,
            )

            logger.info(
                "每日分配金额刷新完成",
                month=month,
                total_capital=total_capital,
                strategy_count=len(entries),
            )
            return {
                "month": month,
                "total_capital": total_capital,
                "strategy_count": len(entries),
            }

        except Exception as e:
            logger.error("每日分配金额刷新异常（保留旧值）", error=str(e), exc_info=True)
            return None

    async def _get_actual_balance(self) -> Optional[float]:
        """
        获取当日实时净资产（USDT）

        委托公共模块 balance_provider.get_actual_balance 实现，保持对外
        方法签名不变，避免破坏调用方。失败或无币安客户端时返回 None，
        交由调用方跳过本次刷新。
        """
        return await get_actual_balance(self.binance_client)

    def _recalculate_entries(
        self,
        entries_raw: Any,
        total_capital: float,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        重算各策略 allocated_amount

        对每个 dict 条目：新 allocated_amount = total_capital × allocated_ratio。
        幂等容忍：非 dict 条目或缺 allocated_ratio 的条目保留原值不清零。

        Args:
            entries_raw: 数据库读取的 entries（可能是字符串或已解析对象）
            total_capital: 当日净资产

        Returns:
            重算后的 entries 列表；entries 非法或为空返回 None
        """
        entries = self._parse_entries(entries_raw)
        if entries is None or not entries:
            return None

        for item in entries:
            if not isinstance(item, dict):
                logger.warning("entries 中存在非 dict 条目，跳过重算", item=str(item)[:80])
                continue
            ratio = item.get("allocated_ratio")
            if ratio is None:
                logger.warning("条目缺少 allocated_ratio，保留原值",
                               strategy_id=item.get("strategy_id"))
                continue
            item["allocated_amount"] = round(total_capital * float(ratio), _ROUND_DECIMALS)
        return entries

    @staticmethod
    def _parse_entries(entries_raw: Any) -> Optional[List[Any]]:
        """
        解析 entries 为列表

        asyncpg 对 jsonb 列可能返回字符串或已解析的 Python 对象，统一处理。

        Args:
            entries_raw: 原始 entries 值

        Returns:
            列表；解析失败或非列表返回 None
        """
        if isinstance(entries_raw, str):
            try:
                return json.loads(entries_raw)
            except json.JSONDecodeError:
                return None
        return entries_raw if isinstance(entries_raw, list) else None