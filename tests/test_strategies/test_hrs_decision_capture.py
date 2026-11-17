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
    with pytest.raises(RuntimeError):
        await client._request('GET', '/fapi/v1/openInterest', params={'symbol': 'BTCUSDT'}, signed=False)
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
