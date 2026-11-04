from dashboard.backend.services.exchange_ledger import ExchangeLedgerDashboardMixin
from shared.accounting.economic_ledger import build_economic_report

def test_attributed_symbol_projection_includes_funding_and_preserves_unknown_total():
    svc=ExchangeLedgerDashboardMixin()
    report=dict(strategy_attribution_complete=False,events=[dict(strategy='hrs',symbol='ETHUSDT',asset='USDT',income_type='COMMISSION',amount='-1')])
    row=svc._symbol_projections(report,'hrs')[0]
    assert row['total_pnl'] is None
    assert row['observed_pnl']=='-1'


def test_offsetting_unresolved_events_do_not_disappear_in_net_zero():
    rows=[dict(account_id='main',tran_id=i,income_type='FUNDING_FEE',income=amount,asset='USDT',symbol='ETHUSDT') for i,amount in [(1,'5'),(2,'-5')]]
    report=build_economic_report([],rows,[])
    assert report['unattributed_usdt_total']=='0'
    assert report['unattributed_usdt_absolute']=='10'
    assert report['unattributed_count']==2
