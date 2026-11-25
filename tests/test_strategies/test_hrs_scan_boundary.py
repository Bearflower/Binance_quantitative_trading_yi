"""Prospective scan clocks require original reconciled receipts, never backdating."""
import copy
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest

from strategies.hrs.equity_evidence import canonical

AT = datetime(2026, 11, 20, tzinfo=timezone.utc)


def boundary_context():
    result = dict(ready=True, allowed=False, reasons=['actual_loss_gate'], ledger_version=7)
    parent = dict(account_id='test', segment_id='segment', as_of=AT.isoformat(),
        available_at=(AT+timedelta(seconds=1)).isoformat(), result=result,
        source_collection=dict(complete=True, gaps=[]),
        income_report=dict(id_match_and_coverage_complete=True, coverage_gaps=[]))
    digest = canonical(parent)[1]
    refresh = dict(result, as_of=AT, evidence_sha256=digest, policy_revision=8)
    context = dict(database_snapshot_coherent=True, replay_bootstrap_ready=True,
        available_at=AT+timedelta(seconds=2),
        entry_policy=dict(account_id='test', strategy_id='hrs', revision=8,
            effective_at=AT-timedelta(days=1), expires_at=AT+timedelta(days=1)),
        ledger_state=dict(version=7, segment_id='segment', ledger_state=dict(as_of=AT.isoformat(), segment_id='segment')),
        equity_gate=dict(refresh),
        equity_source_archives=[dict(account_id='test', segment_id='segment', as_of=AT,
            available_at=AT+timedelta(seconds=1), evidence_sha256=digest, payload=parent)])
    return context, refresh


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return AT+timedelta(seconds=3)


@pytest.mark.asyncio
async def test_scan_provider_refreshes_then_binds_fresh_original_parent_without_changing_gate(monkeypatch):
    import strategies.hrs.decision_context as module
    context, refresh = boundary_context()
    order = []
    async def collect():
        order.append('refresh')
        return refresh
    async def capture(strategy):
        order.append('context')
        return copy.deepcopy(context)
    monkeypatch.setattr(module, 'capture_financial_context', capture)
    strategy = SimpleNamespace(equity_service=SimpleNamespace(refresh=collect))
    # Requested clock precedes the original valuation source in this fixture.
    calls = iter([AT-timedelta(seconds=1), AT+timedelta(seconds=3)])
    monkeypatch.setattr(module, '_scan_now', lambda: next(calls), raising=False)
    result = await module.capture_scan_financial_context(strategy)
    assert order == ['refresh', 'context']
    marker = result['scan_boundary']
    assert marker['as_of'] == AT.isoformat()
    assert marker['available_at'] == (AT+timedelta(seconds=1)).isoformat()
    assert marker['evidence_sha256'] == refresh['evidence_sha256']
    assert result['equity_gate']['allowed'] is False
    assert context.get('scan_boundary') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ['hash', 'ledger_version', 'policy_revision', 'future', 'stale', 'incomplete', 'cash', 'marker'])
async def test_unproven_scan_provider_blocks_scoring_and_still_maintains_positions(monkeypatch, problem):
    import strategies.hrs.decision_context as module
    from strategies.hrs.decision_capture import DecisionRecorder, capture_cycle
    context, refresh = boundary_context()
    marker = dict(version='hrs_reconciled_scan_boundary_v1', requested_at=(AT-timedelta(seconds=1)).isoformat(),
        as_of=AT.isoformat(), available_at=(AT+timedelta(seconds=1)).isoformat(),
        evidence_sha256=refresh['evidence_sha256'], ledger_version=7, policy_revision=8)
    context['scan_boundary'] = marker
    if problem == 'hash':
        context['equity_source_archives'][0]['payload']['result']['ledger_version'] = 8
    elif problem == 'ledger_version': context['ledger_state']['version'] = 8
    elif problem == 'policy_revision': context['entry_policy']['revision'] = 9
    elif problem == 'future': marker['as_of'] = (AT+timedelta(hours=1)).isoformat()
    elif problem == 'stale': marker['requested_at'] = (AT-timedelta(minutes=3)).isoformat()
    elif problem == 'incomplete': context['database_snapshot_coherent'] = False
    elif problem == 'cash':
        context['entry_policy']['reason'] = json.dumps(dict(hrs_risk_budget_version='hrs_minimum_executable_v1',
            hrs_margin_scope='hrs_owned_quota_account_free_v1'))
        context.update(hrs_margin_scope='hrs_owned_quota_account_free_v1', account_available_balance='100',
            account_available_balance_source='papi/v1/account.totalAvailableBalance', account_status='NORMAL',
            account_cash_observed_at=(AT-timedelta(seconds=121)).isoformat(),
            hrs_slot_inputs=dict(observed_at=(AT+timedelta(seconds=1)).isoformat()))
    else: context['scan_boundary'] = 'unproven'
    import strategies.hrs.decision_capture as capture
    calls = iter([AT-timedelta(seconds=2)])
    monkeypatch.setattr(Clock, 'now', classmethod(lambda cls, tz=None: next(calls, AT+timedelta(seconds=3))))
    monkeypatch.setattr(capture, 'datetime', Clock)
    recorder = DecisionRecorder(AsyncMock())
    recorder.record = AsyncMock()
    recorder.context_provider = AsyncMock(return_value=context)
    strategy = SimpleNamespace(decision_recorder=recorder, _monitor_positions=AsyncMock())
    body = AsyncMock()
    @capture_cycle
    async def cycle(self): await body()
    with pytest.raises(ValueError, match='scan_boundary'):
        await cycle(strategy)
    body.assert_not_awaited()
    strategy._monitor_positions.assert_awaited_once()
    assert recorder.record.await_args.args[1] == 'scan_context_failure'


@pytest.mark.asyncio
@pytest.mark.parametrize('fraction', [0, .25])
async def test_refreshed_first_scoring_cycle_seals_and_runs_actual_owned_margin_engine(tmp_path, monkeypatch, fraction):
    """Synthetic clock/order facts prove the causal contract, never performance."""
    import strategies.hrs.decision_capture as capture
    import strategies.hrs.decision_context as context_module
    from strategies.hrs.decision_capture import canonical_payload, DecisionRecorder, capture_cycle
    from tests.test_ai_tuner_mvp.replay.test_hrs_owned_margin_parity import owned_fixture
    from tests.test_ai_tuner_mvp.replay.test_hrs_wiring import AT as origin, broker
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    from ai_tuner.replay.data_portal import DataPortal
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    from ai_tuner.replay.engine import run_replay
    from pathlib import Path
    held = {}
    owned_fixture(tmp_path/'fixture', source_transform=lambda sources: held.update(sources=copy.deepcopy(sources)))
    sources = held['sources']
    boundary = origin+timedelta(hours=1, seconds=fraction)
    start = next(r for r in sources['decision_evidence'] if r['kind']=='scan_start' and r['run_id']=='cycle-0')
    financial = copy.deepcopy(start['payload']['financial_context'])
    parent = copy.deepcopy(next(p for p in sources['equity_evidence']
        if p['evidence_sha256']==financial['equity_gate']['evidence_sha256']))
    old_digest = parent['evidence_sha256']
    raw = parent['payload']
    raw.update(segment_id='test', as_of=boundary.isoformat(), available_at=(boundary+timedelta(seconds=1)).isoformat(),
        result=dict(ready=True, allowed=True, reasons=[], ledger_version=7))
    parent.update(account_id='test', segment_id='test', as_of=boundary,
        available_at=boundary+timedelta(seconds=1), evidence_sha256=canonical(raw)[1])
    sources['equity_evidence'] = [p for p in sources['equity_evidence'] if p['evidence_sha256']!=old_digest]+[parent]
    refresh = dict(raw['result'], as_of=boundary, policy_revision=1, evidence_sha256=parent['evidence_sha256'])
    financial['entry_policy']['strategy_id'] = 'hrs'
    financial['available_at'] = (boundary+timedelta(seconds=2)).isoformat()
    financial['account_cash_observed_at'] = (boundary+timedelta(seconds=1.1)).isoformat()
    financial['hrs_slot_inputs']['observed_at'] = (boundary+timedelta(seconds=1.2)).isoformat()
    financial['ledger_state'].update(version=7, segment_id='test')
    financial['ledger_state']['ledger_state'].update(as_of=boundary.isoformat(), segment_id='test')
    financial['equity_gate'].update(refresh, checked_at=financial['available_at'])
    financial['equity_source_archives'] = [parent]
    clock = dict(now=boundary-timedelta(seconds=2))
    class TimelineMeta(type):
        def __instancecheck__(cls, value): return isinstance(value, datetime)
    class Timeline(datetime, metaclass=TimelineMeta):
        @classmethod
        def now(cls, tz=None): return clock['now']
    monkeypatch.setattr(capture, 'datetime', Timeline)
    monkeypatch.setattr(context_module, '_scan_now', lambda: clock['now'])
    order = []
    async def collect():
        order.append('refresh')
        clock['now'] = boundary+timedelta(seconds=1)
        return refresh
    async def fresh_context(strategy):
        order.append('fresh_cash')
        clock['now'] = boundary+timedelta(seconds=2)
        return copy.deepcopy(financial)
    monkeypatch.setattr(context_module, 'capture_financial_context', fresh_context)
    recorder = DecisionRecorder(AsyncMock())
    observed = []
    async def record(run_id, kind, payload, *, as_of, available_at, symbol=None, **kwargs):
        encoded, digest = canonical_payload(payload)
        observed.append(dict(evidence_id=str(len(observed)), run_id=run_id, kind=kind, symbol=symbol,
            event_time=as_of.isoformat(), available_at=available_at.isoformat(),
            payload=json.loads(encoded), payload_sha256=digest))
    recorder.record = record
    original_snapshot = copy.deepcopy(start['payload'])
    monkeypatch.setattr(capture, 'strategy_snapshot', lambda strategy: copy.deepcopy(original_snapshot))
    strategy = SimpleNamespace(decision_recorder=recorder, equity_service=SimpleNamespace(refresh=collect), _monitor_positions=AsyncMock())
    recorder.context_provider = lambda: context_module.capture_scan_financial_context(strategy)
    cycle_records = [r for r in sources['decision_evidence'] if r['run_id']=='cycle-0' and r['kind'] not in ('scan_start','scan_end')]
    @capture_cycle
    async def cycle(self):
        order.append('scoring')
        clock['now'] = boundary+timedelta(seconds=5)
        for row in cycle_records:
            await recorder.record(recorder.run_id, row['kind'], row['payload'], as_of=clock['now'],
                available_at=clock['now'], symbol=row['symbol'])
    await cycle(strategy)
    assert order == ['refresh', 'fresh_cash', 'scoring']
    first = observed[0]
    assert first['event_time'] == boundary.isoformat()
    assert first['available_at'] == (boundary+timedelta(seconds=2)).isoformat()
    assert first['payload']['requested_start_at'] == (boundary-timedelta(seconds=2)).isoformat()
    assert first['payload']['snapshot_available_at'] == (boundary+timedelta(seconds=2)).isoformat()
    assert first['payload']['candidate_scan'] == original_snapshot['candidate_scan']
    sources['decision_evidence'] = [r for r in sources['decision_evidence'] if r['run_id'] not in ('bootstrap','cycle-0') or (r['run_id']=='bootstrap' and r['kind']=='public_market_response')]+observed
    result = export_evidence_bundle(sources, tmp_path/'output', window_start=boundary, as_of=origin+timedelta(hours=3),
        execution_model=dict(version='hrs_1m_volume_v1', participation_rate='.01', timeout_minutes=15),
        monitoring_model=dict(version='hrs_cycle_monitor_v1', cadence_seconds=3600,
            anchor_at=origin.isoformat()),
        allow_inception_segment=True)
    assert result.get('status') == 'SEALED', result
    path = Path(result['snapshot_dir'])
    state = json.loads((path/'hrs_replay_state.json').read_text())
    assert state['bootstrap_bridge'] is None
    assert state['bootstrap_available_at'] == (boundary+timedelta(seconds=2)).isoformat().replace('+00:00','Z')
    tables = json.loads((path/'hrs_state_tables.json').read_text())
    replay = run_replay(DataPortal(path, 'hrs', origin+timedelta(hours=3), boundary), HrsAdapter(lambda at: tables), broker())
    assert any(e['type']=='hrs_score' for e in replay.audit_events)
    assert result['performance_qualified'] is False


@pytest.mark.asyncio
async def test_scan_refresh_timeout_cancels_prelude_and_runs_exit_maintenance(monkeypatch):
    import asyncio
    import strategies.hrs.decision_context as module
    from strategies.hrs.decision_capture import DecisionRecorder, capture_cycle
    original_wait_for = asyncio.wait_for
    budgets, cancelled = [], []
    async def short_wait_for(awaitable, timeout):
        budgets.append(timeout)
        return await original_wait_for(awaitable, timeout=.01)
    async def blocked_refresh():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    monkeypatch.setattr(module.asyncio, 'wait_for', short_wait_for)
    recorder = DecisionRecorder(AsyncMock())
    recorder.record = AsyncMock()
    strategy = SimpleNamespace(decision_recorder=recorder, _monitor_positions=AsyncMock(),
        equity_service=SimpleNamespace(refresh=blocked_refresh))
    recorder.context_provider = lambda: module.capture_scan_financial_context(strategy)
    body = AsyncMock()
    @capture_cycle
    async def cycle(self): await body()
    with pytest.raises(module.ScanContextUnavailable, match='scan_boundary_timeout') as error:
        await cycle(strategy)
    assert isinstance(error.value.__cause__, asyncio.TimeoutError)
    assert budgets == [120] and cancelled == [True]
    body.assert_not_awaited()
    strategy._monitor_positions.assert_awaited_once()
    assert recorder.record.await_args.args[1] == 'scan_context_failure'


from tests.test_strategies.test_hrs_equity_service import setup as equity_service_setup


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', [False, True])
@pytest.mark.skipif(not os.getenv('HRS_TEST_POSTGRES_DSN'), reason='isolated PostgreSQL DSN required')
async def test_real_postgres_collector_parent_is_bound_to_scan_context(equity_service_setup, monkeypatch, damage):
    """Real collector writes/hash/ledger and readonly SQL; stub exchange facts only."""
    import strategies.hrs.decision_context as module
    service, account, pool, api, clock = equity_service_setup
    boundary = clock[0]
    requested = boundary-timedelta(seconds=1)
    now = boundary+timedelta(seconds=1)
    times = iter([requested, now])
    monkeypatch.setattr(module, '_scan_now', lambda: next(times))
    async def capture(strategy):
        async with pool.acquire() as db, db.transaction(readonly=True, isolation='repeatable_read'):
            ledger = dict(await db.fetchrow('SELECT * FROM trading.hrs_loss_gate_state WHERE account_id=$1', account))
            policy = dict(await db.fetchrow("SELECT p.* FROM trading.strategy_risk_policy p JOIN trading.strategy_risk_active a USING(account_id,strategy_id,revision) WHERE p.account_id=$1 AND p.strategy_id='hrs'", account))
            gate = dict(await db.fetchrow('SELECT * FROM trading.strategy_equity_gate WHERE account_id=$1', account))
            parent = dict(await db.fetchrow('SELECT * FROM trading.hrs_equity_evidence WHERE account_id=$1 AND evidence_sha256=$2', account, gate['evidence_sha256']))
        if damage:
            ledger['version'] += 1
        return dict(database_snapshot_coherent=True, replay_bootstrap_ready=True,
            available_at=now, ledger_state=ledger, entry_policy=policy, equity_gate=gate,
            equity_source_archives=[parent])
    monkeypatch.setattr(module, 'capture_financial_context', capture)
    strategy = SimpleNamespace(equity_service=service)
    if damage:
        with pytest.raises(ValueError, match='scan_boundary'):
            await module.capture_scan_financial_context(strategy)
    else:
        context = await module.capture_scan_financial_context(strategy)
        parent = context['equity_source_archives'][0]
        raw = json.loads(parent['payload'])
        assert canonical(raw)[1] == context['scan_boundary']['evidence_sha256']
        assert context['scan_boundary']['as_of'] == boundary.isoformat()
        assert context['scan_boundary']['ledger_version'] == context['ledger_state']['version']
        assert context['equity_gate']['allowed'] == raw['result']['allowed']


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('ledger_version',8), ('policy_revision',9), ('evidence_sha256','f'*64),
    ('as_of',AT-timedelta(seconds=2)), ('allowed',True)])
async def test_refresh_result_must_match_original_parent_and_policy(monkeypatch, field, value):
    import strategies.hrs.decision_context as module
    context, refresh = boundary_context()
    refresh[field] = value
    times = iter([AT-timedelta(seconds=1), AT+timedelta(seconds=3)])
    monkeypatch.setattr(module, '_scan_now', lambda: next(times))
    monkeypatch.setattr(module, 'capture_financial_context', AsyncMock(return_value=context))
    strategy = SimpleNamespace(equity_service=SimpleNamespace(refresh=AsyncMock(return_value=refresh)))
    with pytest.raises(ValueError, match='scan_boundary'):
        await module.capture_scan_financial_context(strategy)


@pytest.mark.asyncio
async def test_scan_guard_captures_actual_t_minus_one_values_before_financial_refresh(monkeypatch):
    import strategies.hrs.decision_context as module
    from strategies.hrs.decision_capture import canonical_payload
    at=AT+timedelta(minutes=21)
    cfg={'enabled':True,'pool_level':{'threshold':.1}}
    breaker=SimpleNamespace(pool='hrs',load_index=AsyncMock(return_value=-.04),load_index_12h=AsyncMock(return_value=None))
    from shared.circuit_breaker import floor_index_hour
    hour=floor_index_hour(at)-timedelta(hours=1)
    breaker._index_cache={hour.isoformat():-.04,hour.isoformat()+':12h':None}
    item=SimpleNamespace(circuit_breaker=breaker,_market_guard_config=cfg)
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test')
    times=iter([at,at+timedelta(milliseconds=50)])
    monkeypatch.setattr(module,'_scan_now',lambda:next(times))
    result=await module.capture_scan_market_guard(item)
    expected=at.astimezone(timezone(timedelta(hours=8))).replace(minute=0,second=0,microsecond=0)-timedelta(hours=1)
    breaker.load_index.assert_awaited_once_with(expected)
    breaker.load_index_12h.assert_awaited_once_with(expected)
    assert result==dict(version='hrs_market_guard_observation_v1',account_id='test',pool='hrs',index_hour=expected.isoformat(),
                        requested_at=at.isoformat(),available_at=(at+timedelta(milliseconds=50)).isoformat(),
                        pool_index=-.04,pool_index_12h=None,config_sha256=canonical_payload(cfg)[1])


@pytest.mark.asyncio
async def test_guard_capture_is_included_in_the_same_original_scan_payload(monkeypatch):
    import strategies.hrs.decision_context as module
    context,refresh=boundary_context();events=[]
    observed={'version':'hrs_market_guard_observation_v1','account_id':'test','pool':'hrs'}
    async def guard(strategy):events.append('guard');return observed
    async def collect():events.append('refresh');return refresh
    monkeypatch.setattr(module,'capture_scan_market_guard',guard,raising=False)
    monkeypatch.setattr(module,'capture_financial_context',AsyncMock(return_value=context))
    times=iter([AT-timedelta(seconds=1),AT+timedelta(seconds=3)])
    monkeypatch.setattr(module,'_scan_now',lambda:next(times))
    result=await module.capture_scan_financial_context(SimpleNamespace(equity_service=SimpleNamespace(refresh=collect)))
    assert events==['guard','refresh']
    assert result['market_guard_observation']==observed


@pytest.mark.asyncio
async def test_disabled_guard_has_no_invented_index_observation(monkeypatch):
    import strategies.hrs.decision_context as module
    assert await module.capture_scan_market_guard(SimpleNamespace(circuit_breaker=None,_market_guard_config=None)) is None
    with pytest.raises(ValueError,match='market_guard'):
        await module.capture_scan_market_guard(SimpleNamespace(circuit_breaker=object(),_market_guard_config=None))


@pytest.mark.asyncio
async def test_transient_index_read_failure_cannot_attest_uncached_none(monkeypatch):
    import strategies.hrs.decision_context as module
    from shared.circuit_breaker import CircuitBreaker,floor_index_hour,load_circuit_breaker_config
    at=AT+timedelta(minutes=21)
    cfg=load_circuit_breaker_config()
    db=SimpleNamespace(fetch_one=AsyncMock(side_effect=[RuntimeError('temporary read failure'),
        {'equal_weight_12h':.01},{'equal_weight':.04}]))
    breaker=CircuitBreaker(cfg,db,pool='hrs')
    times=iter([at,at+timedelta(milliseconds=50)])
    monkeypatch.setattr(module,'_scan_now',lambda:next(times))
    with pytest.raises(ValueError,match='market_guard_source_cache_unproven'):
        await module.capture_scan_market_guard(SimpleNamespace(circuit_breaker=breaker,_market_guard_config=cfg))
    hour=floor_index_hour(at)-timedelta(hours=1)
    assert hour.isoformat() not in breaker._index_cache
    assert await breaker.load_index(hour)==.04


@pytest.mark.asyncio
async def test_scan_timeout_identifies_stage_joins_cancelled_collection_and_never_uses_cached_context(monkeypatch):
    import asyncio
    import strategies.hrs.decision_context as module
    cancelled = asyncio.Event()
    async def refresh():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    capture = AsyncMock()
    monkeypatch.setattr(module, 'capture_financial_context', capture)
    original = asyncio.wait_for
    async def bounded(task, timeout):
        assert timeout == 120
        return await original(task, timeout=.01)
    monkeypatch.setattr(module.asyncio, 'wait_for', bounded)
    with pytest.raises(ValueError, match='scan_boundary_timeout') as error:
        await module.capture_scan_financial_context(SimpleNamespace(equity_service=SimpleNamespace(refresh=refresh)))
    assert error.value.phase == 'equity_refresh'
    assert error.value.budget_seconds == 120
    assert error.value.elapsed_seconds >= .01
    assert cancelled.is_set()
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_denial_preserves_real_reason_skips_scoring_and_maintains_positions(monkeypatch):
    import strategies.hrs.decision_context as module
    from strategies.hrs.decision_capture import DecisionRecorder, capture_cycle
    capture = AsyncMock()
    monkeypatch.setattr(module, 'capture_financial_context', capture)
    recorder = DecisionRecorder(AsyncMock());recorder.record = AsyncMock()
    denied = dict(ready=False, allowed=False, policy_revision=0,
                  reasons=['local_position_inventory_conflict'], as_of=AT, evidence_sha256='denied')
    strategy = SimpleNamespace(equity_service=SimpleNamespace(refresh=AsyncMock(return_value=denied)),
                               decision_recorder=recorder, _monitor_positions=AsyncMock())
    recorder.context_provider = lambda: module.capture_scan_financial_context(strategy)
    body = AsyncMock()
    @capture_cycle
    async def cycle(self):
        await body()
    with pytest.raises(ValueError, match='scan_boundary_source_denied') as error:
        await cycle(strategy)
    assert error.value.phase == 'equity_refresh'
    assert error.value.reason_codes == ['local_position_inventory_conflict']
    capture.assert_not_awaited()
    body.assert_not_awaited()
    strategy._monitor_positions.assert_awaited_once()
    assert recorder.record.await_args.args[2]['phase'] == 'equity_refresh'
    assert recorder.record.await_args.args[2]['reason_codes'] == ['local_position_inventory_conflict']


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['market_guard', 'equity_refresh', 'financial_context'])
async def test_source_timeout_is_not_reported_as_expired_scan_deadline(monkeypatch, phase):
    import strategies.hrs.decision_context as module
    context, refresh = boundary_context()
    guard = AsyncMock(return_value=None)
    collect = AsyncMock(return_value=refresh)
    capture = AsyncMock(return_value=context)
    {'market_guard': guard, 'equity_refresh': collect, 'financial_context': capture}[phase].side_effect = TimeoutError()
    monkeypatch.setattr(module, 'capture_scan_market_guard', guard)
    monkeypatch.setattr(module, 'capture_financial_context', capture)
    strategy = SimpleNamespace(equity_service=SimpleNamespace(refresh=collect))
    with pytest.raises(module.ScanContextUnavailable, match='scan_boundary_source_timeout') as error:
        await module.capture_scan_financial_context(strategy)
    assert error.value.phase == phase
    assert error.value.elapsed_seconds < 1
    assert isinstance(error.value.__cause__, TimeoutError)
