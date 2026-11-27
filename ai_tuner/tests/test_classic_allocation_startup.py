"""Production config -> real main initialization -> registered monthly callback.

External resources and exchange evidence are isolated; scheduler registration,
MonthlyAllocationJob construction and its approval guards are actual code.
"""
from datetime import datetime,timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
import time
import pytest
from ai_tuner.allocation.allocation_calculator import AllocationEntry,AllocationResult

@pytest.mark.asyncio
@pytest.mark.parametrize('approval',['approved','revoked','automatic_false','stale_risk','not_due'])
async def test_production_disabled_legacy_config_uses_only_approved_classic_monthly_route(monkeypatch,approval):
    from ai_tuner import main
    import ai_tuner.allocation.monthly_job as monthly
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return cls(2026,12,4 if approval=='not_due' else 1,1,30,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(main,'datetime',Clock)
    monkeypatch.setattr(monthly,'datetime',Clock)
    monkeypatch.setenv('BINANCE_API_KEY','fixture-key')
    monkeypatch.setenv('BINANCE_API_SECRET','fixture-secret')
    monkeypatch.setenv('BINANCE_TESTNET','false')
    monkeypatch.setenv('TRADE_ACCOUNT_ID','default')
    monkeypatch.setenv('XAU_MONITOR_WEBHOOK_FILE','/nonexistent/test-hook')
    db=SimpleNamespace(connect=AsyncMock(),disconnect=AsyncMock())
    monkeypatch.setattr(main,'DatabaseManager',lambda **kw:db)
    monkeypatch.setattr(main,'NotificationClient',lambda **kw:Mock(close=AsyncMock()))
    monkeypatch.setattr(main,'MemoryDBHandler',lambda *a:SimpleNamespace(ensure_table_exists=AsyncMock()))
    monkeypatch.setattr(main,'LLMClient',lambda *a:Mock(client=Mock(close=AsyncMock())))
    for name in ('RollbackManager','ConfigOperator','DailyHealthCheck','WeeklyTuningJob'):
        monkeypatch.setattr(main,name,Mock())
    for name in ('ProfitExtractionJob','OrphanCleanupJob','IncomeReconciler','ReducedTrialEvaluator'):
        monkeypatch.setattr(main,name,lambda *a,**kw:None)
    client=SimpleNamespace(use_unified_account=False,close=AsyncMock())
    monkeypatch.setattr(main,'BinanceClient',lambda **kw:client)
    monkeypatch.setattr(main,'ReadOnlyBinanceClient',lambda *a,**kw:SimpleNamespace(use_unified_account=False,close=AsyncMock()))
    ids=('btc_eth','btc_eth_aggressive','hrs','new_coin')
    p=SimpleNamespace(account_id='default',revision=1,receipt_sha256='a'*64,
        automatic_allocation=approval!='automatic_false',risk_limit_ratio=Decimal('.05'),
        accepted_from=Clock(2026,11,1,tzinfo=timezone.utc),accepted_through=Clock(2026,11,27,tzinfo=timezone.utc),
        payload={'allocation_ceilings_usdt':{s:30 for s in ids}})
    risk=dict(status='VERIFIED_WITH_ACCEPTED_HISTORY',account_id='default',policy_revision=1,acceptance_sha256='a'*64,
        known_breach=None,financial_evidence_verified=True,max_drawdown_ratio='.01',current_equity_usd='100',
        as_of_ms=int(time.time()*1000)-(121000 if approval=='stale_risk' else 0))
    jobs=SimpleNamespace(sample_risk=AsyncMock(return_value=risk),latest_risk=lambda:risk)
    monkeypatch.setattr(main,'EvidenceJobs',lambda *a,**kw:jobs)
    monkeypatch.setattr(main,'register_evidence_jobs',lambda *a,**kw:None)
    monkeypatch.setattr(monthly,'read_history_acceptance',AsyncMock(return_value=None if approval=='revoked' else p))
    monkeypatch.setattr(monthly,'approved_hrs_amount',AsyncMock(return_value=Decimal('30')))
    publish=AsyncMock(return_value=True)
    monkeypatch.setattr(monthly,'publish_classic_allocation',publish)
    app=main.StratTuneAI()
    app._setup_http_server=Mock();app._check_catch_up=AsyncMock()
    try:
        await app.initialize()
        assert app.config['capital_allocation']['enabled'] is False
        assert app.config['capital_allocation']['mode']=='shadow_only'
        assert isinstance(app.monthly_job,monthly.MonthlyAllocationJob)
        assert app.monthly_job.account_risk_provider is jobs.sample_risk
        scheduled=app.scheduler.get_job('monthly_allocation')
        assert scheduled is not None and scheduled.func==app._scheduled_allocation
        job=app.monthly_job
        job._check_idempotency=AsyncMock(return_value=False)
        job._check_is_first_month=AsyncMock(return_value=False)
        job._get_actual_balance=AsyncMock(return_value=100)
        job._send_notification=AsyncMock()
        job.pnl_collector.collect_all_realized_pnl=AsyncMock(return_value={'hrs':{'pnl':1}})
        job.calculator.calculate=Mock(return_value=AllocationResult('2026-12',100,20,80,False,
            [AllocationEntry(s,s,0,30,None,None,.2,20) for s in ids]))
        job.config_updater.update_all=AsyncMock()
        await scheduled.func()
        if approval=='approved':publish.assert_awaited_once()
        else:publish.assert_not_awaited()
        job.config_updater.update_all.assert_not_awaited()
        assert app.config['capital_allocation']['enabled'] is False
    finally:
        await app.shutdown()

@pytest.mark.asyncio
async def test_legacy_disabled_flag_still_blocks_without_classic_authority(monkeypatch):
    import ai_tuner.allocation.monthly_job as monthly
    monkeypatch.setattr(monthly,'allocation_frozen',AsyncMock(return_value=False))
    job=monthly.MonthlyAllocationJob.__new__(monthly.MonthlyAllocationJob)
    job._classic_guard=AsyncMock(return_value=(None,None))
    job.allocation_cfg={'enabled':False,'mode':'legacy'}
    job.db_manager=object();job._ensure_table=AsyncMock()
    job._get_actual_balance=AsyncMock()
    job.config_updater=SimpleNamespace(update_all=AsyncMock())
    assert await job.run_monthly_allocation() is None
    job._get_actual_balance.assert_not_awaited()
    job.config_updater.update_all.assert_not_awaited()
