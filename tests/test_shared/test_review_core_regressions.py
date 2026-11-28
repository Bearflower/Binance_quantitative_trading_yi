"""CR-009/010/024/029 离线回归；所有交易所请求均使用模拟对象。"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
import copy
import importlib
import pytest
from shared.binance_api import BinanceAPIError, BinanceClient
from shared.trade_logger import TradeLogger
from tests.test_shared.test_reduce_only_close import FakeExchange, _close


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['CANCELED', 'EXPIRED'])
@pytest.mark.parametrize('quantity', ['0.4', '1e-1000', None, 'NaN', 'bad'])
async def test_cancel_missing_keeps_partial_or_unknown_entry(status, quantity):
    client = BinanceClient('offline', 'offline')
    client.trade_logger = MagicMock(mark_entry_canceled=AsyncMock())
    client._request = AsyncMock(side_effect=BinanceAPIError(-2011, 'not found'))
    order = {'status': status, 'orderId': 123, 'symbol': 'BTCUSDT'}
    if quantity is not None:
        order['executedQty'] = quantity
    client.get_order = AsyncMock(return_value=order)
    with pytest.raises(BinanceAPIError):
        await client.cancel_order('BTCUSDT', order_id=123)
    client.trade_logger.mark_entry_canceled.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['CANCELED', 'EXPIRED'])
async def test_cancel_missing_marks_only_verified_zero(status):
    client = BinanceClient('offline', 'offline')
    client.trade_logger = MagicMock(mark_entry_canceled=AsyncMock())
    client._request = AsyncMock(side_effect=BinanceAPIError(-2011, 'not found'))
    client.get_order = AsyncMock(return_value={'status': status, 'executedQty': '0', 'orderId': 123, 'symbol': 'BTCUSDT'})
    with pytest.raises(BinanceAPIError):
        await client.cancel_order('BTCUSDT', order_id=123)
    client.trade_logger.mark_entry_canceled.assert_awaited_once_with('123', 'BTCUSDT')


def fill_evidence():
    row = {'id': 1, 'symbol': 'BTCUSDT', 'order_id': '123', 'side': 'BUY'}
    order = {'symbol': 'BTCUSDT', 'orderId': 123, 'side': 'BUY', 'status': 'FILLED',
             'executedQty': '0.3', 'origQty': '0.3'}
    trades = [dict(symbol='BTCUSDT', orderId=123, side='BUY', id=1, qty='0.1', price='100'),
              dict(symbol='BTCUSDT', orderId=123, side='BUY', id=2, qty='0.2', price='103')]
    return row, order, trades


def test_entry_fill_uses_complete_identified_trades():
    helper = importlib.import_module('shared.entry_fill_evidence')
    assert helper.verified_fill(*fill_evidence()) == (Decimal('0.3'), Decimal('102'))


@pytest.mark.parametrize('mutation', ['wrong_order', 'wrong_symbol', 'wrong_side', 'duplicate',
                                    'missing_fill', 'nonfinite', 'zero_price', 'live_order', 'full_page'])
def test_entry_fill_rejects_unproven_evidence(mutation):
    helper = importlib.import_module('shared.entry_fill_evidence')
    row, order, trades = fill_evidence()
    if mutation == 'wrong_order': trades[0]['orderId'] = 999
    if mutation == 'wrong_symbol': order['symbol'] = 'ETHUSDT'
    if mutation == 'wrong_side': trades[0]['side'] = 'SELL'
    if mutation == 'duplicate': trades[1]['id'] = 1
    if mutation == 'missing_fill': trades.pop()
    if mutation == 'nonfinite': trades[0]['qty'] = 'NaN'
    if mutation == 'zero_price': trades[0]['price'] = '0'
    if mutation == 'live_order': order['status'] = 'PARTIALLY_FILLED'
    if mutation == 'full_page': trades = [copy.deepcopy(trades[0]) for _ in range(1000)]
    with pytest.raises(ValueError): helper.verified_fill(row, order, trades)


@pytest.mark.asyncio
async def test_reconcile_entry_fills_writes_proven_vwap():
    row, order, trades = fill_evidence()
    db = MagicMock(fetch_all=AsyncMock(return_value=[row]), execute=AsyncMock(return_value='UPDATE 1'))
    client = MagicMock(get_order=AsyncMock(return_value=order), get_user_trades=AsyncMock(return_value=trades))
    logger = TradeLogger(db, 'test', account_id='offline')
    evidence = await logger.reconcile_entry_fills(client)
    assert evidence == [{'id': 1, 'quantity': Decimal('0.3'), 'price': Decimal('102')}]
    assert db.execute.await_args.args[1:3] == (Decimal('0.3'), Decimal('102'))


@pytest.mark.asyncio
@pytest.mark.parametrize('position', ['1', '-1'])
async def test_partial_close_target_is_success_without_flattening(position):
    client = FakeExchange(position, [{'executed': '0.3', 'status': 'FILLED'}])
    outcome = await _close(client, '0.3')
    assert outcome.success
    assert outcome.closed_qty == Decimal('0.3')
    assert abs(client.position) == Decimal('0.7')
    assert len(client.submissions) == 1


@pytest.mark.asyncio
async def test_partial_micro_target_does_not_flatten_large_position():
    client = FakeExchange('1', [], step='0.1')
    outcome = await _close(client, '0.05')
    assert not outcome.success
    assert client.submissions == []
    assert client.position == 1


@pytest.mark.asyncio
async def test_partial_close_requires_position_confirmation():
    client = FakeExchange('1', [{'executed': '0.3', 'status': 'FILLED'}])
    client.get_position = AsyncMock(return_value=[{'symbol': 'BTCUSDT', 'positionAmt': '1'}])
    outcome = await _close(client, '0.3')
    assert not outcome.success
    assert len(client.submissions) == 1


@pytest.mark.asyncio
async def test_partial_close_does_not_borrow_full_close_dust_tolerance():
    client = FakeExchange('1.05', [{'executed': '1', 'status': 'FILLED'}], step='0.1')
    client.get_position = AsyncMock(side_effect=[
        [{'symbol': 'BTCUSDT', 'positionAmt': '1.05'}],
        [{'symbol': 'BTCUSDT', 'positionAmt': '1.05'}],
        *[[{'symbol': 'BTCUSDT', 'positionAmt': '0.09'}]] * 10,
    ])
    outcome = await _close(client, '1')
    assert not outcome.success


@pytest.mark.asyncio
@pytest.mark.parametrize('pm', [True, False])
@pytest.mark.parametrize('bad', ['missing', None, 'NaN', 'Infinity', '-Infinity'])
async def test_account_info_rejects_missing_or_nonfinite_equity(pm, bad):
    client = BinanceClient('offline', 'offline', use_unified_account=pm)
    data = ({'accountEquity': '0', 'totalAvailableBalance': '0'} if pm else
            {name: '0' for name in ('totalMarginBalance', 'totalWalletBalance', 'totalUnrealizedProfit', 'availableBalance')})
    key = 'accountEquity' if pm else 'totalMarginBalance'
    if bad == 'missing': data.pop(key)
    else: data[key] = bad
    client._request = AsyncMock(return_value=data)
    with pytest.raises(ValueError): await client.get_account_info()


@pytest.mark.asyncio
@pytest.mark.parametrize('pm', [True, False])
async def test_account_info_preserves_verified_zero(pm):
    client = BinanceClient('offline', 'offline', use_unified_account=pm)
    data = ({'accountEquity': '0', 'totalAvailableBalance': '0'} if pm else
            {name: '0' for name in ('totalMarginBalance', 'totalWalletBalance', 'totalUnrealizedProfit', 'availableBalance')})
    client._request = AsyncMock(return_value=data)
    result = await client.get_account_info()
    assert result['totalMarginBalance'] == Decimal('0')
    assert result['availableBalance'] == Decimal('0')
