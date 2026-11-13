from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from shared.accounting.historical_funding import recover


def test_handover_isolated_by_account_and_time():
    start = datetime(2026, 11, 12, tzinfo=timezone.utc)
    at = lambda n: start + timedelta(minutes=n)
    base = dict(account_id='default', symbol='ETHUSDT')
    anchor = dict(base, flat_at=at(9), exchange_qty=0)
    event = dict(base, income_type='FUNDING_FEE', asset='USDT', income=D('-0.00001965'), tran_id=9)
    fills = [dict(base, order_id=i, trade_id=i, side=side, qty=D('.035'), trade_time_utc=at(t))
             for i,(side,t) in enumerate([('BUY',1),('SELL',3),('SELL',5),('BUY',7)],1)]
    owners = {str(f['order_id']): dict(f,strategy='original' if f['order_id']<=2 else 'aggressive') for f in fills}
    fills += [dict(f, account_id='grid') for f in fills]
    coverage = [dict(base,source='userTrades',income_type=None,page_complete=True,request_status='ok',
                     window_start_ms=int(at(0).timestamp()*1000),covered_until_ms=int(at(9).timestamp()*1000))]
    def check(t, c=coverage):
        return recover(dict(event,event_time_utc=at(t)),anchor,fills,owners,c)
    assert check(2)[0]['strategy']=='original'
    assert check(6)[0]['strategy']=='aggressive'
    assert check(5)[0] is None
    assert check(2, [dict(coverage[0],account_id='grid')])[0] is None
    del owners['1']
    assert check(2)[0] is None
