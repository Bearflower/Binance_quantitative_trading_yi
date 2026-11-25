import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.accounting.runtime_cutover import initialize_classic_runtime, require_flat, require_runtime_marker, stop_journal_path


def client(pm=False):
    return SimpleNamespace(
        use_unified_account=pm, testnet=False,
        get_position=AsyncMock(return_value=[{"symbol":"ETHUSDT","positionSide":"BOTH","positionAmt":"0"}]),
        get_um_account_positions=AsyncMock(return_value=[]),
        get_open_orders=AsyncMock(return_value=[]), get_open_algo_orders=AsyncMock(return_value=[]),
    )


def test_classic_stop_journal_cannot_reopen_pm_or_another_account(tmp_path, monkeypatch):
    path = tmp_path / "aggressive_stop_intents.jsonl"
    path.write_text("old pending")
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "2000000000000")
    assert stop_journal_path(client(True), path) == path
    one = stop_journal_path(client(), path)
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "other-main")
    assert one != path and one != stop_journal_path(client(), path)
    assert path.read_text() == "old pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,result", [
    ("get_position", []), ("get_position", [{"symbol":"ETHUSDT","positionSide":"BOTH","positionAmt":"NaN"}]),
    ("get_um_account_positions", [{"symbol":"ETHUSDT","positionSide":"BOTH","positionAmt":"1"}]),
    ("get_open_orders", [{"orderId":1}]), ("get_open_algo_orders", [{"algoId":2}]),
])
async def test_partial_or_nonflat_inventory_cannot_retire_state(method, result):
    api = client()
    getattr(api, method).return_value = result
    with pytest.raises(RuntimeError):
        await require_flat(api)


@pytest.mark.asyncio
async def test_real_postgres_first_start_archives_three_strategies_once(monkeypatch):
    import asyncpg
    from urllib.parse import urlsplit
    dsn = os.getenv("CLASSIC_RUNTIME_TEST_DSN")
    if not dsn:
        pytest.skip("设置 CLASSIC_RUNTIME_TEST_DSN 以运行独立本地数据库集成测试")
    url = urlsplit(dsn)
    assert url.hostname in ("127.0.0.1", "localhost") and url.path == "/classic_runtime_tests"
    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=4)
    db = SimpleNamespace(pool=pool, fetch_one=pool.fetchrow, fetch_all=pool.fetch, execute=pool.execute)
    api = client()
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "2000000000000")
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE SCHEMA IF NOT EXISTS trading; CREATE SCHEMA IF NOT EXISTS new_coin;
                CREATE TABLE public.strategy_states(strategy_name text,state_key text,state_data jsonb,
                    updated_at timestamp default now(), UNIQUE(strategy_name,state_key));
                CREATE TABLE trading.account_mode_equity(account_id text,cutover_ms bigint,snapshot_date date,
                    observation text,evidence_ref text,exchange_uid_sha256 text,open_positions integer);
                CREATE TABLE trading.trade_records(account_id text,strategy text,executed_at timestamp,order_id text,algo_id text);
                CREATE TABLE trading.strategy_open_positions(strategy_id text,symbol text);
                CREATE TABLE public.condition_orders(strategy_name text,status text,account_id text,
                    algo_id bigint,created_at timestamp default now(),updated_at timestamp);
                CREATE SCHEMA btc_eth; CREATE TABLE btc_eth.condition_orders (LIKE public.condition_orders INCLUDING ALL);
                CREATE TABLE new_coin.short_positions(id serial,status text,opened_at timestamp default now(),metadata jsonb);
                CREATE TABLE trading.aggressive_stop_bindings(account_id text,strategy text,symbol text,
                    episode_entry_order_id bigint,algo_id bigint,stop_side text,stop_quantity numeric,
                    last_observed_status text,terminal_observations integer,audit_state text,bound_at timestamptz DEFAULT now(),
                    next_audit_at timestamptz DEFAULT now(),UNIQUE(account_id,symbol,algo_id));
                INSERT INTO trading.aggressive_stop_bindings(account_id,strategy,symbol,episode_entry_order_id,
                    algo_id,stop_side,stop_quantity,audit_state)
                VALUES('main','btc_eth_aggressive','ETHUSDT',5,17,'SELL',1,'pending');
                INSERT INTO public.strategy_states VALUES
                    ('btc_eth','main','{"positions":{"ETHUSDT":{"order":17}}}',now()),
                    ('btc_eth_aggressive','position:ETHUSDT','{"positions":{"ETHUSDT":{"order":17}}}',now()),
                    ('new_coin','main','{"positions":{"ETHUSDT":{"algo":17}},"daily_trade_count":4,"peak_pnl":"7"}',now()),
                    ('hrs','main','{"positions":{"PENDING":1}}',now());
                INSERT INTO trading.strategy_open_positions VALUES('btc_eth','ETHUSDT'),('hrs','ETHUSDT');
                INSERT INTO new_coin.short_positions(status,metadata) VALUES('open','{"entry_order_id":17}');
                INSERT INTO public.condition_orders(strategy_name,status,account_id,algo_id) VALUES
                    ('new_coin','OPEN','main',17),('new_coin','OPEN','different',17),('hrs','OPEN','main',17),
                    ('new_coin','OPEN',NULL,17);
                INSERT INTO btc_eth.condition_orders SELECT * FROM public.condition_orders;
                INSERT INTO trading.account_mode_equity VALUES
                    ('main',2000000000000,NULL,'pm_final','pm-evidence','uid',0),
                    ('main',2000000000000,NULL,'classic_open','classic-evidence','uid',0);
            """)
        with pytest.raises(RuntimeError, match="运行状态"):
            await require_runtime_marker(api, db)
        # 同一切点并发执行准备命令，持仓与订单状态只退役一次。
        await asyncio.gather(*(initialize_classic_runtime(api, db) for _ in range(3)))
        marker = json.loads(await pool.fetchval("SELECT state_data FROM public.strategy_states WHERE strategy_name='account_mode_runtime'"))
        assert len(marker["archive"]["states"]) == 3
        assert marker["archive"]["short_positions"][0]["metadata"]["entry_order_id"] == 17
        assert {r["strategy_name"] for r in marker["archive"]["condition_orders"]} == {"new_coin", "hrs"}
        assert {r["strategy_name"] for r in marker["archive"]["strategy_condition_orders"]} == {"new_coin", "hrs"}
        new = json.loads(await pool.fetchval("SELECT state_data FROM public.strategy_states WHERE strategy_name='new_coin'"))
        assert new == {"positions": {}, "daily_trade_count":4, "peak_pnl":"7"}
        assert await pool.fetchval("SELECT count(*) FROM public.strategy_states WHERE state_key LIKE 'position:%'") == 0
        assert await pool.fetchval("SELECT count(*) FROM trading.strategy_open_positions WHERE strategy_id='hrs'") == 1
        assert await pool.fetchval("SELECT count(*) FROM public.condition_orders WHERE status='OPEN'") == 2
        assert await pool.fetchval("SELECT count(*) FROM btc_eth.condition_orders WHERE status='OPEN'") == 2
        assert await pool.fetchval("SELECT status FROM new_coin.short_positions") == 'closed'
        await pool.execute("INSERT INTO trading.strategy_open_positions VALUES('btc_eth','NEWUSDT')")
        api.get_open_algo_orders.return_value = [{"algoId":17}]
        # 第二套策略的启动检查只读标记，已经建仓不要求重新证明空仓。
        await require_runtime_marker(api, db)
        await initialize_classic_runtime(api, db)
        assert await pool.fetchval("SELECT count(*) FROM trading.strategy_open_positions WHERE strategy_id='btc_eth'") == 1
        from shared.accounting.stop_binding import record_stop_binding
        from shared.accounting.stop_exit_audit import audit_due_stop_bindings, unresolved_stop_binding_exists
        owner = ("main", "btc_eth_aggressive")
        assert not await unresolved_stop_binding_exists(db, *owner, "ETHUSDT", since_ms=2000000000000)
        assert await unresolved_stop_binding_exists(db, *owner, "ETHUSDT")
        assert not any((await audit_due_stop_bindings(db, api, *owner)).values())
        assert not await record_stop_binding(db, account_id=owner[0], strategy=owner[1], symbol="ETHUSDT",
                                            episode_entry_order_id=5, algo_id=17, stop_side="SELL", stop_quantity=1,
                                            since_ms=2000000000000)
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("risk,wallet,ordinary,algo", [
    ({}, dict(totalMarginBalance="1", positions=[]), [], []),
    ([], dict(totalMarginBalance="1"), [], []),
    ([], dict(totalMarginBalance="NaN", positions=[]), [], []),
    ([], dict(totalMarginBalance="0", positions=[]), [], []),
    ([], dict(totalMarginBalance="1", positions=[dict(symbol="A", positionSide="BOTH", positionAmt="2")]), [], []),
    ([dict(symbol="A", positionSide="LONG", positionAmt="0")], dict(totalMarginBalance="1", positions=[]), [], []),
    ([], dict(totalMarginBalance="1", positions=[]), None, []),
    ([], dict(totalMarginBalance="1", positions=[]), [], [dict(algoId=8)]),
])
async def test_unfreeze_refuses_incomplete_or_exposed_v3_signed_snapshots(risk, wallet, ordinary, algo):
    from shared.accounting.runtime_cutover import require_signed_classic_flat

    api = client()
    api._request = AsyncMock(return_value=risk)
    api.get_account_info = AsyncMock(return_value=wallet)
    api.get_open_orders.return_value = ordinary
    api.get_open_algo_orders.return_value = algo
    with pytest.raises((ValueError, RuntimeError)):
        await require_signed_classic_flat(api)
    api._request.assert_awaited_once_with("GET", "/fapi/v3/positionRisk", signed=True)
