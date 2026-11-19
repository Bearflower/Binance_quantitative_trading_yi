from copy import deepcopy

import pytest

from ai_tuner.monitor.hrs_closed_lot import verify_closed_lot


def trade(order, identity, stamp, side, price, pnl):
    return {'orderId': order, 'id': identity, 'time': stamp, 'side': side,
            'symbol': 'SUIUSDT', 'positionSide': 'BOTH', 'price': price, 'qty': '2',
            'commissionAsset': 'USDT', 'commission': '.01', 'realizedPnl': pnl}


@pytest.fixture
def sample():
    history = [trade(17, 51, 100, 'BUY', '1', '0'), trade(18, 52, 200, 'SELL', '1.1', '.2')]
    link = dict(symbol='SUIUSDT', kind='TAKE_PROFIT_MARKET', order_id='18',
                trade_times_ms={'52': 200}, strategy='hrs')
    order = dict(symbol='SUIUSDT', orderId=17, status='FILLED', side='BUY', reduceOnly=False,
                 positionSide='BOTH', origQty='2', executedQty='2', clientOrderId='risk_proof')
    return history, link, order, 125, 10, 250


def test_closed_lot_requires_exact_order_and_yields_actual_cost_stress(sample):
    lot = verify_closed_lot(*sample)
    assert lot['entry_order_id'] == '17'
    assert lot['entry_trade_times_ms'] == {'51': 100}
    assert lot['execution_net_usdt'] == '0.18'
    assert lot['execution_stressed_net_usdt'] == '0.1616'


@pytest.mark.parametrize('failure', ['extra_order', 'anchor_boundary_fill', 'hedge_fill', 'partial_lot',
                                     'wrong_algo_time', 'out_of_range', 'truncated', 'entry_reduce_only'])
def test_ambiguous_closure_stays_unverified(sample, failure):
    history, link, order, created, left, flat = deepcopy(sample)
    if failure == 'extra_order':
        history.insert(1, trade(99, 53, 150, 'BUY', '1', '0'))
    elif failure == 'anchor_boundary_fill':
        history.append(trade(99, 53, 250, 'BUY', '1', '0'))
    elif failure == 'hedge_fill':
        history[0]['positionSide'] = 'LONG'
    elif failure == 'partial_lot':
        history[1]['qty'] = '1'
    elif failure == 'wrong_algo_time':
        created = 80
    elif failure == 'out_of_range':
        history[0]['time'] = 9
    elif failure == 'truncated':
        history *= 500
    else:
        order['reduceOnly'] = True
    with pytest.raises(ValueError):
        verify_closed_lot(history, link, order, created, left, flat)
