"""数据后台正常关闭和启动失败都应释放自己持有的连接。"""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from shared.binance_api import BinanceClient

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("APP_ROOT", str(ROOT))
    monkeypatch.syspath_prepend(str(ROOT / "dashboard/backend"))
    spec = importlib.util.spec_from_file_location("data_backend_lifecycle", ROOT / "services/data_backend/src/main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("startup_failure", [False, True])
async def test_backend_closes_shared_session_and_database(backend, monkeypatch, startup_failure):
    client = BinanceClient("test-key", "test-secret")
    await client._init_session()
    db = SimpleNamespace(disconnect=AsyncMock())
    data = SimpleNamespace(_binance_client=client, _db_manager=db)
    scheduler = backend.build_scheduler()
    monkeypatch.setattr(backend, "DataService", lambda: data)
    monkeypatch.setattr(backend, "build_scheduler", lambda: scheduler)
    monkeypatch.setattr(backend, "_register_jobs", Mock(side_effect=ValueError("注册失败") if startup_failure else None))
    monkeypatch.setattr(backend, "_schedule_startup_warmup", Mock(return_value=[]))
    backend._stop.set()
    try:
        if startup_failure:
            with pytest.raises(ValueError, match="注册失败"):
                await backend.main()
        else:
            await backend.main()
        assert client.session.closed
        db.disconnect.assert_awaited_once()
    finally:
        await client.close()


@pytest.mark.parametrize("scheduled", [False, True])
async def test_backend_joins_active_job_before_closing_client(backend, monkeypatch, scheduled):
    monkeypatch.setenv("STARTUP_READY_DELAY_SECONDS", "0")
    monkeypatch.setattr(backend, "load_circuit_breaker_config", lambda: {"enabled": False})
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelling = asyncio.Event()
    workers = set()

    async def job(*args):
        workers.add(asyncio.current_task())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await release.wait()

    for name in ("run_precompute", "run_reconcile", "run_snapshot", "run_performance_metrics"):
        monkeypatch.setattr(backend, name, job)
    client = BinanceClient("test-key", "test-secret")
    await client._init_session()
    db = SimpleNamespace(disconnect=AsyncMock())
    data = SimpleNamespace(_binance_client=client, _db_manager=db, compute_and_store_positions=job)
    monkeypatch.setattr(backend, "DataService", lambda: data)
    if scheduled:
        monkeypatch.setattr(backend, "_register_jobs", lambda scheduler, data: scheduler.add_job(job, "date"))
        monkeypatch.setattr(backend, "_schedule_startup_warmup", lambda *args: [])
    else:
        monkeypatch.setattr(backend, "_register_jobs", lambda *args: None)
    serving = asyncio.create_task(backend.main())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        backend._stop.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not client.session.closed, "任务取消收尾时不能提前关闭共享会话"
        await asyncio.wait_for(cancelling.wait(), 2)
        assert not serving.done()
        release.set()
        await serving
        assert all(worker.done() for worker in workers)
        assert client.session.closed
    finally:
        release.set()
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        backend._stop.set()
        await serving
        await client.close()


async def test_backend_cancels_warmups_that_are_still_waiting(backend, monkeypatch):
    monkeypatch.setenv("STARTUP_READY_DELAY_SECONDS", "3600")
    monkeypatch.setattr(backend, "load_circuit_breaker_config", lambda: {"enabled": False})
    warmups = []
    original = backend._schedule_startup_warmup
    data = SimpleNamespace(_binance_client=None, _db_manager=None)
    monkeypatch.setattr(backend, "DataService", lambda: data)
    monkeypatch.setattr(backend, "_register_jobs", lambda *args: None)

    def schedule(loop, service):
        warmups.extend(original(loop, service))
        return warmups

    monkeypatch.setattr(backend, "_schedule_startup_warmup", schedule)
    backend._stop.set()
    await backend.main()
    assert len(warmups) == 5
    assert all(task.done() and task.cancelled() for task in warmups)
