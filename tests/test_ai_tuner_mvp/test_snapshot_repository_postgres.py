"""HRS universe 快照仓储的真实 PostgreSQL 集成测试。

现有 `test_snapshot_repository.py` 全部基于 FakeDatabase，SQL 从未真正下发到
PostgreSQL 执行，因此 `store_universe` 中「同一参数先后被推定为 text 与
character varying」导致的
`inconsistent types deduced for parameter $1: text versus character varying`
在生产环境持续失败却未被测试发现（线上 universe 两张表长期 0 行）。

本文件在真实 PostgreSQL 上执行仓储 SQL，专门守护上述参数类型推定回归。
默认跳过，需显式提供专用测试库连接串后运行：

    AI_TUNER_SNAPSHOT_TEST_DSN=postgresql://user:pass@127.0.0.1:5432/db \
        python3 -m pytest tests/test_ai_tuner_mvp/test_snapshot_repository_postgres.py -q

测试会在该库中重建并最终删除 public.ai_tuner_universe_* 两张表，
请务必指向可随意清理的专用测试库，不要指向生产库。
"""

import os
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from ai_tuner.snapshots.repository import SnapshotRepository

TEST_DSN_ENV = "AI_TUNER_SNAPSHOT_TEST_DSN"

# 与 database/postgres/init-scripts/13-ai-tuner-market-snapshots.sql 中
# universe 两张表的列定义保持一致（仅去掉角色与授权语句）。
UNIVERSE_DDL = (
    """
    CREATE TABLE public.ai_tuner_universe_scan_runs (
        strategy_id VARCHAR(64) NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        source_run_id VARCHAR(128) NOT NULL,
        config_hash CHAR(64) NOT NULL,
        symbol_count INTEGER NOT NULL CHECK (symbol_count >= 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (strategy_id, observed_at),
        UNIQUE (strategy_id, source_run_id)
    )
    """,
    """
    CREATE TABLE public.ai_tuner_universe_snapshots (
        strategy_id VARCHAR(64) NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        symbol VARCHAR(32) NOT NULL,
        membership VARCHAR(16) NOT NULL CHECK (membership IN ('short', 'long', 'both')),
        source_run_id VARCHAR(128) NOT NULL,
        config_hash CHAR(64) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (strategy_id, observed_at, symbol)
    )
    """,
)

DROP_DDL = (
    "DROP TABLE IF EXISTS public.ai_tuner_universe_snapshots",
    "DROP TABLE IF EXISTS public.ai_tuner_universe_scan_runs",
)

AS_OF = datetime(2026, 10, 26, 0, 5, tzinfo=timezone.utc)


pytestmark = pytest.mark.skipif(
    not os.getenv(TEST_DSN_ENV),
    reason=f"未设置 {TEST_DSN_ENV}，跳过真实 PostgreSQL 集成测试",
)


class _PoolDatabase:
    """把 asyncpg 连接池适配成 SnapshotRepository 需要的数据库接口。"""

    def __init__(self, pool):
        self.pool = pool

    async def execute_transaction(self, queries):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for query, args in queries:
                    await conn.execute(query, *args)
        return True

    async def fetch_all(self, query, *args):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
        return [dict(row) for row in rows]


@pytest.fixture
async def repository_and_pool():
    pool = await asyncpg.create_pool(dsn=os.environ[TEST_DSN_ENV], min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            for statement in DROP_DDL + UNIVERSE_DDL:
                await conn.execute(statement)
        yield SnapshotRepository(_PoolDatabase(pool)), pool
    finally:
        async with pool.acquire() as conn:
            for statement in DROP_DDL:
                await conn.execute(statement)
        await pool.close()


async def _scan_runs(pool):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_run_id, config_hash, symbol_count"
            " FROM public.ai_tuner_universe_scan_runs"
        )
    return [dict(row) for row in rows]


async def _memberships(pool):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT symbol, membership FROM public.ai_tuner_universe_snapshots ORDER BY symbol"
        )
    return {row["symbol"]: row["membership"] for row in rows}


async def test_store_universe_writes_scan_run_and_memberships(repository_and_pool):
    """回归：真实 PG 下 store_universe 不再报参数类型冲突，且成员归属正确。"""
    repository, pool = repository_and_pool
    await repository.store_universe(
        strategy_id="hrs",
        observed_at=AS_OF,
        short_symbols=["BTCUSDT", "ETHUSDT"],
        long_symbols=["ETHUSDT", "SOLUSDT"],
        source_run_id="hrs:integration",
        config_hash="a" * 64,
    )

    runs = await _scan_runs(pool)
    assert len(runs) == 1
    assert runs[0]["symbol_count"] == 3
    assert runs[0]["config_hash"] == "a" * 64

    assert await _memberships(pool) == {
        "BTCUSDT": "short",
        "ETHUSDT": "both",
        "SOLUSDT": "long",
    }


async def test_store_universe_is_idempotent_for_same_observed_at(repository_and_pool):
    """同一扫描时点重复写入（换 source_run_id）不得重复落成员行。"""
    repository, pool = repository_and_pool
    await repository.store_universe(
        strategy_id="hrs",
        observed_at=AS_OF,
        short_symbols=["BTCUSDT"],
        long_symbols=["BTCUSDT"],
        source_run_id="hrs:first",
        config_hash="b" * 64,
    )
    await repository.store_universe(
        strategy_id="hrs",
        observed_at=AS_OF,
        short_symbols=["ETHUSDT"],
        long_symbols=[],
        source_run_id="hrs:second",
        config_hash="c" * 64,
    )

    assert len(await _scan_runs(pool)) == 1
    assert await _memberships(pool) == {"BTCUSDT": "both"}


async def test_store_universe_empty_scan_records_run_only(repository_and_pool):
    """空候选池也要留下一次扫描记录，但不得写入任何成员行。"""
    repository, pool = repository_and_pool
    await repository.store_universe(
        strategy_id="hrs",
        observed_at=AS_OF,
        short_symbols=[],
        long_symbols=[],
        source_run_id="hrs:empty",
        config_hash="d" * 64,
    )

    runs = await _scan_runs(pool)
    assert len(runs) == 1
    assert runs[0]["symbol_count"] == 0
    assert await _memberships(pool) == {}


async def test_latest_universe_reads_back_written_snapshot(repository_and_pool):
    """写入后 latest_universe 应能读回同一批成员（读写链路一致）。"""
    repository, pool = repository_and_pool
    await repository.store_universe(
        strategy_id="hrs",
        observed_at=AS_OF - timedelta(hours=1),
        short_symbols=["BTCUSDT"],
        long_symbols=["ETHUSDT"],
        source_run_id="hrs:previous",
        config_hash="e" * 64,
    )

    assert await repository.latest_universe("hrs", AS_OF) == {"BTCUSDT", "ETHUSDT"}
