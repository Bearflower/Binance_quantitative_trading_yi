"""Explicit own-quota accounting keeps real account cash independent."""
import copy,json
from decimal import Decimal as D
import pytest
from strategies.hrs.risk_budget import FLOOR


def sources():
    inventory=[dict(symbol='OWN',positionAmt='3.69',markPrice='10',leverage='10'),
               dict(symbol='OTHER',positionAmt='64.86',markPrice='10',leverage='10')]
    financial=dict(hrs_margin_scope='hrs_owned_quota_account_free_v1',slot_inputs=dict(inventory=inventory),
        account_equity='500',effective_allocation='26.18',account_available_balance='448.86',
        account_available_balance_source='papi/v1/account.totalAvailableBalance',account_status='NORMAL')
    proof=dict(owners={'OWN':'hrs','OTHER':'new_coin'},hrs_symbols=['OWN'],account_symbols=['OTHER','OWN'])
    policy=dict(reason=json.dumps(dict(hrs_risk_budget_version=FLOOR,hrs_margin_scope='hrs_owned_quota_account_free_v1')),
        multiplier='.5',ratio_cap='.15',entry_ceiling='26.18')
    return financial,proof,policy


def test_own_quota_does_not_charge_proven_foreign_margin():
    from strategies.hrs.margin_budget import margin_limits
    f,p,policy=sources()
    result=margin_limits(f,p,[],[],D('60'),policy)
    assert result['account_margin']==D('68.55')
    assert result['hrs_owned_margin']==D('3.69')
    assert result['margin_cap']==D('26.18')
    assert result['remaining_margin']==D('22.49')
    assert result['account_remaining_margin']==D('448.86')


@pytest.mark.parametrize('field,value', [('account_available_balance',None),('account_available_balance',False),
    ('account_available_balance',448.86),('account_available_balance','NaN'),('account_available_balance','Infinity'),
    ('account_available_balance','0'),('account_status','MARGIN_CALL'),('account_available_balance_source','equity'),
    ('hrs_margin_scope','unknown')])
def test_cash_and_scope_never_infer_missing_or_ambiguous_sources(field,value):
    from strategies.hrs.margin_budget import margin_limits
    f,p,policy=sources();f[field]=value
    with pytest.raises(ValueError):margin_limits(f,p,[],[],D('60'),policy)


def test_account_pending_cash_is_separate_from_hrs_retained_quota():
    from strategies.hrs.margin_budget import margin_limits
    f,p,policy=sources()
    own=dict(client_order_id='h',account_id='test',strategy_id='hrs',margin='2',state='reserved')
    foreign=dict(client_order_id='f',account_id='test',strategy_id='new_coin',margin='445',state='submitting')
    result=margin_limits(f,p,[own],[own,foreign],D('60'),policy)
    assert result['reserved_margin']==D(2) and result['account_reserved_margin']==D(447)
    assert result['remaining_margin']==D('1.86')
    own['margin']='25'
    assert margin_limits(f,p,[own],[own],D(60),policy)['remaining_margin']<0
    foreign['margin']=None
    with pytest.raises(ValueError):margin_limits(f,p,[own],[own,foreign],D(60),policy)


@pytest.mark.parametrize('scope', [None,False,{},'unknown'])
def test_unknown_explicit_margin_scope_is_closed(scope):
    from strategies.hrs.margin_budget import policy_margin_scope
    with pytest.raises(ValueError):policy_margin_scope(dict(reason=json.dumps(dict(hrs_risk_budget_version=FLOOR,hrs_margin_scope=scope))))


def test_missing_scope_preserves_old_account_cap_and_owned_requires_floor():
    from strategies.hrs.margin_budget import ALL_ACCOUNT,HRS_OWNED,policy_margin_scope
    assert policy_margin_scope(dict(reason='legacy'))==ALL_ACCOUNT
    assert policy_margin_scope(dict(reason=json.dumps(dict(hrs_risk_budget_version=FLOOR))))==ALL_ACCOUNT
    with pytest.raises(ValueError):policy_margin_scope(dict(reason=json.dumps(dict(hrs_margin_scope=HRS_OWNED))))


def test_cash_subtraction_preserves_small_real_shortfall_without_decimal_cancellation():
    from strategies.hrs.margin_budget import margin_limits
    f,p,policy=sources();f['account_available_balance']='2'
    foreign=dict(client_order_id='f',account_id='test',strategy_id='new_coin',margin='1.000000000000000000000000000000000000000001',state='submitting')
    result=margin_limits(f,p,[],[foreign],D(60),policy)
    assert result['remaining_margin']<D(1)
    assert result['account_reserved_margin']==D(foreign['margin'])
