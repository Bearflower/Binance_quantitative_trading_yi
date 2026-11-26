"""在隔离的本地 PostgreSQL 运行真实归属和终态 SQL。"""
from datetime import datetime, timedelta

import asyncpg
import pytest

import shared.position_ownership as ownership
from test_policy_postgres import DSN


@pytest.mark.skipif(not DSN, reason="requires the isolated local risk_tests database")
@pytest.mark.asyncio
async def test_classic_owner_and_close_are_scoped_by_account_epoch_and_conflicts(monkeypatch):
    monkeypatch.setenv("BINANCE_ACCOUNT_MODE", "classic")
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "default")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "1000")
    monkeypatch.setattr(ownership, "_TRADE_RECORDS_TABLE", "pg_temp.scope_orders")
    account, since = ownership.entry_scope_args()
    assert since == datetime(1970, 1, 1, 8, 0, 1)
    db = await asyncpg.connect(DSN)
    try:
        await db.execute("""CREATE TEMP TABLE scope_orders(id serial,symbol text,strategy text,
                         account_id text,order_type text,realized_pnl numeric,status text,executed_at timestamp)""")

        async def insert(owner, key, when):
            await db.execute("INSERT INTO scope_orders(symbol,strategy,account_id,order_type,status,executed_at) "
                             "VALUES ('BTCUSDT',$1,$2,'LIMIT','FILLED',$3)", owner, key, when)

        async def owner():
            return await db.fetchval(ownership._open_entry_sql(True), "BTCUSDT", account, since)

        await insert("btc_eth", None, since)
        await insert("btc_eth", account, since - timedelta(milliseconds=1))
        await insert("grid", "sub", since)
        assert await owner() is None
        await insert("btc_eth", account, since)
        assert await owner() == "btc_eth"
        await insert("hrs", account, since)
        conflict = await owner()
        assert ownership._is_owner_blocking(conflict, "btc_eth", ["btc_eth_aggressive"])
        assert ownership._is_owner_blocking(conflict, "hrs", ["btc_eth"])
        result = await db.execute(ownership._mark_entry_terminal_sql("CLOSED"), "BTCUSDT", "btc_eth", account, since)
        assert result == "UPDATE 1"
        assert await owner() == "hrs"
        assert await db.fetchval("SELECT count(*) FROM scope_orders WHERE status='FILLED'") == 4
    finally:
        await db.close()
