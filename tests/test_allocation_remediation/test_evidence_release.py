"""Deployment acceptance must observe this activation, not last release's cache."""
import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('evidence_release',Path(__file__).parents[2]/'deployment/trial-evidence/activate.py')
release=importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def proof():
    return {'account-risk-latest.json':dict(account_id='default',budget_id='default-pm-5pct-20261117-v1',baseline={'equity_usd':'500'},as_of_ms=1001),
            'new-coin-recovery-latest.json': {
        'account_id': 'default', 'status': 'WAIT_OPEN_POSITION',
        'as_of': '1970-01-01T00:00:01.001+00:00',
    },
            'entry-activity-latest.json':dict(account_id='default',source='signed_pm_get_order_v1',finished_ms=1001,batch_id='unique-batch')}


def test_fresh_collection_can_have_honest_data_gaps_and_still_prove_runtime_job_execution():
    body=proof()
    body['account-risk-latest.json']['status']='BLOCKED_DATA'
    assert release.fresh_observation(body,1000)


@pytest.mark.parametrize('kind,change',[
    ('account-risk-latest.json',{'as_of_ms':999}),
    ('entry-activity-latest.json',{'finished_ms':999}),
    ('entry-activity-latest.json',{'account_id':'grid'}),
    ('entry-activity-latest.json',{'source':'local_cache'}),
    ('entry-activity-latest.json',{'batch_id':None}),
    ('account-risk-latest.json',{'budget_id':'old-budget'}),
    ('account-risk-latest.json',{'baseline':None})])
def test_old_or_unbound_cache_does_not_pass_activation(kind,change):
    body=proof();body[kind].update(change)
    assert not release.fresh_observation(body,1000)


def test_missing_or_stale_recovery_report_cannot_prove_activation():
    body = proof()
    body['new-coin-recovery-latest.json']['as_of'] = '1970-01-01T00:00:00+00:00'
    assert not release.fresh_observation(body, 1000)
    del body['new-coin-recovery-latest.json']
    assert not release.fresh_observation(body, 1000)
