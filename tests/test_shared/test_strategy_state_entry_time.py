import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from shared.strategy_state import load_matching_position_entry_time, touch_strategy_state


@pytest.mark.asyncio
async def test_restores_entry_time_only_for_same_tp2_order():
    entry_time = datetime.now() - timedelta(hours=8)
    db = AsyncMock()
    db.fetch_one.return_value = {'state_data': json.dumps({'positions': {
        'SOLUSDT': {
            'entry_time': entry_time.isoformat(),
            'direction': 'SHORT',
            'tp2_order_id': 900,
        },
    }})}

    restored = await load_matching_position_entry_time(
        db, 'btc_eth_aggressive', 'SOLUSDT', 'SHORT', 900,
    )

    assert restored == entry_time
    db.fetch_one.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('position', [
    {'entry_time': '2026-10-28T00:00:00', 'direction': 'SHORT', 'tp2_order_id': 901},
    {'entry_time': '2026-10-28T00:00:00', 'direction': 'LONG', 'tp2_order_id': 900},
    {'entry_time': 'invalid', 'direction': 'SHORT', 'tp2_order_id': 900},
    {'entry_time': (datetime.now() + timedelta(hours=1)).isoformat(),
     'direction': 'SHORT', 'tp2_order_id': 900},
])
async def test_rejects_stale_or_unusable_snapshot(position):
    db = AsyncMock()
    db.fetch_one.return_value = {'state_data': {'positions': {'SOLUSDT': position}}}
    assert await load_matching_position_entry_time(
        db, 'btc_eth_aggressive', 'SOLUSDT', 'SHORT', 900,
    ) is None


@pytest.mark.asyncio
async def test_missing_snapshot_returns_none():
    db = AsyncMock()
    db.fetch_one.return_value = None
    assert await load_matching_position_entry_time(
        db, 'btc_eth_aggressive', 'SOLUSDT', 'SHORT', 900,
    ) is None


@pytest.mark.asyncio
async def test_aware_entry_time_uses_process_local_clock():
    aware_entry = datetime.now().astimezone() - timedelta(hours=8)
    db = AsyncMock()
    db.fetch_one.return_value = {'state_data': {'positions': {'SOLUSDT': {
        'entry_time': aware_entry.isoformat(), 'direction': 'SHORT',
        'tp2_order_id': 900,
    }}}}
    assert await load_matching_position_entry_time(
        db, 'btc_eth_aggressive', 'SOLUSDT', 'SHORT', 900,
    ) == aware_entry.astimezone().replace(tzinfo=None)


@pytest.mark.asyncio
async def test_touch_state_refreshes_heartbeat_without_overwriting_positions():
    db = AsyncMock()
    await touch_strategy_state(db, 'btc_eth_aggressive')
    sql, strategy_name, state_key = db.execute.await_args.args
    assert 'UPDATE public.strategy_states' in sql
    assert 'updated_at = NOW()' in sql
    assert 'state_data' not in sql
    assert strategy_name == 'btc_eth_aggressive'
    assert state_key == 'main'
