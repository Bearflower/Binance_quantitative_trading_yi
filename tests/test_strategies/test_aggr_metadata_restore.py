import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strategies.btc_eth_aggressive.strategy import BTCEthStrategy


ANCHOR = 1793409086845


def make_strategy(order_id=101):
    strategy = object.__new__(BTCEthStrategy)
    strategy.instance_id = "btc_eth_aggressive"
    strategy.my_record_name = "btc_eth_aggressive"
    strategy._unbound_stop_algo_ids = {}
    strategy.db_manager = MagicMock()
    strategy.binance = MagicMock()
    state = {"positions": {"ETHUSDT": {
        "direction": "LONG", "entry_price": "2200", "grade": "B", "atr": "18",
        "entry_time": "2026-10-31T01:11:20", "entry_fill_time_ms": ANCHOR,
        "initial_quantity": "0.03", "current_quantity": "0.023",
        "entry_order_id": 100, "episode_entry_order_id": 100,
        "stop_loss_order_id": 101,
        "tp1_order_id": 102, "tp2_order_id": 103,
        "tp1_hit": False, "tp2_hit": False,
        "trailing_activated": False,
    }}}
    strategy.db_manager.fetch_one = AsyncMock(return_value={"state_data": json.dumps(state)})
    strategy.binance.get_user_trades = AsyncMock(side_effect=[[
        {"id": 1, "time": ANCHOR, "side": "BUY", "qty": "0.03"},
        {"id": 2, "time": ANCHOR + 1000, "side": "SELL", "qty": "0.007"},
    ], []])
    strategy.binance.get_open_algo_orders = AsyncMock(return_value=[
        {"algoId": order_id, "symbol": "ETHUSDT", "side": "SELL", "orderType": "STOP", "reduceOnly": True, "quantity": "0.023"},
        {"algoId": 102, "symbol": "ETHUSDT", "side": "SELL", "orderType": "TAKE_PROFIT", "reduceOnly": True, "quantity": "0.006"},
        {"algoId": 103, "symbol": "ETHUSDT", "side": "SELL", "orderType": "TAKE_PROFIT", "reduceOnly": True, "quantity": "0.009"},
    ])
    return strategy


@pytest.mark.asyncio
async def test_restores_original_grade_atr_and_fill_time_when_exchange_matches():
    s = make_strategy()
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner", new=AsyncMock(return_value="btc_eth_aggressive")):
        pos = await s._load_verified_position_metadata(
            "ETHUSDT", {"symbol": "ETHUSDT", "positionAmt": "0.023", "entryPrice": "2200"}, Decimal("2300")
        )
    assert pos.grade == "B"
    assert pos.atr == Decimal("18")
    assert pos.current_quantity == Decimal("0.023")
    assert pos.entry_fill_time_ms == ANCHOR
    assert pos.episode_entry_order_id == 100
    assert pos.stop_loss_order_id == 101
    assert pos.entry_time.isoformat() == "2026-10-31T01:11:26.845000"
    first_call = s.binance.get_user_trades.await_args_list[0]
    assert first_call.args == ("ETHUSDT",)
    assert first_call.kwargs["start_time"] == ANCHOR
    assert all(call.kwargs["limit"] == 1000 for call in s.binance.get_user_trades.await_args_list)


@pytest.mark.asyncio
async def test_restores_single_tp_exit_plan_without_requiring_tp2():
    s = make_strategy()
    row = await s.db_manager.fetch_one('ignored')
    state = json.loads(row['state_data'])
    data = state['positions']['ETHUSDT']
    data.update(initial_quantity='0.02',current_quantity='0.02',tp2_order_id=None,
                exit_ratios=['0.5','0'])
    s.db_manager.fetch_one.return_value = {'state_data': json.dumps(state)}
    s.binance.get_user_trades.side_effect = [[
        {'id':1,'time':ANCHOR,'side':'BUY','qty':'0.02'}], []]
    s.binance.get_open_algo_orders.return_value = [
        {'algoId':101,'symbol':'ETHUSDT','side':'SELL','orderType':'STOP','reduceOnly':True,'quantity':'0.02'},
        {'algoId':102,'symbol':'ETHUSDT','side':'SELL','orderType':'TAKE_PROFIT','reduceOnly':True,'quantity':'0.01'},
    ]
    with patch('strategies.btc_eth_aggressive.strategy.resolve_position_owner',
               new=AsyncMock(return_value='btc_eth_aggressive')):
        pos = await s._load_verified_position_metadata('ETHUSDT',
            {'symbol':'ETHUSDT','positionAmt':'0.02','entryPrice':'2200'},Decimal('2300'))
    assert pos is not None
    assert pos.exit_ratios == (Decimal('0.5'),Decimal('0'))
    assert 'TP2' in pos.not_placeable_legs


@pytest.mark.asyncio
async def test_restores_finished_tp1_after_restart_before_snapshot_update():
    s = make_strategy()
    s._get_precision_params = AsyncMock(return_value=(Decimal('0.01'),Decimal('0.01')))
    state = json.loads((await s.db_manager.fetch_one('ignored'))['state_data'])
    state['positions']['ETHUSDT'].update(initial_quantity='0.02',current_quantity='0.02',
        tp2_order_id=None,exit_ratios=['0.5','0'],tp1_hit=False)
    s.db_manager.fetch_one.return_value = {'state_data':json.dumps(state)}
    s.binance.get_user_trades.side_effect = [[
        {'id':1,'time':ANCHOR,'side':'BUY','qty':'0.02','orderId':100},
        {'id':2,'time':ANCHOR+1000,'side':'SELL','qty':'0.01','orderId':222},
    ],[]]
    s.binance.get_open_algo_orders.return_value = [
        {'algoId':101,'symbol':'ETHUSDT','side':'SELL','orderType':'STOP',
         'reduceOnly':True,'quantity':'0.02'}]
    s.binance.get_algo_order = AsyncMock(return_value={
        'algoId':102,'symbol':'ETHUSDT','side':'SELL','orderType':'TAKE_PROFIT',
        'reduceOnly':True,'algoStatus':'FINISHED','createTime':ANCHOR+100,
        'actualQty':'0.01','actualOrderId':'222'})
    s.binance.get_order = AsyncMock(return_value={
        'orderId':222,'symbol':'ETHUSDT','side':'SELL','reduceOnly':True,
        'status':'FILLED','executedQty':'0.01'})
    with patch('strategies.btc_eth_aggressive.strategy.resolve_position_owner',
               new=AsyncMock(return_value='btc_eth_aggressive')):
        pos = await s._load_verified_position_metadata('ETHUSDT',
            {'symbol':'ETHUSDT','positionAmt':'0.01','entryPrice':'2200'},Decimal('2300'))
    assert pos is not None and pos.tp1_hit is True
    assert pos.exit_ratios == (Decimal('0.5'),Decimal('0'))


@pytest.mark.asyncio
async def test_rejects_stale_order_or_reopened_lot():
    s = make_strategy(order_id=999)
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner", new=AsyncMock(return_value="btc_eth_aggressive")):
        assert await s._load_verified_position_metadata(
            "ETHUSDT", {"symbol": "ETHUSDT", "positionAmt": "0.023", "entryPrice": "2200"}, Decimal("2300")
        ) is None
    s = make_strategy()
    s.binance.get_user_trades.side_effect = [[
        {"id": 1, "time": ANCHOR, "side": "BUY", "qty": "0.03"},
        {"id": 2, "time": ANCHOR + 1000, "side": "SELL", "qty": "0.03"},
        {"id": 3, "time": ANCHOR + 2000, "side": "BUY", "qty": "0.023"},
    ], []]
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner", new=AsyncMock(return_value="btc_eth_aggressive")):
        assert await s._load_verified_position_metadata(
            "ETHUSDT", {"symbol": "ETHUSDT", "positionAmt": "0.023", "entryPrice": "2200"}, Decimal("2300")
        ) is None


@pytest.mark.asyncio
async def test_adoption_prefers_verified_metadata_even_when_db_order_rows_are_stale():
    s = make_strategy()
    s._get_current_price = AsyncMock(return_value=Decimal("2210"))
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner", new=AsyncMock(return_value="btc_eth_aggressive")):
        pos = await s._build_adopted_position_state(
            "ETHUSDT", {"symbol": "ETHUSDT", "positionAmt": "0.023", "entryPrice": "2200"}, {}
        )
    assert pos is not None
    assert pos.grade == "B"
    assert pos.atr == Decimal("18")


@pytest.mark.asyncio
async def test_restart_keeps_unbound_stop_flag_from_verified_position_snapshot():
    s = make_strategy()
    row = await s.db_manager.fetch_one("ignored")
    state = json.loads(row["state_data"])
    state["positions"]["ETHUSDT"]["unbound_stop_algo_ids"] = [2001]
    s.db_manager.fetch_one.return_value = {"state_data": json.dumps(state)}
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner",
               new=AsyncMock(return_value="btc_eth_aggressive")):
        pos = await s._load_verified_position_metadata(
            "ETHUSDT", {"symbol": "ETHUSDT", "positionAmt": "0.023", "entryPrice": "2200"},
            Decimal("2300"),
        )
    assert pos is not None
    assert s._unbound_stop_algo_ids == {"ETHUSDT": {"2001"}}
