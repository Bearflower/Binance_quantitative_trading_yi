"""
绩效指标定时任务（数据看板提速 · 风险调整收益预计算）

在 data_backend/main.py 中由 APScheduler IntervalTrigger 每 N 秒调度，
调用 data_service.compute_and_store_performance_metrics() 把夏普比率、最大回撤
等风险调整收益指标固化落库到 public.performance_metric_snapshot，
前端请求退化为单次 DB 查询（<10ms），避免每次实时从 equity_snapshot + trade_records 计算。

与 metric_precompute_job.py 同构：失败仅记日志告警，不抛出、不阻断主流程。

用法（在 data_backend/main.py 中注入）：
    from shared.scheduler import add_interval_job
    from services.performance_metric_job import run_performance_metrics
    add_interval_job(..., run_performance_metrics, "performance_metric",
                     env_key="PERFORMANCE_METRIC_INTERVAL_SECONDS",
                     default_seconds=600, misfire_grace=600, args=[data_service])
"""
import structlog

logger = structlog.get_logger()


async def run_performance_metrics(data_service) -> dict:
    """执行一次全量绩效指标计算落库

    遍历 day/week/month/year × total/strategy 计算夏普比率和最大回撤，
    UPSERT 到 public.performance_metric_snapshot。任一步骤失败仅记录错误日志告警，
    不抛出异常，由上层调度器在下一个周期重试。

    Args:
        data_service: DataService 实例（需具备 compute_and_store_performance_metrics 方法）

    Returns:
        dict: {rows} 成功时的落库行数统计；
              失败时返回 {error} 最小信息。
    """
    try:
        result = await data_service.compute_and_store_performance_metrics()
        logger.info(
            "绩效指标预计算完成",
            rows=result.get("rows"),
        )
        return result
    except Exception as e:
        logger.error("绩效指标预计算失败", error=str(e))
        return {"error": str(e)}
