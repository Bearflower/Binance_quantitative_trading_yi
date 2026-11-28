"""2026-11-26 交易代码审查的离线回归用例。"""
import asyncio
import importlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strategies.hrs.position_manager import PositionManager
from strategies.hrs.risk_manager import RiskManager
from strategies.hrs.strategy import HRSStrategy


@pytest.fixture(autouse=True)
def runtime_account(monkeypatch):
    monkeypatch.setenv('TRADE_ACCOUNT_ID', 'test')
    monkeypatch.delenv('BINANCE_CLASSIC_CUTOVER_MS', raising=False)


@pytest.mark.parametrize('module', ['strategies.btc_eth.main', 'strategies.btc_eth.shadow.main',
                                   'strategies.btc_eth.shadow.readiness'])
def test_production_and_shadow_entrypoints_import(module):
    assert importlib.import_module(module)


def test_daily_limits_survive_same_day_restart_and_reset_next_day():
    config = {'trading': {'max_daily_positions': 3, 'max_daily_same_direction': 2}}
    before = RiskManager(config)
    assert before.can_open_position('short')
    for direction in ('short', 'short', 'long'):
        before.record_open(direction)
    after = RiskManager(config)
    after.from_dict(json.loads(json.dumps(before.to_dict())))
    assert not after.can_open_position('short')
    after._daily_reset_date -= timedelta(days=1)
    assert after.can_open_position('short')


def test_tp_progress_requires_complete_cumulative_batch():
    pm = PositionManager({}, MagicMock())
    pm.add_position('AAVEUSDT', 'short', 100, 100, 2)
    assert pm.detect_take_profit_fills('AAVEUSDT', 98) is None
    assert pm.detect_take_profit_fills('AAVEUSDT', 96) is None
    assert pm.get_position('AAVEUSDT')['remaining_quantity'] == 96
    assert pm.detect_take_profit_fills('AAVEUSDT', 70) == 1
    assert pm.detect_take_profit_fills('AAVEUSDT', 68) is None
    assert pm.detect_take_profit_fills('AAVEUSDT', 30) == 2


def test_tp_progress_uses_frozen_exit_geometry_after_restart():
    pm = PositionManager({}, MagicMock())
    pm.add_position('AAVEUSDT', 'short', 100, 100, 2, exit_ratios=(D('.2'), D('.3')))
    pm.detect_take_profit_fills('AAVEUSDT', 90)
    restored = PositionManager({}, MagicMock())
    restored.from_dict(pm.to_dict())
    assert restored.detect_take_profit_fills('AAVEUSDT', 80) == 1
    assert restored.detect_take_profit_fills('AAVEUSDT', 50) == 2


@pytest.mark.asyncio
async def test_newcoin_requires_fills_for_every_exit_order():
    from strategies.new_coin.strategy import NewCoinStrategy
    n = NewCoinStrategy.__new__(NewCoinStrategy)
    n.my_record_name = '新币做空策略'
    n._settlement_entry_quantity = AsyncMock(return_value=D('1'))
    n.positions = {'AAVEUSDT': {'entry_time': datetime.now().isoformat()}}
    n.db = MagicMock(fetch_all=AsyncMock(return_value=[{'order_id': '102'}, {'order_id': '101'}]))
    n.binance_client = MagicMock(get_user_trades=AsyncMock(side_effect=[
        [], [{'id':1101,'symbol':'AAVEUSDT','side':'BUY','orderId': 101, 'realizedPnl': '5', 'qty': '.3'}]]))
    n.binance_client.get_order = AsyncMock(side_effect=lambda symbol, oid: {
        'symbol':symbol, 'side':'BUY', 'orderId': oid, 'status': 'FILLED', 'executedQty': '.7' if oid == 102 else '.3'})
    assert await n._get_position_pnl('AAVEUSDT', 100) is None
    n.binance_client.get_user_trades.side_effect = [
        [{'id':1102,'symbol':'AAVEUSDT','side':'BUY','orderId': 102, 'realizedPnl': '-20', 'qty': '.7'}],
        [{'id':1101,'symbol':'AAVEUSDT','side':'BUY','orderId': 101, 'realizedPnl': '5', 'qty': '.3'}]]
    assert await n._get_position_pnl('AAVEUSDT', 100) == D('-15')


@pytest.mark.asyncio
async def test_newcoin_excludes_canceled_zero_but_requires_complete_filled_quantity():
    from strategies.new_coin.strategy import NewCoinStrategy
    n = NewCoinStrategy.__new__(NewCoinStrategy)
    n.my_record_name = '新币做空策略'
    n._settlement_entry_quantity = AsyncMock(return_value=D('1'))
    n.positions = {'AAVEUSDT': {'entry_time': datetime.now().isoformat()}}
    n.db = MagicMock(fetch_all=AsyncMock(return_value=[{'order_id':'101'}, {'order_id':'102'}]))
    n.binance_client = MagicMock()
    n.binance_client.get_order = AsyncMock(side_effect=lambda symbol, oid: {
        'symbol':symbol, 'side':'BUY', 'orderId':oid, 'status':'CANCELED' if oid == 101 else 'FILLED',
        'executedQty':'0' if oid == 101 else '1'})
    n.binance_client.get_user_trades = AsyncMock(return_value=[{'id':1102,'symbol':'AAVEUSDT','side':'BUY','orderId':102,'qty':'.5','realizedPnl':'-10'}])
    assert await n._get_position_pnl('AAVEUSDT', 100) is None
    n.binance_client.get_user_trades.return_value = [{'id':1102,'symbol':'AAVEUSDT','side':'BUY','orderId':102,'qty':'1','realizedPnl':'-20'}]
    assert await n._get_position_pnl('AAVEUSDT', 100) == D('-20')
    assert all(c.kwargs['order_id'] == '102' for c in n.binance_client.get_user_trades.await_args_list)


@pytest.mark.asyncio
async def test_newcoin_missing_pnl_retains_tracking_until_next_verified_cycle():
    from strategies.new_coin.tests.test_stop_loss_mark import make_strategy, make_position_entry
    n = make_strategy()
    n.positions['AAVEUSDT'] = make_position_entry()
    n._get_position_pnl = AsyncMock(side_effect=[None, D('-20')])
    await n._monitor_positions()
    assert n.positions['AAVEUSDT']['settlement_pending']
    n.trading_executor.clear_position_tracking.assert_not_called()
    assert n.consecutive_losses == 0
    await n._monitor_positions()
    assert 'AAVEUSDT' not in n.positions
    n.trading_executor.clear_position_tracking.assert_called_once_with('AAVEUSDT')
    assert n.consecutive_losses == 1


@pytest.mark.asyncio
async def test_newcoin_restart_baseline_preserves_flat_pending_settlement():
    from strategies.new_coin.tests.test_stop_loss_mark import make_strategy, make_position_entry
    n = make_strategy()
    n.positions['AAVEUSDT'] = dict(make_position_entry(), settlement_pending=True)
    n.trading_executor.get_contract_size_map = AsyncMock(return_value={})
    n._filter_own_positions = AsyncMock(return_value={})
    n._calc_rebuilt_atr_map = AsyncMock(return_value={})
    n._sync_baseline_to_tracking = AsyncMock()
    with patch('strategies.new_coin.strategy.rebuild_from_exchange', new=AsyncMock(
        return_value=MagicMock(success=True, positions={}))):
        await n._rebuild_position_baseline()
    assert n.positions['AAVEUSDT']['settlement_pending']
    n.trading_executor.clear_position_tracking.assert_not_called()
    await n._close_stale_memory_positions(set())
    assert n.positions['AAVEUSDT']['settlement_pending']


@pytest.mark.asyncio
async def test_tp_small_residual_does_not_expand_to_entire_position(mtpcs_variant):
    s, p = aggressive_fixture(mtpcs_variant, 'FILLED', '.1')
    assert await s._close_position('ETHUSDT', p, D('.1'), 'TP1', D('110'))
    assert s.binance.place_order.await_args.kwargs['quantity'] == D('.1')
    assert p.current_quantity == D('.9')


@pytest.mark.asyncio
async def test_newcoin_market_fallback_submits_only_unfilled_target():
    from strategies.new_coin.executor import TradingExecutor
    from shared.reduce_only_close import CloseOutcome
    n = TradingExecutor.__new__(TradingExecutor)
    n.config = {}
    n._read_short_target = AsyncMock(return_value=(D('4'), D('.1'), D('.1')))
    n._resolve_close_limit_price = AsyncMock(return_value=D('100'))
    n._close_with_reduce_only = AsyncMock(side_effect=[
        CloseOutcome(status='PARTIAL', target_qty=D('4'), closed_qty=D('1')),
        CloseOutcome(status='FILLED', target_qty=D('3'), closed_qty=D('3'), success=True)])
    n._update_short_position_closed = AsyncMock()
    n._notify_close_failure = AsyncMock()
    assert await n._close_position('AAVEUSDT', close_percent=D('.4'), reason='TP1')
    assert n._close_with_reduce_only.await_args_list[1].args[1] == D('3')
    n._update_short_position_closed.assert_not_awaited()


@pytest.fixture(params=['strategies.btc_eth.strategy', 'strategies.btc_eth_aggressive.strategy'])
def mtpcs_variant(request, monkeypatch):
    monkeypatch.setenv('TRADE_ACCOUNT_ID', 'test')
    monkeypatch.delenv('BINANCE_CLASSIC_CUTOVER_MS', raising=False)
    return importlib.import_module(request.param)


def actual_ledger():
    """使用同账户已核验回执的实际 PnL 记账替身。"""
    ledger = MagicMock(strategy_name='test', account_id='test')
    ledger.db.execute = AsyncMock(return_value='UPDATE 1')
    ledger._record_epoch.return_value = '[2026-01-01,2027-01-01)'
    ledger._receipt_in_epoch.return_value = True
    ledger._epoch_trades.side_effect = lambda fills, epoch: fills
    return ledger


def aggressive_fixture(variant, status='CANCELED', executed='0'):
    BTCEthStrategy, PositionState = variant.BTCEthStrategy, variant.PositionState
    s = BTCEthStrategy.__new__(BTCEthStrategy)
    p = PositionState()
    p.direction = 'LONG'; p.entry_price = D('100')
    p.current_quantity = p.initial_quantity = D('1'); p.stop_loss_order_id = 88
    s.strategy_name = 'MTPCS激进策略'
    s._sync_position_with_exchange = AsyncMock(return_value={
        'closed': False, 'partially_closed': False, 'actual_quantity': D('1')})
    s._get_symbol_precision = AsyncMock(return_value={'stepSize': '.01', 'tickSize': D('.01')})
    s.risk_config = {'position_sizing': {'min_close_notional_usdt': 20},
                     'close_limit_order': {'max_retries': 0, 'poll_interval_seconds': .001,
                                           'timeout_seconds': .001}}
    order = {'orderId': 7, 'status': status, 'executedQty': executed, 'avgPrice': '110'}
    s.binance = MagicMock(use_unified_account=True)
    s.binance.get_orderbook = AsyncMock(return_value={'bids': [['110','1']], 'asks': [['111','1']]})
    orders = {}
    class OrderMock(AsyncMock):
        async def _execute_mock_call(self, *args, **kwargs):
            row = await super()._execute_mock_call(*args, **kwargs)
            row = dict(row, symbol=kwargs['symbol'], side=kwargs['side'],
                       origQty=str(kwargs['quantity']), reduceOnly=True)
            orders[str(row['orderId'])] = row
            return row
    s.binance.place_order = OrderMock(return_value=order)
    s.binance.get_order = AsyncMock(side_effect=lambda symbol, oid: orders[str(oid)])
    async def trades(symbol, order_id, **kwargs):
        row = orders[str(order_id)]
        qty, price = D(row['executedQty']), D(row['avgPrice'])
        return [dict(id=int(order_id)+100, orderId=int(order_id), symbol=symbol, side=row['side'],
                     qty=str(qty), price=str(price), realizedPnl=str(qty*(price-D('100'))))]
    s.binance.get_user_trades = AsyncMock(side_effect=trades)
    s.binance.get_open_orders = AsyncMock(return_value=[])
    s.binance.cancel_order = AsyncMock(return_value=order)
    s.binance.trade_logger = actual_ledger()
    s.notification = MagicMock(send_trade_notification=AsyncMock(), send_error_notification=AsyncMock())
    s._cleanup_position_orders = AsyncMock()
    s._cancel_symbol_conditional_orders = AsyncMock()
    return s, p


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['CANCELED', 'EXPIRED'])
async def test_missing_aggressive_close_order_is_not_fill_evidence(mtpcs_variant, status):
    s, p = aggressive_fixture(mtpcs_variant, status)
    assert not await s._close_position('ETHUSDT', p, D('1'), 'TRAILING_STOP', D('110'))
    await asyncio.sleep(0)
    assert p.current_quantity == D('1')
    s._cleanup_position_orders.assert_not_awaited()
    s._cancel_symbol_conditional_orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_aggressive_partial_close_updates_only_confirmed_execution(mtpcs_variant):
    s, p = aggressive_fixture(mtpcs_variant, 'CANCELED', '.4')
    assert not await s._close_position('ETHUSDT', p, D('1'), 'TRAILING_STOP', D('110'))
    assert p.current_quantity == D('.6')
    s._cleanup_position_orders.assert_not_awaited()
    s._cancel_symbol_conditional_orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_aggressive_retry_submits_only_residual_and_subtracts_once(mtpcs_variant):
    s, p = aggressive_fixture(mtpcs_variant, 'CANCELED', '.4')
    s.risk_config['close_limit_order'].update(max_retries=1, retry_interval_seconds=0)
    s.binance.place_order.side_effect = [
        {'orderId': 7, 'status': 'CANCELED', 'executedQty': '.4', 'avgPrice': '110'},
        {'orderId': 8, 'status': 'FILLED', 'executedQty': '.6', 'avgPrice': '110'}]
    assert await s._close_position('ETHUSDT', p, D('1'), 'TRAILING_STOP', D('110'))
    assert s.binance.place_order.await_args_list[1].kwargs['quantity'] == D('.6')
    assert p.current_quantity == 0
    await asyncio.sleep(0)
    s._cleanup_position_orders.assert_awaited_once()


@pytest.mark.asyncio
async def test_aggressive_unknown_cancel_requires_order_evidence(mtpcs_variant):
    from shared.binance_api import BinanceAPIError
    s, p = aggressive_fixture(mtpcs_variant, 'NEW', '0')
    s.binance.cancel_order.side_effect = BinanceAPIError(-2011, 'unknown order')
    s.binance.get_order.side_effect = [
        {'orderId': 7, 'status': 'NEW', 'executedQty': '0'},
        {'orderId': 7, 'status': 'CANCELED', 'executedQty': '0'}]
    assert not await s._close_position('ETHUSDT', p, D('1'), 'TRAILING_STOP', D('110'))
    assert p.current_quantity == 1
    s._cleanup_position_orders.assert_not_awaited()
    s._cancel_symbol_conditional_orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_pnl_is_written_per_actual_order_quantity(mtpcs_variant):
    s, p = aggressive_fixture(mtpcs_variant)
    s.risk_config['close_limit_order'].update(max_retries=1, retry_interval_seconds=0)
    s.binance.place_order.side_effect = [
        {'orderId':7,'status':'CANCELED','executedQty':'.4','avgPrice':'105'},
        {'orderId':8,'status':'FILLED','executedQty':'.6','avgPrice':'115'}]
    s.binance.trade_logger = actual_ledger()
    s._mark_stop_loss_if_needed = AsyncMock()
    assert await s._close_position('ETHUSDT', p, D('1'), 'TRAILING_STOP', D('110'))
    calls = s.binance.trade_logger.db.execute.await_args_list
    assert [(c.args[9], c.args[1]) for c in calls] == [('7', D('2')), ('8', D('9'))]
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', [1, 2])
async def test_mtpcs_tp_retry_restores_only_stage_residual(mtpcs_variant, stage):
    saved = {}
    ledger = actual_ledger()
    async def persist(sql, strategy, key, data): saved[key] = json.loads(data)
    async def read(sql, strategy, key):
        return {'state_data': saved[key]} if key in saved else None
    db = MagicMock(execute=AsyncMock(side_effect=persist), fetch_one=AsyncMock(side_effect=read))
    def setup():
        s, p = aggressive_fixture(mtpcs_variant)
        s.db_manager = db; s.instance_id = 'test'
        s.binance.trade_logger = ledger
        s._mark_stop_loss_if_needed = AsyncMock()
        p.entry_order_id = 77; p.tp2_order_id = 33; p.atr = D('1'); p.grade = 'A'
        config = {'tp1_close_ratio':'.3','tp2_close_ratio':'.4','remaining_ratio':'.3'}
        s._get_grade_risk = lambda _: {'partial_take_profit':config}
        s._partial_config_for_position = lambda _: config
        s._calculate_tp_price = lambda entry,atr,direction,level,grade: D('105') if level == 1 else D('120')
        return s, p
    s, p = setup()
    if stage == 2:
        p.current_quantity = D('.7'); p.tp1_hit = True
        p.close_progress = {'TP1':D('.3')}
    s.binance.place_order.return_value = {'orderId':7,'status':'CANCELED','executedQty':'.1','avgPrice':'130'}
    price = D('110') if stage == 1 else D('130')
    await s._check_partial_take_profit('ETHUSDT', p, price)
    next_s, restored = setup()
    restored.initial_quantity = restored.current_quantity = p.current_quantity
    residual = D('.2') if stage == 1 else D('.3')
    next_s.binance.place_order.return_value = {'orderId':8,'status':'FILLED','executedQty':str(residual),'avgPrice':'130'}
    await next_s._check_partial_take_profit('ETHUSDT', restored, price)
    assert next_s.binance.place_order.await_args.kwargs['quantity'] == residual
    assert restored.current_quantity == (D('.7') if stage == 1 else D('.3'))
    assert [call.args[9] for call in ledger.db.execute.await_args_list] == ['7', '8']


@pytest.mark.asyncio
async def test_mtpcs_other_close_reason_is_not_tp_progress(mtpcs_variant):
    s, p = aggressive_fixture(mtpcs_variant, 'CANCELED', '.1')
    assert not await s._close_position('ETHUSDT', p, D('1'), 'ALLOCATION_LIMIT', D('110'))
    p.atr = D('1'); p.grade = 'A'
    config = {'tp1_close_ratio':'.3','tp2_close_ratio':'.4','remaining_ratio':'.3'}
    s._get_grade_risk = lambda _: {'partial_take_profit':config}
    s._partial_config_for_position = lambda _: config
    s._calculate_tp_price = lambda entry,atr,direction,level,grade: D('105') if level == 1 else D('120')
    s.binance.place_order.return_value = {'orderId':8,'status':'FILLED','executedQty':'.3','avgPrice':'110'}
    await s._check_partial_take_profit('ETHUSDT', p, D('110'))
    assert s.binance.place_order.await_args.kwargs['quantity'] == D('.3')


@pytest.mark.asyncio
async def test_mtpcs_real_adoption_restores_progress_by_live_tp2_identity(mtpcs_variant):
    s, _ = aggressive_fixture(mtpcs_variant)
    s.instance_id = s.my_record_name = 'test'
    data = {'context':{'account_id':'test','mode':'pm','cutover_ms':None},
            'direction':'LONG','entry_order_id':77,'tp2_order_id':33,
            'initial_quantity':'1','confirmed':{'TP1':'.1'}}
    async def read(sql, strategy, key):
        return {'state_data':data} if key == 'close-progress:ETHUSDT' else None
    s.db_manager = MagicMock(fetch_one=AsyncMock(side_effect=read),execute=AsyncMock())
    s._get_current_price = AsyncMock(return_value=D('100'))
    s._calc_protection_atr = AsyncMock(return_value=D('1'))
    cfg = {'tp1_close_ratio':'.3','tp2_close_ratio':'.4','remaining_ratio':'.3'}
    s._get_grade_risk = lambda _: {'partial_take_profit':cfg}
    s._partial_config_for_position = lambda _: cfg
    s._calculate_tp_price = lambda entry,atr,direction,level,grade: D('105') if level == 1 else D('120')
    orders = {'STOP_LOSS':[{'algo_id':88}],
              'TAKE_PROFIT':[{'algo_id':22,'tp_level':1},{'algo_id':33,'tp_level':2}]}
    with patch(f'{mtpcs_variant.__name__}.resolve_position_owner', new=AsyncMock(return_value='test')):
        p = await s._build_adopted_position_state('ETHUSDT', {'positionAmt':'.9','entryPrice':'100'}, orders)
    assert p is not None and p.entry_order_id is None
    s.binance.place_order.return_value = {'orderId':8,'status':'FILLED','executedQty':'.2','avgPrice':'110'}
    await s._check_partial_take_profit('ETHUSDT', p, D('110'))
    assert s.binance.place_order.await_args.kwargs['quantity'] == D('.2')
    assert p.current_quantity == D('.7')


@pytest.mark.asyncio
async def test_mtpcs_unsaved_progress_blocks_next_tp_and_keeps_actual_pnl(mtpcs_variant):
    s, p = aggressive_fixture(mtpcs_variant, 'CANCELED', '.1')
    s.instance_id = 'test'
    s.db_manager = MagicMock(fetch_one=AsyncMock(return_value=None),
                             execute=AsyncMock(side_effect=RuntimeError('离线保存失败')))
    s.binance.trade_logger = actual_ledger()
    s._mark_stop_loss_if_needed = AsyncMock()
    assert not await s._close_position('ETHUSDT', p, D('.3'), 'TP1', D('110'))
    s.binance.trade_logger.db.execute.assert_awaited_once()
    assert s.binance.trade_logger.db.execute.await_args.args[9] == '7'
    s.binance.place_order.reset_mock()
    assert not await s._close_position('ETHUSDT', p, D('.2'), 'TP1', D('110'))
    s.binance.place_order.assert_not_awaited()


def hrs_fixture():
    h = HRSStrategy.__new__(HRSStrategy)
    h._total_pnl = 0.; h.db = MagicMock(execute=AsyncMock()); h.strategy_name = 'hrs'
    h.config = {}; h.keep_count = 168; h._klines_cache = {}; h._klines_4h_cache = {}
    h._registered_symbols = set(); h._add_position_count = {}; h._paused = False
    h.position_manager = PositionManager({}, MagicMock())
    h.risk_manager = RiskManager({'trading': {'max_drawdown': {'threshold': .15}}})
    h._restore_klines_from_tables = AsyncMock(); h._reregister_klines_services = AsyncMock()
    h._reconcile_positions = AsyncMock()
    h._reconcile_closed_position = AsyncMock()
    return h


@pytest.mark.asyncio
async def test_hrs_automatic_close_counts_real_pnl_and_restores_drawdown():
    h = hrs_fixture()
    h._enforce_allocation_holdings = AsyncMock()
    h._should_unregister = lambda symbol: False
    h.position_manager.cancel_all_orders = AsyncMock(return_value={"failed": 0})
    h.position_manager.add_position('AAVEUSDT', 'long', 100, 10, 2)
    h.binance_client = MagicMock(get_ticker=AsyncMock(return_value={'lastPrice': '80'}))
    h._get_exchange_position_qty = AsyncMock(return_value=0)
    h._writeback_pnl_for_full_close = AsyncMock(return_value=D('-200'))
    with patch('strategies.hrs.strategy.release_claim', new_callable=AsyncMock):
        await h._monitor_positions()
    assert h._total_pnl == -200
    assert not await h.risk_manager.check_drawdown(h._total_pnl, 1000)
    await h._save_state()
    meta = [{'key': call.args[1], 'value': call.args[2]}
            for call in h.db.execute.await_args_list if 'INSERT INTO hrs.hrs_meta' in call.args[0]]
    restored = hrs_fixture()
    restored.db.fetch_all = AsyncMock(side_effect=[[], [], [], meta])
    restored.db.fetch_one = AsyncMock(return_value=None)
    await restored._restore_state()
    assert restored._total_pnl == -200
    assert not await restored.risk_manager.check_drawdown(restored._total_pnl, 1000)


@pytest.mark.asyncio
async def test_hrs_meta_daily_date_survives_without_positions():
    h = hrs_fixture()
    h.risk_manager.can_open_position('short')
    h.risk_manager.record_open('short'); h.risk_manager.record_open('short')
    await h._save_state()
    meta = [{'key': call.args[1], 'value': call.args[2]}
            for call in h.db.execute.await_args_list if 'INSERT INTO hrs.hrs_meta' in call.args[0]]
    restored = hrs_fixture()
    restored.db.fetch_all = AsyncMock(side_effect=[[], [], [], meta])
    restored.db.fetch_one = AsyncMock(return_value=None)
    await restored._restore_state()
    assert not restored.risk_manager.can_open_position('short')


@pytest.mark.asyncio
async def test_hrs_closed_position_accounting_is_idempotent_after_json_restore():
    h = hrs_fixture()
    h.position_manager.add_position('AAVEUSDT', 'short', 100, 10, 2)
    pos = h.position_manager.get_position('AAVEUSDT')
    assert await h._account_position_close('AAVEUSDT', pos, D('-20'), 102, 'test')
    await h._save_state()
    state = next(json.loads(c.args[2]) for c in h.db.execute.await_args_list
                 if 'INSERT INTO hrs.hrs_state' in c.args[0])
    restored = hrs_fixture()
    restored.db.fetch_one = AsyncMock(return_value={'state_data': state})
    await restored._restore_state_from_jsonb_fallback()
    assert await restored._account_position_close('AAVEUSDT', pos, D('-20'), 102, 'recovered')
    assert restored._total_pnl == -20
    assert restored.risk_manager.consecutive_losses == 1


@pytest.mark.asyncio
async def test_hrs_missing_actual_pnl_retains_settlement_identity():
    h = hrs_fixture()
    h._enforce_allocation_holdings = AsyncMock()
    h.position_manager.cancel_all_orders = AsyncMock(return_value={"failed": 0})
    h.position_manager.add_position('AAVEUSDT', 'long', 100, 10, 2)
    h.binance_client = MagicMock(get_ticker=AsyncMock(return_value={'lastPrice': '80'}))
    h._get_exchange_position_qty = AsyncMock(return_value=0)
    h._writeback_pnl_for_full_close = AsyncMock(return_value=None)
    await h._monitor_positions()
    assert h.position_manager.has_position('AAVEUSDT')
    assert h._total_pnl == 0
    assert 'AAVEUSDT' in h._pending_close_pnl


@pytest.mark.asyncio
async def test_hrs_pending_pnl_blocks_entry_but_cycle_still_monitors():
    h = hrs_fixture()
    h._pending_close_pnl = {'AAVEUSDT'}
    h.binance_client = MagicMock(get_ticker=AsyncMock(return_value={'lastPrice':'100'}))
    h._is_shadow_entry_mode = lambda *_: False
    h.trading_executor = MagicMock()
    assert not await h.execute_signal({'symbol':'OTHERUSDT','direction':'short'})
    h.trading_executor.assert_not_called()
    h._is_settlement_skip_window = lambda: False
    h._trim_klines_cache = MagicMock()
    h._cleanup_orphan_orders = AsyncMock()
    h._monitor_positions = AsyncMock()
    h._report_positions_to_dashboard = AsyncMock()
    await h._execute_cycle()
    h._monitor_positions.assert_awaited_once()


@pytest.mark.asyncio
async def test_locked_grid_tick_continues_cleanup_without_new_decisions():
    from strategies.grid.execution.main import ExecutorService
    from strategies.grid.execution.risk import RiskAssessment, RiskStatus
    service = ExecutorService.__new__(ExecutorService)
    service.guard = asyncio.Lock()
    service._risk = AsyncMock(return_value=RiskAssessment(RiskStatus.ALLOW, ()))
    service.store = MagicMock()
    service.store.active_epoch_id.return_value = 1
    service.store.engine_state.return_value = {'state': 'LOCKED'}
    service.store.epoch.return_value = {'stop_loss_low': '1800', 'stop_loss_high': '2200'}
    service.store.pending_decisions.return_value = []
    service.store.next_decision.return_value = None
    service.engine = MagicMock(exit_all=AsyncMock())
    service._write_heartbeat = MagicMock()
    service._drain_backlog = AsyncMock(return_value=True)
    await service.tick()
    service.engine.exit_all.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_locked_grid_protects_residual_even_when_cancel_uncertain(tmp_path):
    from strategies.grid.execution.engine import GridEngine
    from strategies.grid.execution.store import DecisionStore, migrate
    path = tmp_path / 'locked.sqlite'; migrate(path); store = DecisionStore(path)
    epoch = store.create_epoch('d')
    for i in (1, 2):
        store.prepare_order(epoch, i, 'ENTRY', 'BUY', D('1900'), D('.02'), f'eg-{i}')
    store.set_engine_state('LOCKED')
    ex = MagicMock()
    ex.cancel_order = AsyncMock(side_effect=[TimeoutError('cancel unknown'),
                                            {'orderId':2,'status':'CANCELED','executedQty':'0'}])
    ex.position = AsyncMock(return_value=[{'positionSide':'BOTH','positionAmt':'.02'}])
    ex.open_stops = AsyncMock(return_value=[])
    ex.place_stop = AsyncMock(return_value={'algoId':3})
    async def stop(cid):
        return {'algoId':3,'clientAlgoId':cid,'side':'SELL','triggerPrice':'1800','algoStatus':'NEW'}
    ex.query_stop = AsyncMock(side_effect=stop)
    engine = GridEngine(store, ex)
    assert not await engine.on_order_update({'e':'ORDER_TRADE_UPDATE'}, epoch_id=epoch,
        levels=(), filters=None, stop_loss_low=D('1800'), stop_loss_high=D('2200'))
    assert ex.cancel_order.await_count == 2
    ex.place_stop.assert_awaited_once()
    assert store.engine_state()['state'] == 'LOCKED'
    store.close()


@pytest.mark.asyncio
async def test_locked_grid_records_owned_late_fill_and_rejects_unknown_client(tmp_path):
    from strategies.grid.execution.engine import GridEngine
    from strategies.grid.execution.store import DecisionStore, migrate
    path = tmp_path / 'late.sqlite'; migrate(path); store = DecisionStore(path)
    epoch = store.create_epoch('d')
    store.prepare_order(epoch, 1, 'ENTRY', 'BUY', D('1900'), D('.02'), 'eg-owned')
    store.set_engine_state('LOCKED')
    engine = GridEngine(store, MagicMock())
    engine._secure_locked_epoch = AsyncMock(return_value=False)
    event = {'e':'ORDER_TRADE_UPDATE','o':{'s':'ETHUSDT','c':'eg-owned','i':1,
             'S':'BUY','x':'TRADE','X':'FILLED','l':'.02','z':'.02','L':'1900','t':7}}
    kwargs = dict(epoch_id=epoch, levels=(), filters=None, stop_loss_low=D('1800'), stop_loss_high=D('2200'))
    await engine.on_order_update(event, **kwargs)
    await engine.on_order_update(event, **kwargs)
    assert len(store.all_fills()) == 1
    event['o']['c'] = 'eg-foreign'
    await engine.on_order_update(event, **kwargs)
    assert len(store.all_fills()) == 1
    store.close()


@pytest.mark.asyncio
async def test_grid_start_failure_cancels_known_orders_immediately(tmp_path):
    from strategies.grid.execution.engine import GridEngine
    from strategies.grid.execution.planner import GridPlan, PlannedOrder
    from strategies.grid.execution.risk import RiskAssessment, RiskStatus
    from strategies.grid.execution.store import DecisionStore, migrate
    path = tmp_path / 'grid.sqlite'; migrate(path); store = DecisionStore(path)
    now = 1_800_000_000_000
    store.publish({'symbol':'ETHUSDT','state':'OSCILLATION','calculated_at_ms':now-1000,
                   'last_closed_kline_ms':{tf:now-60000 for tf in ('15m','1h','4h')},
                   'executable':True,'feasibility':{'feasible':True},
                   'grid_params':{'stop_loss_low':'1800','stop_loss_high':'2200'}}, 'hash')
    plan = GridPlan(tuple(PlannedOrder(i, side, D(price), D('.02')) for i,side,price in
                          [(0,'BUY','1900'),(2,'SELL','2100')]),
                    (D('1900'),D('2000'),D('2100')), D('80'),D('8'))
    ex = MagicMock(open_orders=AsyncMock(return_value=[]), open_stops=AsyncMock(return_value=[]),
                   position=AsyncMock(return_value=[{'positionSide':'BOTH','positionAmt':'0'}]),
                   verify_account=AsyncMock(),account_info=AsyncMock(return_value={'totalAvailableBalance':'100'}))
    ex.place_limit = AsyncMock(side_effect=[{'orderId':1,'status':'NEW'}, TimeoutError('unknown POST')])
    ex.cancel_order = AsyncMock(side_effect=[{'orderId':1,'status':'CANCELED','executedQty':'0'},
                                           {'orderId':2,'status':'CANCELED','executedQty':'0'}])
    ex.query_order = AsyncMock(side_effect=lambda cid: {'orderId':1 if store.get_order(cid)['level_index']==0 else 2,
                                                       'status':'CANCELED','executedQty':'0'})
    engine = GridEngine(store, ex)
    assert not await engine.start_grid(store.next_decision(), plan, RiskAssessment(RiskStatus.ALLOW,()),
                                       leverage=10,now_ms=now)
    assert ex.cancel_order.await_count == 2
    assert store.engine_state()['state'] == 'LOCKED'
    store.close()


@pytest.mark.asyncio
async def test_progress_database_timeout_does_not_hold_risk_exit(mtpcs_variant, monkeypatch):
    from shared import mtpcs_close_progress
    s, p = aggressive_fixture(mtpcs_variant, 'FILLED', '1')
    s.instance_id = 'btc_eth'
    async def stalled(*args):
        await asyncio.Event().wait()
    s.db_manager = MagicMock(fetch_one=AsyncMock(side_effect=stalled), execute=AsyncMock(side_effect=stalled))
    monkeypatch.setattr(mtpcs_close_progress, 'IO_TIMEOUT_SECONDS', .01, raising=False)
    s._write_confirmed_close_pnl = AsyncMock()
    assert await asyncio.wait_for(s._close_position('ETHUSDT', p, D('1'), 'TRAILING_STOP', D('110')), .2)
    assert p.current_quantity == 0
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_progress_save_failure_does_not_abandon_risk_exit_residual(mtpcs_variant, monkeypatch):
    s, p = aggressive_fixture(mtpcs_variant, 'CANCELED', '.4')
    s.risk_config['close_limit_order'].update(max_retries=1, retry_interval_seconds=0)
    s.binance.place_order.side_effect = [
        {'orderId':7, 'status':'CANCELED', 'executedQty':'.4', 'avgPrice':'110'},
        {'orderId':8, 'status':'FILLED', 'executedQty':'.6', 'avgPrice':'110'}]
    monkeypatch.setattr(mtpcs_variant, 'save_close_progress', AsyncMock(return_value=False))
    s._write_confirmed_close_pnl = AsyncMock()
    assert await s._close_position('ETHUSDT', p, D('1'), 'TRAILING_STOP', D('110'))
    assert [c.kwargs['quantity'] for c in s.binance.place_order.await_args_list] == [D('1'), D('.6')]
    assert p.current_quantity == 0
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_unconfirmed_tp_order_blocks_duplicate_across_cycles_and_restart(mtpcs_variant):
    s, p = aggressive_fixture(mtpcs_variant, 'NEW', '0')
    p.entry_order_id = 11
    s.instance_id = 'btc_eth'
    saved = {}
    async def persist(sql, strategy, key, data):
        saved[key] = json.loads(data)
        return 'INSERT 0 1'
    async def read(sql, strategy, key):
        return {'state_data': saved[key]} if key in saved else None
    s.db_manager = MagicMock(execute=AsyncMock(side_effect=persist), fetch_one=AsyncMock(side_effect=read))
    s._write_confirmed_close_pnl = AsyncMock()
    s.binance.get_order.side_effect = OSError('query unavailable after accepted limit order')
    assert not await s._close_position('ETHUSDT', p, D('.3'), 'TP1', D('110'))
    pending = saved['close-progress:ETHUSDT']['pending_orders']
    assert list(pending) == ['7']
    assert D(pending['7']['quantity']) == D('.3')
    open_order = dict(orderId=7, symbol='ETHUSDT', side='SELL', reduceOnly=True,
                      origQty='.3', executedQty='.1', status='PARTIALLY_FILLED', avgPrice='110')
    s.binance.get_order = AsyncMock(return_value=open_order)
    assert not await s._close_position('ETHUSDT', p, D('.3'), 'TP1', D('110'))
    assert s.binance.place_order.await_count == 1
    # Restore the same position into a fresh object, then resolve that exact old order.
    restored = mtpcs_variant.PositionState()
    restored.direction = 'LONG'; restored.entry_order_id = 11
    restored.entry_price = D(100); restored.initial_quantity = D(1); restored.current_quantity = D('.9')
    terminal = dict(open_order, status='CANCELED')
    s.binance.get_order.side_effect = lambda symbol, oid: terminal if oid == 7 else dict(
        orderId=8, symbol=symbol, side='SELL', reduceOnly=True, origQty='.2', executedQty='.2',
        status='FILLED', avgPrice='110')
    s.binance.place_order.return_value = dict(orderId=8, status='FILLED', executedQty='.2', avgPrice='110')
    assert await s._close_position('ETHUSDT', restored, D('.3'), 'TP1', D('110'))
    assert s.binance.place_order.await_args.kwargs['quantity'] == D('.2')
    assert restored.close_progress['TP1'] == D('.3')
    assert restored.current_quantity == D('.7')
    assert saved['close-progress:ETHUSDT']['pending_orders'] == {}


@pytest.mark.asyncio
async def test_unresolved_tp_does_not_block_confirmed_risk_reduction(mtpcs_variant):
    from strategies.btc_eth_aggressive.close_evidence import remember_close_order
    s, p = aggressive_fixture(mtpcs_variant, 'FILLED', '1')
    remember_close_order(s, 'ETHUSDT', p, 6, D('.3'), 'TP1')
    s.binance.place_order.return_value = dict(orderId=7, status='FILLED', executedQty='1', avgPrice='110')
    async def query(symbol, order_id):
        if order_id == 6:
            raise OSError('previous TP status unavailable')
        return dict(orderId=7, status='FILLED', executedQty='1', origQty='1',
                    symbol=symbol, side='SELL', reduceOnly=True, avgPrice='110')
    s.binance.get_order.side_effect = query
    assert await s._close_position('ETHUSDT', p, D('1'), 'TRAILING_STOP', D('110'))
    assert p.current_quantity == 0
    assert s.binance.place_order.await_args.kwargs['reduce_only'] is True
    s._cancel_symbol_conditional_orders.assert_not_awaited()
    await asyncio.sleep(0)
