import json
from datetime import datetime,timedelta,timezone
from ai_tuner.ops.hrs_acceptance import POLICIES,evaluate_policies
from strategies.hrs.risk_budget import FLOOR
from strategies.hrs.margin_budget import HRS_OWNED

def rows():
    now=datetime(2026,11,19,tzinfo=timezone.utc)
    return now,[dict(strategy_id=s,revision=r,mode=m,freeze_allocation=True,allow_add=False,
        effective_at=now,expires_at=now+timedelta(days=20),audited=True,entry_ceiling='26.18',
        reason=json.dumps({'hrs_risk_budget_version':FLOOR,'hrs_margin_scope':HRS_OWNED}) if s=='hrs' else 'old approved reason')
        for s,r,m in [('hrs',8,'reduced'),('btc_eth',5,'capped'),('btc_eth_aggressive',5,'reduced'),('new_coin',5,'capped')]]

def test_daily_inspector_expected_only_hrs_budget_revision_changes():
    assert POLICIES=={'hrs':(8,'reduced'),'btc_eth':(5,'capped'),'btc_eth_aggressive':(5,'reduced'),'new_coin':(5,'capped')}
    now,data=rows();assert evaluate_policies(data,now)==[]

def test_revision_number_alone_does_not_prove_new_budget_mode():
    now,data=rows();data[0]['reason']='old percent reason'
    assert any('预算版本' in x for x in evaluate_policies(data,now))
    data[0]['reason']=json.dumps({'hrs_risk_budget_version':'unknown'})
    assert any('预算版本' in x for x in evaluate_policies(data,now))

def test_other_policy_and_margin_drift_still_alarm():
    now,data=rows();data[1]['revision']=6;data[0]['entry_ceiling']='27'
    issues=evaluate_policies(data,now)
    assert any('btc_eth r6' in x for x in issues) and any('hrs r8' in x for x in issues)


def test_owned_quota_policy_requires_explicit_verified_margin_contract():
    now,data=rows();data[0]["reason"]=json.dumps({"hrs_risk_budget_version":FLOOR})
    assert any("保证金口径" in x for x in evaluate_policies(data,now))
    data[0]["reason"]=json.dumps({"hrs_risk_budget_version":FLOOR,"hrs_margin_scope":"unknown"})
    assert any("保证金口径" in x for x in evaluate_policies(data,now))
