"""Execution proof admits only bounded exchange entryPrice serialization."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from strategies.hrs.equity_evidence import (
    EvidenceBlocked, canonical, certify_opening_inventory,
    owned_fill_positions, prove_positions,
)

T = datetime(2026, 11, 19, 13, 53, tzinfo=timezone.utc)
SYMBOL = 'ETNUSDT'
REPORTED = '338.5227586207'


def etn_evidence(strategy='new_coin', reported=REPORTED):
    # Actual signed order 320067, with its complete two-fill execution.
    fills = [dict(account_id='test', symbol=SYMBOL, trade_id=trade_id,
                  order_id=320067, side='SELL', qty=qty, price=price,
                  trade_time_utc=T)
             for trade_id, qty, price in [(8898, '.07', '338.72'), (8899, '.22', '338.46')]]
    orders = [dict(account_id='test', symbol=SYMBOL, order_id='320067', strategy=strategy)]
    inventory = [dict(symbol=SYMBOL, positionAmt='-.29', entryPrice=reported)]
    independent = [dict(symbol=SYMBOL, positionAmt='-.29')]
    local = ({SYMBOL: dict(direction='short', remaining_quantity='.29', position_id='hrs-etn')}
             if strategy == 'hrs' else {})
    marks = {SYMBOL: dict(symbol=SYMBOL, markPrice='338.50', time=int(T.timestamp() * 1000))}
    return inventory, independent, local, fills, orders, marks


def prove(evidence, **kwargs):
    return prove_positions('test', 'existing-segment', T, *evidence, bootstrap=False, **kwargs)


@pytest.mark.parametrize('strategy', ['new_coin', 'hrs'])
def test_actual_etn_full_fills_match_reported_serialization_without_mutating_evidence(strategy):
    evidence = etn_evidence(strategy)
    original = deepcopy(evidence)
    original_hash = canonical(evidence)[1]
    computed = owned_fill_positions('test', evidence[3], evidence[4])[(strategy, SYMBOL)][1]
    assert computed == D('338.5227586206896551724137931')
    assert computed != D(REPORTED)
    result = prove(evidence)
    assert len(result) == (1 if strategy == 'hrs' else 0)
    if result:
        assert result[0].entry_price == D(REPORTED)
    assert evidence == original
    assert canonical(evidence)[1] == original_hash
    assert owned_fill_positions('test', evidence[3], evidence[4])[(strategy, SYMBOL)][1] == computed


@pytest.mark.parametrize('strategy', ['new_coin', 'hrs'])
def test_existing_inventory_certification_uses_same_reported_serialization_contract(strategy):
    inventory, independent, local, fills, orders, marks = etn_evidence(strategy)
    result, opening = certify_opening_inventory(
        'test', 'existing-segment', T, inventory, independent, local, fills, orders, marks,
        [dict(id=1, symbol=SYMBOL, strategy=strategy)], covered_symbols={SYMBOL})
    assert len(result) == (1 if strategy == 'hrs' else 0)
    assert opening == (dict(strategy=strategy, symbol=SYMBOL, quantity='-0.29', entry_price=REPORTED),)


def matches(computed, reported):
    from strategies.hrs.equity_evidence import execution_basis_matches_reported
    return execution_basis_matches_reported(computed, reported)


@pytest.mark.parametrize(('computed', 'reported', 'expected'), [
    ('100', '100', True),
    ('338.5227586206896551724137931', REPORTED, True),
    ('100.000000000049999999', '100.0000000000', True),
    ('100.000000000050000001', '100.0000000000', False),
    ('100.00000000005', '100.0000000001', True),  # HALF_UP tie goes upward.
    ('100.00000000005', '100.0000000000', False),
    ('99.99999999995', '100.0000000000', True),
    ('99.999999999949999999', '100.0000000000', False),
    ('100.000000004999', '100', True),  # Coarse display capped at 1e-8.
    ('100.000000005001', '100', False),
    ('100.000000004999', '100.00', True),
    ('100.000000005001', '100.00', False),
    ('100.1', '100', False),
    ('100.001', '100.00', False),
    ('100.000000000049', '100.000000000000', False),  # Trailing zeros only narrow.
    (REPORTED, '338.5227586206896551724137931', False),  # Asymmetric.
])
def test_matcher_uses_only_capped_reported_decimal_quantum(computed, reported, expected):
    assert matches(D(computed), D(reported)) is expected


@pytest.mark.parametrize('invalid', [None, True, False, '', 'bad', 'NaN', 'sNaN', 'Infinity', '-Infinity', '0', '-1'])
def test_matcher_invalid_values_fail_closed_in_either_argument(invalid):
    assert matches(invalid, '100') is False
    assert matches('100', invalid) is False


def test_matcher_preserves_large_decimal_digits_despite_low_ambient_precision():
    from decimal import Inexact, Rounded, localcontext
    prefix = '1234567890' * 6
    with localcontext() as context:
        context.prec = 6
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        assert matches(prefix + '.123456789049', prefix + '.1234567890')
        assert not matches(prefix + '.123456789051', prefix + '.1234567890')
        assert context.prec == 6
        assert context.traps[Inexact] and context.traps[Rounded]


def test_matcher_bounds_nonexact_work_without_breaking_exact_equality():
    assert matches('1E+1000000', '1E+1000000')
    assert matches('1E-256', '1.1E-256') is False
    assert matches('1E+257', '1.1E+257') is False
    assert matches('1E-257', '1.1E-257') is False
    assert matches('1.' + '1' * 256, '1.11111111') is False
    # Supported coefficient/exponent boundary still has enough working precision.
    assert matches('1' * 245 + '.12345678904', '1' * 245 + '.1234567890')


@pytest.mark.parametrize('strategy', ['new_coin', 'hrs'])
def test_rounding_never_relaxes_missing_conflicting_or_multiple_position_owners(strategy):
    for change in ('missing', 'conflicting', 'multiple_positions'):
        evidence = etn_evidence(strategy)
        fills, orders = evidence[3:5]
        if change == 'missing':
            orders.clear()
        elif change == 'conflicting':
            orders.append(dict(orders[0], strategy='grid'))
        else:
            # Two economic owners with the same account net must remain blocked.
            fills[0]['order_id'] = 320068
            orders.append(dict(orders[0], order_id='320068', strategy='grid'))
        with pytest.raises(EvidenceBlocked, match='(fill_order_owner|position_order_owner)_unproven'):
            prove(evidence)


@pytest.mark.parametrize('strategy', ['new_coin', 'hrs'])
def test_rounding_never_relaxes_exact_fill_or_independent_inventory_quantity(strategy):
    for change in ('fill', 'independent'):
        evidence = etn_evidence(strategy)
        if change == 'fill':
            evidence[3][0]['qty'] = '.0700000000000001'
        else:
            evidence[1][0]['positionAmt'] = '-.2900000000000001'
        with pytest.raises(EvidenceBlocked, match='(position_order_owner_unproven|position_quantity_inventory_conflict)'):
            prove(evidence)


def test_independently_reported_basis_remains_exact_and_bootstrap_stays_flat_only():
    evidence = etn_evidence()
    evidence[1][0]['entryPrice'] = '338.5227586206896551724137931'
    with pytest.raises(EvidenceBlocked, match='position_quantity_inventory_conflict'):
        prove(evidence)
    evidence = etn_evidence()
    with pytest.raises(EvidenceBlocked, match='bootstrap_opening_inventory_unproven'):
        prove_positions('test', 'new-segment', T, *evidence, bootstrap=True)


@pytest.mark.parametrize('reported', ['338.5227586206', '338.52', '338', '338.6'])
def test_non_serialization_basis_difference_blocks_both_proof_entry_points(reported):
    evidence = etn_evidence(reported=reported)
    with pytest.raises(EvidenceBlocked, match='position_order_owner_unproven'):
        prove(evidence)
    inventory, independent, local, fills, orders, marks = evidence
    with pytest.raises(EvidenceBlocked, match='opening_position_order_fill_proof_missing'):
        certify_opening_inventory('test', 'seg', T, inventory, independent, local, fills, orders, marks,
            [dict(id=1, symbol=SYMBOL, strategy='new_coin')], covered_symbols={SYMBOL})


def test_reported_rounding_does_not_replace_certified_history_or_require_reanchor():
    evidence = etn_evidence('hrs')
    inventory, independent, local, fills, orders, marks = evidence
    # Continue a pre-existing certified zero anchor; the original fills remain
    # the basis source and the current reported price remains the valuation input.
    result = prove(evidence, inception=T.replace(day=17), opening_inventory=())
    assert result[0].entry_price == D(REPORTED)
    # Claims, coverage and local quantity are still required in the certification path.
    with pytest.raises(EvidenceBlocked, match='opening_owner_claim_unproven'):
        certify_opening_inventory('test', 'seg', T, inventory, independent, local, fills, orders, marks,
            [], covered_symbols={SYMBOL})
    with pytest.raises(EvidenceBlocked, match='opening_fill_coverage_missing'):
        certify_opening_inventory('test', 'seg', T, inventory, independent, local, fills, orders, marks,
            [dict(id=1, symbol=SYMBOL, strategy='hrs')], covered_symbols=set())
    local[SYMBOL]['remaining_quantity'] = '.2900000000000001'
    with pytest.raises(EvidenceBlocked, match='position_quantity_local_conflict'):
        prove(evidence)
