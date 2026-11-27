from datetime import datetime,timezone,timedelta
from decimal import Decimal
from types import SimpleNamespace
import pytest

T=1795763736522

def policy():
    return SimpleNamespace(account_id='default',revision=1,receipt_sha256='a'*64,
        exchange_uid_sha256='b'*64,classic_flat_at=datetime.fromtimestamp((T-86400000)/1000,timezone.utc),
        accepted_through=datetime.fromtimestamp(T/1000,timezone.utc),classic_open_equity=Decimal('100'),
        risk_limit_ratio=Decimal('.05'),automatic_allocation=True,
        payload={'classic_risk_method':'classic_fixed_capital_cashflow_adjusted_drawdown_v1',
                 'classic_known_equity_points':[{'as_of_ms':T,'equity_usdt':'102','net_transfer_usdt':'0','source_sha256':'c'*64}]})

def observation(amount='101',income='1',flow='0'):
    return dict(as_of_ms=T+1000,observed_at_ms=T+1100,uid_sha256='b'*64,
        wallet_balance_usdt=amount,margin_balance_usdt=amount,unrealized_pnl_usdt='0',
        cumulative_income_usdt=income,cumulative_net_transfer_usdt=flow,
        coverage_start_ms=T-86400000,coverage_end_ms=T+1000,coverage_complete=True,
        native_source='signed:GET /fapi/v1/income',single_asset_usdt=True,
        raw_snapshot_sha256='d'*64)

def test_fixed_capital_drawdown_keeps_historical_highwater():
    from ai_tuner.monitor.classic_account_risk import assess_classic_risk
    r=assess_classic_risk(policy(),observation(),None,[])
    assert r['status']=='VERIFIED_WITH_ACCEPTED_HISTORY'
    assert Decimal(r['current_drawdown_ratio'])==Decimal('.01')
    assert r['history_status']=='ACCEPTED_GAPS' and r['full_historical_nav_verified'] is False


def test_deposits_do_not_expand_budget_or_create_profit():
    from ai_tuner.monitor.classic_account_risk import assess_classic_risk
    r=assess_classic_risk(policy(),observation('197','-3','100'),None,[])
    assert Decimal(r['adjusted_equity_usdt'])==97
    assert Decimal(r['current_drawdown_ratio'])==Decimal('.05')
    assert r['status']=='SAFETY_REVIEW' and r['known_breach']
    assert Decimal(r['budget_loss_usdt'])==5


def test_known_pm_breach_survives_bad_current_evidence():
    from ai_tuner.monitor.classic_account_risk import assess_classic_risk
    o=observation();o['coverage_complete']=False
    r=assess_classic_risk(policy(),o,None,[{'account_id':'default','max_drawdown_ratio':'.07'}])
    assert r['status']=='SAFETY_REVIEW' and r['known_breach']
    assert 'classic_income_coverage_incomplete' in r['reasons']


@pytest.mark.parametrize('key,value,reason',[
    ('uid_sha256','bad','classic_account_identity_mismatch'),
    ('wallet_balance_usdt','102','classic_wallet_conservation_failed'),
    ('single_asset_usdt',False,'classic_single_usdt_required'),
    ('coverage_complete',False,'classic_income_coverage_incomplete'),
    ('as_of_ms',T+400000,'classic_sampling_gap'),
])
def test_current_faults_not_waived(key,value,reason):
    from ai_tuner.monitor.classic_account_risk import assess_classic_risk
    o=observation();o[key]=value
    if key=='as_of_ms':o.update(observed_at_ms=value+1,coverage_end_ms=value)
    r=assess_classic_risk(policy(),o,None,[])
    assert r['status']=='BLOCKED_DATA' and reason in r['reasons']


def test_recovery_and_revision_never_clear_breach_or_peak():
    from ai_tuner.monitor.classic_account_risk import assess_classic_risk
    old=assess_classic_risk(policy(),observation('96','-4'),None,[])
    p=policy();p.revision=2
    o=observation('103','3');o['as_of_ms']+=1000;o['coverage_end_ms']+=1000;o['observed_at_ms']+=1000
    r=assess_classic_risk(p,o,old,[])
    assert r['status']=='SAFETY_REVIEW' and Decimal(r['high_water_adjusted_equity_usdt'])==103
    assert Decimal(r['max_drawdown_ratio'])==Decimal('.06')


def test_out_of_order_history_retains_real_drawdown():
    from ai_tuner.monitor.classic_account_risk import assess_classic_risk
    p=policy();p.payload['classic_known_equity_points']=[
        {'as_of_ms':T,'equity_usdt':'96','net_transfer_usdt':'0','source_sha256':'c'*64},
        {'as_of_ms':T-1000,'equity_usdt':'103','net_transfer_usdt':'0','source_sha256':'d'*64}]
    r=assess_classic_risk(p,observation('103','3'))
    assert r['status']=='SAFETY_REVIEW'
    assert Decimal(r['max_drawdown_ratio'])==Decimal('.07')


@pytest.mark.parametrize('change',[{'as_of_ms':T},{'source_sha256':'z'*64}])
def test_invalid_history_identity_rejected(change):
    from ai_tuner.monitor.classic_account_risk import assess_classic_risk
    p=policy();point=dict(p.payload['classic_known_equity_points'][0]);point['as_of_ms']=T-1000;point.update(change)
    p.payload['classic_known_equity_points'].append(point)
    with pytest.raises(ValueError):assess_classic_risk(p,observation())


def test_pending_attribution_cannot_leave_persisted_ready_status():
    from ai_tuner.monitor.classic_account_risk import assess_classic_risk, apply_attribution_gate
    p=policy();r=assess_classic_risk(p,observation())
    assert apply_attribution_gate(r,{'ready':False,'pending':['owner']})['status']=='BLOCKED_DATA'
    assert r['financial_evidence_verified']  # cumulative financial capture remains usable


def test_attribution_must_be_fresh_and_explicit():
    from ai_tuner.monitor.classic_account_risk import assess_classic_risk, apply_attribution_gate
    for evidence in (None,{'ready':True}, {'ready':True,'coverage_end_ms':T-999999}):
        r=assess_classic_risk(policy(),observation())
        assert apply_attribution_gate(r,evidence)['status']=='BLOCKED_DATA'
    r=assess_classic_risk(policy(),observation())
    assert apply_attribution_gate(r,{'ready':True,'coverage_end_ms':T,'account_id':'default','receipt_sha256':'a'*64})['status']=='VERIFIED_WITH_ACCEPTED_HISTORY'


def test_historical_breach_survives_invalid_current_observation():
    from ai_tuner.monitor.classic_account_risk import assess_classic_risk
    p=policy();p.payload['classic_known_equity_points']=[
        {'as_of_ms':T-1000,'equity_usdt':'103','net_transfer_usdt':'0','source_sha256':'c'*64},
        {'as_of_ms':T,'equity_usdt':'96','net_transfer_usdt':'0','source_sha256':'d'*64}]
    o=observation();o['coverage_complete']=False
    r=assess_classic_risk(p,o)
    assert r['status']=='SAFETY_REVIEW' and r['known_breach']
    assert Decimal(r['max_drawdown_ratio'])==Decimal('.07')


@pytest.mark.parametrize('field,value',[('account_id','grid'),('receipt_sha256','f'*64)])
def test_other_account_or_revision_attribution_is_not_reused(field,value):
    from ai_tuner.monitor.classic_account_risk import assess_classic_risk,apply_attribution_gate
    proof={'ready':True,'coverage_end_ms':T,'account_id':'default','receipt_sha256':'a'*64}
    proof[field]=value
    assert apply_attribution_gate(assess_classic_risk(policy(),observation()),proof)['status']=='BLOCKED_DATA'
