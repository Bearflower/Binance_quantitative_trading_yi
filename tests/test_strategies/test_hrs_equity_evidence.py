from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from strategies.hrs.equity_evidence import (
    EvidenceBlocked, allocation_evidence, attributed_incomes, prove_positions,
    flat_boundary_observation,
)
from strategies.hrs.risk_ledger import LedgerObservation, initialize_ledger

T = datetime(2026, 11, 17, 15, tzinfo=timezone.utc)


def fill(trade_id=1, order_id=10, side='BUY', qty='2', at=T, symbol='BTCUSDT'):
    return dict(account_id='test', symbol=symbol, trade_id=trade_id, order_id=order_id,
                side=side, qty=D(qty), price=D('100'), commission=D('0'),
                commission_asset='USDT', realized_pnl=D('0'), trade_time_utc=at)


def owner(order_id=10, strategy='hrs'):
    return dict(account_id='test', symbol='BTCUSDT', order_id=str(order_id), strategy=strategy)


def test_allocation_requires_current_explicit_db_amount_and_rejects_ambiguity():
    row = dict(id=4, month='2026-11', created_at=datetime(2026, 11, 1),
               entries=[dict(strategy_id='hrs', allocated_amount='1000')])
    proof = allocation_evidence(row, T)
    assert proof.amount == D('1000')
    assert proof.effective_at.tzinfo is not None
    with pytest.raises(EvidenceBlocked, match='allocation_month'):
        allocation_evidence(dict(row, month='2026-10'), T)
    with pytest.raises(EvidenceBlocked, match='allocation_identity'):
        allocation_evidence(dict(row, entries=row['entries'] * 2), T)


def test_income_report_must_cover_all_sources_and_never_count_fill_amounts_twice():
    event = dict(account_id='test', tran_id='5', income_type='REALIZED_PNL', symbol='BTCUSDT',
                 asset='USDT', amount='-3', strategy='hrs', reason=None, event_time_utc=T.isoformat())
    report = dict(id_match_and_coverage_complete=True, coverage_gaps=[], missing_income_fills=[],
                  events=[event], unattributed=[])
    result = attributed_incomes(report, 'test', 'seg')
    assert len(result) == 1 and result[0].amount == D('-3')
    assert result[0].event_id == 'REALIZED_PNL:5'
    for changed in (dict(report, id_match_and_coverage_complete=False),
                    dict(report, events=[dict(event, asset='BNB')]),
                    dict(report, events=[dict(event, strategy=None, reason='funding_owner_unproven')])):
        with pytest.raises(EvidenceBlocked):
            attributed_incomes(changed, 'test', 'seg')


def test_exact_order_fills_current_inventory_and_local_position_must_agree():
    inventory = [dict(symbol='BTCUSDT', positionAmt='2', entryPrice='100')]
    local = {'BTCUSDT': dict(direction='long', remaining_quantity=D('2'), position_id='p1')}
    marks = {'BTCUSDT': dict(symbol='BTCUSDT', markPrice='98', time=int(T.timestamp()*1000))}
    result = prove_positions('test', 'seg', T, inventory, inventory, local,
                             [fill()], [owner()], marks, bootstrap=False)
    assert result[0].quantity == D('2')
    assert result[0].mark_price == D('98')
    with pytest.raises(EvidenceBlocked, match='position_quantity'):
        prove_positions('test', 'seg', T, [dict(inventory[0], positionAmt='3')], inventory,
                        local, [fill()], [owner()], marks, bootstrap=False)
    with pytest.raises(EvidenceBlocked, match='order_owner'):
        prove_positions('test', 'seg', T, inventory, inventory, local,
                        [fill()], [], marks, bootstrap=False)


def test_bootstrap_rejects_nonflat_hrs_and_unknown_account_positions():
    inventory = [dict(symbol='BTCUSDT', positionAmt='2', entryPrice='100')]
    for orders in ([owner()], []):
        with pytest.raises(EvidenceBlocked):
            prove_positions('test', 'seg', T, inventory, inventory, {},
                            [fill()], orders, {}, bootstrap=True)
    assert prove_positions('test', 'seg', T, [], [], {}, [], [], {}, bootstrap=True) == ()


def test_other_strategy_position_needs_exact_fill_proof_and_never_enters_hrs_nav():
    inventory = [dict(symbol='BTCUSDT', positionAmt='2', entryPrice='100')]
    assert prove_positions('test', 'seg', T, inventory, inventory, {}, [fill()],
                           [owner(strategy='btc_eth')], {}, bootstrap=True) == ()
    with pytest.raises(EvidenceBlocked):
        prove_positions('test', 'seg', T, inventory, inventory, {}, [],
                        [owner(strategy='btc_eth')], {}, bootstrap=True)


def test_flat_boundary_reconstructs_net_cash_at_midnight_without_using_current_flatness():
    obs = LedgerObservation(T, 'seg', (), (), (), T, T, True, True)
    state = initialize_ledger('seg', D('1000'), obs)
    boundary = datetime(2026, 11, 17, 16, tzinfo=timezone.utc)
    # A completed roundtrip BEFORE midnight is flat at the boundary.
    fills = [fill(at=T + timedelta(minutes=1)), fill(2, 11, 'SELL', at=T + timedelta(minutes=2))]
    result = flat_boundary_observation(state, boundary, boundary + timedelta(minutes=1),
                                       fills, [owner(), owner(11)], (), coverage_complete=True)
    assert result.as_of == boundary
    assert result.positions == ()
    with pytest.raises(EvidenceBlocked, match='boundary_not_flat'):
        flat_boundary_observation(state, boundary, boundary + timedelta(minutes=1),
                                  [fill(at=T + timedelta(minutes=1))], [owner()], (), coverage_complete=True)
    with pytest.raises(EvidenceBlocked, match='boundary_coverage'):
        flat_boundary_observation(state, boundary, boundary + timedelta(minutes=1),
                                  [], [], (), coverage_complete=False)


def test_unattributed_non_usdt_or_stale_mark_never_becomes_zero():
    inventory = [dict(symbol='BTCUSDT', positionAmt='2', entryPrice='100')]
    local = {'BTCUSDT': dict(direction='long', remaining_quantity='2', position_id='p1')}
    with pytest.raises(EvidenceBlocked, match='mark_stale'):
        prove_positions('test', 'seg', T, inventory, inventory, local, [fill()], [owner()],
                        {'BTCUSDT': dict(symbol='BTCUSDT', markPrice='98', time=int((T-timedelta(minutes=3)).timestamp()*1000))}, bootstrap=False)
