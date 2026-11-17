from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest


def test_capture_canonicalization_preserves_modes_and_redacts_credentials():
    from strategies.hrs.decision_capture import canonical_payload
    payload, digest = canonical_payload({'mode': 'lv_rm', 'amount': Decimal('0.1'),
        'auth': {'api_key': 'secret', 'password': 'hidden'}, 'values': [1, 2]})
    assert 'secret' not in payload and 'hidden' not in payload
    assert 'lv_rm' in payload and '0.1' in payload and len(digest) == 64
    with pytest.raises(ValueError):
        canonical_payload({'value': float('nan')})


@pytest.mark.asyncio
async def test_capture_conflicting_identity_never_overwrites_evidence():
    from strategies.hrs.decision_capture import DecisionRecorder
    db = AsyncMock()
    db.fetch_all.return_value = [{'payload_sha256': 'different'}]
    recorder = DecisionRecorder(db)
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match='evidence_identity_conflict'):
        await recorder.record('scan-1', 'scan', {'symbols': ['BTCUSDT']}, as_of=now, available_at=now, evidence_id='fixed')


@pytest.mark.asyncio
async def test_public_transport_capture_failure_blocks_further_entry_evidence():
    from strategies.hrs.decision_capture import DecisionRecorder
    db = AsyncMock()
    db.execute.side_effect = RuntimeError('database unavailable')
    recorder = DecisionRecorder(db)
    client = AsyncMock()
    client._request.return_value = {'openInterest': '10'}
    recorder.attach_public_transport(client)
    assert await client._request('GET', '/fapi/v1/openInterest', params={'symbol': 'BTCUSDT'}, signed=False) == {'openInterest': '10'}
    assert recorder.failed


@pytest.mark.asyncio
async def test_capture_does_not_intercept_private_order_writes():
    from strategies.hrs.decision_capture import DecisionRecorder
    recorder = DecisionRecorder(AsyncMock())
    recorder.record = AsyncMock()
    client = AsyncMock()
    client._request.return_value = {'orderId': 10}
    recorder.attach_public_transport(client)
    assert await client._request('POST', '/papi/v1/um/order', params={'symbol': 'BTCUSDT'}, signed=True) == {'orderId': 10}
    recorder.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_cycle_keeps_raw_state_and_analysis_without_credentials():
    from types import SimpleNamespace
    from strategies.hrs.decision_capture import capture_cycle, capture_analysis
    recorder = AsyncMock()
    recorder.run_id = 'run'
    strategy = SimpleNamespace(decision_recorder=recorder, config={'api_key': 'secret'},
        _klines_cache={'BTCUSDT': [{'close': 1}]}, _klines_4h_cache={},
        _signal_timestamps={}, candidate_pool=None, position_manager=None, risk_manager=None)

    @capture_analysis
    async def analyze(self, symbol):
        return {'symbol': symbol, 'short': None, 'skip': True, 'reason': 'missing'}

    @capture_cycle
    async def cycle(self):
        return await analyze(self, 'BTCUSDT')

    result = await cycle(strategy)
    assert result['skip'] is True
    recorder.begin.assert_awaited_once()
    assert recorder.record.await_count == 2
    kinds = [c.args[1] for c in recorder.record.await_args_list]
    assert kinds == ['analysis', 'scan_end']


@pytest.mark.asyncio
async def test_capture_setup_failure_still_maintains_existing_protection():
    from types import SimpleNamespace
    from strategies.hrs.decision_capture import capture_cycle
    recorder = AsyncMock()
    recorder.begin.side_effect = RuntimeError('db unavailable')
    strategy = SimpleNamespace(decision_recorder=recorder, config={}, candidate_pool=None,
        _klines_cache={}, _klines_4h_cache={}, _signal_timestamps={},
        position_manager=None, risk_manager=None, _monitor_positions=AsyncMock())
    callback = AsyncMock()

    @capture_cycle
    async def cycle(self):
        await callback()

    with pytest.raises(RuntimeError):
        await cycle(strategy)
    callback.assert_not_awaited()
    strategy._monitor_positions.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_postgres_idempotency_and_envelope_conflicts():
    import os
    import uuid
    from pathlib import Path
    import asyncpg
    from strategies.hrs.decision_capture import DecisionRecorder
    dsn = os.getenv('HRS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('dedicated PostgreSQL DSN required')
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute('CREATE SCHEMA IF NOT EXISTS hrs')
        migration = Path(__file__).parents[2]/'database/postgres/migrations/2026-11-17-hrs-decision-evidence.sql'
        await connection.execute(migration.read_text())

        class DB:
            async def execute(self, query, *args):
                return await connection.execute(query, *args)

            async def fetch_all(self, query, *args):
                return await connection.fetch(query, *args)

        recorder = DecisionRecorder(DB())
        now = datetime.now(timezone.utc)
        identity = uuid.uuid4().hex
        args = dict(as_of=now, available_at=now, evidence_id=identity)
        await recorder.record(identity, 'analysis', {'value': 1}, **args)
        await recorder.record(identity, 'analysis', {'value': 1}, **args)
        assert await connection.fetchval('SELECT count(*) FROM hrs.hrs_decision_evidence WHERE evidence_id=$1', identity) == 1
        with pytest.raises(ValueError, match='evidence_identity_conflict'):
            await recorder.record(identity, 'different_kind', {'value': 1}, **args)
        assert recorder.failed
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_nested_capture_scans_restore_outer_id_and_never_clear_failure():
    from types import SimpleNamespace
    from strategies.hrs.decision_capture import DecisionRecorder, capture_cycle
    recorder = DecisionRecorder(AsyncMock())
    events = []

    async def record(run_id, kind, *args, **kwargs):
        events.append((run_id, kind))

    recorder.record = record
    strategy = SimpleNamespace(decision_recorder=recorder, config={}, candidate_pool=None,
        position_manager=None, risk_manager=None, _signal_timestamps={})

    @capture_cycle
    async def inner(self):
        assert recorder.failed

    @capture_cycle
    async def outer(self):
        recorder.failed = True
        await inner(self)

    await outer(strategy)
    assert recorder.failed
    assert events[0][0] == events[-1][0]
    assert events[1][0] == events[2][0]
    assert events[0][0] != events[1][0]


@pytest.mark.asyncio
async def test_caught_capture_failure_cannot_produce_complete_scan():
    from types import SimpleNamespace
    from strategies.hrs.decision_capture import DecisionRecorder, capture_cycle
    recorder = DecisionRecorder(AsyncMock())
    events = []

    async def record(run_id, kind, payload, **kwargs):
        events.append((kind, payload))

    recorder.record = record
    strategy = SimpleNamespace(decision_recorder=recorder, config={}, candidate_pool=None,
        position_manager=None, risk_manager=None, _signal_timestamps={})

    @capture_cycle
    async def cycle(self):
        recorder.failed = True

    await cycle(strategy)
    assert events[-1][1]['status'] == 'incomplete'


@pytest.mark.asyncio
@pytest.mark.parametrize('veto', [False, True])
async def test_real_lv_path_persists_eligible_and_vetoed_scores(veto):
    from strategies.hrs.decision_capture import DecisionRecorder
    from strategies.hrs.tests.test_lv_rm_empty_pool_execution import _setup_strategy
    strategy = _setup_strategy()[0]
    recorder = DecisionRecorder(AsyncMock())
    recorder.record = AsyncMock()
    strategy.decision_recorder = recorder
    if veto:
        strategy.scoring_engine.should_entry.return_value = False
        strategy.scoring_engine.should_entry.side_effect = None
    short, long = [], []
    await strategy._check_lv_rm_entries(['TESTUSDT'], short, long)
    analyses = [call for call in recorder.record.await_args_list if call.args[1] == 'lv_rm_analysis']
    assert len(analyses) == 1
    assert analyses[0].args[2]['market_cap'] > 0
    assert analyses[0].args[2]['short_should_entry'] is (not veto)
    assert len(short) == (0 if veto else 1)


@pytest.mark.asyncio
async def test_failed_public_response_retains_unavailable_provenance_without_error_secrets():
    from strategies.hrs.decision_capture import DecisionRecorder
    recorder = DecisionRecorder(AsyncMock())
    recorder.record = AsyncMock()
    client = AsyncMock()
    client._request.side_effect = TimeoutError('url?signature=credential')
    recorder.attach_public_transport(client)
    with pytest.raises(TimeoutError):
        await client._request('GET', '/fapi/v1/openInterest', params={'symbol': 'BTCUSDT'}, signed=False)
    call = recorder.record.await_args
    assert call is not None
    assert call.args[1] == 'public_market_failure'
    assert call.args[2]['error_type'] == 'TimeoutError'
    assert call.args[2]['available'] is False
    assert 'credential' not in str(call.args)


@pytest.mark.asyncio
async def test_failed_provider_does_not_appear_as_observed_zero():
    from types import SimpleNamespace
    from strategies.hrs.decision_capture import DecisionRecorder
    recorder = DecisionRecorder(AsyncMock())
    recorder.record = AsyncMock()
    provider = SimpleNamespace(**{key: AsyncMock(side_effect=ValueError('secret')) for key in (
        'get_market_cap_provenance', 'get_oi_usd', 'get_funding_rate', 'get_24h_volume')})
    provider.preload_market_caps = AsyncMock(return_value={})
    recorder.attach_market_provider(provider)
    with pytest.raises(ValueError):
        await provider.get_oi_usd('BTCUSDT')
    call = recorder.record.await_args
    assert call.args[1] == 'market_input_failure'
    assert call.args[2]['operation'] == 'get_oi_usd'
    assert call.args[2]['available'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['standard_candidate_overlap', 'symbol_circuit_breaker', 'insufficient_klines'])
async def test_lv_early_veto_is_retained_per_symbol(reason):
    from strategies.hrs.decision_capture import DecisionRecorder
    from strategies.hrs.tests.test_lv_rm_empty_pool_execution import _setup_strategy
    strategy = _setup_strategy()[0]
    recorder = DecisionRecorder(AsyncMock())
    recorder.record = AsyncMock()
    strategy.decision_recorder = recorder
    if reason == 'standard_candidate_overlap':
        strategy.candidate_pool.get_short_candidates.return_value = ['TESTUSDT']
    elif reason == 'symbol_circuit_breaker':
        strategy.risk_manager.is_circuit_breaker_active.return_value = True
    else:
        strategy._klines_cache = {}
        strategy._warmup_klines = AsyncMock(return_value=[])
    await strategy._check_lv_rm_entries(['TESTUSDT'], [], [])
    terminal = [c for c in recorder.record.await_args_list if c.args[1] == 'lv_rm_candidate_outcome']
    assert len(terminal) == 1
    assert terminal[0].kwargs['symbol'] == 'TESTUSDT'
    assert terminal[0].args[2]['reason'] == reason


@pytest.mark.asyncio
async def test_order_evidence_preserves_partial_fill_unknown_average_and_failed_protection():
    from types import SimpleNamespace
    from strategies.hrs.decision_capture import capture_open_execution
    from strategies.hrs.executor import OpenResult
    recorder = SimpleNamespace(record=AsyncMock(), failed=False, run_id='actual-order')
    actual = OpenResult(True, False, ['sl'], {'orderId': 123, 'status': 'CANCELED'},
                        filled_quantity=2, avg_fill_price=None)
    executor = SimpleNamespace(decision_recorder=recorder)
    @capture_open_execution
    async def execute(self, symbol):
        return actual
    assert await execute(executor, 'TESTUSDT') is actual
    payload = recorder.record.await_args.args[2]
    assert payload['outcome'] is actual
    assert actual.avg_fill_price is None
    assert recorder.record.await_args.kwargs['symbol'] == 'TESTUSDT'


@pytest.mark.asyncio
async def test_surviving_batch_preserves_live_short_then_long_execution_order():
    from types import SimpleNamespace
    from strategies.hrs.strategy import HRSStrategy
    recorder = SimpleNamespace(record=AsyncMock(), failed=False, run_id='batch')
    strategy = object.__new__(HRSStrategy)
    strategy.decision_recorder = recorder
    strategy.execute_signal = AsyncMock(return_value=False)
    short = [{'symbol': 'ZUSDT', 'direction': 'short', 'score': 80},
             {'symbol': 'AUSDT', 'direction': 'short', 'score': 90}]
    long = [{'symbol': 'BUSDT', 'direction': 'long', 'score': 70}]
    await strategy._resolve_conflicts(short, long)
    call = recorder.record.await_args
    assert call.args[1] == 'execution_batch'
    assert [row['symbol'] for row in call.args[2]['ordered_signals']] == ['ZUSDT', 'AUSDT', 'BUSDT']


def test_capture_retains_provider_identity_attached_to_market_cap_dataclass():
    import json
    from shared.market_cap import MarketCapProvenance
    from strategies.hrs.decision_capture import canonical_payload
    prov = MarketCapProvenance(value=123, source='coinmarketcap')
    prov.provider = 'coinmarketcap'
    prov.provider_asset_id = '74'
    payload = json.loads(canonical_payload(prov)[0])
    assert payload['provider'] == 'coinmarketcap'
    assert payload['provider_asset_id'] == '74'


@pytest.mark.asyncio
async def test_market_cap_preload_records_full_requested_denominator_and_missing_identity():
    from types import SimpleNamespace
    from shared.market_cap import MarketCapProvenance
    from strategies.hrs.market_data import MarketDataProvider
    from strategies.hrs.decision_capture import DecisionRecorder
    provider = MarketDataProvider(AsyncMock(), AsyncMock(), {})
    quotes = {'BTCUSDT': MarketCapProvenance(value=10, source='coingecko'),
              'UNKNOWNUSDT': MarketCapProvenance(missing_reason='identity_not_verified')}
    provider.market_cap_service = SimpleNamespace(get_batch_market_caps_with_provenance=AsyncMock(return_value=quotes))
    recorder = DecisionRecorder(AsyncMock())
    recorder.record = AsyncMock()
    recorder.attach_market_provider(provider)
    await provider.preload_market_caps(['BTCUSDT', 'UNKNOWNUSDT'])
    call = recorder.record.await_args
    assert call.args[1] == 'market_cap_batch'
    assert call.args[2]['requested_symbols'] == ['BTCUSDT', 'UNKNOWNUSDT']
    assert call.args[2]['quotes']['UNKNOWNUSDT'].missing_reason == 'identity_not_verified'


@pytest.mark.asyncio
async def test_standard_preanalysis_circuit_veto_is_not_silently_lost():
    from strategies.hrs.decision_capture import DecisionRecorder
    from strategies.hrs.tests.test_lv_rm_empty_pool_execution import _setup_strategy
    from unittest.mock import MagicMock
    strategy = _setup_strategy()[0]
    recorder = DecisionRecorder(AsyncMock())
    recorder.record = AsyncMock()
    strategy.decision_recorder = recorder
    strategy._is_settlement_skip_window = MagicMock(return_value=False)
    strategy._check_candidate_update = AsyncMock()
    strategy.candidate_pool.get_active_symbols.return_value = ['TESTUSDT']
    strategy.candidate_pool.get_lv_rm_scan_range = AsyncMock(return_value=[])
    strategy.risk_manager.is_circuit_breaker_active.return_value = True
    strategy._finalize_and_execute_signals = AsyncMock()
    strategy._monitor_positions = AsyncMock()
    strategy._report_positions_to_dashboard = AsyncMock()
    strategy._cleanup_orphan_orders = AsyncMock()
    await strategy._execute_cycle()
    rows = [c.args[2] for c in recorder.record.await_args_list if c.args[1] == 'standard_candidate_outcome']
    assert len(rows) == 1 and rows[0]['reason'] == 'symbol_circuit_breaker'


@pytest.mark.asyncio
async def test_settlement_cycle_retains_explicit_denial_reason():
    from strategies.hrs.strategy import HRSStrategy
    from strategies.hrs.decision_capture import DecisionRecorder
    from unittest.mock import MagicMock
    strategy = HRSStrategy({'strategy': {'name': 'hrs'}})
    recorder = DecisionRecorder(AsyncMock())
    recorder.record = AsyncMock()
    strategy.decision_recorder = recorder
    strategy._is_settlement_skip_window = MagicMock(return_value=True)
    strategy._monitor_positions = AsyncMock()
    strategy._report_positions_to_dashboard = AsyncMock()
    strategy._cleanup_orphan_orders = AsyncMock()
    await strategy._execute_cycle()
    rows = [c.args[2] for c in recorder.record.await_args_list if c.args[1] == 'scan_denial']
    assert rows == [{'reason': 'settlement_window'}]


@pytest.mark.asyncio
async def test_actual_protection_ack_is_recorded_without_blocking_existing_risk_on_record_failure():
    from strategies.hrs.executor import TradingExecutor
    from strategies.hrs.position_manager import PositionManager
    from strategies.hrs.decision_capture import DecisionRecorder
    pm = PositionManager({}, AsyncMock())
    pm.add_position('TESTUSDT', 'long', 100, 10, 2)
    executor = TradingExecutor({}, AsyncMock(), AsyncMock(), AsyncMock(), pm)
    recorder = DecisionRecorder(AsyncMock())
    recorder.record = AsyncMock()
    executor.decision_recorder = recorder
    actual = {'algoId': 987, 'symbol': 'TESTUSDT', 'quantity': '10', 'triggerPrice': '95'}
    executor._submit_conditional_order = AsyncMock(return_value=(actual, 987))
    result = await executor._place_protection_order('TESTUSDT', 'sl', 'SELL', 'STOP_MARKET',
        Decimal('95'), Decimal('94.8'), Decimal('10'), close_position=True)
    assert result['success'] is True
    payload = recorder.record.await_args.args[2]
    assert payload['response'] is actual
    assert payload['role'] == 'sl' and payload['algo_id'] == 987
    assert payload['position_id'] == pm.get_position('TESTUSDT')['position_id']
    recorder.record.side_effect = RuntimeError('evidence store offline')
    result = await executor._place_protection_order('TESTUSDT', 'sl', 'SELL', 'STOP_MARKET',
        Decimal('95'), Decimal('94.8'), Decimal('10'), close_position=True)
    assert result['success'] is True
    assert recorder.failed


@pytest.mark.asyncio
async def test_scan_available_time_follows_slow_context_collection():
    import asyncio
    from types import SimpleNamespace
    from strategies.hrs.decision_capture import DecisionRecorder, capture_cycle
    recorder = DecisionRecorder(AsyncMock())
    recorder.record = AsyncMock()
    available = None
    async def context():
        nonlocal available
        await asyncio.sleep(.001)
        available = datetime.now(timezone.utc)
        return {'available_at': available}
    recorder.context_provider = context
    strategy = SimpleNamespace(decision_recorder=recorder, config={}, candidate_pool=None,
        position_manager=None, risk_manager=None, _signal_timestamps={})
    @capture_cycle
    async def cycle(self):
        pass
    await cycle(strategy)
    start = recorder.record.await_args_list[0]
    assert start.args[1] == 'scan_start'
    assert start.kwargs['as_of'] < available <= start.kwargs['available_at']


@pytest.mark.asyncio
async def test_context_fetch_outage_blocks_one_cycle_then_recovers_with_durable_failure_evidence():
    from types import SimpleNamespace
    from strategies.hrs.decision_capture import DecisionRecorder, capture_cycle
    recorder = DecisionRecorder(AsyncMock())
    recorder.record = AsyncMock()
    recorder.context_provider = AsyncMock(side_effect=[TimeoutError('private source unavailable'), {}])
    maintenance = AsyncMock()
    strategy = SimpleNamespace(decision_recorder=recorder, config={}, candidate_pool=None,
        position_manager=None, risk_manager=None, _signal_timestamps={}, _monitor_positions=maintenance)
    body = AsyncMock()
    @capture_cycle
    async def cycle(self):
        await body()
    with pytest.raises(TimeoutError):
        await cycle(strategy)
    assert recorder.failed is False
    assert recorder.record.await_args.args[1] == 'scan_context_failure'
    body.assert_not_awaited()
    maintenance.assert_awaited_once()
    await cycle(strategy)
    body.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_cycle_updates_continuous_acceptance_report(monkeypatch):
    from types import SimpleNamespace
    from strategies.hrs.decision_capture import DecisionRecorder, capture_cycle
    import strategies.hrs.acceptance_report as reports
    monkeypatch.setenv('HRS_ACCEPTANCE_REPORT_ENABLED', '1')
    monkeypatch.setenv('TRADE_ACCOUNT_ID', 'test-account')
    report = {'technical': 'COLLECTING', 'performance_accepted': False}
    build = AsyncMock(return_value=report)
    writer = __import__('unittest.mock', fromlist=['MagicMock']).MagicMock()
    monkeypatch.setattr(reports, 'build_report', build)
    monkeypatch.setattr(reports, 'write_report_atomic', writer)
    recorder = DecisionRecorder(AsyncMock())
    recorder.record = AsyncMock()
    strategy = SimpleNamespace(decision_recorder=recorder, db=object(), config={}, candidate_pool=None,
        position_manager=None, risk_manager=None, _signal_timestamps={})
    @capture_cycle
    async def cycle(self):
        pass
    await cycle(strategy)
    build.assert_awaited_once_with(strategy.db, account_id='test-account')
    writer.assert_called_once_with(report, '/app/logs/hrs-acceptance.json')
