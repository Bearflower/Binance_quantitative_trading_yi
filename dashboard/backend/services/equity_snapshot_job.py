"""
每日净资产快照任务（模块①：净资产历史快照）

在天 23:30（北京时间）由 APScheduler 调度，将账户净资产实时快照写入
public.equity_snapshot 表（同日 UPSERT 幂等），供收益模块与风控模块读取。

用法（在 main_docker.py lifespan 中注入）：
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(run_snapshot, CronTrigger(hour=23, minute=30), args=(data_service,))
    scheduler.start()
"""
from datetime import datetime, timedelta, timezone

import structlog

logger = structlog.get_logger()

# 北京时区（与 data_service_docker 保持一致）
BEIJING_TZ = timezone(timedelta(hours=8))

# 快照 UPSERT SQL（同日幂等，失败仅记日志不阻断主流程）
_SNAPSHOT_UPSERT_SQL = """
INSERT INTO public.equity_snapshot
    (snapshot_date, total_equity, available_balance, open_positions)
VALUES ($1, $2, $3, $4)
ON CONFLICT (snapshot_date) DO UPDATE SET
    total_equity = EXCLUDED.total_equity,
    available_balance = EXCLUDED.available_balance,
    open_positions = EXCLUDED.open_positions,
    updated_at = CURRENT_TIMESTAMP
"""


async def run_snapshot(data_service) -> dict:
    """执行一次净资产快照落库

    计算北京今日 snapshot_date，将 get_account_equity 的快照写入
    public.equity_snapshot（UPSERT）。任一步骤失败仅记录警告日志告警，不抛出异常。

    Args:
        data_service: DataService 实例（需具备 get_account_equity 与数据库连接）

    Returns:
        dict: {snapshot_date, total_equity, available_balance, open_positions}
              失败时返回包含 error 的最小信息。
    """
    snapshot_date = datetime.now(BEIJING_TZ).date()
    try:
        equity = await data_service.get_account_equity()
        total_equity = equity.get("total_equity", "0")
        available_balance = equity.get("available_balance", "0")
        open_positions = int(equity.get("open_positions", 0))

        await data_service._db_manager.execute(
            _SNAPSHOT_UPSERT_SQL,
            snapshot_date,
            str(total_equity),
            str(available_balance),
            open_positions,
        )

        logger.info(
            "净资产快照已落库",
            snapshot_date=snapshot_date.isoformat(),
            total_equity=total_equity,
            open_positions=open_positions,
        )
        return {
            "snapshot_date": snapshot_date.isoformat(),
            "total_equity": total_equity,
            "available_balance": available_balance,
            "open_positions": open_positions,
        }
    except Exception as e:
        logger.error(
            "净资产快照落库失败", snapshot_date=snapshot_date.isoformat(), error=str(e)
        )
        return {"snapshot_date": snapshot_date.isoformat(), "error": str(e)}