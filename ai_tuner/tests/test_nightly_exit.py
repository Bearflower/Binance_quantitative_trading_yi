from decimal import Decimal as D
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_tuner.reconciler.nightly_exit import repair_order, verify_fill


ORDER = dict(symbol='ARIAUSDT', orderId=123, side='BUY', status='FILLED', executedQty='3')
ROW = dict(side='BUY', qty=D(1), price=D(2), realized_pnl=D('0.5'), incomes=[D('0.5')],
           commission_asset='USDT', commission=D('.01'), trade_time_utc=1)


def test_partial_fills_are_summed_once():
    values = verify_fill(ORDER, [ROW, dict(ROW, qty=D(2), price=D(5))], 'ARIAUSDT', 'BUY', 123)
    assert values == (3, 4, D('-.02'), 1, 1)


@pytest.mark.parametrize('change', [dict(qty=D(2)), dict(incomes=[]), dict(incomes=[D(1)]),
                                    dict(side='SELL'), dict(commission_asset='BNB')])
def test_incomplete_evidence_blocks_insert(change):
    with pytest.raises(ValueError):
        verify_fill(ORDER, [dict(ROW, qty=D(3)) | change], 'ARIAUSDT', 'BUY', 123)


@pytest.mark.asyncio
@pytest.mark.parametrize('duplicate,conflict', [(False, False), (True, False), (False, True)])
async def test_exact_parent_and_duplicate_guard(duplicate, conflict):
    rec = MagicMock(account_id='default', excluded_strategies=[])
    owner = dict(strategy='hrs', algo_id='1', side='BUY')
    rec.db.fetch_all = AsyncMock(return_value=[owner])
    rec.binance.get_algo_order = AsyncMock(return_value=dict(actualOrderId='123', symbol='ARIAUSDT',
        side='BUY', algoStatus='FINISHED', orderType='TAKE_PROFIT_MARKET'))
    rec.binance.get_order = AsyncMock(return_value=ORDER)
    conn = MagicMock()
    rec.db.pool.acquire.return_value.__aenter__.return_value = conn
    conn.fetch = AsyncMock(side_effect=[[owner,dict(owner,strategy='other')] if conflict else [owner],
                                         [dict(ROW, qty=D(3))]])
    conn.fetchval = AsyncMock(return_value=int(duplicate))
    conn.execute = AsyncMock()
    gap = dict(symbol='ARIAUSDT', order_id=123, side='BUY')
    if conflict:
        with pytest.raises(ValueError):
            await repair_order(rec, gap)
    else:
        assert await repair_order(rec, gap) is (not duplicate)
    writes = [c for c in conn.execute.await_args_list if 'INSERT INTO' in c.args[0]]
    assert len(writes) == int(not duplicate and not conflict)
