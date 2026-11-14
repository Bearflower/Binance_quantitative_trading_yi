"""订单终态和按角色恢复保护单的回归验证。"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from pathlib import Path

from strategies.hrs.executor import TradingExecutor
from strategies.hrs.position_manager import PositionManager
from shared.binance_api import BinanceAPIError
from shared.reduce_only_close import CloseOutcome, CLOSE_FAILED, CLOSE_FILLED


@pytest.fixture
def executor():
    config = yaml.safe_load((Path(__file__).parents[1] / 'config.yaml').read_text())
    api = MagicMock(use_unified_account=True)
    async def lookup(algo_id=None, client_algo_id=None):
        if client_algo_id:
            raise BinanceAPIError(-2013, 'Order does not exist')
        return {'algoId': algo_id, 'algoStatus': 'CANCELED'}
    api.get_algo_order = AsyncMock(side_effect=lookup)
    api.get_symbol_info = AsyncMock(return_value={'tickSize': '0.01', 'stepSize': '0.001'})
    api.get_open_algo_orders = AsyncMock(return_value=[{'algoId': 1}, {'algoId': 3}])
    pm = PositionManager(config, api)
    pm.add_position('AAVEUSDT', 'short', 100, 1, 2, entry_mode='lv_rm')
    pm.add_algo_id('AAVEUSDT', 'sl', 1)
    pm.add_algo_id('AAVEUSDT', 'tp2', 3)
    return TradingExecutor(config, api, MagicMock(), MagicMock(), pm)


@pytest.mark.asyncio
async def test_nearly_filled_order_waits_for_terminal(executor):
    executor.entry_timeout_check_interval_seconds = 0
    executor.binance_api.get_order = AsyncMock(return_value={'status': 'FILLED', 'executedQty': '10000', 'origQty': '10000'})
    result = await executor._check_order_fill_with_timeout('AAVEUSDT', {'orderId': 7, 'status': 'PARTIALLY_FILLED', 'executedQty': '9999', 'origQty': '10000'}, 'short', Decimal('10000'), Decimal('1'))
    assert result['status'] == 'FILLED'
    executor.binance_api.get_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_replenish_keeps_sl_and_uses_lv_rm(executor):
    executor._replenish_sl_role = AsyncMock(return_value=(1, []))
    executor._replenish_tp_role = AsyncMock(return_value=(1, [], None))
    executor._summarize_replenish = AsyncMock()
    executor.position_manager.cancel_all_orders = AsyncMock()
    await executor.replenish_position_orders('AAVEUSDT', 'short', 100, 1, 2, entry_mode='lv_rm')
    executor._replenish_sl_role.assert_not_awaited()
    executor.position_manager.cancel_all_orders.assert_not_awaited()
    args = executor._replenish_tp_role.await_args.args
    assert args[2] == 'tp1'
    assert args[8]['atr_multiplier'] == executor.lv_rm_atr_multiplier


@pytest.mark.asyncio
async def test_stale_sl_id_is_replaced(executor):
    executor.binance_api.get_open_algo_orders.return_value = [{'algoId': 3}]
    executor._replenish_sl_role = AsyncMock(return_value=(1, []))
    executor._replenish_tp_role = AsyncMock(return_value=(1, [], None))
    executor._summarize_replenish = AsyncMock()
    await executor.replenish_position_orders('AAVEUSDT', 'short', 100, 1, 2, entry_mode='lv_rm')
    executor._replenish_sl_role.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_close_forces_market_and_reduce_only(executor):
    executor.config['trading']['close_order'] = {'reduce_only': False}
    executor._read_close_target_qty = AsyncMock(return_value=(Decimal('1'), Decimal('.001'), Decimal('.01')))
    outcome = CloseOutcome(CLOSE_FILLED, Decimal('1'), Decimal('1'), True)
    with patch('strategies.hrs.executor.close_remaining', new=AsyncMock(return_value=outcome)) as close:
        result = await executor.close_position('AAVEUSDT', 'short', force_market=True)
    assert result.success
    assert close.await_args.kwargs['order_type'] == 'MARKET'
    assert close.await_args.kwargs['reduce_only'] is True

@pytest.mark.asyncio
async def test_recover_tp_after_ack_before_local_save(executor):
    tag = executor._protection_client_id('AAVEUSDT', 'tp1')
    executor.binance_api.get_open_algo_orders.return_value.append({'algoId': 2, 'clientAlgoId': tag})
    result = await executor.replenish_position_orders('AAVEUSDT', 'short', 100, 1, 2, entry_mode='lv_rm')
    assert result.success and result.placed == 0
    assert executor.position_manager.get_algo_id('AAVEUSDT', 'tp1') == 2
    executor.binance_api.place_conditional_order.assert_not_called()


@pytest.mark.asyncio
async def test_unverified_standard_account_stop_cannot_report_success(executor):
    executor.binance_api.use_unified_account = False
    executor.binance_api.get_open_orders = AsyncMock(return_value=[])
    executor.binance_api.get_order = AsyncMock(return_value=None)
    result = await executor.replenish_position_orders('AAVEUSDT', 'short', 100, 1, 2)
    assert not result.success and result.note == 'protection_unconfirmed'
    executor.binance_api.place_conditional_order.assert_not_called()


@pytest.mark.asyncio
async def test_unattributed_legacy_tp_blocks_duplicate_submission(executor):
    executor.binance_api.get_open_algo_orders.return_value.append(
        {'algoId': 99, 'side': 'BUY', 'orderType': 'TAKE_PROFIT'})
    result = await executor.replenish_position_orders('AAVEUSDT', 'short', 100, 1, 2)
    assert not result.success and result.note == 'protection_unconfirmed'
    executor.binance_api.place_conditional_order.assert_not_called()


@pytest.mark.asyncio
async def test_triggered_stop_is_not_replaced(executor):
    executor.binance_api.get_open_algo_orders.return_value = [{'algoId': 3}]
    executor.binance_api.get_algo_order.side_effect = None
    executor.binance_api.get_algo_order.return_value = {'algoId': 1, 'algoStatus': 'TRIGGERED', 'actualOrderId': 900}
    result = await executor.replenish_position_orders('AAVEUSDT', 'short', 100, 1, 2)
    assert not result.success and result.note == 'protection_unconfirmed'
    executor.binance_api.place_conditional_order.assert_not_called()


@pytest.mark.asyncio
async def test_untracked_triggered_tp_is_not_submitted_again(executor):
    executor.binance_api.get_algo_order.side_effect = None
    executor.binance_api.get_algo_order.return_value = {'algoId': 2, 'algoStatus': 'FINISHED', 'actualOrderId': 901}
    result = await executor.replenish_position_orders('AAVEUSDT', 'short', 100, 1, 2)
    assert not result.success and result.note == 'protection_unconfirmed'
    executor.binance_api.place_conditional_order.assert_not_called()


@pytest.mark.asyncio
async def test_state_write_failure_does_not_skip_stop_creation(executor):
    executor.persist_fill = AsyncMock(side_effect=RuntimeError('database unavailable'))
    executor._set_leverage = AsyncMock()
    executor.binance_api.place_order = AsyncMock(return_value={
        'symbol': 'AAVEUSDT', 'orderId': 55, 'status': 'FILLED',
        'executedQty': '1', 'avgPrice': '100'})
    async def submit(*args):
        return await executor.binance_api.place_order()
    executor._submit_reserved_hrs_entry = AsyncMock(side_effect=submit)
    executor._create_protection_orders = AsyncMock(return_value=[])
    executor._send_notification = AsyncMock()
    with patch('strategies.hrs.executor.entry_allowed', new=AsyncMock(return_value=True)):
        from types import SimpleNamespace
        result = await executor.execute_short('AAVEUSDT', 100, 2, Decimal('1'), 8, risk_decision=SimpleNamespace(exit_ratios=(Decimal('.3'),Decimal('.4'))))
    assert result.order_filled and result.protection_complete
    executor._create_protection_orders.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirmed_tp1_does_not_block_tp2_recovery(executor):
    executor.position_manager.add_algo_id('AAVEUSDT', 'tp1', 2)
    executor.position_manager.remove_algo_id('AAVEUSDT', 'tp2')
    executor.binance_api.get_open_algo_orders.return_value = [{'algoId': 1}]
    executor._replenish_tp_role = AsyncMock(return_value=(1, [], None))
    executor._summarize_replenish = AsyncMock()
    await executor.replenish_position_orders('AAVEUSDT', 'short', 100, 1, 2,
                                            target1_reached=True, entry_mode='lv_rm')
    calls = executor._replenish_tp_role.await_args_list
    assert any(call.args[2] == 'tp2' for call in calls)
    assert not executor.position_manager.has_algo_id('AAVEUSDT', 'tp1')
