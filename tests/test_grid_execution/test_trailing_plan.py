"""Pure bounded trailing policy, independent of the journal and exchange."""
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, ROUND_FLOOR

import pytest

from strategies.grid.execution.planner import PlanLimitExceeded
from strategies.grid.execution.trailing_plan import (
    TrailingPolicy, new_order_quantity, next_shift, price_at, termination,
)

D = Decimal


def policy(**changes):
    base = TrailingPolicy(D('100'), D('140'), 4, 'ARITHMETIC', D('.01'),
                          D('.001'), D('.01'), D('5'), D('50'), D('200'),
                          D('180'), D('60'), D('20'))
    return replace(base, **changes)


def test_absolute_arithmetic_levels_and_immutable_policy():
    p = policy()
    assert [price_at(p, i) for i in (-2, 0, 4, 7)] == list(map(D, ['80', '100', '140', '170']))
    with pytest.raises(FrozenInstanceError):
        p.raw_lower = D('99')


@pytest.mark.parametrize('index', [-3, -1, 0, 1, 4, 8])
def test_geometric_signed_levels_match_initial_planner_formula(index):
    p = policy(mode='GEOMETRIC', raw_lower=D('99.123'), raw_upper=D('153.987'))
    expected = p.raw_lower * ((p.raw_upper / p.raw_lower) ** (D(index) / D(p.grid_count)))
    expected = (expected / p.tick_size).to_integral_value(rounding=ROUND_FLOOR) * p.tick_size
    assert price_at(p, index) == expected


def test_arithmetic_spacing_is_derived_from_raw_boundaries_without_compounding_rounding():
    p = policy(raw_lower=D('100.009'), raw_upper=D('140.019'))
    assert price_at(p, 8) == D('180.02')
    assert price_at(p, -1) == D('90.00')


def test_inclusive_up_trigger_adds_buy_at_old_upper_and_only_one_step():
    p = policy()
    assert next_shift(p, 0, 4, D('149.99')) is None
    shift = next_shift(p, 0, 4, D('150'))
    assert (shift.direction, shift.lower_index, shift.upper_index) == ('UP', 1, 5)
    assert (shift.remove_index, shift.add_index, shift.side, shift.price) == (0, 4, 'BUY', D('140'))
    assert (shift.stop_low, shift.stop_high) == (D('50'), D('200'))
    assert shift.quantity == D('.142')
    assert next_shift(p, 0, 4, D('179')).upper_index == 5


def test_inclusive_down_trigger_adds_sell_at_old_lower():
    p = policy()
    assert next_shift(p, 0, 4, D('90.01')) is None
    shift = next_shift(p, 0, 4, D('90'))
    assert (shift.direction, shift.lower_index, shift.upper_index) == ('DOWN', -1, 3)
    assert (shift.remove_index, shift.add_index, shift.side, shift.price) == (4, 0, 'SELL', D('100'))
    assert shift.quantity == D('.2')


def test_caps_allow_exact_edge_and_disable_further_motion():
    p = policy()
    assert next_shift(p, 3, 7, D('180')).upper_index == 8
    assert next_shift(p, 4, 8, D('190')) is None
    assert next_shift(p, -3, 1, D('60')).lower_index == -4
    assert next_shift(p, -4, 0, D('59')) is None
    assert next_shift(policy(move_up=None), 0, 4, D('150')) is None
    assert next_shift(policy(move_down=None), 0, 4, D('90')) is None


def test_one_way_stops_follow_corresponding_window_boundary():
    up = next_shift(policy(move_down=None), 0, 4, D('150'))
    assert (up.stop_low, up.stop_high) == (D('60'), D('200'))
    down = next_shift(policy(move_up=None), 0, 4, D('90'))
    assert (down.stop_low, down.stop_high) == (D('50'), D('190'))
    assert next_shift(policy(move_down=None), 3, 7, D('79')) is None


@pytest.mark.parametrize('price, expected', [('50', 'LOW'), ('49', 'LOW'), ('200', 'HIGH'), ('201', 'HIGH'), ('100', None)])
def test_inclusive_termination_has_priority_over_moving(price, expected):
    p = policy()
    assert termination(D(price), p.stop_low, p.stop_high) == expected
    if expected:
        assert next_shift(p, 0, 4, D(price)) is None


def test_shift_cannot_touch_or_cross_fixed_stop():
    assert next_shift(policy(stop_high=D('150'), move_up=D('149')), 0, 4, D('149.99')) is None
    assert next_shift(policy(stop_high=D('150'), move_up=D('149')), 0, 4, D('150')) is None
    # Existing window is valid, but its proposed next lower equals the fixed stop.
    assert next_shift(policy(stop_low=D('90'), move_down=D('91')), 0, 4, D('90')) is None


def test_quantities_use_fixed_quote_budget_and_floor_to_arbitrary_step():
    p = policy(step_size=D('.003'))
    assert new_order_quantity(p, D('140')) == D('.141')
    assert new_order_quantity(p, D('100')) == D('.198')
    with pytest.raises(PlanLimitExceeded, match='quantity'):
        new_order_quantity(policy(quote_per_order=D('.1')), D('100'))
    with pytest.raises(PlanLimitExceeded, match='notional'):
        new_order_quantity(policy(min_notional=D('20')), D('140'))


@pytest.mark.parametrize('changes', [
    {'grid_count': 0}, {'grid_count': True}, {'grid_count': D('4')},
    {'mode': 'UNKNOWN'}, {'raw_lower': D('0')}, {'raw_upper': D('99')},
    {'tick_size': D('0')}, {'step_size': D('-1')}, {'min_qty': D('NaN')},
    {'min_notional': D('Infinity')}, {'stop_low': D('100')},
    {'stop_high': D('140')}, {'stop_low': D('-1')},
    {'move_up': D('130')}, {'move_down': D('110')},
    {'move_up': D('200')}, {'move_down': D('50')},
    {'quote_per_order': D('0')}, {'move_up': D('NaN')},
])
def test_invalid_policy_is_rejected(changes):
    with pytest.raises(ValueError):
        price_at(policy(**changes), 0)


@pytest.mark.parametrize('value', ['NaN', 'sNaN', 'Infinity', '-Infinity', '0', '-1'])
def test_invalid_live_price_is_rejected(value):
    for call in (lambda: termination(D(value), D('50'), D('200')),
                 lambda: next_shift(policy(), 0, 4, D(value)),
                 lambda: new_order_quantity(policy(), D(value))):
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize('bounds', [('200', '50'), ('50', '50'), ('NaN', '200'), ('0', '200')])
def test_invalid_terminal_bounds_are_rejected(bounds):
    with pytest.raises(ValueError):
        termination(D('100'), *map(D, bounds))


def test_rejects_invalid_indices_and_nonmonotonic_rounded_levels():
    for lower, upper in [(0, 3), (True, 5), (0, D('4'))]:
        with pytest.raises(ValueError):
            next_shift(policy(), lower, upper, D('150'))
    for index in [True, D('1'), -11]:
        with pytest.raises(ValueError):
            price_at(policy(), index)
    with pytest.raises(ValueError, match='overlap'):
        price_at(policy(tick_size=D('20')), 0)


def test_down_cap_can_preclude_nonpositive_arithmetic_predecessor():
    p = policy(raw_upper=D('1000'), grid_count=1, stop_high=D('1500'),
               move_up=None, move_down=D('60'))
    assert next_shift(p, 0, 1, D('90')) is None
