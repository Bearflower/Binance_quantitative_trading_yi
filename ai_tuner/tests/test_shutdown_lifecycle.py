"""验证应用所有者在退出与启动失败时释放共享会话。"""
import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ai_tuner import main as module
from shared.binance_api import BinanceClient


async def test_shutdown_closes_shared_binance_session_once():
    app = module.StratTuneAI()
    client = BinanceClient("test-key", "test-secret")
    await client._init_session()
    app.binance_client = client
    app.monthly_job = SimpleNamespace(binance_client=client)
    app.orphan_cleanup_job = SimpleNamespace(binance_client=client)
    original_close = client.close
    client.close = AsyncMock(wraps=original_close)
    try:
        await app.shutdown()
        await app.shutdown()
        assert client.session.closed
        client.close.assert_awaited_once()
    finally:
        await original_close()


async def test_startup_failure_retains_client_for_cleanup(monkeypatch):
    app = module.StratTuneAI()
    client = BinanceClient("test-key", "test-secret")
    await client._init_session()
    db = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
    notification = Mock(close=AsyncMock())
    monkeypatch.setenv("BINANCE_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test-secret")
    monkeypatch.setattr(app, "load_config", lambda: {})
    monkeypatch.setattr(module, "DatabaseManager", lambda **kwargs: db)
    monkeypatch.setattr(module, "NotificationClient", lambda **kwargs: notification)
    monkeypatch.setattr(module, "MemoryDBHandler", lambda *args: SimpleNamespace(ensure_table_exists=AsyncMock()))
    monkeypatch.setattr(module, "LLMClient", lambda *args: Mock(client=Mock(close=AsyncMock())))
    monkeypatch.setattr(module, "WeeklyTuningJob", Mock())
    monkeypatch.setattr(module, "BinanceClient", lambda **kwargs: client)
    monkeypatch.setattr(module, "MonthlyAllocationJob", Mock(side_effect=ValueError("初始化失败")))
    try:
        with pytest.raises(ValueError, match="初始化失败"):
            await app.initialize()
        await app.shutdown()
        assert client.session.closed
        db.disconnect.assert_awaited_once()
    finally:
        await client.close()


async def test_shutdown_before_scheduler_start_still_closes_resources():
    app = module.StratTuneAI()
    app.scheduler = module.AsyncIOScheduler()
    app.notification_client = SimpleNamespace(close=AsyncMock())
    await app.shutdown()
    app.notification_client.close.assert_awaited_once()


async def test_main_waits_for_signal_cleanup(monkeypatch):
    app = module.StratTuneAI()
    closing = asyncio.Event()
    release = asyncio.Event()
    handlers = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: handlers.setdefault(sig, callback))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: True)
    monkeypatch.setattr(module, "StratTuneAI", lambda: app)
    app.initialize = AsyncMock()

    async def start():
        app._running = True
        try:
            while app._running:
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass

    async def shutdown():
        app._running = False
        closing.set()
        await release.wait()

    app.start = start
    app.shutdown = shutdown
    task = asyncio.create_task(module.main())
    try:
        await asyncio.sleep(0)
        handlers[signal.SIGTERM]()
        await closing.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done(), "主协程必须等待清理结束后再退出"
    finally:
        release.set()
        await task
        await asyncio.sleep(0)


async def test_shutdown_joins_running_scheduler_job_before_closing_client():
    app = module.StratTuneAI()
    app.scheduler = module.AsyncIOScheduler()
    client = BinanceClient("test-key", "test-secret")
    await client._init_session()
    app.binance_client = client
    entered = asyncio.Event()
    cancelling = asyncio.Event()
    release = asyncio.Event()

    async def job():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await release.wait()

    app.scheduler.add_job(job, "date")
    serving = asyncio.create_task(app.start())
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        closing = asyncio.create_task(app.shutdown())
        await asyncio.wait_for(cancelling.wait(), 2)
        assert not client.session.closed, "任务取消收尾时不能提前关闭共享会话"
        assert not closing.done()
    finally:
        release.set()
        if closing is not None:
            await closing
        serving.cancel()
        await serving
        await client.close()


async def test_shutdown_joins_job_submitted_before_wrapper_has_started():
    from datetime import datetime, timedelta, timezone

    app = module.StratTuneAI()
    app.scheduler = module.AsyncIOScheduler()
    client = BinanceClient("test-key", "test-secret")
    await client._init_session()
    app.binance_client = client
    cancelled = asyncio.Event()
    release = asyncio.Event()
    observed = []

    async def job():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            await release.wait()

    job_handle = app.scheduler.add_job(job, "date", run_date=datetime.now(timezone.utc) + timedelta(days=1))
    serving = asyncio.create_task(app.start())
    await asyncio.sleep(0)
    # 强制提交一个真实执行器任务，尚不让事件循环开始执行包装函数。
    job_handle.modify(next_run_time=datetime.now(timezone.utc))
    app.scheduler._process_jobs()

    async def observe_cleanup():
        await cancelled.wait()
        observed.append(client.session.closed)
        release.set()

    observer = asyncio.create_task(observe_cleanup())
    try:
        await app.shutdown()
        await observer
        assert observed == [False], "已提交但尚未开始的任务也必须先取消收尾"
    finally:
        release.set()
        serving.cancel()
        await serving
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        await client.close()
