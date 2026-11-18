from datetime import datetime, timezone
from decimal import Decimal

import pytest

from scripts.recover_proven_order_income import archive_order_plan, closed_lot_plan, recovery_plan


def facts():
    at = datetime(2026, 8, 3, 4, 5, tzinfo=timezone.utc)
    income = dict(account_id='default', symbol='ETHUSDT', tran_id=5,
                  income_type='COMMISSION', income=Decimal('-0.1'), asset='USDT',
                  event_time_utc=at, trade_id=7)
    fill = dict(account_id='default', symbol='ETHUSDT', trade_id=7, order_id=9,
                side='BUY', trade_time_utc=at, commission=Decimal('0.1'),
                commission_asset='USDT', realized_pnl=Decimal('0'))
    trade = dict(id=11, account_id=None, symbol='ETHUSDT', order_id='9',
                 side='BUY', strategy='btc_eth', executed_at=datetime(2026, 8, 3, 12, 5))
    owner = dict(account_id='default', symbol='ETHUSDT', order_id=9, strategy='btc_eth',
                 source_kind='trade_record', source_id='11', evidence=dict(
                     source_kind='trade_record', order_id='9', source_ids=['11'],
                     account_proof='authenticated_exchange_order_symbol_side_time'))
    return income, fill, trade, owner


def test_existing_proof_recovers_exact_fee_and_is_idempotent():
    income, fill, trade, owner = facts()
    plan, digest, count = recovery_plan('default', [income], [fill], [owner], [trade], [], [])
    assert count == 1 and len(plan['derived_rows']) == 1
    assert plan['derived_rows'][0]['income'] == Decimal('-0.1')
    assert plan['order_rows'] == []
    again, same_digest, _ = recovery_plan('default', [income], [fill], [owner], [trade], [], plan['derived_rows'])
    assert again['derived_rows'] == [] and same_digest == digest


@pytest.mark.parametrize('fault', ['account', 'side', 'time', 'strategy', 'missing_source', 'amount'])
def test_mismatched_legacy_proof_never_recovers(fault):
    income, fill, trade, owner = facts()
    if fault == 'account':
        trade['account_id'] = 'other'
    elif fault == 'side':
        trade['side'] = 'SELL'
    elif fault == 'time':
        trade['executed_at'] = datetime(2026, 8, 3, 4, 5)
    elif fault == 'strategy':
        trade['strategy'] = 'hrs'
    elif fault == 'missing_source':
        owner['evidence']['source_ids'] = ['12']
    else:
        income['income'] = Decimal('-0.2')
    plan, _, _ = recovery_plan('default', [income], [fill], [owner], [trade], [], [])
    assert plan['derived_rows'] == []


def test_manual_and_prior_owner_conflicts_block_plan():
    income, fill, trade, owner = facts()
    manual = dict(income, strategy='hrs')
    plan, _, _ = recovery_plan('default', [income], [fill], [owner], [trade], [manual], [])
    assert plan['conflicts'][0]['reason'] == 'manual_owner_conflict'
    prior = dict(income, strategy='hrs', method='exact_order', evidence={}, algorithm_version=1)
    plan, _, _ = recovery_plan('default', [income], [fill], [owner], [trade], [], [prior])
    assert plan['conflicts'][0]['reason'] == 'derived_evidence_conflict'


def test_archive_creation_identity_recovers_delayed_fill():
    from datetime import timedelta

    income, fill, trade, _ = facts()
    fill['qty'] = Decimal('1')
    fill['trade_time_utc'] += timedelta(hours=2)
    archive = [{'Uid': 'account-export', 'Symbol': 'ETHUSDT', 'Order No': '9',
                'Executed Amount': '1', 'Side': 'BUY', 'Time(UTC)': '2026-08-03 04:05:00'}]
    owners = archive_order_plan('default', archive, 'a' * 64, 'download', [fill], [trade], [])
    assert len(owners) == 1
    plan, _, _ = recovery_plan('default', [income], [fill], owners, [trade], [], [])
    assert len(plan['derived_rows']) == 1
    fill['qty'] = Decimal('0.5')
    assert archive_order_plan('default', archive, 'a' * 64, 'download', [fill], [trade], []) == []
    with pytest.raises(ValueError, match='duplicate'):
        archive_order_plan('default', archive * 2, 'a' * 64, 'download', [fill], [trade], [])


def lot_facts():
    from datetime import timedelta

    income, opening, _, owner = facts()
    t = opening['trade_time_utc']
    opening.update(qty=Decimal('1'), side='BUY')
    closing = dict(opening, trade_id=8, order_id=11, side='SELL',
                   trade_time_utc=t + timedelta(seconds=10), realized_pnl=Decimal('-2'))
    loss = dict(income, income_type='REALIZED_PNL', tran_id=30, trade_id=8,
                income=Decimal('-2'), event_time_utc=closing['trade_time_utc'])
    fee = dict(loss, tran_id=31, income_type='COMMISSION', income=Decimal('-0.1'))
    funding = dict(loss, tran_id=32, income_type='FUNDING_FEE', trade_id=None,
                   income=Decimal('-0.02'), event_time_utc=t + timedelta(seconds=5))
    anchor = dict(account_id='default', symbol='ETHUSDT', exchange_qty=Decimal(0),
                  flat_at=t + timedelta(seconds=20), evidence={'signed_snapshot': 'hash'})
    cursors = [dict(account_id='default', source='userTrades', income_type=None,
                    symbol='ETHUSDT', window_start_ms=int((t - timedelta(seconds=1)).timestamp() * 1000),
                    covered_until_ms=int(anchor['flat_at'].timestamp() * 1000),
                    page_complete=True, request_status='ok')]
    return [loss, fee, funding], [opening, closing], [owner], [anchor], cursors


def test_closed_lot_recovers_reducing_fill_fee_and_funding_with_stable_repeat():
    from collections import UserDict

    events, fills, owners, anchors, cursors = lot_facts()
    fills = [UserDict(row) for row in fills]
    plan, digest, rejected = closed_lot_plan('default', events, fills, owners, anchors, cursors, [], [])
    assert rejected == [] and len(plan['derived_rows']) == 3
    assert sum(r['income'] for r in plan['derived_rows']) == Decimal('-2.12')
    assert {r['strategy'] for r in plan['derived_rows']} == {'btc_eth'}
    assert {r['method'] for r in plan['derived_rows']} == {'verified_position_interval'}
    again, same, _ = closed_lot_plan('default', events, fills, owners, anchors, cursors, [], plan['derived_rows'])
    assert again['derived_rows'] == [] and same == digest


@pytest.mark.parametrize('fault', ['unknown_open', 'conflicting_close', 'coverage_gap', 'not_flat', 'anchor_collision'])
def test_closed_lot_does_not_infer_through_missing_or_conflicting_evidence(fault):
    events, fills, owners, anchors, cursors = lot_facts()
    if fault == 'unknown_open':
        owners = []
    elif fault == 'conflicting_close':
        owners.append(dict(owners[0], order_id=11, strategy='hrs'))
    elif fault == 'coverage_gap':
        cursors[0]['page_complete'] = False
    elif fault == 'not_flat':
        anchors[0]['exchange_qty'] = Decimal('1')
    else:
        anchors[0]['flat_at'] = fills[-1]['trade_time_utc']
    if fault in ('not_flat', 'anchor_collision'):
        with pytest.raises(ValueError):
            closed_lot_plan('default', events, fills, owners, anchors, cursors, [], [])
    else:
        plan, _, rejected = closed_lot_plan('default', events, fills, owners, anchors, cursors, [], [])
        assert plan['derived_rows'] == [] and rejected


def test_unrelated_later_unknown_lot_does_not_change_proven_earlier_lot():
    from datetime import timedelta

    events, fills, owners, anchors, cursors = lot_facts()
    t = fills[0]['trade_time_utc']
    fills.extend([dict(fills[0], trade_id=20, order_id=20, trade_time_utc=t + timedelta(seconds=30)),
                  dict(fills[1], trade_id=21, order_id=21, trade_time_utc=t + timedelta(seconds=35))])
    anchors[0]['flat_at'] = t + timedelta(seconds=40)
    cursors[0]['covered_until_ms'] = int(anchors[0]['flat_at'].timestamp() * 1000)
    plan, _, rejected = closed_lot_plan('default', events, fills, owners, anchors, cursors, [], [])
    assert len(plan['derived_rows']) == 3 and rejected == []


def test_closed_lot_preserves_manual_conflict_and_funding_boundary_unknown():
    events, fills, owners, anchors, cursors = lot_facts()
    events[-1]['event_time_utc'] = fills[-1]['trade_time_utc']
    manual = dict(events[0], strategy='hrs')
    plan, _, _ = closed_lot_plan('default', events, fills, owners, anchors, cursors, [manual], [])
    assert plan['conflicts']
    assert not any(r['income_type'] == 'FUNDING_FEE' for r in plan['derived_rows'])


def test_same_millisecond_close_reopen_close_uses_sequential_position_owner():
    events, fills, owners, anchors, cursors = lot_facts()
    stamp = fills[-1]['trade_time_utc']
    fills.extend([dict(fills[0], trade_id=9, order_id=12, trade_time_utc=stamp),
                  dict(fills[1], trade_id=10, order_id=13, trade_time_utc=stamp)])
    owners.append(dict(owners[0], order_id=12, strategy='hrs'))
    event = dict(events[0], tran_id=40, trade_id=10)
    plan, _, _ = closed_lot_plan('default', [events[0], event], fills, owners, anchors, cursors, [], [])
    assert {r['tran_id']: r['strategy'] for r in plan['derived_rows']} == {30: 'btc_eth', 40: 'hrs'}


def test_reversal_fill_is_excluded_and_subsequent_reduction_uses_new_owner():
    from datetime import timedelta

    events, fills, owners, anchors, cursors = lot_facts()
    fills[1]['qty'] = Decimal('2')
    fills.append(dict(fills[0], trade_id=9, order_id=12,
                      trade_time_utc=fills[-1]['trade_time_utc'] + timedelta(seconds=2),
                      realized_pnl=Decimal('-3')))
    # A cross-strategy reversal requires a distinct ownership allocation contract.
    owners.append(dict(owners[0], order_id=11))
    event = dict(events[0], tran_id=40, trade_id=9, income=Decimal('-3'),
                 event_time_utc=fills[-1]['trade_time_utc'])
    plan, _, _ = closed_lot_plan('default', [events[0], event], fills, owners, anchors, cursors, [], [])
    assert [r['tran_id'] for r in plan['derived_rows']] == [40]


def test_closed_lot_scope_requires_exact_signed_anchor_and_no_trade_in_snapshot_gap():
    from scripts.recover_proven_order_income import validate_closed_lot_anchors

    point = datetime(2026, 11, 13, 9, 17, 51, 27000, tzinfo=timezone.utc)
    cutoff = int(point.timestamp() * 1000)
    evidence = dict(cutoff_ms=cutoff, snapshot_observed_ms=cutoff+50,
                    position_side='BOTH', gap_trade_count=0, position_payload_sha256='a'*64)
    row = dict(account_id='default', symbol='WLDUSDT', flat_at=point,
               exchange_qty=Decimal('0'), evidence=evidence)
    validate_closed_lot_anchors([row], 'default', ['WLDUSDT'], point)
    for bad in ([row, row], [], [dict(row, account_id='other')], [dict(row, symbol='ETHUSDT')],
                [dict(row, evidence=dict(evidence, gap_trade_count=1))],
                [dict(row, evidence=dict(evidence, cutoff_ms=cutoff-1))],
                [dict(row, evidence=dict(evidence, position_payload_sha256='invalid'))],
                [dict(row, flat_at=datetime(2026, 11, 12, tzinfo=timezone.utc))]):
        with pytest.raises(ValueError):
            validate_closed_lot_anchors(bad, 'default', ['WLDUSDT'], point)
