from copy import deepcopy
from decimal import Decimal

import pytest

from ai_tuner.monitor.um_margin_evidence import reconcile_um_margin


def fixture():
    def margin(pos, orders):
        return dict(positionInitialMargin=str(pos), openOrderInitialMargin=str(orders),
                    initialMargin=str(Decimal(pos) + Decimal(orders)))
    detail = {
        'assets': [dict(asset='USDT', **margin('65.06985150','0'))],
        'positions': [
            dict(symbol=symbol, positionSide='BOTH', positionAmt=qty, updateTime=100+i,
                 **margin(allocated,'0'))
            for i, (symbol, qty, allocated) in enumerate([
                ('ETHUSDT','0.012','5.41578000'), ('BNBUSDT','0.04','5.90470000'),
                ('WLDUSDT','28','3.67500000'), ('ETNUSDT','-0.29','50.07437150'),
            ])
        ],
    }
    primary = [{k: r[k] for k in ('symbol','positionSide','positionAmt','updateTime')}
               for r in detail['positions']]
    return primary, detail


def test_exchange_position_margin_sums_separately_from_order_margin():
    pos, detail = fixture()
    report = reconcile_um_margin(pos, detail, {'BNBUSDT','ETHUSDT'})
    assert report['status'] == 'VERIFIED'
    assert report['owned_position_initial_margin_usdt'] == '11.32048000'
    assert report['account_um_position_initial_margin_usdt'] == '65.06985150'
    assert report['account_um_open_order_initial_margin_usdt'] == '0'


@pytest.mark.parametrize('fault', ['missing_field','quantity','time','asset_sum','unknown_asset',
                                   'duplicate','missing_owned_position','negative'])
def test_unverified_margin_cannot_become_a_numeric_amount(fault):
    pos, detail = fixture()
    owns = {'WLDUSDT'}
    row = detail['positions'][2]
    if fault == 'missing_field':
        row.pop('positionInitialMargin')
    elif fault == 'quantity':
        row['positionAmt'] = '29'
    elif fault == 'time':
        row['updateTime'] += 1
    elif fault == 'asset_sum':
        detail['assets'][0]['positionInitialMargin'] = '65.06985151'
    elif fault == 'unknown_asset':
        detail['assets'].append(dict(detail['assets'][0],asset='USDC'))
    elif fault == 'duplicate':
        detail['positions'].append(deepcopy(row))
    elif fault == 'missing_owned_position':
        owns.add('BTCUSDT')
    elif fault == 'negative':
        row['positionInitialMargin'] = '-1'
    report = reconcile_um_margin(pos, detail, owns)
    assert report['status'] == 'BLOCKED_DATA'
    assert report['owned_position_initial_margin_usdt'] is None
