from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strategies.btc_eth_aggressive.strategy import BTCEthStrategy


@pytest.mark.asyncio
async def test_startup_refuses_guessed_software_state_for_legacy_lot():
    s = object.__new__(BTCEthStrategy)
    s.positions = {}
    s._unverified_entry_time_symbols = set()
    s._untracked_stop_retry_symbols = set()
    s._verified_legacy_positions = {}
    s.min_position_amt = 0.00001
    s.my_record_name = 'btc_eth_aggressive'
    s.instance_id = 'btc_eth_aggressive'
    s.db_manager = MagicMock()
    s.db_manager.fetch_one = AsyncMock(return_value=None)
    s.binance = MagicMock()
    s.binance.get_user_trades = AsyncMock(return_value=[{'id': 1, 'time': 1793409086845, 'side': 'BUY', 'qty': '0.023'}])
    s.binance.get_open_algo_orders = AsyncMock(return_value=[{'symbol': 'ETHUSDT', 'side': 'SELL', 'orderType': 'STOP', 'reduceOnly': True, 'quantity': '0.023'}])
    s._get_current_price = AsyncMock(return_value=Decimal('2300'))
    s._calc_protection_atr = AsyncMock(return_value=Decimal('20'))
    s._get_protection_precision = AsyncMock(return_value=(Decimal('0.01'), '0.001'))
    s._place_missing_tp_orders = AsyncMock()
    with patch('strategies.btc_eth_aggressive.strategy.resolve_position_owner', new=AsyncMock(return_value='btc_eth_aggressive')):
        await s._ensure_symbol_protection(
            {'symbol': 'ETHUSDT', 'positionAmt': '0.023', 'entryPrice': '2200'},
            {'ETHUSDT'}, {}, Decimal('0.002'), Decimal('0.0015'), Decimal('1'))
    assert 'ETHUSDT' not in s.positions
    s._place_missing_tp_orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_legacy_expiry_keeps_exchange_protection():
    s = object.__new__(BTCEthStrategy)
    s.my_record_name = 'btc_eth_aggressive'
    s.db_manager = MagicMock()
    s.binance = MagicMock()
    s.binance.get_position = AsyncMock(return_value=[{'symbol': 'ETHUSDT', 'positionAmt': '0.023', 'entryPrice': '2200'}])
    s.binance.get_symbol_info = AsyncMock(return_value={'stepSize': '0.001'})
    s.binance.get_user_trades = AsyncMock(return_value=[
        {'id': 1, 'time': 1793409086845, 'side': 'BUY', 'qty': '0.023'},
    ])
    s.binance.place_order = AsyncMock(side_effect=RuntimeError('close rejected'))
    s.binance.get_open_algo_orders = AsyncMock(return_value=[{'symbol': 'ETHUSDT', 'side': 'SELL', 'orderType': 'STOP', 'reduceOnly': True, 'quantity': '0.023'}])
    s._cancel_symbol_conditional_orders = AsyncMock()
    with patch('strategies.btc_eth_aggressive.strategy.resolve_position_owner', new=AsyncMock(return_value='btc_eth_aggressive')):
        assert not await s._close_untracked_exchange_position('ETHUSDT', Decimal('0.023'), 'LEGACY_TIME_STOP')
    s._cancel_symbol_conditional_orders.assert_not_awaited()

@pytest.mark.asyncio
async def test_invalid_snapshot_cannot_be_guessed_during_startup_protection():
    s = object.__new__(BTCEthStrategy)
    s.positions = {}
    s._unverified_entry_time_symbols = set()
    s._untracked_stop_retry_symbols = set()
    s.min_position_amt = 0.00001
    s.my_record_name = 'btc_eth_aggressive'
    s.db_manager = MagicMock()
    s.db_manager.fetch_one = AsyncMock(return_value={'state_data': '{"positions":{"ETHUSDT":{"grade":"A"}}}'})
    s.binance = MagicMock()
    s.binance.get_open_algo_orders = AsyncMock(return_value=[
        {'symbol': 'ETHUSDT', 'side': 'SELL', 'orderType': 'STOP',
         'reduceOnly': True, 'quantity': '0.023', 'triggerPrice': '2000'},
    ])
    s._place_missing_tp_orders = AsyncMock()
    with patch('strategies.btc_eth_aggressive.strategy.resolve_position_owner',
               new=AsyncMock(return_value='btc_eth_aggressive')):
        await s._ensure_symbol_protection(
            {'symbol': 'ETHUSDT', 'positionAmt': '0.023', 'entryPrice': '2200'},
            {'ETHUSDT'}, {}, Decimal('0.002'), Decimal('0.0015'), Decimal('1'))
    assert s.positions == {}
    s._place_missing_tp_orders.assert_not_awaited()


def test_partial_stop_does_not_count_as_full_protection():
    orders = [
        {'symbol': 'XRPUSDT', 'side': 'SELL', 'orderType': 'STOP',
         'reduceOnly': True, 'quantity': '40.7', 'triggerPrice': '1.2'},
    ]
    assert not BTCEthStrategy._hard_stop_covers(orders, 'XRPUSDT', 'SELL', Decimal('128.2'))
    orders.append({'symbol': 'XRPUSDT', 'side': 'SELL', 'orderType': 'STOP',
                   'reduceOnly': True, 'quantity': '87.5', 'triggerPrice': '1.2'})
    assert BTCEthStrategy._hard_stop_covers(orders, 'XRPUSDT', 'SELL', Decimal('128.2'))


@pytest.mark.asyncio
async def test_allocation_refuses_untracked_lot_that_has_reopened():
    s = object.__new__(BTCEthStrategy)
    s.positions = {}
    s.min_position_amt = 0.00001
    s.my_record_name = 'btc_eth_aggressive'
    s.db_manager = MagicMock()
    s.binance = MagicMock()
    s.binance.get_position = AsyncMock(return_value=[
        {'symbol': 'ETHUSDT', 'positionAmt': '0.023', 'markPrice': '2300',
         'leverage': '5', 'entryPrice': '2200'},
    ])
    s.binance.get_user_trades = AsyncMock(return_value=[
        {'id': 1, 'time': 1793409086845, 'side': 'BUY', 'qty': '0.023'},
        {'id': 2, 'time': 1793409087845, 'side': 'SELL', 'qty': '0.023'},
        {'id': 3, 'time': 1793409088845, 'side': 'BUY', 'qty': '0.023'},
    ])
    s._close_untracked_exchange_position = AsyncMock()
    async def enforce(margins, close):
        return await close('ETHUSDT')
    s.capital_mgr = MagicMock()
    s.capital_mgr.enforce_existing_positions = AsyncMock(side_effect=enforce)
    with patch('strategies.btc_eth_aggressive.strategy.resolve_position_owner',
               new=AsyncMock(return_value='btc_eth_aggressive')):
        assert not await s._enforce_allocation_holdings()
    s._close_untracked_exchange_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_report_uses_one_strict_owned_set_for_snapshot_and_dashboard():
    import importlib
    main_module = importlib.import_module('strategies.btc_eth_aggressive.main')
    filter_report_owned = main_module.filter_report_owned
    values = {'ETHUSDT': {'quantity': 1}, 'SOLUSDT': {'quantity': 2}}
    async def owner(db, symbol, status_filter=False):
        return 'btc_eth_aggressive' if symbol == 'ETHUSDT' else 'btc_eth'
    with patch.object(main_module, 'resolve_position_owner', new=owner):
        positions, margins, quantities = await filter_report_owned(
            MagicMock(), 'btc_eth_aggressive', values,
            {'ETHUSDT': 10, 'SOLUSDT': 20}, {'ETHUSDT': 1, 'SOLUSDT': 2})
    assert set(positions) == set(margins) == set(quantities) == {'ETHUSDT'}
    with patch.object(main_module, 'resolve_position_owner',
                      new=AsyncMock(return_value=None)):
        assert await filter_report_owned(MagicMock(), 'btc_eth_aggressive', values, {}, {}) is None

@pytest.mark.asyncio
async def test_new_entry_rejects_exchange_position_even_if_memory_is_empty():
    s = object.__new__(BTCEthStrategy)
    s.positions = {}
    s.binance = MagicMock()
    s.binance.get_position = AsyncMock(return_value=[
        {'symbol': 'ETHUSDT', 'positionAmt': '0.023'},
    ])
    s._place_and_wait_entry_order = AsyncMock()
    signal = {'symbol': 'ETHUSDT', 'direction': 'SHORT', 'leverage': 4}
    s._check_entry_limits = AsyncMock(return_value=True)
    s.binance.set_leverage = AsyncMock()
    assert await s._place_entry_order('ETHUSDT', signal) is None
    s._place_and_wait_entry_order.assert_not_awaited()

@pytest.mark.asyncio
async def test_verified_legacy_owner_fallback_keeps_snapshot_and_dashboard_row():
    import importlib
    main_module = importlib.import_module('strategies.btc_eth_aggressive.main')
    with patch.object(main_module, 'resolve_position_owner',
                      new=AsyncMock(return_value=None)):
        report = await main_module.filter_report_owned(
            MagicMock(), 'btc_eth_aggressive',
            {'SOLUSDT': {'quantity': 0.47}},
            {'SOLUSDT': 10.0}, {'SOLUSDT': 0.47},
            {'SOLUSDT': {'entry_ms': 1793347876165}},
        )
    assert report[0]['SOLUSDT']['quantity'] == 0.47
    assert report[1]['SOLUSDT'] == 10.0
