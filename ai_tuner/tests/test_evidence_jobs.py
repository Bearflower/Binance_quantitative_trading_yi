"""Integration boundaries: scheduled observations cannot become live allocations."""
import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ai_tuner.monitor import evidence_jobs as m
from ai_tuner.monitor.reduced_trial import BEIJING, assess_snapshot


def config():
    return {'account_risk': {'budget': {'account_id':'default','budget_id':'budget','max_snapshot_age_ms':120000}},
            'capital_allocation': {'participating_strategies':['hrs'],'reserve_ratio':0.15,'rank_ratios':[0.85]}}


def job(tmp_path):
    return m.EvidenceJobs(None, None, None, SimpleNamespace(send_alert=AsyncMock(return_value=True)),config(),tmp_path)


def test_latest_risk_rejects_other_budget_or_stale_evidence(tmp_path):
    obj=job(tmp_path)
    for stamp,budget in ((int(time.time()*1000)-180000,'budget'),(int(time.time()*1000),'other')):
        (tmp_path/'account-risk-latest.json').write_text(json.dumps(dict(account_id='default',budget_id=budget,as_of_ms=stamp,status='VERIFIED')))
        assert obj.latest_risk()['status']=='BLOCKED_DATA'


@pytest.mark.asyncio
async def test_breached_risk_blocks_activation_even_with_complete_profit_inputs(tmp_path,monkeypatch):
    obj=job(tmp_path)
    now=datetime(2026,11,17,tzinfo=BEIJING)
    risk=dict(account_id='default',budget_id='budget',status='SAFETY_REVIEW',current_equity_usd='100',max_drawdown_ratio='0.05')
    monkeypatch.setattr(obj,'latest_risk',lambda:risk)
    start=int(datetime(2026,10,1,tzinfo=BEIJING).timestamp()*1000)
    end=int(datetime(2026,11,1,tzinfo=BEIJING).timestamp()*1000)
    common=dict(account_id='default',start_ms=start,end_ms=end,coverage_complete=True,source_id='s')
    report=dict(account_id='default',start_ms=start,end_ms=end,requested_end_ms=end,
                window_key=f'monthly:{start}',publication_status='published',refresh_error=None,
                report_version='exchange-income-v1-derived-v1',account_coverage_complete=True,
                strategy_attribution_complete=True,report_id='r',by_strategy={'hrs':{'net_usdt':'2'}})
    monkeypatch.setattr(m,'read_published_report',AsyncMock(return_value=report))
    monkeypatch.setattr(m,'read_capital_evidence',AsyncMock(return_value={'hrs':dict(common,average_capital_usdt='20')}))
    monkeypatch.setattr(m,'read_activity_evidence',AsyncMock(return_value={'hrs':dict(common,owned_entry_fills=3)}))
    result=await obj.run_allocation_shadow(now=now)
    assert result['status']=='PREVIEW'
    assert result['live_activation_eligible'] is False
    assert 'account_risk_unverified_or_budget_breached' in result['activation_blocks']
    assert result['allocation']['entries'][0]['allocated_amount']==85
    assert (tmp_path/'allocation-shadow-latest.json').exists()


@pytest.mark.asyncio
async def test_missing_risk_never_uses_configured_total_as_capital_pool(tmp_path,monkeypatch):
    obj=job(tmp_path)
    obj.config['capital_allocation']['total_capital']=1000
    monkeypatch.setattr(m,'read_published_report',AsyncMock(return_value={}))
    monkeypatch.setattr(m,'read_capital_evidence',AsyncMock(return_value={}))
    monkeypatch.setattr(m,'read_activity_evidence',AsyncMock(return_value={}))
    result=await obj.run_allocation_shadow(now=datetime(2026,11,17,tzinfo=BEIJING))
    assert result['status']=='BLOCKED_DATA'
    assert result['allocation'] is None
    assert 'capital_pool_unverified' in result['reasons']


@pytest.mark.asyncio
async def test_repeated_risk_failure_notifies_only_once_and_omits_raw_evidence(tmp_path,monkeypatch):
    obj=job(tmp_path)
    monkeypatch.setattr(m,'collect_account_risk',AsyncMock(return_value={
        'status':'BLOCKED_DATA','reasons':['cashflow_gap:1:2'],'evidence':{'secret_raw':'not_public'}}))
    await obj.sample_risk(); await obj.sample_risk()
    obj.messenger.send_alert.assert_awaited_once()
    assert 'evidence' not in json.loads((tmp_path/'account-risk-latest.json').read_text())


def test_scheduler_registers_independent_minute_collectors_and_daily_shadow(tmp_path):
    calls=[]
    m.register_evidence_jobs(SimpleNamespace(add_job=lambda *a,**kw:calls.append((a,kw))),job(tmp_path))
    assert {kwargs['id'] for _,kwargs in calls}=={'account_risk','entry_activity','pm_equity_valuation','allocation_shadow'}
    assert all(kwargs['max_instances']==1 for _,kwargs in calls)


def test_stale_previously_confirmed_safety_review_never_becomes_ordinary_data_gap(tmp_path):
    body=dict(account_id='default',budget_id='budget',as_of_ms=int(time.time()*1000)-180000,
              status='SAFETY_REVIEW',max_drawdown_ratio='0.05',known_breach=None)
    (tmp_path/'account-risk-latest.json').write_text(json.dumps(body))
    risk=job(tmp_path).latest_risk()
    assert risk['status']=='SAFETY_REVIEW'
    assert risk['max_drawdown_ratio'] is None


@pytest.mark.asyncio
async def test_first_baseline_pin_is_kept_and_passed_on_restart(tmp_path,monkeypatch):
    obj=job(tmp_path)
    obj.budget['account_identity']='default-pm'
    first=dict(account_id='default',account_identity='default-pm',exchange_time_ms=100)
    collect=AsyncMock(side_effect=[dict(status='VERIFIED',baseline=first),
                                  dict(status='BLOCKED_DATA',baseline=None)])
    monkeypatch.setattr(m,'collect_account_risk',collect)
    await obj.sample_risk(); await obj.sample_risk()
    assert json.loads((tmp_path/'account-risk-baseline-pin.json').read_text())==first
    assert collect.await_args_list[1].args[-1]['expected_baseline_time_ms']==100


@pytest.mark.asyncio
async def test_deployed_shadow_mode_still_blocks_live_job_after_policy_unfreeze(monkeypatch):
    from ai_tuner.allocation.monthly_job import MonthlyAllocationJob
    import ai_tuner.allocation.monthly_job as monthly
    import yaml
    cfg=yaml.safe_load((__import__('pathlib').Path(__file__).parents[1]/'config.yaml').read_text())
    assert cfg['capital_allocation']['mode']=='shadow_only'
    assert cfg['capital_allocation']['enabled'] is False
    db=SimpleNamespace(execute=AsyncMock(),execute_ddl=AsyncMock(),fetch_one=AsyncMock())
    monkeypatch.setattr(monthly,'allocation_frozen',AsyncMock(return_value=False))
    obj=MonthlyAllocationJob(cfg,db,None,None,None,None)
    assert await obj.run_monthly_allocation() is None
    db.execute.assert_not_called(); db.execute_ddl.assert_not_called()


@pytest.mark.asyncio
async def test_valuation_observations_keep_valid_and_failed_evidence(tmp_path, monkeypatch):
    obj = job(tmp_path)
    obj.budget['account_identity'] = 'default-pm'
    capture = AsyncMock(side_effect=[
        dict(status='VERIFIED_POINT', index_time_ms=123, reconstructed_usd='500.00'),
        TimeoutError(),
    ])
    monkeypatch.setattr(m, 'observe_pm_equity', capture)
    await obj.sample_equity_valuation()
    await obj.sample_equity_valuation()

    samples = [json.loads(p.read_text()) for p in (tmp_path / 'pm-equity').rglob('*.json')]
    assert len(samples) == 2
    assert {sample['status'] for sample in samples} == {'BLOCKED_DATA', 'VERIFIED_POINT'}
    assert {sample['account_scope'] for sample in samples} == {'default'}
    assert {sample['account_identity'] for sample in samples} == {'default-pm'}
    assert all(sample['method'] == 'pm-usdt-equity-bridge-v1' for sample in samples)
    assert all(p.stat().st_mode & 0o777 == 0o600
               for p in (tmp_path / 'pm-equity').rglob('*.json'))
    obj.messenger.send_alert.assert_not_awaited()
