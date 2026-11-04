from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import json
import pytest
from shared.accounting.published_report import read_published_report, coverage_cutoff

class Connection:
    def __init__(self, previous=None):
        self.fetchrow=AsyncMock(return_value=previous)
        self.fetch=AsyncMock(return_value=[])
        self.fetchval=AsyncMock(return_value=0)
        self.execute=AsyncMock()
        self.transaction_args=None
    @asynccontextmanager
    async def transaction(self, **kwargs):
        self.transaction_args=kwargs
        yield self

class DB:
    def __init__(self, previous=None):
        self.connection=Connection(previous)
        self.pool=self
    @asynccontextmanager
    async def acquire(self): yield self.connection


def report(complete=True):
    return dict(account_coverage_complete=complete, strategy_attribution_complete=False,
                account_usdt_total='5', report_id='old', end_ms=2000, start_ms=1000,
                account_id='main', report_version='exchange-income-v1', coverage_gaps=[])

@pytest.mark.asyncio
async def test_publish_verified_snapshot_and_activate_in_one_transaction():
    db=DB()
    with patch('shared.accounting.published_report.read_monthly_economic_report', AsyncMock(return_value=report())):
        result=await read_published_report(db,'main',1000,2000,'month:1970-01')
    assert result['publication_status']=='published'
    assert db.connection.transaction_args['isolation']=='repeatable_read'
    queries=[call.args[0] for call in db.connection.execute.call_args_list if not call.args[0].startswith("SELECT pg_advisory_")]
    assert 'ledger_report_run' in queries[0]
    assert 'ledger_report_active' in queries[1]
    assert 'WHERE' in queries[1]  # concurrent refresh cannot move watermark backwards
    assert result['report_id'] != 'old'

@pytest.mark.asyncio
async def test_failed_refresh_retains_last_verified_run_explicitly_stale():
    prior=report(); prior.update(report_id='verified',as_of='1970-01-01T00:00:02+00:00')
    db=DB(dict(report=prior,created_at=datetime(1970,1,1,tzinfo=timezone.utc)))
    with patch('shared.accounting.published_report.read_monthly_economic_report', AsyncMock(side_effect=RuntimeError('read failed'))):
        result=await read_published_report(db,'main',1000,3000,'month:1970-01',current=True)
    assert result['report_id']=='verified'
    assert result['publication_status']=='stale'
    assert result['refresh_error']
    assert result['stale_seconds']==1
    assert all(call.args[0].startswith("SELECT pg_advisory_") for call in db.connection.execute.call_args_list)

@pytest.mark.asyncio
async def test_incomplete_refresh_cannot_replace_verified_snapshot():
    db=DB(dict(report=report(),created_at=datetime(1970,1,1,tzinfo=timezone.utc)))
    with patch('shared.accounting.published_report.read_monthly_economic_report', AsyncMock(return_value=report(False))):
        result=await read_published_report(db,'main',1000,3000,'month:1970-01',current=True)
    assert result['publication_status']=='stale'
    assert result['account_coverage_complete'] is True
    assert all(call.args[0].startswith("SELECT pg_advisory_") for call in db.connection.execute.call_args_list)

@pytest.mark.asyncio
async def test_no_coverage_has_no_fresh_as_of_or_published_run():
    db=DB()
    with patch('shared.accounting.published_report.read_monthly_economic_report', AsyncMock(return_value=report(False))):
        result=await read_published_report(db,'main',1000,2000,'month:1970-01')
    assert result['as_of'] is None
    assert result['publication_status']=='unverified'
    assert all(call.args[0].startswith("SELECT pg_advisory_") for call in db.connection.execute.call_args_list)

@pytest.mark.asyncio
async def test_read_error_without_verified_snapshot_propagates():
    db=DB()
    with patch('shared.accounting.published_report.read_monthly_economic_report', AsyncMock(side_effect=RuntimeError('read failed'))):
        with pytest.raises(RuntimeError): await read_published_report(db,'main',1000,2000,'month:1970-01')


def test_contiguous_watermark_requires_every_income_type():
    rows=[dict(account_id='main',source='um/income',income_type=kind,symbol=None,
               window_start_ms=1000,covered_until_ms=end,page_complete=True,request_status='ok')
          for kind,end in [('REALIZED_PNL',2900),('COMMISSION',2800),('FUNDING_FEE',2700)]]
    assert coverage_cutoff(rows,'main',1000,3000)==2700
    assert coverage_cutoff(rows[:-1],'main',1000,3000)==1000
