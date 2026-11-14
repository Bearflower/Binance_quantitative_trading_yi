import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from strategies.hrs.timeout_recovery import PendingReconciliation, TimeoutRecovery


class JournalDB:
    def __init__(self):
        self.rows = {}

    async def execute(self, sql, key, *args):
        if sql.startswith('INSERT'):
            self.rows.setdefault(key, args[0])
        elif sql.startswith('UPDATE'):
            self.rows[key] = args[0]
        else:
            del self.rows[key]

    async def fetch_one(self, sql, key, *args):
        if sql.startswith('UPDATE'):
            if json.loads(self.rows[key]) != json.loads(args[1]):
                return None
            self.rows[key] = args[0]
        return {'key': key, 'value': self.rows[key]}

    async def fetch_all(self, *args):
        return [dict(key=k, value=v) for k, v in self.rows.items()]


def terminal(fill):
    return dict(orderId=7, symbol='AAVEUSDT', executedQty=str(fill), avgPrice='101', status='CANCELED')


@pytest.mark.asyncio
async def test_zero_fill_preserves_unrelated_position():
    db, api = JournalDB(), AsyncMock()
    recovery = TimeoutRecovery(db, api)
    key, data = await recovery.begin('AAVEUSDT', 7, 'short', 0, 2, 'lv_rm')
    api.cancel_order.return_value = terminal(0)
    assert await recovery.reconcile(key, data) is None
    api.get_position.assert_not_awaited()
    api.place_order.assert_not_awaited()
    assert not db.rows


@pytest.mark.asyncio
async def test_restart_after_unknown_entry_and_exit_is_bounded_by_own_fills():
    db, api = JournalDB(), AsyncMock()
    recovery = TimeoutRecovery(db, api)
    key, data = await recovery.begin('AAVEUSDT', 7, 'short', 0, 2, 'lv_rm')
    api.cancel_order.side_effect = TimeoutError()
    api.get_order.return_value = None
    with pytest.raises(PendingReconciliation):
        await recovery.reconcile(key, data)
    recovery = TimeoutRecovery(db, api)
    api.get_order.return_value = terminal('.2')
    api.get_position.side_effect = [[], [dict(symbol='AAVEUSDT', positionAmt='-2.2')],
                                  [dict(symbol='AAVEUSDT', positionAmt='-.1')]]
    api.place_order.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await recovery.reconcile(key, json.loads(db.rows[key]))
    submission = api.place_order.await_args.kwargs
    assert submission['quantity'] == Decimal('.2')
    assert submission['reduceOnly'] == 'true'
    api.get_order_by_client_id.return_value = dict(symbol='AAVEUSDT', status='EXPIRED',
        executedQty='.1', clientOrderId=submission['client_order_id'])
    recovery = TimeoutRecovery(db, api)
    remaining = await recovery.reconcile(key, json.loads(db.rows[key]))
    assert Decimal(remaining['executedQty']) == Decimal('.1')
    assert remaining['avgPrice'] == '101'
    assert api.place_order.await_count == 1
    assert key in db.rows


@pytest.mark.asyncio
async def test_delayed_position_and_final_exit():
    db, api = JournalDB(), AsyncMock()
    recovery = TimeoutRecovery(db, api)
    key, data = await recovery.begin('AAVEUSDT', 7, 'short', 0, 2, 'lv_rm')
    api.cancel_order.return_value = terminal('.2')
    api.get_position.side_effect = [[], [], [], [dict(symbol='AAVEUSDT', positionAmt='-.2')]]
    with pytest.raises(PendingReconciliation):
        await recovery.reconcile(key, data)
    api.place_order.assert_not_awaited()

    async def fill(**kwargs):
        return dict(symbol='AAVEUSDT', status='FILLED', executedQty='.2', clientOrderId=kwargs['client_order_id'])
    api.place_order.side_effect = fill
    assert await recovery.reconcile(key, json.loads(db.rows[key])) is None
    assert not db.rows


@pytest.mark.asyncio
async def test_stale_recovery_snapshot_cannot_submit_exit_again():
    db, api = JournalDB(), AsyncMock()
    recovery = TimeoutRecovery(db, api)
    key, stale = await recovery.begin('AAVEUSDT', 7, 'short', 0, 2, 'lv_rm')
    db.rows[key] = json.dumps(dict(stale, previously_closed='.1'))
    api.cancel_order.return_value = terminal('.2')
    api.get_position.return_value = [{'symbol': 'AAVEUSDT', 'positionAmt': '-.1'}]
    with pytest.raises(PendingReconciliation):
        await recovery.reconcile(key, stale)
    api.place_order.assert_not_awaited()
