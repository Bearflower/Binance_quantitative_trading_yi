"""HRS universe 快照仓储的真实 PostgreSQL 集成测试。

现有 `test_snapshot_repository.py` 全部基于 FakeDatabase，SQL 从未真正下发到
PostgreSQL 执行，因此 `store_universe` 中「同一参数先后被推定为 text 与
character varying」导致的
`inconsistent types deduced for parameter $1: text versus character varying`
在生产环境持续失败却未被测试发现（线上 universe 两张表长期 0 行）。

本文件在真实 PostgreSQL 上执行仓储 SQL，专门守护上述参数类型推定回归。
默认跳过，需显式提供专用测试库连接串后运行：

    AI_TUNER_SNAPSHOT_TEST_DSN=postgresql://user:pass@127.0.0.1:5432/snapshot_test AI_TUNER_SNAPSHOT_TEST_DB_ACK=disposable \
        python3 -m pytest tests/test_ai_tuner_mvp/test_snapshot_repository_postgres.py -q

测试会在该库中重建并最终删除 public.ai_tuner_universe_* 两张表，
请务必指向可随意清理的专用测试库，不要指向生产库。
"""

import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
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
    parsed = urlsplit(os.environ[TEST_DSN_ENV])
    if (parsed.scheme not in {"postgresql", "postgres"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not parsed.path.endswith("_test") or parsed.query
            or os.getenv("AI_TUNER_SNAPSHOT_TEST_DB_ACK") != "disposable"):
        pytest.fail("snapshot tests require a disposable local *_test database and explicit ACK")
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

    with pytest.raises(ValueError, match="missing or incomplete"):
        await repository.latest_universe("hrs", AS_OF)
    assert await repository.latest_universe("hrs", datetime.now(timezone.utc)) == {"BTCUSDT", "ETHUSDT"}


@asynccontextmanager
async def _isolated_connection(pool):
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            yield conn
        finally:
            await tx.rollback()


async def test_real_auditor_prepares_hour_buckets(repository_and_pool):
    """Use real PostgreSQL overload resolution and a known one-day denominator."""
    from pathlib import Path
    from ai_tuner.readiness.auditor import ReadinessAuditor

    repository, pool = repository_and_pool
    ddl = (Path(__file__).resolve().parents[2] / "database/postgres/init-scripts/13-ai-tuner-market-snapshots.sql").read_text()
    table_sql = next(stmt for stmt in ddl.split(";") if "CREATE TABLE IF NOT EXISTS public.ai_tuner_market_snapshots" in stmt)
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    await repository.store_universe("hrs", start, ["BTCUSDT"], [], "audit-run", "a" * 64)
    async with _isolated_connection(pool) as conn:
        await conn.execute(table_sql)
        class Query:
            fetch_one = conn.fetchrow

        auditor = ReadinessAuditor.__new__(ReadinessAuditor)
        auditor.db = Query()
        result = await auditor._query_external_snapshot("hrs", start, end)
        assert result.expected == 24
        assert result.complete == 0
        assert result.gaps == []

        # Partial or wrong-identity members cannot shrink the denominator and
        # turn a genuinely incomplete run into a qualified universe.
        await conn.execute("UPDATE public.ai_tuner_universe_scan_runs SET symbol_count = 2")
        partial = await auditor._query_external_snapshot("hrs", start, end)
        assert partial.coverage == 0 and partial.gaps == ["historical_universe_missing_members"]
        await conn.execute("UPDATE public.ai_tuner_universe_scan_runs SET symbol_count = 1")
        await conn.execute(
            """INSERT INTO public.ai_tuner_market_snapshots (
                   strategy_id, symbol, observed_at, bucket_start, open_interest_usd,
                   funding_rate, market_cap_usd, oi_market_cap_ratio, sentiment_score,
                   sentiment_formula_version, oi_source, funding_source, market_cap_source,
                   source_observed_at, collected_at, payload_hash, raw_payload)
               VALUES ('hrs','BTCUSDT',$1,$1,10,0,100,0.1,0,'funding_annual_pct_v1',
                       'binance','binance','coingecko',$1,$2,repeat('a',64),'{}')""",
            start, start + timedelta(hours=1),
        )
        late = await auditor._query_external_snapshot("hrs", start, end)
        assert late.expected == 24 and late.complete == 0
        await conn.execute(
            "UPDATE public.ai_tuner_market_snapshots SET collected_at = $1",
            start + timedelta(minutes=59),
        )
        timely = await auditor._query_external_snapshot("hrs", start, end)
        assert timely.complete == 1


async def test_closed_trade_count_requires_unambiguous_order_evidence(repository_and_pool, monkeypatch):
    from pathlib import Path
    from ai_tuner.readiness.auditor import ReadinessAuditor

    _, pool = repository_and_pool
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "default")
    ddl = (Path(__file__).resolve().parents[2] / "database/postgres/init-scripts/03-create-trade-records.sql").read_text()
    table_sql = next(stmt for stmt in ddl.split(";") if "CREATE TABLE IF NOT EXISTS trading.trade_records" in stmt)
    async with _isolated_connection(pool) as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS trading")
        await conn.execute(table_sql)
        await conn.execute(
            """INSERT INTO trading.trade_records (
                   account_id, strategy, symbol, order_id, side, order_type, quantity,
                   price, status, executed_at, realized_pnl, pnl_source)
               SELECT 'default','hrs','BTCUSDT',proof.id,'SELL','MARKET',1,10,
                      'FILLED',TIMESTAMP '2026-10-26 08:00:00',1,proof.source
               FROM (VALUES ('dup','exchange_fill'),('dup','exchange_fill'),
                            ('one','exchange_fill'),('entry',NULL),('  ','exchange_fill'))
                    AS proof(id,source)"""
        )

        class Query:
            fetch_one = conn.fetchrow

        auditor = ReadinessAuditor.__new__(ReadinessAuditor)
        auditor.db = Query()
        start = datetime(2026, 10, 26, tzinfo=timezone.utc)
        assert await auditor._query_trade_records("hrs", start, start + timedelta(hours=1)) == (1, 1, 0)


async def test_aggressive_signal_requires_matching_account_fill(repository_and_pool):
    from ai_tuner.monitor.reduced_trial import _AGGRESSIVE_SIGNALS_SQL

    _, pool = repository_and_pool
    async with _isolated_connection(pool) as conn:
        await conn.execute("""
            CREATE SCHEMA btc_eth_aggressive;
            CREATE SCHEMA trading;
            CREATE TABLE btc_eth_aggressive.trade_signals
                (symbol text, direction text, metadata jsonb, status text, created_at timestamp);
            CREATE TABLE trading.trade_records
                (symbol text, strategy text, account_id text, order_id text, order_type text,
                 status text, quantity numeric, price numeric, side text);
            CREATE TABLE trading.execution_fact_fill
                (account_id text, symbol text, order_id bigint, side text, qty numeric, trade_time_utc timestamptz);
            INSERT INTO btc_eth_aggressive.trade_signals
            SELECT 'BTCUSDT', 'LONG', jsonb_build_object('entry_order_id', id), 'executed',
                   timestamp '2026-11-01' FROM generate_series(1,6) AS id;
            INSERT INTO trading.trade_records VALUES
                ('BTCUSDT','btc_eth_aggressive','mine','1','LIMIT','FILLED',1,100,'BUY'),
                ('BTCUSDT','btc_eth_aggressive','other','2','MARKET','FILLED',1,100,'BUY'),
                ('ETHUSDT','btc_eth_aggressive','mine','3','MARKET','FILLED',1,100,'BUY'),
                ('BTCUSDT','btc_eth_aggressive','mine','4','MARKET','FILLED',1,100,'BUY'),
                ('BTCUSDT','btc_eth_aggressive','mine','5','MARKET','FILLED',1,100,'BUY'),
                ('BTCUSDT','btc_eth_aggressive','mine','6','MARKET','FILLED',1,100,'BUY');
            INSERT INTO trading.execution_fact_fill VALUES
                ('mine','BTCUSDT',1,'BUY',1,'2026-11-01 00:00+00'),
                ('mine','BTCUSDT',2,'BUY',1,'2026-11-01 00:00+00'),
                ('mine','BTCUSDT',3,'BUY',1,'2026-11-01 00:00+00'),
                ('mine','BTCUSDT',4,'SELL',1,'2026-11-01 00:00+00'),
                ('mine','BTCUSDT',5,'BUY',1,'2026-11-03 00:00+00')
        """)
        counts = await conn.fetchrow(
            _AGGRESSIVE_SIGNALS_SQL, datetime(2026, 11, 1), datetime(2026, 11, 2), "mine"
        )
        assert counts["executed_signals"] == 6
        assert counts["executed_without_order_id"] == 5


async def test_weekly_claim_blocks_duplicate_and_interrupted_publication(repository_and_pool):
    from ai_tuner.memory.db_handler import MemoryDBHandler

    _, pool = repository_and_pool
    async with _isolated_connection(pool) as conn:
        class LocalDB:
            execute_ddl = conn.execute
            execute = conn.execute
            fetch_one = conn.fetchrow

        await conn.execute("CREATE SCHEMA weekly_claim_test")
        memory = MemoryDBHandler(LocalDB())
        await memory.ensure_table_exists("weekly_claim_test")
        week = datetime(2026, 11, 9).date()
        assert await memory.claim_week("grid", week, False)
        assert not await memory.claim_week("grid", week, True)
        await memory.finish_week("grid", week, "error")
        assert await memory.claim_week("grid", week, False)
        await memory.finish_week("grid", week, "success")
        assert not await memory.claim_week("grid", week, False)
        assert await memory.claim_week("grid", week, True)

        pending = await conn.fetchval(
            "INSERT INTO weekly_claim_test.strategy_memory (strategy_id) VALUES ('grid') RETURNING id"
        )
        assert await memory.claim_approval(pending, 48)
        assert not await memory.claim_approval(pending, 48)
        assert not await memory.mark_rejected(pending)
        await memory.mark_published(pending, "V20261113", "review")
        await memory.acknowledge_version("grid", "V20261113")
        assert await conn.fetchval("SELECT is_applied FROM weekly_claim_test.strategy_memory WHERE id=$1", pending)
