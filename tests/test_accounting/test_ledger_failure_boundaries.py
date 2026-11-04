"""Critical publication/validation boundaries independent of happy-path totals."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import json
import pytest

from shared.accounting.economic_ledger import build_economic_report, coverage_gaps
from shared.accounting.report_reader import read_monthly_economic_report, read_hrs_summary_provenance
from shared.accounting.published_report import read_published_report
from tests.test_accounting.test_ledger_publication import DB, report
from tests.test_accounting.test_economic_ledger import _fill, _income, _owner


@pytest.mark.asyncio
async def test_fresh_verified_run_reused_without_reading_or_writing_facts():
    body=report();body['as_of']='1970-01-01T00:00:02+00:00'
    db=DB(dict(report=json.dumps(body),created_at=datetime.now(timezone.utc)))
    with patch('shared.accounting.published_report.read_monthly_economic_report', AsyncMock()) as reader:
        result=await read_published_report(db,'main',1000,2000,'month:1970-01')
    assert result['report_id']==body['report_id']
    assert result['publication_status']=='published'
    reader.assert_not_awaited()
    db.connection.fetch.assert_not_awaited()
    db.connection.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed',[
    {'account_id':'another-account'}, {'start_ms':999}, {'report_version':'legacy'},
    {'account_coverage_complete':False}, {'end_ms':2001}, {'end_ms':1999},
])
async def test_invalid_cached_identity_never_becomes_verified_fallback(changed):
    body=report();body.update(changed)
    db=DB(dict(report=body,created_at=datetime.now(timezone.utc)))
    with patch('shared.accounting.published_report.read_monthly_economic_report', AsyncMock(side_effect=RuntimeError('refresh failed'))):
        with pytest.raises(RuntimeError,match='refresh failed'):
            await read_published_report(db,'main',1000,2000,'month:1970-01')
    db.connection.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_losing_activation_returns_newer_winner_not_its_older_candidate():
    winner=report();winner.update(report_id='newer',end_ms=2500,account_usdt_total='7',as_of='1970-01-01T00:00:02.500000+00:00')
    db=DB();db.connection.fetchrow.side_effect=[None,{'report':json.dumps(winner)}]
    db.connection.execute.side_effect=['INSERT 0 1','INSERT 0 0']
    with patch('shared.accounting.published_report.read_monthly_economic_report', AsyncMock(return_value=report())):
        result=await read_published_report(db,'main',1000,3000,'month:1970-01',current=True)
    assert result['report_id']=='newer'
    assert result['account_usdt_total']=='7'
    assert result['end_ms']==2500
    assert result['stale_seconds']==0.5


class SerializationConflict(RuntimeError):
    sqlstate='40001'


@pytest.mark.asyncio
async def test_serialization_conflict_retries_whole_snapshot_before_publication():
    db=DB();db.connection.fetchrow.side_effect=[SerializationConflict('concurrent commit'),None]
    with patch('shared.accounting.published_report.read_monthly_economic_report', AsyncMock(return_value=report())) as reader:
        result=await read_published_report(db,'main',1000,2000,'month:1970-01')
    assert result['publication_status']=='published'
    assert db.connection.fetchrow.await_count==2
    assert reader.await_count==1
    assert db.connection.execute.await_count==2


@pytest.mark.asyncio
async def test_serialization_retry_limit_never_publishes_unread_report():
    db=DB();db.connection.fetchrow.side_effect=SerializationConflict('still conflicting')
    with pytest.raises(SerializationConflict):
        await read_published_report(db,'main',1000,2000,'month:1970-01')
    assert db.connection.fetchrow.await_count==3
    db.connection.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('start,end',[(1000,1000),(2000,1000)])
async def test_invalid_windows_rejected_before_any_database_access(start,end):
    db=DB()
    with pytest.raises(ValueError): await read_published_report(db,'main',start,end,'invalid')
    db.connection.fetchrow.assert_not_awaited()
    reader_db=AsyncMock()
    for reader in (read_monthly_economic_report,read_hrs_summary_provenance):
        with pytest.raises(ValueError): await reader(reader_db,'main',start,end)
    reader_db.fetch_all.assert_not_awaited()
    with pytest.raises(ValueError): coverage_gaps([],'main',start,end,set())


@pytest.mark.parametrize('amount',[None,'NaN','Infinity','-Infinity'])
def test_missing_or_nonfinite_income_rejected(amount):
    row=_income(1,'FUNDING_FEE','1');row['income']=amount
    with pytest.raises(ValueError): build_economic_report([],[row],[])


def test_conflicting_fill_and_reviewed_evidence_rejected():
    with pytest.raises(ValueError,match='fill trade_id'):
        build_economic_report([_fill(),_fill(pnl='6')],[],[])
    override=dict(account_id='main',income_type='FUNDING_FEE',tran_id=1,strategy='hrs')
    with pytest.raises(ValueError,match='reviewed override'):
        build_economic_report([],[],[],[override,{**override,'strategy':'other'}])


@pytest.mark.parametrize('kind,fills,reason',[
    ('TRANSFER',[],'unsupported_income_type'),
    ('REALIZED_PNL',[],'fill_not_found'),
    ('COMMISSION',[{**_fill(),'commission_asset':'BNB'}],'fill_commission_non_usdt'),
])
def test_unproven_or_unsupported_income_remains_unassigned(kind,fills,reason):
    row=_income(1,kind,'-0.2' if kind=='COMMISSION' else '5',11)
    result=build_economic_report(fills,[row],[_owner()])
    assert result['by_strategy']=={}
    assert result['unattributed'][0]['reason']==reason
    assert result['account_usdt_total']==result['unattributed_usdt_total']


def test_late_cursor_does_not_cover_an_earlier_hole():
    rows=[dict(source='um/income',account_id='main',income_type=kind,symbol=None,
               window_start_ms=50,covered_until_ms=100,page_complete=True,request_status='ok')
          for kind in ('REALIZED_PNL','COMMISSION','FUNDING_FEE')]
    assert set(coverage_gaps(rows,'main',0,100,set()))=={'income:REALIZED_PNL','income:COMMISSION','income:FUNDING_FEE'}


@pytest.mark.asyncio
async def test_outside_window_fills_do_not_invalidate_period_income_coverage():
    fills=[{**_fill(trade_id=i),'trade_time_utc':time} for i,time in enumerate([
        None,datetime.fromtimestamp(0,timezone.utc),datetime.fromtimestamp(2,timezone.utc)])]
    # A zero-valued fill inside the half-open window requires no nonzero income event.
    fills.append({**_fill(trade_id=99,pnl='0',fee='0'),'trade_time_utc':datetime.fromtimestamp(1.5,timezone.utc)})
    cursors=[dict(source='um/income',account_id='main',income_type=kind,symbol=None,
                  window_start_ms=1000,covered_until_ms=2000,page_complete=True,request_status='ok')
             for kind in ('REALIZED_PNL','COMMISSION','FUNDING_FEE')]
    db=AsyncMock();db.fetch_all.side_effect=[[],fills,[],cursors]
    result=await read_monthly_economic_report(db,'main',1000,2000)
    assert result['account_coverage_complete'] is True
    assert result['missing_income_fills']==[]
