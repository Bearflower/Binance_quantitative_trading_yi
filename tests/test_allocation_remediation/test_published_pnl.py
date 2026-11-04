import copy
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import pytest
from ai_tuner.allocation import pnl_collector as module
from shared.accounting.published_report import REPORT_VERSION

CST=timezone(timedelta(hours=8))
START=datetime(2026,10,1,tzinfo=CST);END=datetime(2026,11,1,tzinfo=CST)
START_MS=int(START.timestamp()*1000);END_MS=int(END.timestamp()*1000)


def report():
    return dict(account_id='test-account',start_ms=START_MS,end_ms=END_MS,requested_end_ms=END_MS,
        window_key=f'monthly:{START_MS}',report_version=REPORT_VERSION,report_id='verified-id',
        account_coverage_complete=True,strategy_attribution_complete=True,
        publication_status='published',refresh_error=None,
        by_strategy={'hrs':{'net_usdt':'-1.25'},'btc_eth':{'net_usdt':'2.35'}})


def collector():
    return module.PnLCollector(NS(fetch_one=AsyncMock(),fetch_all=AsyncMock()),
        [{'strategy_id':'hrs'},{'strategy_id':'btc_eth'}],binance_client=AsyncMock(),strict_hrs_income=True)


@pytest.mark.asyncio
async def test_one_same_published_report_then_explicit_missing_evidence(monkeypatch):
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test-account')
    assert hasattr(module,'AllocationInputUnavailable')
    reader=AsyncMock(return_value=report())
    obj=collector()
    with patch.object(module,'read_published_report',reader):
        with pytest.raises(module.AllocationInputUnavailable) as failure:
            await obj.collect_all_realized_pnl(START,END)
    reader.assert_awaited_once_with(obj.db_manager,'test-account',START_MS,END_MS,f'monthly:{START_MS}',current=False)
    assert failure.value.reasons==('missing_capital_evidence','missing_activity_evidence')
    assert failure.value.report_id=='verified-id'
    assert failure.value.verified_net_pnl=={'hrs':Decimal('-1.25'),'btc_eth':Decimal('2.35')}
    obj.db_manager.fetch_one.assert_not_called();obj.db_manager.fetch_all.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('changes',[
    {'account_id':'other'},{'start_ms':START_MS+1},{'end_ms':END_MS-1},
    {'requested_end_ms':END_MS+1},{'window_key':'different'},{'report_version':'old'},
    {'report_id':None},{'account_coverage_complete':False},{'strategy_attribution_complete':False},
    {'publication_status':'stale'},{'refresh_error':'timeout'},
    {'by_strategy':{}},{'by_strategy':{'hrs':{'net_usdt':'NaN'},'btc_eth':{'net_usdt':'1'}}},
])
async def test_invalid_report_never_becomes_zero_or_partial(changes,monkeypatch):
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test-account')
    assert hasattr(module,'AllocationInputUnavailable')
    body=report();body.update(changes)
    with patch.object(module,'read_published_report',AsyncMock(return_value=body)):
        with pytest.raises(module.AllocationInputUnavailable):await collector().collect_all_realized_pnl(START,END)

@pytest.mark.asyncio
@pytest.mark.parametrize('body',[None,[],{},dict(report(),report_id=''),dict(report(),report_id=1)])
async def test_malformed_report(body,monkeypatch):
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test-account')
    with patch.object(module,'read_published_report',AsyncMock(return_value=body)):
        with pytest.raises(module.AllocationInputUnavailable):await collector().collect_all_realized_pnl(START,END)

@pytest.mark.asyncio
async def test_reader_failure_is_typed():
    with patch.object(module,'read_published_report',AsyncMock(side_effect=ConnectionError('offline'))):
        with pytest.raises(module.AllocationInputUnavailable,match='published_report_unavailable'):
            await collector().collect_all_realized_pnl(START,END)

@pytest.mark.asyncio
@pytest.mark.parametrize('strategies',[[{}],[{'strategy_id':None}],[{'strategy_id':''}],[{'strategy_id':'hrs'}]*2,[],[{'strategy_id':'missing'}]])
async def test_strategy_identity_or_missing_evidence_is_never_rankable(strategies,monkeypatch):
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test-account');obj=collector();obj.strategies=strategies
    with patch.object(module,'read_published_report',AsyncMock(return_value=report())):
        with pytest.raises(module.AllocationInputUnavailable):await obj.collect_all_realized_pnl(START,END)

@pytest.mark.parametrize('left,right',[(None,END),(START,None),(START.replace(tzinfo=None),END),(START,END.replace(tzinfo=None)),(START.replace(day=2),END),(START.replace(hour=1),END),(START,END.replace(day=2)),(START.replace(year=2099),END.replace(year=2099))])
def test_invalid_period(left,right):
    with pytest.raises(module.AllocationInputUnavailable,match='invalid_month_window'):
        module.PnLCollector._completed_month_window(left,right)
