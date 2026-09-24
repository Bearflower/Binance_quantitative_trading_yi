"""
共享调度工具箱单测

覆盖：调度器创建、间隔/cron 任务注册（env 驱动参数、延迟、防重入）、
启动预热、幂等关闭。
"""

import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from shared.scheduler import (
    add_cron_job,
    add_interval_job,
    build_scheduler,
    schedule_startup_run,
    shutdown_scheduler,
)


class TestBuildScheduler:
    """调度器创建"""

    def test_build_scheduler_timezone(self):
        """创建调度器并确认时区为北京时区"""
        scheduler = build_scheduler()
        assert isinstance(scheduler, AsyncIOScheduler)
        assert str(scheduler.timezone) == "Asia/Shanghai"


class TestAddIntervalJob:
    """间隔触发任务注册"""

    async def _dummy_job(self):
        return None

    def test_interval_from_env(self, monkeypatch):
        """间隔秒数从环境变量读取"""
        monkeypatch.setenv("TEST_INTERVAL_SECONDS", "42")
        scheduler = build_scheduler()
        interval = add_interval_job(
            scheduler,
            self._dummy_job,
            job_id="test_interval",
            env_key="TEST_INTERVAL_SECONDS",
            default_seconds=60,
        )
        assert interval == 42
        job = scheduler.get_job("test_interval")
        assert job is not None
        assert job.trigger.interval.total_seconds() == 42
        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.misfire_grace_time == 120

    def test_interval_default_when_env_missing(self, monkeypatch):
        """环境变量缺失时使用默认值"""
        monkeypatch.delenv("TEST_INTERVAL_SECONDS", raising=False)
        scheduler = build_scheduler()
        interval = add_interval_job(
            scheduler,
            self._dummy_job,
            job_id="test_interval_default",
            env_key="TEST_INTERVAL_SECONDS",
            default_seconds=60,
        )
        assert interval == 60
        job = scheduler.get_job("test_interval_default")
        assert job.trigger.interval.total_seconds() == 60

    def test_interval_start_date_delayed(self, monkeypatch):
        """首次周期触发 = 启动延迟 + 一个周期（预热承担首次，避免首轮双跑）"""
        monkeypatch.setenv("STARTUP_READY_DELAY_SECONDS", "5")
        scheduler = build_scheduler()
        add_interval_job(
            scheduler,
            self._dummy_job,
            job_id="test_interval_delay",
            env_key="TEST_INTERVAL_SECONDS",
            default_seconds=60,
        )
        job = scheduler.get_job("test_interval_delay")
        assert job.trigger.start_date is not None
        # 首次触发应在 now + delay(5s) + interval(60s) = 65s 之后（±2s 容差，时区与调度器一致）
        expected = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(seconds=5 + 60)
        assert abs((job.trigger.start_date - expected).total_seconds()) < 2


class TestAddCronJob:
    """cron 触发任务注册"""

    async def _dummy_job(self):
        return None

    def test_cron_from_env(self, monkeypatch):
        """执行时（时/分）从环境变量读取"""
        monkeypatch.setenv("TEST_SNAPSHOT_HOUR", "7")
        monkeypatch.setenv("TEST_SNAPSHOT_MINUTE", "15")
        scheduler = build_scheduler()
        hour, minute = add_cron_job(
            scheduler,
            self._dummy_job,
            job_id="test_cron",
            env_hour="TEST_SNAPSHOT_HOUR",
            env_minute="TEST_SNAPSHOT_MINUTE",
            default_hour=23,
            default_minute=30,
        )
        assert (hour, minute) == (7, 15)
        job = scheduler.get_job("test_cron")
        assert job.trigger.fields[5].expressions[0].first == 7
        assert job.trigger.fields[6].expressions[0].first == 15

    def test_cron_default_when_env_missing(self, monkeypatch):
        """环境变量缺失时使用默认时/分"""
        monkeypatch.delenv("TEST_SNAPSHOT_HOUR", raising=False)
        monkeypatch.delenv("TEST_SNAPSHOT_MINUTE", raising=False)
        scheduler = build_scheduler()
        hour, minute = add_cron_job(
            scheduler,
            self._dummy_job,
            job_id="test_cron_default",
            env_hour="TEST_SNAPSHOT_HOUR",
            env_minute="TEST_SNAPSHOT_MINUTE",
            default_hour=23,
            default_minute=30,
        )
        assert (hour, minute) == (23, 30)
        job = scheduler.get_job("test_cron_default")
        assert job.trigger.fields[5].expressions[0].first == 23
        assert job.trigger.fields[6].expressions[0].first == 30


class TestScheduleStartupRun:
    """启动预热"""

    @pytest.mark.asyncio
    async def test_startup_run_executed_once(self, monkeypatch):
        """预热协程被调度且执行一次"""
        monkeypatch.setenv("STARTUP_READY_DELAY_SECONDS", "0.01")
        ran = asyncio.Event()

        async def coro_factory():
            ran.set()

        loop = asyncio.get_event_loop()
        schedule_startup_run(loop, coro_factory)
        await asyncio.wait_for(ran.wait(), timeout=2)
        assert ran.is_set()


class TestShutdownScheduler:
    """幂等关闭"""

    def test_shutdown_not_running_no_error(self):
        """未运行的调度器关闭不抛异常"""
        scheduler = build_scheduler()
        shutdown_scheduler(scheduler)  # 不应抛错

    @pytest.mark.asyncio
    async def test_shutdown_running(self):
        """运行中的调度器可正常关闭（wait=False 需一轮事件循环后生效）"""
        scheduler = build_scheduler()
        scheduler.start()
        assert scheduler.running
        shutdown_scheduler(scheduler)
        shutdown_scheduler(scheduler)  # 幂等：重复关闭不抛错
        await asyncio.sleep(0)
        assert not scheduler.running
