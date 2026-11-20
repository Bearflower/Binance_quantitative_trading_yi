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
    del owners['2']
    assert check(2)[0]['strategy']=='original'
    owners['2'] = dict(fills[1], strategy='aggressive')
    assert check(2)[0] is None
    del owners['2']
    del owners['1']
    assert check(2)[0] is None


def test_funding_recovers_before_open_position_reaches_flat():
    from shared.accounting.historical_funding import recover_from_checkpoint
    start = datetime(2026, 11, 19, tzinfo=timezone.utc)
    at = lambda n: start + timedelta(minutes=n)
    scope = dict(account_id='default', symbol='ETNUSDT')
    event = dict(scope, income_type='FUNDING_FEE', asset='USDT', income=D('-.03'),
                 tran_id=3, event_time_utc=at(2))
    fills = [dict(scope, order_id=1, trade_id=1, trade_time_utc=at(1), side='SELL', qty=D('.29')),
             dict(scope, order_id=2, trade_id=2, trade_time_utc=at(3), side='BUY', qty=D('.08'))]
    owners = {str(f['order_id']): dict(f, strategy='new_coin') for f in fills}
    cursors = [dict(scope, source='userTrades', income_type=None, page_complete=True,
                    request_status='ok', window_start_ms=int(at(0).timestamp()*1000),
                    covered_until_ms=int(at(4).timestamp()*1000))]
    proof = {"position_side": "BOTH", "cutoff_ms": int(at(4).timestamp()*1000),
             "snapshot_observed_ms": int(at(5).timestamp()*1000),
             "position_payload_sha256": "a"*64, "gap_trade_count": 0}
    row, reason = recover_from_checkpoint(event, at(4), D('-.21'), proof, fills, owners, cursors)
    assert reason is None and row['strategy'] == 'new_coin'
    assert recover_from_checkpoint(event, at(4), D('-.22'), proof, fills, owners, cursors)[0] is None
    assert recover_from_checkpoint(event, at(4), D('-.21'), proof, fills, owners, [])[0] is None
    assert recover_from_checkpoint(event, at(4), D('-.21'), dict(proof, gap_trade_count=1), fills, owners, cursors)[0] is None
