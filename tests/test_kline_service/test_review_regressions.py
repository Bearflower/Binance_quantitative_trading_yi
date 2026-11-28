"""全量审查发现的续期与数据库互斥锁回归。"""
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.kline_service.models.registered_symbol import RegisteredSymbolConfig
from services.kline_service.core.registry import SymbolRegistry


def test_renewal_keeps_per_request_duration_within_schema():
    config = RegisteredSymbolConfig.create("TESTUSDT", ["1h"], 10)
    before = config.expires_at
    config.renew(30)
    assert config.duration_days == 30
    assert config.expires_at == before + timedelta(days=30)
    config.renew(30)
    assert config.duration_days == 30
    assert config.expires_at == before + timedelta(days=60)
    RegisteredSymbolConfig(**config.model_dump())


@pytest.mark.parametrize("days", [0, -1, 31])
def test_invalid_renewal_does_not_mutate_config(days):
    config = RegisteredSymbolConfig.create("TESTUSDT", ["1h"], 10)
    before = config.model_dump()
    with pytest.raises(ValueError):
        config.renew(days)
    assert config.model_dump() == before


@pytest.mark.asyncio
async def test_failed_renewal_keeps_cached_expiry():
    registry = SymbolRegistry()
    config = RegisteredSymbolConfig.create("TESTUSDT", ["1h"], 10)
    before = config.model_dump()
    registry._cache = {config.symbol: config}
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("写入失败"))

    @asynccontextmanager
    async def connection():
        yield conn

    with patch("services.kline_service.core.registry.db_manager") as database:
        database.get_connection = connection
        with pytest.raises(RuntimeError, match="写入失败"):
            await registry.renew(config.symbol, 30)
    assert registry._cache[config.symbol].model_dump() == before


@pytest.fixture
def main_module(monkeypatch):
    import shared.core.config as config
    monkeypatch.setattr(config.settings, "APP_VERSION", "test")
    monkeypatch.setattr(config.settings, "API_PREFIX", "/api")
    from services.kline_service.src import main
    return main


@pytest.mark.asyncio
async def test_startup_lock_keeps_dedicated_connection(main_module):
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=True)
    conn.close = AsyncMock()
    pooled = MagicMock()
    pooled.fetch_val = AsyncMock(return_value=True)

    @asynccontextmanager
    async def pool_connection():
        yield pooled

    with patch.object(main_module, "db_manager") as database, patch("asyncpg.connect", AsyncMock(return_value=conn)):
        database.database.url = "postgresql://unused/test"
        database.get_connection = pool_connection
        acquired = await main_module._acquire_startup_lock()
    assert acquired is conn
    conn.close.assert_not_awaited()
    pooled.fetch_val.assert_not_awaited()


@pytest.mark.asyncio
async def test_contended_lock_closes_only_failed_connection(main_module):
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=False)
    conn.close = AsyncMock()
    with patch.object(main_module, "db_manager") as database, patch("asyncpg.connect", AsyncMock(return_value=conn)):
        database.database.url = "postgresql://unused/test"
        with pytest.raises(RuntimeError, match="另一个 kline"):
            await main_module._acquire_startup_lock()
    conn.close.assert_awaited_once()
    conn.add_termination_listener.assert_not_called()


@pytest.mark.asyncio
async def test_initialization_failure_releases_lock_and_pool(main_module):
    conn = MagicMock()
    conn.close = AsyncMock()
    database = MagicMock(connect=AsyncMock(), disconnect=AsyncMock())
    registry = MagicMock(initialize=AsyncMock(side_effect=RuntimeError("初始化失败")))
    with patch.object(main_module, "db_manager", database), \
            patch.object(main_module, "registry", registry), \
            patch.object(main_module, "_verify_database_identity", AsyncMock()), \
            patch.object(main_module, "_acquire_startup_lock", AsyncMock(return_value=conn)):
        with pytest.raises(RuntimeError, match="初始化失败"):
            async with main_module.lifespan(main_module.app):
                pytest.fail("初始化失败时不能开始服务")
    conn.remove_termination_listener.assert_called_once_with(main_module._on_startup_lock_lost)
    conn.close.assert_awaited_once()
    database.disconnect.assert_awaited_once()


def test_unexpected_lock_loss_stops_scheduler_and_process(main_module):
    scheduler = MagicMock()
    with patch.object(main_module, "scheduler", scheduler), patch.object(main_module.os, "kill") as kill:
        main_module._on_startup_lock_lost(MagicMock())
    scheduler.shutdown.assert_called_once_with(wait=False)
    kill.assert_called_once_with(main_module.os.getpid(), main_module.signal.SIGTERM)
