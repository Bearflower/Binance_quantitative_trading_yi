#!/usr/bin/env python3
"""
数据后台独立容器入口

承载数据维护定时任务（净资产快照 / 指标预计算 / 持仓对账 / 佣金回填），
与 dashboard 解耦：dashboard 仅负责 API 展示，可随时重建/改造，数据连续性不受影响。

调度参数全部走环境变量（禁止硬编码），job 函数复用 dashboard 数据服务层：
- equity_snapshot_job.run_snapshot        → public.equity_snapshot（每日 23:30 北京时间）
- metric_precompute_job.run_precompute    → public.metric_snapshot（默认每 60 秒）
- DataService.compute_and_store_positions → strategy_position_snapshot（默认每 300 秒）
- commission_reconcile_job.run_reconcile  → trading.trade_records 佣金回填（默认每 3600 秒）
"""
import asyncio
import os
import signal
import sys

# 将项目根与 dashboard 包加入导入路径（须置于最前，避免与项目根 services/ 命名空间包冲突）
APP_ROOT = os.getenv("APP_ROOT", "/app")
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "dashboard/backend"))

import structlog

from shared.scheduler import (
    add_cron_job,
    add_interval_job,
    build_scheduler,
    schedule_startup_run,
    shutdown_scheduler,
)
from services.commission_reconcile_job import run_reconcile
from services.data_service_docker import DataService
from services.equity_snapshot_job import run_snapshot
from services.market_circuit_breaker_job import run_breaker_index
from services.metric_precompute_job import run_precompute
from shared.circuit_breaker import load_circuit_breaker_config, parse_cron

logger = structlog.get_logger()

# 优雅退出标记（SIGTERM/SIGINT 触发）
_stop = asyncio.Event()

# 注册的任务清单（用于日志展示与一致性核对）
_JOBS = [
    "equity_daily_snapshot",
    "metric_precompute",
    "position_reconcile",
    "commission_reconcile",
    "market_breaker_index",
]


def _register_jobs(scheduler, data_service: DataService) -> None:
    """注册全部数据维护任务（调度参数走环境变量，禁止硬编码）"""
    add_cron_job(
        scheduler,
        run_snapshot,
        "equity_daily_snapshot",
        env_hour="SNAPSHOT_HOUR",
        env_minute="SNAPSHOT_MINUTE",
        default_hour=23,
        default_minute=30,
        args=[data_service],
        misfire_grace=3600,
    )
    add_interval_job(
        scheduler,
        run_precompute,
        "metric_precompute",
        env_key="PRECOMPUTE_INTERVAL_SECONDS",
        default_seconds=60,
        args=[data_service],
        misfire_grace=120,
    )
    add_interval_job(
        scheduler,
        data_service.compute_and_store_positions,
        "position_reconcile",
        env_key="POSITION_RECONCILE_INTERVAL_SECONDS",
        default_seconds=300,
        misfire_grace=300,
    )
    add_interval_job(
        scheduler,
        run_reconcile,
        "commission_reconcile",
        env_key="COMMISSION_RECONCILE_INTERVAL_SECONDS",
        default_seconds=3600,
        args=[data_service],
        misfire_grace=600,
    )
    # 组合级熔断指数计算（每小时 03 分，与策略执行高峰错开；enabled=false 时不注册）
    cb_cfg = load_circuit_breaker_config()
    if cb_cfg.get("enabled"):
        index_hour_cron, index_minute_cron = parse_cron(cb_cfg["index_cron"])
        add_cron_job(
            scheduler,
            run_breaker_index,
            "market_breaker_index",
            env_hour="BREAKER_INDEX_HOUR",
            env_minute="BREAKER_INDEX_MINUTE",
            default_hour=index_hour_cron,
            default_minute=index_minute_cron,
            args=[data_service],
            misfire_grace=3600,
        )


def _schedule_startup_warmup(loop, data_service: DataService) -> None:
    """启动预热：延迟至依赖就绪后各执行一次，避免重启后下游读库为空"""
    schedule_startup_run(loop, lambda: run_precompute(data_service))
    schedule_startup_run(loop, lambda: data_service.compute_and_store_positions())
    schedule_startup_run(loop, lambda: run_reconcile(data_service))
    # 熔断开关开启时预热一次指数，保证首小时策略开仓即可读到指数
    if load_circuit_breaker_config().get("enabled"):
        schedule_startup_run(loop, lambda: run_breaker_index(data_service))


async def main() -> None:
    """主流程：注册并启动调度器，等待退出信号，优雅关闭"""
    # 单 DataService 实例，4 个 job 共用连接池与 Binance 客户端
    data_service = DataService()
    scheduler = build_scheduler()
    _register_jobs(scheduler, data_service)
    try:
        scheduler.start()
        logger.info("数据后台调度器已启动", jobs=_JOBS)
        _schedule_startup_warmup(asyncio.get_running_loop(), data_service)
        await _stop.wait()
    finally:
        shutdown_scheduler(scheduler)
        db = getattr(data_service, "_db_manager", None)
        if db is not None:
            await db.disconnect()
        logger.info("数据后台已关闭")


def _handle_signal() -> None:
    """信号处理：置位退出事件，触发优雅关闭"""
    _stop.set()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
