from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from strategies.hrs.equity_evidence import (
    EvidenceBlocked, allocation_evidence, attributed_incomes, prove_positions,
    flat_boundary_observation,
)
from strategies.hrs.risk_ledger import LedgerObservation, initialize_ledger,reconcile_ledger

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
    with pytest.raises(EvidenceBlocked,match='opening_inventory'):
        prove_positions('test', 'seg', T, inventory, inventory, {}, [fill()],
                        [owner(strategy='btc_eth')], {}, bootstrap=True)
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


def test_partial_cached_other_owner_fills_cannot_certify_a_new_flat_hrs_anchor():
    # Missing historical HRS entry/exit facts can leave a misleading exact-looking
    # other-strategy subtotal. No certified opening point exists for this stream.
    inventory=[dict(symbol='BTCUSDT',positionAmt='2',entryPrice='100')]
    with pytest.raises(EvidenceBlocked,match='opening_inventory'):
        prove_positions('test','seg',T,inventory,inventory,{},[fill()],
                        [owner(strategy='btc_eth')],{},bootstrap=True)


def test_allocation_journal_replays_each_committed_delta_and_checks_full_chain():
    from strategies.hrs.equity_evidence import allocation_event_changes
    base=dict(id=1,month='2026-11',status='active',created_at='2026-11-01T00:00:00',
              entries=[dict(strategy_id='hrs',allocated_amount='1000')])
    deposit=dict(base,entries=[dict(strategy_id='hrs',allocated_amount='2000')])
    events=[dict(event_id=1,transaction_id=10,statement_at=T+timedelta(seconds=1),operation='UPDATE',old_row=base,new_row=deposit),
            dict(event_id=2,transaction_id=11,statement_at=T+timedelta(seconds=2),operation='UPDATE',old_row=deposit,new_row=base)]
    changes=allocation_event_changes([base],events,[base],T,T+timedelta(seconds=3))
    assert [change.delta for change in changes]==[D('1000'),D('-1000')]
    with pytest.raises(EvidenceBlocked,match='allocation_event_chain'):
        allocation_event_changes([base],events[1:],[base],T,T+timedelta(seconds=3))
    # One atomic transaction exposes only its final selected allocation.
    atomic=[dict(event,transaction_id=10) for event in events]
    assert allocation_event_changes([base],atomic,[base],T,T+timedelta(seconds=3))==()


def test_allocation_status_activation_cannot_hide_a_material_selected_balance():
    from strategies.hrs.equity_evidence import allocation_event_changes
    base=dict(id=1,month='2026-11',status='active',created_at='2026-11-01T00:00:00',
              entries=[dict(strategy_id='hrs',allocated_amount='1000')])
    future=dict(base,id=2,status='draft',created_at='2026-11-02T00:00:00',
                entries=[dict(strategy_id='hrs',allocated_amount='2000')])
    active=dict(future,status='active')
    events=[dict(event_id=1,transaction_id=10,statement_at=T+timedelta(seconds=1),operation='UPDATE',old_row=future,new_row=active),
            dict(event_id=2,transaction_id=11,statement_at=T+timedelta(seconds=2),operation='UPDATE',old_row=active,new_row=future)]
    result=allocation_event_changes([base,future],events,[base,future],T,T+timedelta(seconds=3))
    assert [change.delta for change in result]==[D('1000'),D('-1000')]


def test_allocation_transaction_crossing_calendar_selection_boundary_is_not_backdated():
    from strategies.hrs.equity_evidence import allocation_event_changes
    before=datetime(2026,11,30,15,tzinfo=timezone.utc)
    midnight=datetime(2026,11,30,16,tzinfo=timezone.utc)
    november=dict(id=1,month='2026-11',status='active',created_at='2026-11-01T00:00:00',entries=[dict(strategy_id='hrs',allocated_amount='1000')])
    december=dict(id=2,month='2026-12',status='draft',created_at='2026-11-01T00:00:00',entries=[dict(strategy_id='hrs',allocated_amount='2000')])
    active=dict(december,status='active')
    retired=dict(november,status='draft')
    events=[dict(event_id=1,transaction_id=10,statement_at=midnight-timedelta(minutes=1),operation='UPDATE',old_row=december,new_row=active),
            dict(event_id=2,transaction_id=10,statement_at=midnight+timedelta(minutes=1),operation='UPDATE',old_row=november,new_row=retired)]
    with pytest.raises(EvidenceBlocked,match='allocation_calendar_transaction'):
        allocation_event_changes([november,december],events,[retired,active],before,midnight+timedelta(minutes=2))


def test_certified_existing_positions_require_fresh_complete_fills_and_claims():
    from strategies.hrs.equity_evidence import certify_opening_inventory
    inventory=[dict(symbol='BTCUSDT',positionAmt='2',entryPrice='100')]
    claims=[dict(id=1,symbol='BTCUSDT',strategy='btc_eth')]
    marks,opening=certify_opening_inventory('test','seg',T,inventory,inventory,{},[fill()],
        [owner(strategy='btc_eth')],{},claims,covered_symbols={'BTCUSDT'})
    assert marks==()
    assert opening[0]['strategy']=='btc_eth'
    assert opening[0]['quantity']=='2'
    with pytest.raises(EvidenceBlocked,match='opening_fill_coverage'):
        certify_opening_inventory('test','seg',T,inventory,inventory,{},[fill()],
            [owner(strategy='btc_eth')],{},claims,covered_symbols=set())
    with pytest.raises(EvidenceBlocked,match='opening_owner_claim'):
        certify_opening_inventory('test','seg',T,inventory,inventory,{},[fill()],
            [owner(strategy='btc_eth')],{},[],covered_symbols={'BTCUSDT'})


def test_funding_ownership_is_reconstructed_backwards_from_current_verified_inventory():
    from strategies.hrs.equity_evidence import attribute_funding_from_inventory
    event=dict(account_id='test',tran_id='1',income_type='FUNDING_FEE',symbol='BTCUSDT',asset='USDT',
               amount='-2',strategy=None,reason='funding_owner_unproven',event_time_utc=T.isoformat())
    report=dict(events=[event],unattributed=[dict(event)],coverage_gaps=[],missing_income_fills=[],account_coverage_complete=True,
                id_match_and_coverage_complete=False)
    # The current position is flat. Its exact HRS close AFTER funding proves the
    # prior owned quantity when the intervening full fill stream is complete.
    close=fill(trade_id=2,order_id=11,side='SELL',at=T+timedelta(minutes=1))
    result=attribute_funding_from_inventory(report,'test',T+timedelta(minutes=2),[close],[owner(11)],(),coverage_complete=True)
    assert result['events'][0]['strategy']=='hrs'
    assert result['events'][0]['attribution_source']=='verified_fill_inventory_interval_v1'
    assert result['id_match_and_coverage_complete']
    assert result['by_strategy']['hrs']['funding_usdt']=='-2'
    assert 'reviewed_by' not in result['events'][0]
    with pytest.raises(EvidenceBlocked,match='funding_fill_coverage'):
        attribute_funding_from_inventory(report,'test',T+timedelta(minutes=2),[close],[owner(11)],(),coverage_complete=False)


def test_nonflat_midnight_uses_actual_closed_historical_mark_with_real_timestamp():
    from strategies.hrs.equity_evidence import historical_boundary_observation
    from strategies.hrs.risk_ledger import PositionMark
    initial=LedgerObservation(T,'seg',(),(),(PositionMark('p','seg',D('2'),D('100'),D('100'),T,True),),T,T,True,True)
    state=initialize_ledger('seg',D('1000'),initial)
    boundary=T+timedelta(hours=1)
    end=int(boundary.timestamp()*1000)-1
    bars={f'BTCUSDT:{int(boundary.timestamp()*1000)}':dict(bar=[end-59999,'100','100','99','99','0',end],available_at=boundary+timedelta(seconds=30))}
    opening=(dict(strategy='hrs',symbol='BTCUSDT',quantity='2',entry_price='100'),)
    obs=historical_boundary_observation(state,boundary,boundary+timedelta(seconds=30),[],[],(),
        coverage_complete=True,opening_inventory=opening,historical_bars=bars,account='test')
    assert obs.positions[0].marked_at==boundary-timedelta(milliseconds=1)
    assert obs.positions[0].mark_price==D('99')
    assert reconcile_ledger(state,obs).equity==D('998')
    with pytest.raises(EvidenceBlocked,match='historical_mark'):
        historical_boundary_observation(state,boundary,boundary+timedelta(seconds=30),[],[],(),
            coverage_complete=True,opening_inventory=opening,historical_bars={},account='test')


def test_known_held_funding_deadline_blocks_empty_published_page_until_receipt_or_proven_zero():
    from strategies.hrs.equity_evidence import require_funding_publication
    due=T+timedelta(seconds=30)
    now=T+timedelta(seconds=60)
    inventory=(dict(strategy='hrs',symbol='BTCUSDT',quantity='1',entry_price='100'),)
    forecasts=[dict(symbol='BTCUSDT',source_time=T,received_at=T,next_funding_time=due),
               dict(symbol='BTCUSDT',source_time=now,received_at=now,next_funding_time=now+timedelta(hours=1))]
    report=dict(events=[])
    with pytest.raises(EvidenceBlocked,match='funding_settlement_pending'):
        require_funding_publication('test',T,now,[],[],inventory,forecasts,{},report)
    published={'BTCUSDT':[dict(fundingTime=int(due.timestamp()*1000),fundingRate='0',markPrice='100')]}
    proof=require_funding_publication('test',T,now,[],[],inventory,forecasts,published,report)
    assert proof[0]['resolution']=='authoritative_zero_rate'
    nonzero={'BTCUSDT':[dict(fundingTime=int(due.timestamp()*1000),fundingRate='.001',markPrice='100')]}
    with pytest.raises(EvidenceBlocked,match='funding_settlement_pending'):
        require_funding_publication('test',T,now,[],[],inventory,forecasts,nonzero,report)
    receipt=dict(account_id='test',symbol='BTCUSDT',income_type='FUNDING_FEE',tran_id='55',strategy='hrs',asset='USDT',amount='-.1',event_time_utc=(due+timedelta(milliseconds=27)).isoformat())
    assert require_funding_publication('test',T,now,[],[],inventory,forecasts,nonzero,dict(events=[receipt]))[0]['resolution']=='income_receipt'


def test_independent_pm_account_quantity_inventory_does_not_invent_missing_entry_price():
    from strategies.hrs.equity_evidence import certify_opening_inventory
    inventory=[dict(symbol='BTCUSDT',positionAmt='2',entryPrice='100')]
    independent=[dict(symbol='BTCUSDT',positionAmt='2',positionSide='BOTH')]
    _,opening=certify_opening_inventory('test','seg',T,inventory,independent,{},[fill()],
        [owner(strategy='btc_eth')],{},[dict(id=1,symbol='BTCUSDT',strategy='btc_eth')],covered_symbols={'BTCUSDT'})
    assert opening[0]['entry_price']=='100'
    with pytest.raises(EvidenceBlocked,match='inventory_conflict'):
        certify_opening_inventory('test','seg',T,inventory,[dict(independent[0],positionAmt='1')],{},[fill()],
            [owner(strategy='btc_eth')],{},[dict(id=1,symbol='BTCUSDT',strategy='btc_eth')],covered_symbols={'BTCUSDT'})


def test_reduce_only_close_owner_requires_complete_zero_anchor_and_unique_owned_lifecycle():
    from strategies.hrs.equity_evidence import certify_reduce_only_lifecycle
    entered=fill(trade_id=1,order_id=10,side='SELL',at=T-timedelta(days=1))
    closed=fill(trade_id=2,order_id=11,side='BUY',at=T-timedelta(hours=1))
    orders=[owner(10,strategy='new_coin')]
    response=dict(orderId=11,symbol='BTCUSDT',side='BUY',status='FILLED',reduceOnly=True,executedQty='2',origQty='2')
    proof=certify_reduce_only_lifecycle('test','BTCUSDT',D('0'),[entered,closed],orders,{11:response},T-timedelta(days=2),T,coverage_complete=True)
    assert proof[0]['strategy']=='new_coin' and proof[0]['order_id']=='11'
    assert proof[0]['certified_start_quantity']=='0'
    with pytest.raises(EvidenceBlocked,match='lifecycle_fill_coverage'):
        certify_reduce_only_lifecycle('test','BTCUSDT',D('0'),[entered,closed],orders,{11:response},T-timedelta(days=2),T,coverage_complete=False)
    with pytest.raises(EvidenceBlocked,match='lifecycle_zero_anchor'):
        certify_reduce_only_lifecycle('test','BTCUSDT',D('0'),[closed],orders,{11:response},T-timedelta(days=2),T,coverage_complete=True)
    with pytest.raises(EvidenceBlocked,match='lifecycle_reduce_only'):
        certify_reduce_only_lifecycle('test','BTCUSDT',D('0'),[entered,closed],orders,{11:dict(response,reduceOnly=False)},T-timedelta(days=2),T,coverage_complete=True)
