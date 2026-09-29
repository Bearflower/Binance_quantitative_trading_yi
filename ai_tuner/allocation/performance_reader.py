"""
统一绩效指标读取器

从 public.performance_metric_snapshot 预计算表读取夏普率、最大回撤等精准绩效指标。
该表由数据后台每 600s 定时写入，dashboard 和 ai-tuner 共享同一张表，口径一致。

本模块仅读，不写入。
"""

import structlog
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

from shared.strategy_ids import PERFORMANCE_STRATEGY_IDS

logger = structlog.get_logger()

# 北京时区（UTC+8），与 dashboard 数据服务口径一致
BEIJING_TZ = timezone(timedelta(hours=8))


def _get_default_bucket_key(granularity: str) -> date:
    """根据粒度计算默认桶起点（北京时区自然周/月/年/日）

    口径与 dashboard data_service_docker.py 中 period_start 计算保持一致：
    - week:  当前自然周周一
    - month: 本月 1 号
    - year:  今年 1 月 1 号
    - day:   今天

    Args:
        granularity: day | week | month | year

    Returns:
        date: 对应桶的起点日期
    """
    now_beijing = datetime.now(BEIJING_TZ).date()
    if granularity == "week":
        # Python weekday(): 周一=0, 周日=6
        return now_beijing - timedelta(days=now_beijing.weekday())
    if granularity == "month":
        return date(now_beijing.year, now_beijing.month, 1)
    if granularity == "year":
        return date(now_beijing.year, 1, 1)
    # 默认 day
    return now_beijing


class UnifiedPerformanceReader:
    """从 public.performance_metric_snapshot 读取精准绩效指标（仅读）

    与 dashboard 看板同源：两者都读同一张预计算表，口径完全一致。
    与 PerformanceMetrics.sharpe_approx / RiskMetrics.max_drawdown_pct 不同：
    那两个是各适配器逐笔近似值（口径不同），本类读取的是事后统计的精准值。
    """

    # 预计算表名和字段（集中定义，避免硬编码散落）
    _TABLE_NAME = "public.performance_metric_snapshot"
    _QUERY_TEMPLATE = f"""
        SELECT bucket_key, sample_count, sharpe, max_drawdown
        FROM {_TABLE_NAME}
        WHERE granularity = $1
          AND scope = 'strategy'
          AND strategy_id = $2
          AND bucket_key <= $3
        ORDER BY bucket_key DESC
        LIMIT 1
    """

    # 粒度白名单（防止 SQL 拼接误用）
    _VALID_GRANULARITIES = {"day", "week", "month", "year"}

    def __init__(self, db_manager):
        """
        初始化绩效读取器

        Args:
            db_manager: shared.database.DatabaseManager 实例
                        （需提供 fetch_one(query, *args) -> Optional[Dict] 接口）
        """
        self.db = db_manager

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def get_latest(
        self,
        strategy_id: str,
        granularity: str = "week",
        bucket_key: Optional[date] = None,
    ) -> Optional[Dict[str, Any]]:
        """读取某策略指定粒度最新一条绩效指标

        Args:
            strategy_id: 策略规范 id（如 btc_eth）
            granularity: day | week | month | year
            bucket_key:  指定桶起点（DATE 类型）。None 时使用默认值：
                        自然周周一 / 本月 1 号 / 今年 1 月 1 号 / 今天。

        Returns:
            dict: {"bucket_key", "sample_count", "sharpe", "max_drawdown"}，
                  字段可能为 None（预计算表尚未有值）；
                  或 None（未查到任何记录）。
        """
        # 参数校验
        if granularity not in self._VALID_GRANULARITIES:
            logger.warning(
                "无效粒度，回退 week",
                granularity=granularity,
                strategy_id=strategy_id,
            )
            granularity = "week"

        target = bucket_key if bucket_key is not None else _get_default_bucket_key(granularity)

        try:
            row = await self.db.fetch_one(
                self._QUERY_TEMPLATE,
                granularity,
                strategy_id,
                target,
            )
        except Exception as e:
            # 数据库查询异常，不阻断上层流程
            logger.warning(
                "绩效快照查询异常",
                strategy_id=strategy_id,
                granularity=granularity,
                error=str(e),
            )
            return None

        if row is None:
            logger.info(
                "绩效快照无数据（新表尚未填充）",
                strategy_id=strategy_id,
                granularity=granularity,
                bucket_key=str(target),
            )
            return None

        return {
            "bucket_key": row.get("bucket_key"),
            "sample_count": row.get("sample_count"),
            "sharpe": row.get("sharpe"),
            "max_drawdown": row.get("max_drawdown"),
        }
