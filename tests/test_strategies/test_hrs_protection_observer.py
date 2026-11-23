"""Synthetic parent/child proofs, optional worker isolation, and append-only I/O."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.test_strategies.test_hrs_acceptance_report import (
    ACCOUNT, NOW, START, engineering_evidence, event, financial, technical,
    initialized_report_schema,
)
from strategies.hrs.decision_capture import DecisionRecorder, canonical_payload



def original_sources():
    captures = engineering_evidence()
    ack = next(row for row in captures if row['kind'] == 'protection_order_ack' and row['payload']['role'] == 'tp1')
    ack['payload']['request']['order_type'] = 'TAKE_PROFIT_MARKET'
    ack['payload']['response'].update(algoId=81, orderType='TAKE_PROFIT_MARKET', actualOrderId='')
    ack['payload']['algo_id'] = 81
    ack['payload_sha256'] = canonical_payload(ack['payload'])[1]
    return captures, ack


def observation(captures, ack, *, at=NOW-timedelta(seconds=10)):
    start = next(row for row in captures if row['kind'] == 'scan_start' and row['run_id'] == ack['run_id'])
    response = dict(ack['payload']['response'], algoStatus='FINISHED', actualOrderId=2,
                    updateTime=int((at-timedelta(seconds=1)).timestamp()*1000))
    payload = dict(version='hrs_protection_observation_v1', account_id=ACCOUNT, run_id=ack['run_id'],
        symbol=ack['symbol'], position_id=ack['payload']['position_id'], role=ack['payload']['role'],
        ack_evidence_id=ack['evidence_id'], ack_payload_sha256=ack['payload_sha256'],
        scan_start_evidence_id=start['evidence_id'], scan_start_payload_sha256=start['payload_sha256'],
        query={'method': 'GET', 'endpoint': '/papi/v1/um/algo/algoOrder', 'params': {'algoId': 81}},
        requested_at=(at-timedelta(seconds=2)).isoformat(), response=response,
        response_sha256=canonical_payload(response)[1])
    return event(ack['run_id'], 'protection_order_observation', at, payload, ack['symbol'])


def reseal(row):
    row['payload_sha256'] = canonical_payload(row['payload'])[1]


def test_late_real_child_proof_links_exit_without_changing_original_ack_or_economics():
    captures, ack = original_sources()
    before = copy.deepcopy(ack)
    prior = financial(decision_evidence=captures)
    assert prior['samples'][0]['engineering_checks']['exit_role_reconciliation']['status'] == 'PENDING'
    report = financial(decision_evidence=[*captures, observation(captures, ack)])
    assert report['samples'][0]['engineering_checks']['exit_role_reconciliation']['status'] == 'RECONCILED'
    assert ack == before
    assert report['samples'][0]['net_income_usdt'] == prior['samples'][0]['net_income_usdt']
    assert report['performance_accepted'] is False


@pytest.mark.parametrize('damage', ['future', 'account', 'run', 'parent_hash', 'source_hash', 'role',
    'child', 'status', 'client', 'symbol', 'side', 'type', 'update_time', 'endpoint', 'method', 'response_hash',
    'missing_ack', 'duplicate_scan', 'missing_scan'])
def test_unproven_late_observation_cannot_supply_an_exit_role(damage):
    captures, ack = original_sources()
    row = observation(captures, ack)
    if damage == 'future':
        row['event_time'] = row['available_at'] = NOW+timedelta(seconds=1)
    elif damage == 'account': row['payload']['account_id'] = 'other-account'
    elif damage == 'run': row['payload']['run_id'] = 'other-run'
    elif damage == 'parent_hash': row['payload']['ack_payload_sha256'] = 'f'*64
    elif damage == 'source_hash': row['payload']['scan_start_payload_sha256'] = 'f'*64
    elif damage == 'role': row['payload']['role'] = 'sl'
    elif damage == 'child': row['payload']['response']['actualOrderId'] = 999
    elif damage == 'status': row['payload']['response']['algoStatus'] = 'EXPIRED'
    elif damage == 'client': row['payload']['response']['clientAlgoId'] = 'wrong-client'
    elif damage == 'symbol': row['payload']['response']['symbol'] = 'OTHERUSDT'
    elif damage == 'side': row['payload']['response']['side'] = 'BUY'
    elif damage == 'type': row['payload']['response']['orderType'] = 'STOP_MARKET'
    elif damage == 'update_time': row['payload']['response']['updateTime'] = int((NOW+timedelta(seconds=5)).timestamp()*1000)
    elif damage == 'endpoint': row['payload']['query']['endpoint'] = '/papi/v1/um/order'
    elif damage == 'method': row['payload']['query']['method'] = 'POST'
    elif damage == 'response_hash': row['payload']['response_sha256'] = 'f'*64
    elif damage == 'missing_ack': captures = [r for r in captures if r is not ack]
    elif damage == 'duplicate_scan': captures.append(copy.deepcopy(next(r for r in captures if r['kind'] == 'scan_start')))
    elif damage == 'missing_scan': captures = [r for r in captures if r['kind'] != 'scan_start']
    if damage not in ('response_hash',):
        row['payload']['response_sha256'] = canonical_payload(row['payload']['response'])[1]
    reseal(row)
    result = financial(decision_evidence=[*captures, row])
    assert result['samples'][0]['engineering_checks']['exit_role_reconciliation']['status'] != 'RECONCILED'


def test_conflicting_children_do_not_select_the_one_that_happens_to_match_fill():
    captures, ack = original_sources()
    one = observation(captures, ack)
    two = copy.deepcopy(one)
    two['evidence_id'] = 'another-observation'
    two['payload']['response']['actualOrderId'] = 999
    two['payload']['response_sha256'] = canonical_payload(two['payload']['response'])[1]
    reseal(two)
    result = financial(decision_evidence=[*captures, one, two])
    assert result['samples'][0]['engineering_checks']['exit_role_reconciliation']['status'] == 'PENDING'


def test_identical_observations_are_idempotent_and_payload_remains_fully_hash_bound():
    from strategies.hrs.acceptance_report import _compact_record
    captures, ack = original_sources()
    one = observation(captures, ack)
    assert _compact_record(one)['payload_sha256'] == one['payload_sha256']
    two = copy.deepcopy(one);two['evidence_id'] = 'duplicate-response'
    result = financial(decision_evidence=[*captures, one, two])
    assert result['samples'][0]['engineering_checks']['exit_role_reconciliation']['status'] == 'RECONCILED'


@pytest.mark.parametrize('child', [False, True, 'broken', '-2', '2.5', '999'])
def test_malformed_or_conflicting_nonempty_original_child_cannot_be_replaced_by_late_proof(child):
    captures, ack = original_sources()
    ack['payload']['response']['actualOrderId'] = child
    reseal(ack)
    row = observation(captures, ack)
    result = financial(decision_evidence=[*captures, row])
    assert result['samples'][0]['engineering_checks']['exit_role_reconciliation']['status'] == 'PENDING'


class Connection:
    def __init__(self, captures, observations=()):
        self.captures, self.observations, self.prefetches = captures, observations, []
    def transaction(self, **kwargs):
        assert kwargs == {'isolation': 'repeatable_read', 'readonly': True}
        return self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def cursor(self, sql, *args, prefetch):
        self.prefetches.append(prefetch)
        assert 'CASE WHEN' in sql and 'LIMIT' in sql
        if args[0] == 'protection_order_ack':
            assert sql.endswith('LIMIT 5')
            rows = sorted((r for r in self.captures if r['kind'] == 'protection_order_ack'
                           and (r['event_time'], r['evidence_id']) > (args[2], args[3])),
                          key=lambda r: (r['event_time'], r['evidence_id']))[:5]
        elif args[0] == 'scan_start':
            rows = [r for r in self.captures if r['kind'] == 'scan_start' and r['run_id'] == args[1]]
        else: rows = self.observations
        async def iterator():
            for row in rows: yield copy.deepcopy(row)
        return iterator()


class Pool:
    def __init__(self, connection): self.connection = connection
    def acquire(self): return self.connection


@pytest.mark.asyncio
async def test_observer_get_only_appends_to_original_run_and_restart_skips_terminal(monkeypatch):
    from strategies.hrs import protection_observer as module
    captures, ack = original_sources()
    captures = [r for r in captures if r['kind'] == 'scan_start' or r is ack]
    clock = NOW-timedelta(seconds=10)
    monkeypatch.setattr(module, 'utc_now', lambda: clock)
    db = SimpleNamespace(pool=Pool(Connection(captures)), execute=AsyncMock(), fetch_all=AsyncMock(return_value=[]))
    source_recorder = DecisionRecorder(db)
    api = SimpleNamespace(get_algo_order=AsyncMock(return_value=observation(captures, ack)['payload']['response']),
                          _request=AsyncMock())
    worker = module.ProtectionObserver(db, api, source_recorder, account_id=ACCOUNT)
    worker.recorder.record = AsyncMock(return_value='observation')
    await worker.collect_once()
    api.get_algo_order.assert_awaited_once_with(algo_id=81)
    api._request.assert_not_awaited()
    args, kwargs = worker.recorder.record.await_args
    assert args[0:2] == (ack['run_id'], 'protection_order_observation')
    assert args[2]['ack_payload_sha256'] == ack['payload_sha256']
    assert kwargs['as_of'] == kwargs['available_at'] == clock
    assert source_recorder.failed is False
    recorded = event(args[0], args[1], clock, args[2], ack['symbol'])
    restarted = module.ProtectionObserver(SimpleNamespace(pool=Pool(Connection(captures, [recorded]))), api,
                                           source_recorder, account_id=ACCOUNT)
    restarted.recorder.record = AsyncMock()
    await restarted.collect_once()
    assert api.get_algo_order.await_count == 1
    restarted.recorder.record.assert_not_awaited()
    assert set(db.pool.connection.prefetches) == {1}


@pytest.mark.asyncio
async def test_optional_observer_persistence_failure_does_not_poison_primary_recorder(monkeypatch):
    from strategies.hrs import protection_observer as module
    captures, ack = original_sources()
    captures = [r for r in captures if r['kind'] == 'scan_start' or r is ack]
    monkeypatch.setattr(module, 'utc_now', lambda: NOW-timedelta(seconds=10))
    db = SimpleNamespace(pool=Pool(Connection(captures)), execute=AsyncMock(side_effect=RuntimeError('unavailable')), fetch_all=AsyncMock())
    recorder = DecisionRecorder(db)
    api = SimpleNamespace(get_algo_order=AsyncMock(return_value=observation(captures, ack)['payload']['response']))
    worker = module.ProtectionObserver(db, api, recorder, account_id=ACCOUNT)
    await worker.collect_once()
    assert recorder.failed is False
    assert worker.recorder.failed is True


@pytest.mark.asyncio
async def test_main_optional_task_starts_without_waiting_and_is_joined_on_cancel(monkeypatch):
    from strategies.hrs import main as module
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def run():
        entered.set()
        try: await asyncio.Event().wait()
        finally: stopped.set()
    factory = Mock(return_value=SimpleNamespace(run=run))
    monkeypatch.setattr(module, 'ProtectionObserver', factory)
    async def trading(*args):
        await entered.wait()
        await asyncio.Event().wait()
    monkeypatch.setattr(module, 'run_with_equity_service', trading)
    item = SimpleNamespace(decision_recorder=object())
    task = asyncio.create_task(module.run_with_protection_observer(item, object(), object()))
    await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert stopped.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['ack_hash', 'scan_hash', 'account', 'duplicate_scan'])
async def test_observer_does_not_query_unverified_or_ambiguous_original_sources(monkeypatch, damage):
    from strategies.hrs import protection_observer as module
    captures, ack = original_sources()
    captures = [r for r in captures if r['kind'] == 'scan_start' or r is ack]
    start = next(r for r in captures if r['kind'] == 'scan_start')
    if damage == 'ack_hash': ack['payload']['role'] = 'sl'
    elif damage == 'scan_hash': start['payload']['tampered'] = True
    elif damage == 'account':
        start['payload']['financial_context']['entry_policy']['account_id'] = 'wrong-account';reseal(start)
    else: captures.append(copy.deepcopy(start))
    monkeypatch.setattr(module, 'utc_now', lambda: NOW)
    db = SimpleNamespace(pool=Pool(Connection(captures)))
    api = SimpleNamespace(get_algo_order=AsyncMock())
    worker = module.ProtectionObserver(db, api, DecisionRecorder(db), account_id=ACCOUNT)
    worker.recorder.record = AsyncMock()
    await worker.collect_once()
    api.get_algo_order.assert_not_awaited()
    worker.recorder.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_timeout_is_joined_and_diagnostics_never_include_raw_exception(monkeypatch):
    from strategies.hrs import protection_observer as module
    captures, ack = original_sources()
    captures = [r for r in captures if r['kind'] == 'scan_start' or r is ack]
    monkeypatch.setattr(module, 'utc_now', lambda: NOW)
    monkeypatch.setattr(module, 'GET_TIMEOUT_SECONDS', .01)
    logger = Mock();monkeypatch.setattr(module, 'log', logger)
    cancelled = asyncio.Event()
    async def pending(**kwargs):
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    db = SimpleNamespace(pool=Pool(Connection(captures)))
    worker = module.ProtectionObserver(db, SimpleNamespace(get_algo_order=pending), DecisionRecorder(db), account_id=ACCOUNT)
    worker.recorder.record = AsyncMock()
    await worker.collect_once()
    assert cancelled.is_set()
    worker.recorder.record.assert_not_awaited()
    assert logger.warning.call_args.kwargs == {'error_type': 'TimeoutError'}


@pytest.mark.asyncio
async def test_worker_defect_is_optional_and_gate_disabled_path_keeps_existing_result(monkeypatch):
    from strategies.hrs import main as module
    entered = asyncio.Event()
    async def broken():
        entered.set()
        raise TypeError('optional failure with signed URL must stay private')
    monkeypatch.setattr(module, 'ProtectionObserver', Mock(return_value=SimpleNamespace(run=broken)))
    async def trading(*args):
        await entered.wait()
        return 'ordinary-result'
    monkeypatch.setattr(module, 'run_with_equity_service', trading)
    assert await module.run_with_protection_observer(SimpleNamespace(decision_recorder=object()), object(), object()) == 'ordinary-result'


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv('HRS_TEST_POSTGRES_DSN'), reason='isolated PostgreSQL required')
async def test_real_postgres_observer_only_appends_observation_and_leaves_sources_unchanged(initialized_report_schema):
    import asyncpg
    from strategies.hrs.protection_observer import ProtectionObserver
    pool = await asyncpg.create_pool(os.environ['HRS_TEST_POSTGRES_DSN'], min_size=1, max_size=2)
    writes = []
    class Database:
        async def execute(self, sql, *args):
            writes.append(sql)
            async with pool.acquire() as connection: return await connection.execute(sql, *args)
        async def fetch_all(self, sql, *args):
            async with pool.acquire() as connection: return await connection.fetch(sql, *args)
    db = Database();db.pool = pool
    recorder = DecisionRecorder(db)
    account, run = 'synthetic-observer-account', 'synthetic-observer-' + __import__('uuid').uuid4().hex
    now = datetime.now(timezone.utc)
    captures, ack = original_sources()
    start = next(r for r in captures if r['kind'] == 'scan_start')
    start['payload']['financial_context']['entry_policy']['account_id'] = account
    for row, offset in ((start, 5), (ack, 4)):
        row.update(run_id=run, event_time=now-timedelta(seconds=offset), available_at=now-timedelta(seconds=offset))
        reseal(row)
    try:
        for row in (start, ack):
            await recorder.record(run, row['kind'], row['payload'], as_of=row['event_time'],
                available_at=row['available_at'], symbol=row['symbol'], evidence_id=row['evidence_id'])
        writes.clear()
        response = dict(ack['payload']['response'], algoStatus='FINISHED', actualOrderId=2,
            updateTime=int((now-timedelta(seconds=1)).timestamp()*1000))
        api = SimpleNamespace(get_algo_order=AsyncMock(return_value=response))
        worker = ProtectionObserver(db, api, recorder, account_id=account)
        worker._after = (now-timedelta(seconds=6), '')
        await worker.collect_once()
        assert len(writes) == 1 and writes[0].lstrip().startswith('INSERT INTO hrs.hrs_decision_evidence')
        async with pool.acquire() as connection:
            rows = await connection.fetch('SELECT * FROM hrs.hrs_decision_evidence WHERE run_id=$1', run)
        assert len(rows) == 3
        saved = next(r for r in rows if r['kind'] == 'protection_order_ack')
        assert saved['payload_sha256'] == ack['payload_sha256']
        observed = next(r for r in rows if r['kind'] == 'protection_order_observation')
        assert observed['event_time'] >= now and observed['available_at'] >= now
        assert recorder.failed is False
    finally:
        async with pool.acquire() as connection:
            await connection.execute('DELETE FROM hrs.hrs_decision_evidence WHERE run_id=$1', run)
        await pool.close()


@pytest.mark.asyncio
async def test_optional_observer_constructor_failure_does_not_prevent_ordinary_strategy_run(monkeypatch):
    from strategies.hrs import main as module
    monkeypatch.setattr(module, 'ProtectionObserver', Mock(side_effect=ValueError('optional dependency unavailable')))
    ordinary = AsyncMock(return_value='main-keeps-running')
    monkeypatch.setattr(module, 'run_with_equity_service', ordinary)
    assert await module.run_with_protection_observer(SimpleNamespace(decision_recorder=object()), object(), object()) == 'main-keeps-running'
    ordinary.assert_awaited_once()


def test_ack_original_hash_bound_payload_is_never_compacted():
    from strategies.hrs.acceptance_report import _compact_record
    _, ack = original_sources()
    ack['payload']['klines_1h'] = ['unexpected but source-hash-bound']
    reseal(ack)
    result = _compact_record(ack)
    assert result['payload'] == ack['payload']
    assert result['payload_sha256'] == ack['payload_sha256']


def test_late_observation_does_not_reintroduce_pre_window_cycle_into_exporter(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    from tests.test_ai_tuner_mvp.test_hrs_evidence_export import captured_sources, envelope, AT
    sources, bootstrap = captured_sources(tmp_path)
    old_at = AT-timedelta(days=1)
    sources['decision_evidence'] += [envelope('old', 'scan_start', {}, old_at),
        envelope('old', 'scan_end', {'status': 'complete'}, old_at+timedelta(seconds=1))]
    options = dict(window_start=AT, as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'], monitoring_model=bootstrap['monitoring_model'],
        allow_inception_segment=True)
    before = export_evidence_bundle(sources, tmp_path/'before', **options)
    assert before.get('status') == 'SEALED', before
    late = envelope('old', 'protection_order_observation', {'read_only': True}, AT+timedelta(minutes=1))
    sources['decision_evidence'].append(late)
    after = export_evidence_bundle(sources, tmp_path/'after', **options)
    assert after.get('status') == 'SEALED', after
    from pathlib import Path
    first, second = Path(before['snapshot_dir']), Path(after['snapshot_dir'])
    for filename in ('hrs_decision_calendar.jsonl', 'hrs_decision_inputs.jsonl'):
        assert (first/filename).read_bytes() == (second/filename).read_bytes()
    raw = json.loads((second/'raw_hrs_evidence.json').read_text())
    assert len(raw['decision_evidence']) == len(sources['decision_evidence'])
    assert late in raw['decision_evidence']
    receipt = json.loads((second/'hrs_export_receipt.json').read_text())
    assert receipt['decision_payload_hashes'][late['evidence_id']] == late['payload_sha256']


@pytest.mark.asyncio
async def test_unchanged_new_snapshot_is_recorded_once_and_can_be_rechecked_without_poisoning_main(monkeypatch):
    from strategies.hrs import protection_observer as module
    captures, ack = original_sources()
    captures = [r for r in captures if r['kind'] == 'scan_start' or r is ack]
    created = int((ack['event_time']-timedelta(seconds=1)).timestamp()*1000)
    ack['payload']['response'].update(createTime=created, updateTime=created, algoStatus='NEW')
    reseal(ack)
    monkeypatch.setattr(module, 'utc_now', lambda: NOW-timedelta(seconds=10))
    db = SimpleNamespace(pool=Pool(Connection(captures)))
    api = SimpleNamespace(get_algo_order=AsyncMock(return_value=ack['payload']['response']))
    recorder = DecisionRecorder(db)
    worker = module.ProtectionObserver(db, api, recorder, account_id=ACCOUNT)
    worker.recorder.record = AsyncMock()
    await worker.collect_once()
    worker.recorder.record.assert_awaited_once()
    args, kwargs = worker.recorder.record.await_args
    saved = event(args[0], args[1], kwargs['as_of'], args[2], ack['symbol'])
    restart = module.ProtectionObserver(SimpleNamespace(pool=Pool(Connection(captures, [saved]))), api,
                                       recorder, account_id=ACCOUNT)
    restart.recorder.record = AsyncMock()
    await restart.collect_once()
    assert api.get_algo_order.await_count == 2
    restart.recorder.record.assert_not_awaited()
    assert recorder.failed is False


def test_conflicting_direct_parent_before_original_ack_cannot_be_lost_by_record_order():
    captures, ack = original_sources()
    other = copy.deepcopy(ack)
    other['evidence_id'] = 'different-parent-ack'
    other['payload']['algo_id'] = other['payload']['response']['algoId'] = 82
    other['payload']['response']['actualOrderId'] = '999'
    reseal(other)
    captures.insert(0, other)
    row = observation(captures, ack)
    result = financial(decision_evidence=[*captures, row])
    assert result['samples'][0]['engineering_checks']['exit_role_reconciliation']['status'] == 'PENDING'


@pytest.mark.asyncio
async def test_end_of_keyset_wraps_without_skipping_the_next_new_order_poll(monkeypatch):
    from strategies.hrs import protection_observer as module
    captures, ack = original_sources()
    captures = [r for r in captures if r['kind'] == 'scan_start' or r is ack]
    monkeypatch.setattr(module, 'utc_now', lambda: NOW-timedelta(seconds=10))
    response = dict(observation(captures, ack)['payload']['response'], algoStatus='NEW', actualOrderId='')
    api = SimpleNamespace(get_algo_order=AsyncMock(return_value=response))
    connection = Connection(captures)
    db = SimpleNamespace(pool=Pool(connection))
    worker = module.ProtectionObserver(db, api, DecisionRecorder(db), account_id=ACCOUNT)
    worker.recorder.record = AsyncMock()
    await worker.collect_once()
    args, kwargs = worker.recorder.record.await_args
    connection.observations = [event(args[0], args[1], kwargs['as_of'], args[2], ack['symbol'])]
    await worker.collect_once()
    assert api.get_algo_order.await_count == 2
    assert worker.recorder.record.await_count == 1


@pytest.mark.parametrize('field', ['client_id', 'order_type'])
def test_boolean_is_not_a_protection_client_or_order_type_identity(field):
    captures, ack = original_sources()
    ack['payload']['request'][field] = True
    ack['payload']['response']['clientAlgoId' if field == 'client_id' else 'orderType'] = True
    reseal(ack)
    row = observation(captures, ack)
    result = financial(decision_evidence=[*captures, row])
    assert result['samples'][0]['engineering_checks']['exit_role_reconciliation']['status'] == 'PENDING'


@pytest.mark.asyncio
async def test_one_poll_never_issues_more_than_five_parent_gets(monkeypatch):
    from strategies.hrs import protection_observer as module
    captures, ack = original_sources()
    start = next(row for row in captures if row['kind'] == 'scan_start')
    acks = []
    for index in range(7):
        row = copy.deepcopy(ack)
        row['evidence_id'] = 'bounded-ack-' + str(index)
        row['payload']['algo_id'] = row['payload']['response']['algoId'] = 100+index
        reseal(row);acks.append(row)
    monkeypatch.setattr(module, 'utc_now', lambda: NOW-timedelta(seconds=10))
    async def get(*, algo_id):
        row = next(row for row in acks if row['payload']['algo_id'] == algo_id)
        return dict(row['payload']['response'], algoStatus='FINISHED', actualOrderId=2,
                    updateTime=int((NOW-timedelta(seconds=11)).timestamp()*1000))
    api = SimpleNamespace(get_algo_order=AsyncMock(side_effect=get))
    db = SimpleNamespace(pool=Pool(Connection([start, *acks])))
    worker = module.ProtectionObserver(db, api, DecisionRecorder(db), account_id=ACCOUNT)
    worker.recorder.record = AsyncMock()
    await worker.collect_once()
    assert api.get_algo_order.await_count == worker.recorder.record.await_count == 5


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['bad_context', 'scan_hash'])
async def test_bad_scan_source_cannot_discard_a_valid_candidate_or_starve_later_polls(monkeypatch, damage):
    from strategies.hrs import protection_observer as module
    captures, good = original_sources()
    good_start = next(row for row in captures if row['kind'] == 'scan_start')
    bad = copy.deepcopy(good)
    bad.update(evidence_id='bad-ack', run_id='bad-run', event_time=good['event_time']+timedelta(seconds=1),
               available_at=good['available_at']+timedelta(seconds=1))
    bad['payload']['algo_id'] = bad['payload']['response']['algoId'] = 82
    reseal(bad)
    bad_start = copy.deepcopy(good_start)
    bad_start.update(evidence_id='bad-scan', run_id='bad-run')
    if damage == 'bad_context':
        bad_start['payload']['financial_context'] = None
        reseal(bad_start)
    else:
        bad_start['payload']['tampered_after_hash'] = True
    monkeypatch.setattr(module, 'utc_now', lambda: NOW-timedelta(seconds=10))
    response = observation([good_start, good], good)['payload']['response']
    api = SimpleNamespace(get_algo_order=AsyncMock(return_value=response))
    db = SimpleNamespace(pool=Pool(Connection([good_start, good, bad_start, bad])))
    worker = module.ProtectionObserver(db, api, DecisionRecorder(db), account_id=ACCOUNT)
    worker.recorder.record = AsyncMock()
    for _ in range(3):
        await worker.collect_once()
    assert api.get_algo_order.await_count >= 1
    assert all(call.kwargs == {'algo_id': 81} for call in api.get_algo_order.await_args_list)
    assert worker.recorder.record.await_count >= 1
    assert all(call.args[2]['ack_evidence_id'] == good['evidence_id']
               for call in worker.recorder.record.await_args_list)
