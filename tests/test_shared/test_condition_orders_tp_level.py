"""TP 等级必须随条件单落库，并由启动建表兼容旧 schema。"""
from unittest.mock import AsyncMock

import pytest

from shared import condition_orders


@pytest.mark.asyncio
async def test_ensure_table_adds_tp_level_to_existing_table():
    db = AsyncMock()
    await condition_orders.ensure_table(db)
    ddl = [call.args[0] for call in db.execute_ddl.await_args_list]
    assert any('ADD COLUMN IF NOT EXISTS tp_level' in statement for statement in ddl)
    assert next(i for i, statement in enumerate(ddl)
                if 'ADD COLUMN IF NOT EXISTS tp_level' in statement) == 1


@pytest.mark.asyncio
async def test_record_tp2_persists_level_with_algo_id():
    db = AsyncMock()
    await condition_orders.record_condition_order(
        db, 'btc_eth', 'BTCUSDT', algo_id=900,
        order_type='TAKE_PROFIT', tp_level=2,
    )
    sql, *args = db.execute.await_args.args
    assert 'tp_level' in sql
    assert args == ['btc_eth', 'BTCUSDT', 900, 'TAKE_PROFIT', 2]


@pytest.mark.asyncio
async def test_legacy_tp_remains_unlabelled():
    db = AsyncMock()
    await condition_orders.record_condition_order(
        db, 'btc_eth', 'BTCUSDT', algo_id=900,
        order_type='TAKE_PROFIT',
    )
    assert db.execute.await_args.args[-1] is None
