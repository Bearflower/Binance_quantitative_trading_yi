from unittest.mock import AsyncMock
from decimal import Decimal
import pytest
from dashboard.backend.services.exchange_ledger import ExchangeLedgerDashboardMixin

class Service(ExchangeLedgerDashboardMixin):
    _STRATEGY_NAME_MAP={'hrs':'HRS'}
    _STRATEGY_EMOJI_MAP={}
    get_open_positions_summary=AsyncMock(return_value={})
    _query_unrealized_pnl=AsyncMock(return_value=0)
    def _get_date_range(self,kind): return None,None

@pytest.mark.asyncio
async def test_proven_custom_owner_is_projected_and_detail_available():
    report=dict(account_components=dict(net_usdt='5',realized_pnl_usdt='5',commission_usdt='0',funding_usdt='0'),
                account_usdt_total='5',unattributed_usdt_total='0',account_coverage_complete=True,
                strategy_attribution_complete=True,by_strategy={'legacy_alpha':{'net_usdt':'5','realized_pnl_usdt':'5'}},events=[],
                start_ms=1000,end_ms=2000,publication_status='published')
    svc=Service();svc._ledger_report=AsyncMock(return_value=report)
    overview=await svc.get_overview()
    assert sum(Decimal(s['total_pnl']) for s in overview['strategies'])+Decimal(overview['unattributed_pnl'])==Decimal(overview['total_pnl'])
    assert overview['strategies'][1]['id']=='legacy_alpha'
    detail=await svc.get_strategy_detail('legacy_alpha')
    assert detail['total_pnl']=='5'
