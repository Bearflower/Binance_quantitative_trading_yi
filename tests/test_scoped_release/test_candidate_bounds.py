import pytest
from scripts.ledger_risk_manifest import ReleaseBlocked


def candidate():
    return dict(account_id='default',strategy_id='btc_eth_aggressive',mode='reduced',
                allow_add=False,multiplier='.5',ratio_cap='.15',entry_ceiling='70.00',
                allocation_amount='100',reason='approved')


def test_equivalent_decimal_notation_and_lower_ceiling():
    from scripts.scoped_ledger_risk_release import validate_candidate_bounds
    old=candidate()
    actual=dict(old,multiplier='0.50',ratio_cap='0.150',entry_ceiling='69.0',allocation_amount='100.0')
    validate_candidate_bounds([actual],[old])


@pytest.mark.parametrize('key,value',[
    ('multiplier','.6'),('multiplier','.4'),('ratio_cap','.16'),('ratio_cap','.14'),
    ('entry_ceiling','70.01'),('allocation_amount','101'),('allocation_amount','99'),
    ('account_id','other'),('strategy_id','other'),('mode','capped'),
    ('allow_add',True),('allow_add',0),('reason','other'),
])
def test_candidate_changes_rejected(key,value):
    from scripts.scoped_ledger_risk_release import validate_candidate_bounds
    old=candidate()
    with pytest.raises(ReleaseBlocked):
        validate_candidate_bounds([dict(old,**{key:value})],[old])


@pytest.mark.parametrize('key',['multiplier','ratio_cap','entry_ceiling','allocation_amount'])
@pytest.mark.parametrize('value',['NaN','Infinity','-Infinity','invalid',None,True])
@pytest.mark.parametrize('side',['actual','approved'])
def test_candidate_numbers_must_be_finite(key,value,side):
    from scripts.scoped_ledger_risk_release import validate_candidate_bounds
    actual=candidate();approved=candidate()
    (actual if side=='actual' else approved)[key]=value
    with pytest.raises(ReleaseBlocked):
        validate_candidate_bounds([actual],[approved])


def test_keys_and_candidate_membership_are_exact():
    from scripts.scoped_ledger_risk_release import validate_candidate_bounds
    old=candidate()
    missing=dict(old);missing.pop('allocation_amount')
    for actual in ([missing],[dict(old,unexpected='value')],[],[old,old]):
        with pytest.raises(ReleaseBlocked):validate_candidate_bounds(actual,[old])
    with pytest.raises(ReleaseBlocked):validate_candidate_bounds([old,old],[old,old])


def test_activation_cannot_lower_ceiling_after_dry_run():
    from scripts.scoped_ledger_risk_release import validate_candidate_bounds
    old=candidate()
    with pytest.raises(ReleaseBlocked):
        validate_candidate_bounds([dict(old,entry_ceiling='69')],[old],allow_lower_ceiling=False)
    validate_candidate_bounds([dict(old,entry_ceiling='70.0')],[old],allow_lower_ceiling=False)
