from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from ai_tuner.monitor.protective_exit_evidence import read_protective_exits, verify_protective_exit


@pytest.fixture
def example():
    row = dict(account_id='default', strategy='btc_eth_aggressive', symbol='SOLUSDT',
               order_id=18, algo_id='17', actual_order_id='18', payload_sha256='a' * 64,
               entry_order_id=16, episode_side='SELL', episode_qty='0.08', episode_trade_ids=['19'],
               fills=[dict(id='19', time=201, qty='0.080', side='SELL', price='12.00')])
    algo = dict(symbol='SOLUSDT', algoId=17, actualOrderId='18', reduceOnly=True,
                positionSide='BOTH', side='SELL', algoStatus='FINISHED', orderType='STOP_MARKET')
    order = dict(symbol='SOLUSDT', orderId=18, status='FILLED', side='SELL',
                 positionSide='BOTH', reduceOnly=True, executedQty='0.08', origQty='0.08')
    trades = [dict(symbol='SOLUSDT', orderId=18, side='SELL', id=19, time=201,
                   positionSide='BOTH', qty='0.08', price='12')]
    return row, algo, order, trades


def test_exact_stop_and_take_profit_are_distinguished(example):
    row, algo, order, trades = example
    assert verify_protective_exit(*example)['entry_order_id'] == '16'
    row.update(strategy='hybrid_reversal', entry_order_id=None)
    algo['orderType'] = 'TAKE_PROFIT_MARKET'
    outcome = verify_protective_exit(row, algo, order, trades)
    assert outcome['entry_order_id'] is None
    assert outcome['trade_ids'] == ['19']


@pytest.mark.parametrize('source,field,value', [
    (0, 'actual_order_id', '180'), (1, 'algoStatus', 'NEW'),
    (1, 'actualOrderId', '180'), (1, 'reduceOnly', False),
    (2, 'reduceOnly', False), (2, 'positionSide', 'SHORT'),
    (2, 'origQty', '0.09'), (3, 'id', 190), (3, 'time', 202),
    (3, 'qty', '0.09'), (3, 'positionSide', 'SHORT'), (3, 'price', '13'),
])
def test_unproven_order_or_trade_is_rejected(example, source, field, value):
    changed = deepcopy(example)
    target = changed[source][0] if source == 3 else changed[source]
    target[field] = value
    with pytest.raises(ValueError):
        verify_protective_exit(*changed)


@pytest.mark.asyncio
async def test_exact_reduction_positions_are_window_scoped(example):
    row, algo, order, trades = example
    db = AsyncMock()
    db.fetch_all.return_value = [row]
    exchange = AsyncMock()
    exchange.get_algo_order.return_value = algo
    exchange.get_order.return_value = order
    exchange.get_user_trades.return_value = trades
    report, keys = await read_protective_exits(db, exchange, 'default', 200, 300)
    assert report['links'][0]['entry_order_id'] == '16'
    assert report['scope'] == 'known_pm_protective_orders'
    assert not report['issues']
    assert keys == {('default', 'SOLUSDT', '19', '18', 201)}
    _, keys = await read_protective_exits(db, exchange, 'default', 100, 200)
    assert not keys
