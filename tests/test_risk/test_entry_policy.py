from decimal import Decimal as D
import importlib.util
import pytest


def test_policy_module_exists():
    assert importlib.util.find_spec('shared.entry_risk_policy') is not None


def test_pressure_sizing_floor_and_immutable_input():
    from shared.entry_risk_policy import size_reduced_entry
    signal = dict(quantity=D('10.03'), entry_price=D('100.034'), leverage=5,
                  initial_stop_loss=D('95'), direction='LONG')
    filters = dict(stepSize='0.1', tickSize='0.05', minQty='0.1', minNotional='5')
    sized = size_reduced_entry(signal, filters, D('.5'), D('.002'), [D('.3'), D('.3')])
    assert sized.quantity == D('5.0')
    assert sized.price == D('100.05')
    assert sized.risk <= sized.original_risk * D('.5')
    assert signal['quantity'] == D('10.03')
    assert size_reduced_entry(signal, filters, D('.5'), D('.002'), [D('.3'), D('.3')]) == sized


@pytest.mark.parametrize('field,value', [('quantity','NaN'), ('leverage','0'), ('initial_stop_loss','105')])
def test_invalid_signal_rejected(field,value):
    from shared.entry_risk_policy import size_reduced_entry, EntryDenied
    signal = dict(quantity='10', entry_price='100', leverage=5, initial_stop_loss='95', direction='LONG')
    signal[field] = value
    with pytest.raises(EntryDenied):
        size_reduced_entry(signal, dict(stepSize='.1',tickSize='.01',minQty='.1',minNotional='5'), D('.5'), D('.002'),[D('.3'),D('.3')])


def test_small_tp_leg_rejected_without_roundup():
    from shared.entry_risk_policy import size_reduced_entry, EntryDenied
    with pytest.raises(EntryDenied):
        size_reduced_entry(dict(quantity='1',entry_price='100',leverage=5,initial_stop_loss='95',direction='LONG'),dict(stepSize='1',tickSize='.01',minQty='1',minNotional='5'),D('.5'),D('.002'),[D('.3'),D('.3')])

@pytest.mark.asyncio
async def test_hrs_all_entry_modes_and_direct_add_deny_without_database():
    from strategies.hrs.strategy import HRSStrategy
    obj=object.__new__(HRSStrategy)
    for mode in ('standard','lv_rm'):
        assert await obj.execute_signal({'entry_mode':mode}) is False
    assert await obj._handle_add_position('AAA','SHORT',10,100,[]) is False


@pytest.mark.asyncio
async def test_aggressive_direct_entry_and_add_deny_before_any_exchange_writer():
    from strategies.btc_eth_aggressive.strategy import BTCEthStrategy
    obj=object.__new__(BTCEthStrategy)
    assert await obj.execute_signal({}) is False
    assert await obj._place_entry_order('AAA',{}) is None
    assert await obj._add_position('AAA',None,{}) is False


@pytest.mark.asyncio
async def test_shared_final_gate_cannot_be_bypassed():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from shared.protection_retry import place_and_wait_entry_order
    strategy=SimpleNamespace(my_record_name='btc_eth_aggressive',binance=AsyncMock())
    assert await place_and_wait_entry_order(strategy,'AAA',{}) is None
    strategy.binance.place_order.assert_not_called()


def test_short_actual_tp_limit_notional_is_checked():
    from shared.entry_risk_policy import size_reduced_entry, EntryDenied
    with pytest.raises(EntryDenied):
        size_reduced_entry(dict(quantity='10',entry_price='100',leverage=5,initial_stop_loss='105',direction='SHORT',tp1_price='1',tp2_price='2'),dict(stepSize='.1',tickSize='.01',minQty='.1',minNotional='5'),D('.5'),D('.002'),[D('.3'),D('.3')],tp_offset=D('.0015'))

@pytest.mark.asyncio
async def test_hrs_executor_direct_writers_deny():
    from strategies.hrs.executor import TradingExecutor
    from unittest.mock import AsyncMock
    obj=object.__new__(TradingExecutor)
    obj.binance_client=AsyncMock()
    obj._get_symbol_precision=AsyncMock(side_effect=RuntimeError("must not reach entry preparation"))
    for method in (obj.execute_short,obj.execute_long):
        for mode in ('standard','lv_rm'):
            result=await method('AAA',100,2,D(1),80,entry_mode=mode)
            assert not result.order_filled
    assert await obj.add_to_position('AAA','short',100,2,D(1),80) is None
    obj._get_symbol_precision.assert_not_called()
    obj.binance_client.set_leverage.assert_not_called()
    obj.binance_client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_confirmed_partial_fill_survives_cancel_failure():
    from shared.order_fill_waiter import wait_order_final_state
    from unittest.mock import AsyncMock
    client=AsyncMock()
    client.get_order.return_value={'status':'PARTIALLY_FILLED','executedQty':'2','origQty':'5','avgPrice':'100','orderId':123}
    client.cancel_order.side_effect=TimeoutError('cancel result unknown')
    result=await wait_order_final_state(client,'AAA',order_id=123,timeout_seconds=.001,check_interval=.002,visibility_delay=0,preserve_confirmed=True)
    assert result.executed_qty==D(2)
    assert result.remaining_qty==D(3)
    assert result.status=='UNKNOWN'

@pytest.mark.asyncio
async def test_empty_position_monitor_always_reconciles_reservations():
    from strategies.btc_eth_aggressive.strategy import BTCEthStrategy
    from unittest.mock import AsyncMock,patch
    obj=object.__new__(BTCEthStrategy)
    obj.positions={}
    obj._cycle_count=0
    obj._unverified_entry_time_symbols=set()
    obj._untracked_stop_retry_symbols=set()
    for method in ('_enforce_allocation_holdings','_check_legacy_untracked_time_stop','_retry_pending_cancellations','_cleanup_residual_orders','_retry_rebuild_pending','_retry_protection_pending','_maybe_cleanup_expired_claims'):
        setattr(obj,method,AsyncMock())
    with patch('strategies.btc_eth_aggressive.strategy.recover_reserved_protection',new_callable=AsyncMock) as recovery:
        await obj.update_positions()
        recovery.assert_awaited_once_with(obj)

@pytest.mark.asyncio
async def test_submission_confirmed_fill_survives_first_read_failure():
    from shared.order_fill_waiter import wait_order_final_state
    from unittest.mock import AsyncMock
    client=AsyncMock()
    client.get_order.side_effect=TimeoutError('query unavailable')
    result=await wait_order_final_state(client,'AAA',order_id=123,timeout_seconds=.001,visibility_delay=0,preserve_confirmed=True,
        initial_order={'status':'PARTIALLY_FILLED','executedQty':'2','origQty':'5','avgPrice':'100','orderId':123})
    assert result.executed_qty==D(2)
