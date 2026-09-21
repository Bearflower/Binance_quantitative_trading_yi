"""
指标预计算任务（数据看板提速）

在 main_docker.py lifespan 中由 APScheduler IntervalTrigger 每 N 秒调度，
调用 data_service.compute_and_store_metrics() 把 Binance 实时聚合结果固化落库到
public.metric_snapshot，前端请求退化为单次 DB 查询（<10ms）。

与 equity_snapshot_job.py 同构：失败仅记日志告警，不抛出、不阻断主流程。

用法（在 main_docker.py lifespan 中注入）：
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(run_precompute, IntervalTrigger(seconds=60), args=(data_service,))
    scheduler.start()
"""
import structlog

logger = structlog.get_logger()


async def run_precompute(data_service) -> dict:
    """执行一次全量指标预计算落库

    聚合全量数据（day/week/month × total/strategy/symbol）UPSERT 到
    public.metric_snapshot。任一步骤失败仅记录错误日志告警，不抛出异常，
    由上层调度器在下一个周期重试。

    Args:
        data_service: DataService 实例（需具备 compute_and_store_metrics 方法）

    Returns:
        dict: {granularities, rows} 成功时的行数统计；
              失败时返回 {error} 最小信息。
    """
    try:
        metrics = await data_service.compute_and_store_metrics()
        logger.info(
            "指标预计算完成",
            granularities=metrics.get("granularities"),
            rows=metrics.get("rows"),
        )
        return metrics
    except Exception as e:
        logger.error("指标预计算失败", error=str(e))
        return {"error": str(e)}