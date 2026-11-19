from copy import deepcopy

import pytest

from ai_tuner.monitor.ordinary_execution_evidence import verify_ordinary_execution


def evidence():
    row = dict(account_id='default', strategy='new_coin', symbol='ETNUSDT', order_id=320067,
               stored_side='SELL', fills=[dict(id='8898', time=100, side='SELL', qty='0.29', price='300')])
    order = dict(symbol='ETNUSDT', orderId=320067, side='SELL', status='FILLED',
                 origQty='0.29', executedQty='0.29', reduceOnly=False, positionSide='BOTH')
    trades = [dict(symbol='ETNUSDT', orderId=320067, side='SELL', positionSide='BOTH',
                   id=8898, time=100, qty='0.29', price='300', commission='0.04',
                   commissionAsset='USDT', realizedPnl='0')]
    return row, order, trades


def test_verified_regular_close_and_other_strategy_fill():
    row, order, trades = evidence()
    assert verify_ordinary_execution(row, order, trades)['classification'] == 'known_strategy_execution'
    order['reduceOnly'] = True
    trades[0]['realizedPnl'] = '0.3'
    link = verify_ordinary_execution(row, order, trades)
    assert link['classification'] == 'ordinary_reduce_only'
    assert link['trade_times_ms'] == {'8898': 100}
    assert link['execution_net_usdt'] == '0.26'


@pytest.mark.parametrize('patch', [{'symbol': 'OTHERUSDT'}, {'reduceOnly': None}, {'origQty': '0.30'},
                                  {'positionSide': 'SHORT'}, {'status': 'NEW'}])
def test_unknown_order_does_not_establish_close_or_other_owner(patch):
    row, order, trades = deepcopy(evidence())
    with pytest.raises(ValueError):
        verify_ordinary_execution(row, dict(order, **patch), trades)


def test_disagreement_with_collected_trade_remains_unknown():
    row, order, trades = evidence()
    trades[0]['time'] += 1
    with pytest.raises(ValueError):
        verify_ordinary_execution(row, order, trades)
