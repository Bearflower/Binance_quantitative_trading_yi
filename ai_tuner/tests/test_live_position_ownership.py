from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_tuner.monitor.live_position_ownership import resolve_unclaimed_positions


NOW = datetime(2026, 11, 19, 7, tzinfo=timezone.utc)
AT = int(NOW.timestamp() * 1000) - 120000
SYMBOL = 'ETNUSDT'
ENTRY = {'orderId': 320067, 'symbol': SYMBOL, 'side': 'SELL', 'status': 'FILLED',
         'type': 'MARKET', 'positionSide': 'BOTH', 'reduceOnly': False, 'time': AT-5,
         'origQty': '0.29', 'executedQty': '0.29', 'avgPrice': '338.5227586'}
FILL = {'id': 8898, 'orderId': 320067, 'symbol': SYMBOL, 'side': 'SELL',
        'positionSide': 'BOTH', 'time': AT, 'qty': '0.07', 'price': '338.72'}
FILL2 = {**FILL, 'id': 8899, 'qty': '0.22', 'price': '338.46'}
POSITION = {'symbol': SYMBOL, 'positionSide': 'BOTH', 'positionAmt': '-0.29',
            'entryPrice': '338.5227586207'}
OWNER = {'symbol': SYMBOL, 'order_id': '320067', 'strategy': 'new_coin', 'side': 'SELL',
         'ledger_trades': [[str(f['id']), f['qty'], f['price'], f['side'], f['time']]
                           for f in (FILL, FILL2)]}


def context():
    api = SimpleNamespace(get_order=AsyncMock(return_value=dict(ENTRY)),
                          get_user_trades=AsyncMock(side_effect=[[FILL, FILL2], [FILL, FILL2]]),
                          get_position=AsyncMock(return_value=[POSITION]))
    db = SimpleNamespace(fetch_all=AsyncMock(return_value=[OWNER]))
    return db, api


@pytest.mark.asyncio
async def test_expired_claim_recovers_from_same_signed_order_and_fills():
    db, api = context()
    proofs, gaps = await resolve_unclaimed_positions(db,api,'default',[POSITION],set(),now=NOW)
    assert not gaps
    assert proofs[SYMBOL]['strategy'] == 'new_coin'
    assert proofs[SYMBOL]['entry_order_id'] == '320067'
    assert proofs[SYMBOL]['entry_trade_ids'] == ['8898','8899']


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['other_owner', 'same_side_add', 'quantity', 'scope',
                                   'time', 'position_changed', 'full_page'])
async def test_unsafe_or_incomplete_evidence_stays_unowned(fault):
    db, api = context()
    account, positions = 'default', [POSITION]
    if fault == 'other_owner':
        db.fetch_all.return_value = [OWNER,{**OWNER,'strategy':'hrs'}]
    elif fault == 'same_side_add':
        api.get_user_trades.side_effect = [[FILL,FILL2],[FILL,FILL2,{**FILL,'id':9999,'orderId':12}]]
    elif fault == 'quantity':
        positions = [{**POSITION,'positionAmt':'-0.30'}]
    elif fault == 'scope':
        account = 'grid_subaccount_pm'
    elif fault == 'time':
        api.get_order.return_value['time'] = AT-86400*1000*8
    elif fault == 'position_changed':
        api.get_position.return_value = [{**POSITION,'positionAmt':'-0.20'}]
    elif fault == 'full_page':
        api.get_user_trades.side_effect = [[FILL,FILL2],[FILL]*1000]
    proofs,gaps = await resolve_unclaimed_positions(db,api,account,positions,set(),now=NOW)
    assert not proofs and SYMBOL in gaps


@pytest.mark.asyncio
async def test_reduce_only_fills_can_explain_partial_position_without_transferring_ownership():
    db, api = context()
    reduction = {**FILL,'id':9001,'orderId':777,'side':'BUY','time':AT+1000,'qty':'0.08'}
    order = {**ENTRY,'orderId':777,'side':'BUY','executedQty':'0.08','origQty':'0.08',
             'avgPrice':reduction['price'],'reduceOnly':True}
    position = {**POSITION,'positionAmt':'-0.21'}
    api.get_order.side_effect = [ENTRY,order]
    api.get_user_trades.side_effect = [[FILL,FILL2],[FILL,FILL2,reduction]]
    api.get_position.return_value = [position]
    proofs, gaps = await resolve_unclaimed_positions(db,api,'default',[position],set(),now=NOW)
    assert not gaps and proofs[SYMBOL]['reduction_order_ids'] == ['777']


@pytest.mark.asyncio
async def test_trial_snapshot_accounts_for_proven_position_without_assigning_it_to_hrs(monkeypatch):
    from datetime import timedelta
    from ai_tuner.monitor import reduced_trial

    proof = {'strategy': 'new_coin', 'entry_order_id': '320067'}
    resolver = AsyncMock(return_value=({SYMBOL: proof}, {}))
    monkeypatch.setattr(reduced_trial, 'resolve_unclaimed_positions', resolver, raising=False)
    db = SimpleNamespace(fetch_all=AsyncMock(return_value=[]), fetch_one=AsyncMock(return_value={}))
    exchange = SimpleNamespace(get_position=AsyncMock(return_value=[POSITION]),
                               get_open_orders=AsyncMock(return_value=[]),
                               get_open_algo_orders=AsyncMock(return_value=[]))
    result = await reduced_trial.collect_snapshot(
        db, exchange, 'default', 'hrs', NOW-timedelta(days=1), NOW,
        income_reader=AsyncMock(return_value={}))

    assert result['exchange']['ownership_complete'] is True
    assert result['exchange']['owned_positions'] == []
    assert result['exchange']['position_ownership_proofs'] == {SYMBOL: proof}
