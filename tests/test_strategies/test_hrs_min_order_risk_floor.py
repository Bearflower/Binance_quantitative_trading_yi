"""Versioned HRS minimum executable risk floor, without live dependencies."""
from dataclasses import asdict
from decimal import Decimal as D
import json

import pytest

from strategies.hrs import risk_budget as risk

STRICT = 'hrs_percent_v1'
FLOOR = 'hrs_minimum_executable_v1'


def inputs(**overrides):
    result = dict(allocation=D('52.36'), entry_price=D('.24'), stop_price=D('.30'),
                  direction='short', existing_open_risk=D('.52'), leverage=D(2),
                  remaining_margin=D(20), qty_step=D(1), min_qty=D(1),
                  min_notional=D(5), fee_rate=D('.0005'), pressure_slippage=D('.02'),
                  single_risk_ratio=D('.01'), portfolio_risk_ratio=D('.03'),
                  take_profit_ratios=(D('.30'), D('.40')),
                  take_profit_prices=(D('.22'), D('.20')))
    result.update(overrides)
    return result


def floor(**overrides):
    return risk.assess_entry(**inputs(**overrides), budget_version=FLOOR)


def test_floor_admits_minimal_short_order_above_both_percent_budgets():
    old = risk.assess_entry(**inputs())
    assert not old.allowed and old.reason == 'minimum_order_exceeds_risk_budget'
    decision = floor()
    assert decision.allowed
    assert decision.quantity == decision.minimum_quantity == D(23)
    assert decision.minimum_risk == D(23) * D('.066273')
    assert decision.worst_loss == decision.minimum_risk == decision.risk_limit
    assert decision.base_single_limit == D('.5236')
    assert decision.base_portfolio_limit == D('1.5708')
    assert decision.effective_single_limit == decision.minimum_risk
    assert decision.effective_portfolio_limit == D('.52') + decision.minimum_risk
    assert decision.exit_ratios == (D(1), D(0))
    assert decision.budget_version == FLOOR


@pytest.mark.parametrize('minimum,step,target,expected', [
    ('1', '1', '.25', '20'), ('1', '1', '.24', '21'),
    ('.15', '.1', '1', '5.05'), ('.15', '.1', '.99', '5.15'),
])
def test_minimum_uses_ceiling_on_min_qty_anchored_lattice(minimum, step, target, expected):
    decision = floor(entry_price=D(2), stop_price=D('2.2'), remaining_margin=D(100), min_qty=D(minimum),
                     qty_step=D(step), take_profit_prices=(D(target), D(target)))
    assert decision.allowed
    assert decision.minimum_quantity == D(expected)
    assert decision.quantity == D(expected)
    assert (decision.quantity - D(minimum)) % D(step) == 0
    assert decision.quantity * D(target) >= D(5)
    assert decision.quantity - D(step) < D(minimum) or (decision.quantity - D(step)) * D(target) < D(5)


def test_floor_retains_normal_percent_sizing_when_no_exception_needed():
    arguments = inputs(allocation=D(1000), existing_open_risk=D(0), remaining_margin=D(500))
    old = risk.assess_entry(**arguments)
    new = risk.assess_entry(**arguments, budget_version=FLOOR)
    assert old.allowed and new.allowed
    assert new.quantity == old.quantity and new.worst_loss == old.worst_loss
    assert new.risk_limit == old.risk_limit == D(10)
    assert new.effective_single_limit == new.base_single_limit
    assert new.effective_portfolio_limit == new.base_portfolio_limit


def test_floor_supports_long_direction_without_changing_geometry():
    decision = floor(entry_price=D('.24'), stop_price=D('.18'), direction='long',
                     take_profit_prices=(D('.26'), D('.28')))
    assert decision.allowed and decision.quantity == D(21)
    assert decision.minimum_risk == D(21) * D('.0638082')
    assert decision.budget_inputs['stop_price'] == D('.18')
    assert decision.budget_inputs['take_profit_prices'] == (D('.26'), D('.28'))


def test_floor_reports_physical_margin_failure_separately():
    decision = floor(remaining_margin=D('2.75'))
    assert not decision.allowed and decision.reason == 'minimum_order_exceeds_margin'
    assert decision.minimum_quantity == D(23)
    exact = floor(remaining_margin=D('2.76'))
    assert exact.allowed and exact.quantity * D('.24') / D(2) == D('2.76')


def test_floor_never_exceeds_margin_when_normal_budget_is_large():
    decision = floor(allocation=D(10000), remaining_margin=D('3.13'))
    assert decision.allowed and decision.quantity == D(26)
    assert decision.quantity * D('.24') / D(2) <= D('3.13')


@pytest.mark.parametrize('field,value', [
    ('allocation', None), ('allocation', D('NaN')), ('existing_open_risk', D('-1')),
    ('existing_open_risk', D('Infinity')), ('remaining_margin', D(0)),
    ('remaining_margin', D('-1')), ('fee_rate', D('-0.1')),
    ('pressure_slippage', D(1)), ('leverage', D('NaN')), ('qty_step', D(0)),
    ('min_qty', D('Infinity')), ('single_risk_ratio', D(0)),
    ('portfolio_risk_ratio', D('-1')), ('entry_price', True),
    ('take_profit_ratios', (D(1), D('.1'))),
    ('take_profit_ratios', (D('NaN'), D('.4'))),
    ('take_profit_prices', (D(0), D('.2'))),
    ('take_profit_prices', (D('.22'), D('Infinity'))),
])
def test_floor_fails_closed_for_invalid_inputs(field, value):
    decision = floor(**{field: value})
    assert not decision.allowed


@pytest.mark.parametrize('direction,stop', [('short', '.24'), ('short', '.20'), ('long', '.30')])
def test_floor_never_repairs_nonprotective_stop(direction, stop):
    decision = floor(direction=direction, stop_price=D(stop))
    assert not decision.allowed and decision.reason == 'stop_not_protective'


@pytest.mark.parametrize('version', ['future', '', None, True])
def test_unknown_assessment_version_fails_closed(version):
    decision = risk.assess_entry(**inputs(), budget_version=version)
    assert not decision.allowed and decision.reason == 'invalid_budget_version'


def test_full_tp1_fallback_does_not_inflate_quantity_to_support_tiny_split():
    decision = floor(take_profit_ratios=(D('.01'), D('.01')))
    assert decision.allowed and decision.quantity == D(23)
    assert decision.exit_ratios == (D(1), D(0))


@pytest.mark.parametrize('reason', [None, '', 'historical approval', 'hrs_minimum_executable_v1', '{}'])
def test_historical_policy_reason_never_implicitly_enables_floor(reason):
    assert hasattr(risk, 'policy_budget_version'), 'version parser missing'
    assert risk.policy_budget_version({'reason': reason}) == STRICT


@pytest.mark.parametrize('version', [STRICT, FLOOR])
def test_explicit_structured_policy_selects_version(version):
    assert hasattr(risk, 'policy_budget_version'), 'version parser missing'
    assert risk.policy_budget_version({'reason': json.dumps({'hrs_risk_budget_version': version})}) == version


@pytest.mark.parametrize('reason', ['{"hrs_risk_budget_version":"future"}',
    '{"hrs_risk_budget_version":true}', '{"hrs_risk_budget_version":null}', '{bad', '[]'])
def test_invalid_structured_policy_fails_closed(reason):
    assert hasattr(risk, 'policy_budget_version'), 'version parser missing'
    with pytest.raises(ValueError):
        risk.policy_budget_version({'reason': reason})


def test_audit_captures_complete_reconstructable_decimal_inputs():
    assert hasattr(risk, 'assess_budget_inputs'), 'audit reconstruction missing'
    decision = floor()
    expected = inputs(budget_version=FLOOR)
    assert decision.budget_inputs == expected
    encoded = json.loads(json.dumps(asdict(decision), default=str))
    replayed = risk.assess_budget_inputs(encoded['budget_inputs'])
    assert replayed == decision
    assert risk.assess_budget_inputs(risk.assess_entry(**inputs()).budget_inputs).allowed is False


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'nonfinite', 'boolean'])
def test_audit_reconstruction_rejects_incomplete_or_ambiguous_contract(mutation):
    assert hasattr(risk, 'assess_budget_inputs'), 'audit reconstruction missing'
    contract = inputs(budget_version=FLOOR)
    if mutation == 'missing':
        del contract['fee_rate']
    elif mutation == 'extra':
        contract['secret'] = 'do not accept arbitrary captured fields'
    else:
        contract['allocation'] = 'NaN' if mutation == 'nonfinite' else True
    with pytest.raises(ValueError):
        risk.assess_budget_inputs(contract)


@pytest.mark.parametrize('overrides', [
    {'single_risk_ratio': D('.011')}, {'portfolio_risk_ratio': D('.031')},
    {'take_profit_prices': (D('.25'), D('.20'))},
    {'take_profit_prices': (D('.22'), D('.24'))},
    {'direction': 'long', 'stop_price': D('.18'), 'take_profit_prices': (D('.23'), D('.28'))},
])
def test_floor_rejects_enlarged_base_ratios_and_wrong_side_targets(overrides):
    assert not floor(**overrides).allowed


def test_floor_requires_explicit_targets_while_legacy_default_is_preserved():
    assert not floor(take_profit_prices=None).allowed
    assert risk.assess_entry(**inputs(allocation=D(1000), take_profit_prices=None)).allowed


def test_audit_reconstruction_rejects_invalid_direction_without_type_error():
    contract = inputs(budget_version=STRICT, direction=[])
    with pytest.raises(ValueError):
        risk.assess_budget_inputs(contract)


@pytest.mark.parametrize('field', ['entry_price', 'allocation', 'fee_rate'])
def test_floor_fails_closed_on_finite_decimal_arithmetic_overflow(field):
    decision = floor(**{field: D('1e1000005')})
    assert not decision.allowed


@pytest.mark.parametrize('open_risk,fee', [
    ('9.000000000000000000000000001', '.0005'),
    ('10', '.0005000000000000000000000001'),
])
def test_floor_preserves_minimum_risk_without_portfolio_decimal_cancellation(open_risk, fee):
    from decimal import localcontext

    decision = floor(existing_open_risk=D(open_risk), fee_rate=D(fee))
    assert decision.allowed
    assert decision.quantity == decision.minimum_quantity == D(23)
    assert decision.worst_loss == decision.minimum_risk == decision.risk_limit
    # Verify metadata against exact products/sums, independently of ambient
    # precision; no epsilon may excuse an understated budget or pressure loss.
    with localcontext() as context:
        context.prec = 120
        adverse_stop = D('.30') * D('1.02')
        expected_loss = D(23) * (adverse_stop - D('.24') + D(fee) * (D('.24') + adverse_stop))
        assert decision.minimum_risk == expected_loss
        assert decision.effective_portfolio_limit == D(open_risk) + expected_loss
        assert decision.effective_portfolio_limit - D(open_risk) == decision.risk_limit
        assert decision.quantity * D('.24') / D(2) <= D(20)
