"""
共享调度工具箱

提供定时任务注册的通用封装，供各容器宿主复用（data_backend、dashboard、策略等）：
- build_scheduler:      创建 AsyncIOScheduler 实例
- add_interval_job:     注册间隔触发任务（间隔秒数走环境变量，禁止硬编码）
- add_cron_job:         注册 cron 触发任务（时分走环境变量，禁止硬编码）
- schedule_startup_run: 启动预热——延迟至依赖就绪后执行一次协程
- shutdown_scheduler:   幂等关闭调度器

约定：
- 所有业务参数（间隔、cron 时分、启动延迟）一律走环境变量，代码不写死固定值。
- 本模块仅依赖 APScheduler + os + asyncio，不 import 任何业务包（shared 禁止反向依赖 dashboard）。
- 具体 job 函数与数据服务实例由宿主传入。
"""

import asyncio
import os
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional, Sequence

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# 默认时区（与项目数据口径一致）
_DEFAULT_TZ = "Asia/Shanghai"

# 启动期依赖就绪延迟（秒），走环境变量注入
_STARTUP_DELAY_ENV = "STARTUP_READY_DELAY_SECONDS"
_STARTUP_DELAY_DEFAULT = "30"


def build_scheduler(timezone: str = _DEFAULT_TZ) -> AsyncIOScheduler:
    """创建调度器实例

    Args:
        timezone: 调度器时区，默认 Asia/Shanghai（北京时间）

    Returns:
        AsyncIOScheduler 实例
    """
    return AsyncIOScheduler(timezone=timezone)


def _startup_ready_delay() -> float:
    """读取启动期依赖就绪延迟（秒），走环境变量，禁止硬编码"""
    return float(os.getenv(_STARTUP_DELAY_ENV, _STARTUP_DELAY_DEFAULT))


def add_interval_job(
    scheduler: AsyncIOScheduler,
    func: Callable,
    job_id: str,
    env_key: str,
    default_seconds: int,
    args: Optional[Sequence] = None,
    misfire_grace: int = 120,
    coalesce: bool = True,
    max_instances: int = 1,
) -> int:
    """注册间隔触发任务

    间隔秒数从环境变量 env_key 读取（默认 default_seconds），
    首个执行时间延迟 STARTUP_READY_DELAY_SECONDS，避免容器启动瞬间
    依赖（DB/网络）未就绪时产生噪音告警。

    Args:
        scheduler: 目标调度器
        func: 待执行的异步任务函数
        job_id: 任务唯一 ID（用于去重与幂等）
        env_key: 间隔秒数的环境变量名
        default_seconds: 环境变量缺失时的默认间隔（秒）
        args: 传给 func 的位置参数
        misfire_grace: 允许任务延迟执行的宽限时间（秒）
        coalesce: 积压任务是否合并执行
        max_instances: 最大并发实例数

    Returns:
        实际生效的间隔秒数
    """
    interval = int(os.getenv(env_key, str(default_seconds)))
    delay = _startup_ready_delay()
    start_date = datetime.now() + timedelta(seconds=delay) if delay > 0 else None
    scheduler.add_job(
        func,
        IntervalTrigger(seconds=interval, start_date=start_date),
        args=list(args) if args else [],
        id=job_id,
        max_instances=max_instances,
        coalesce=coalesce,
        misfire_grace_time=misfire_grace,
    )
    return interval


def add_cron_job(
    scheduler: AsyncIOScheduler,
    func: Callable,
    job_id: str,
    env_hour: str,
    env_minute: str,
    default_hour: int,
    default_minute: int,
    args: Optional[Sequence] = None,
    misfire_grace: int = 3600,
) -> tuple:
    """注册 cron 触发任务

    执行时（小时/分钟）从环境变量 env_hour / env_minute 读取（默认值兜底）。

    Args:
        scheduler: 目标调度器
        func: 待执行的异步任务函数
        job_id: 任务唯一 ID
        env_hour: 小时的环境变量名
        env_minute: 分钟的环境变量名
        default_hour: 环境变量缺失时的默认小时
        default_minute: 环境变量缺失时的默认分钟
        args: 传给 func 的位置参数
        misfire_grace: 允许任务延迟执行的宽限时间（秒）

    Returns:
        (hour, minute) 实际生效的执行时间
    """
    hour = int(os.getenv(env_hour, str(default_hour)))
    minute = int(os.getenv(env_minute, str(default_minute)))
    scheduler.add_job(
        func,
        CronTrigger(hour=hour, minute=minute),
        args=list(args) if args else [],
        id=job_id,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=misfire_grace,
    )
    return hour, minute


def schedule_startup_run(
    loop: Optional[asyncio.AbstractEventLoop],
    coro_factory: Callable[[], Awaitable],
    delay_env: str = _STARTUP_DELAY_ENV,
) -> None:
    """启动预热：延迟至依赖就绪后执行一次协程

    重启后立即执行一次（而非等首个周期），保证下游读取的数据非空。

    Args:
        loop: 目标事件循环（None 时取当前运行循环）
        coro_factory: 返回 awaitable 的零参可调用对象（延迟构造，避免启动期提前创建资源）
        delay_env: 延迟秒数的环境变量名
    """
    delay = float(os.getenv(delay_env, _STARTUP_DELAY_DEFAULT))

    async def _run_after_ready():
        if delay > 0:
            await asyncio.sleep(delay)
        await coro_factory()

    target_loop = loop or asyncio.get_event_loop()
    asyncio.ensure_future(_run_after_ready(), loop=target_loop)


def shutdown_scheduler(scheduler: AsyncIOScheduler) -> None:
    """幂等关闭调度器（未运行则静默跳过）"""
    if scheduler.running:
        scheduler.shutdown(wait=False)
