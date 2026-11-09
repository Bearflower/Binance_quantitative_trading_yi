from unittest.mock import AsyncMock

import pytest

from shared.condition_orders import ensure_table, record_condition_order


@pytest.mark.asyncio
async def test_condition_order_persists_account_with_algo_identity(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "pm-main")
    db = AsyncMock()
    await record_condition_order(db, "btc_eth_aggressive", "SOLUSDT", algo_id=123)
    sql, *args = db.execute.await_args.args
    assert "account_id" in sql
    assert "pm-main" in args
    assert 123 in args


@pytest.mark.asyncio
async def test_ensure_table_upgrades_existing_condition_orders():
    db = AsyncMock()
    await ensure_table(db)
    ddl = "\n".join(call.args[0] for call in db.execute_ddl.await_args_list)
    assert "ADD COLUMN IF NOT EXISTS account_id" in ddl
