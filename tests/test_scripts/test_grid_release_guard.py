import asyncio
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from tests.test_grid_execution.test_trailing_execution import moving
from scripts import grid_release_guard as guard


def settings():
    return SimpleNamespace(max_margin_usdt=D(240), max_notional_usdt=D(2400),
        max_open_orders=13, expected_leverage=5, max_daily_loss_usdt=D(50),
        max_drawdown_usdt=D(100))


@pytest.mark.asyncio
async def test_maintenance_cancels_owned_orders_and_never_places_entries_or_closes(moving):
    store, exchange, _, _, _, epoch = moving
    result = await guard.pause_owned(store, exchange, settings())
    assert result['pause_complete'] is True
    assert not await exchange.open_orders() and not exchange.place_calls
    exchange.close_position.assert_not_awaited()
    assert store.engine_state()['state'] == 'PAUSED'


@pytest.mark.asyncio
async def test_foreign_or_changed_order_prevents_any_cancel(moving):
    store, exchange, _, _, _, epoch = moving
    exchange.orders['eg-old-0']['side'] = 'SELL'
    with pytest.raises(guard.GuardError, match='ownership'):
        await guard.pause_owned(store, exchange, settings())
    assert not exchange.cancel_calls
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_during_cancel_retains_protection_and_rejects_flat_release(moving):
    store, exchange, _, _, _, epoch = moving
    original = exchange.cancel_order
    async def fill(client):
        if client == 'eg-old-0':
            row = exchange.orders[client]
            row.update(status='FILLED', executedQty='.1')
            exchange.quantity = D('.1')
            exchange.trades = [dict(symbol='ETHUSDT', orderId=1, side='BUY', id=123, qty='.1', price='100')]
            return dict(row)
        return await original(client)
    exchange.cancel_order = fill
    result = await guard.pause_owned(store, exchange, settings())
    assert result['flat'] is False and result['protected'] is True
    assert result['outcome'] == 'safe_nonflat_abort'
    assert len(await exchange.open_stops()) == 1
    assert not exchange.place_calls
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_locked_state_is_never_unlocked_or_promoted(moving):
    store, exchange, _, _, _, _ = moving
    store.set_engine_state('LOCKED', 'risk limit')
    with pytest.raises(guard.GuardError, match='state'):
        await guard.pause_owned(store, exchange, settings())
    assert store.engine_state()['state'] == 'LOCKED' and not exchange.cancel_calls


@pytest.mark.asyncio
async def test_readonly_wrapper_blocks_every_write_before_exchange(moving):
    store, exchange, _, _, _, _ = moving
    client = guard.RestrictedExchange(exchange, store, apply=False)
    for call in (client.place_limit('BUY', D('.1'), D(100), 'new'),
                 client.close_position('SELL', D('.1'), 'close'),
                 client.cancel_order('eg-old-0'),
                 client.place_stop('SELL', D(50), 'stop')):
        with pytest.raises(guard.GuardError):
            await call
    assert not exchange.place_calls and not exchange.cancel_calls
    exchange.close_position.assert_not_awaited()


def test_snapshot_backup_is_independent_and_never_mutates_source(moving, tmp_path):
    store, _, _, _, _, _ = moving
    source = store.conn.execute('PRAGMA database_list').fetchone()[2]
    before = guard.journal_digest(store.conn)
    clone = guard.copy_journal(source, tmp_path / 'clone')
    clone.set_engine_state('LOCKED', 'test clone only')
    clone.close()
    assert guard.journal_digest(store.conn) == before


def test_execution_lock_excludes_a_second_maintenance_writer(tmp_path):
    from strategies.grid.execution.store import ExecutionLock
    with ExecutionLock(tmp_path / 'exclusive'):
        with pytest.raises(RuntimeError, match='held'):
            with ExecutionLock(tmp_path / 'exclusive'):
                pytest.fail('second writer acquired lock')


@pytest.mark.asyncio
async def test_cancel_error_with_unprotected_fill_is_explicit_urgent_failure(moving):
    from unittest.mock import AsyncMock
    store, exchange, _, _, _, _ = moving
    async def failed(client):
        exchange.quantity = D('.1')
        raise OSError('lost cancellation response')
    exchange.cancel_order = failed
    exchange.place_stop = AsyncMock(side_effect=OSError('stop unavailable'))
    result = await guard.pause_owned(store, exchange, settings())
    assert result['outcome'] == 'urgent_unprotected_failure'
    assert result['ready_for_writer'] is False and result['protected'] is False
    assert store.engine_state()['state'] == 'LOCKED'
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonterminal_cancel_receipt_cannot_prove_flat_maintenance(moving):
    from unittest.mock import AsyncMock
    store, exchange, _, _, _, _ = moving
    exchange.cancel_order = AsyncMock(return_value=dict(exchange.orders['eg-old-0']))
    result = await guard.pause_owned(store, exchange, settings())
    assert result['ready_for_writer'] is False and result['pause_complete'] is False
    assert result['ordinary_orders'] == 2


@pytest.fixture
def bound_exchange(moving, monkeypatch, tmp_path):
    import json
    import time
    from unittest.mock import AsyncMock
    from strategies.grid.execution.risk import beijing_day
    store, exchange, _, _, _, _ = moving
    clock = [time.time()]
    monkeypatch.setattr(guard, 'time', SimpleNamespace(time=lambda: clock[0]))
    async def advance(seconds): clock[0] += seconds
    monkeypatch.setattr(guard.asyncio, 'sleep', advance)
    env = dict(GRID_EXECUTION_MODE='live', GRID_SUBACCOUNT_MODE='classic', GRID_SUBACCOUNT_API_KEY='unit-key',
               GRID_SUBACCOUNT_API_SECRET='unit-secret', GRID_ACCOUNT_ID='grid-unit', GRID_MAX_MARGIN_USDT='240',
               GRID_MAX_NOTIONAL_USDT='2400', GRID_MAX_DAILY_LOSS_USDT='15', GRID_MAX_DRAWDOWN_USDT='30',
               GRID_MAX_OPEN_ORDERS='13', GRID_EXPECTED_LEVERAGE='5', GRID_CLASSIC_RISK_CONTINUITY_SHA256='a' * 64,
               GRID_EXECUTION_DB=store.conn.execute('PRAGMA database_list').fetchone()[2],
               GRID_EXECUTION_HEARTBEAT=str(tmp_path / 'heartbeat.json'),
               GRID_EXECUTION_LOCK=str(tmp_path / 'lifetime-lock'))
    (tmp_path / 'heartbeat.json').write_text(json.dumps(dict(updated_at_ms=int(clock[0]*1000),
        state='ACTIVE', risk_status='ALLOW', stream_ready=True)))
    continuity = dict(identity='classic:unit', manifest_sha256='a' * 64, baseline='300', peak='300',
                      day=beijing_day(int(time.time() * 1000)), transfers=[])
    store.conn.execute("INSERT INTO schema_meta VALUES('account_identity',?)", ('classic:unit',))
    store.conn.execute("INSERT INTO schema_meta VALUES('classic_risk_continuity_v1',?)", (json.dumps(continuity),))
    exchange.pm = False
    exchange.account_info = AsyncMock(return_value=dict(accountEquity='300', totalAvailableBalance='300'))
    exchange.income = AsyncMock(return_value=[])
    exchange.journal_identity = AsyncMock(return_value='classic:unit')
    exchange.close = AsyncMock()
    exchange.client = SimpleNamespace(_request_once=AsyncMock(), _get_endpoint=lambda path:path)
    monkeypatch.setattr(guard.GridExchange, 'from_settings', lambda settings:exchange)
    monkeypatch.setenv('GRID_CLASSIC_RISK_CONTINUITY_SHA256', 'a' * 64)
    expected = guard.binding(store, guard.ExecutionSettings.from_env(env), 'classic:unit', env)
    return store, exchange, env, expected


@pytest.mark.asyncio
async def test_live_gate_reconciles_owned_activity_only_on_private_backup(bound_exchange):
    store, exchange, env, _ = bound_exchange
    before = guard.journal_digest(store.conn)
    result = await guard.inspect_grid(env, exchange=exchange)
    assert result['ordinary_orders'] == 2 and result['risk_status'] == 'ALLOW'
    assert result['read_only'] and result['live_journal_untouched']
    assert guard.journal_digest(store.conn) == before
    assert not exchange.cancel_calls and not exchange.place_calls


@pytest.mark.asyncio
async def test_live_gate_retries_concurrent_journal_mutation_then_refuses(bound_exchange):
    store, exchange, env, _ = bound_exchange
    async def change():
        store.conn.execute('UPDATE engine_state SET updated_at_ms=updated_at_ms+1')
        return dict(accountEquity='300', totalAvailableBalance='300')
    exchange.account_info = change
    with pytest.raises(guard.GuardError, match='changed during all'):
        await guard.inspect_grid(env, exchange=exchange)
    assert not exchange.cancel_calls


@pytest.mark.asyncio
async def test_maintenance_holds_lock_preserves_risk_history_and_collects_two_samples(bound_exchange):
    store, exchange, env, expected = bound_exchange
    before = guard.risk_history(store.conn)
    result = await guard.maintain_grid(env, expected)
    assert result['ready_for_writer'] is True and len(result['samples']) == 2
    assert result['samples'][1]['observed_at'] - result['samples'][0]['observed_at'] >= 5
    assert result['before'] == result['after'] and guard.risk_history(store.conn) == before
    assert result['lock_acquired_at'] <= result['lock_released_at']
    assert result['writes'] and all('client_id_sha256' in row for row in result['writes'])
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_writer_lock_blocks_before_exchange_or_mutation(bound_exchange):
    store, exchange, env, expected = bound_exchange
    from strategies.grid.execution.store import ExecutionLock
    before = guard.journal_digest(store.conn)
    with ExecutionLock(env['GRID_EXECUTION_LOCK']):
        result = await guard.maintain_grid(env, expected)
    assert result['ready_for_writer'] is False
    exchange.journal_identity.assert_not_awaited()
    assert guard.journal_digest(store.conn) == before and not exchange.cancel_calls


@pytest.mark.asyncio
async def test_bound_identity_change_prevents_all_maintenance_writes(bound_exchange):
    store, exchange, env, expected = bound_exchange
    expected['account_identity_sha256'] = '0' * 64
    result = await guard.maintain_grid(env, expected)
    assert result['ready_for_writer'] is False and not exchange.cancel_calls


@pytest.mark.asyncio
async def test_missing_protection_fails_readonly_gate_without_creating_stop(bound_exchange):
    store, exchange, env, _ = bound_exchange
    exchange.quantity = D('.1')
    exchange.orders['eg-old-0'].update(status='FILLED', executedQty='.1')
    exchange.trades = [dict(orderId=1,symbol='ETHUSDT',side='BUY',id=4,qty='.1',price='100')]
    with pytest.raises(guard.GuardError, match='reconciliation'):
        await guard.inspect_grid(env, exchange=exchange)
    assert not exchange.stops and not store.all_fills()


@pytest.mark.asyncio
async def test_retiring_only_protective_stop_is_forbidden(moving):
    store, exchange, _, _, _, epoch = moving
    exchange.quantity = D('.1')
    client = guard.RestrictedExchange(exchange, store, apply=True)
    from strategies.grid.execution.fills import ProtectionCoordinator
    assert await ProtectionCoordinator(store, client).ensure(epoch, D('.1'), D(50), D(190))
    only = (await exchange.open_stops())[0]['clientAlgoId']
    with pytest.raises(guard.GuardError, match='replacement'):
        await client.cancel_stop(only)
    assert len(await exchange.open_stops()) == 1


@pytest.mark.asyncio
async def test_unlock_failure_never_claims_released_lock_or_ready_writer(bound_exchange, monkeypatch):
    store, _, env, expected = bound_exchange
    Original = guard.ExecutionLock
    class FailedUnlock(Original):
        def __exit__(self, *args):
            super().__exit__(*args)
            raise OSError('simulated unlock failure')
    monkeypatch.setattr(guard, 'ExecutionLock', FailedUnlock)
    result = await guard.maintain_grid(env, expected)
    assert result['ready_for_writer'] is False
    assert 'lock_released_at' not in result


@pytest.mark.asyncio
async def test_explicit_pause_preserves_entry_restore_when_sigint_callback_never_ran(moving):
    store, exchange, _, _, _, epoch = moving
    result = await guard.pause_owned(store, exchange, settings(), restore_entries=True)
    assert result['ready_for_writer'] is True
    rows = store.conn.execute('SELECT source_id,outcome FROM grid_stream_restore ORDER BY source_id').fetchall()
    assert [tuple(r) for r in rows] == [('eg-old-0', None), ('eg-old-1', None)]
    assert not exchange.place_calls


@pytest.mark.asyncio
async def test_request_trace_preserves_budget_dispatch_and_rejects_nonapproved_writes():
    from unittest.mock import AsyncMock
    request = AsyncMock(return_value={})
    exchange = SimpleNamespace(client=SimpleNamespace(_request_once=request, _get_endpoint=lambda path:path))
    trace = []
    guard.trace_client(exchange, trace)
    await exchange.client._request_once('GET', '/fapi/v1/premiumIndex', {}, False)
    await exchange.client._request_once('GET', '/fapi/v3/account')
    for method, path, params in [('POST','/fapi/v1/order', {}), ('DELETE','/fapi/v1/order', {}),
                                 ('POST','/fapi/v1/algoOrder', {'type':'STOP_MARKET','closePosition':'true'}),
                                 ('PUT','/fapi/v1/listenKey', {})]:
        with pytest.raises(guard.GuardError):
            await exchange.client._request_once(method, path, params)
    assert request.await_count == 2 and all(r['method'] == 'GET' for r in trace)


@pytest.mark.asyncio
async def test_maintenance_request_allowlist_never_permits_market_or_limit_entry():
    from unittest.mock import AsyncMock
    request = AsyncMock(return_value={})
    exchange = SimpleNamespace(client=SimpleNamespace(_request_once=request, _get_endpoint=lambda path:path))
    guard.trace_client(exchange, [], apply=True)
    for params in ({'type':'LIMIT'}, {'type':'MARKET','reduceOnly':'true'},
                   {'type':'STOP_MARKET','closePosition':'false'}):
        with pytest.raises(guard.GuardError):
            await exchange.client._request_once('POST', '/fapi/v1/order', params)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_heartbeat_rejects_readonly_safe_gate(bound_exchange):
    import json
    from pathlib import Path
    _, exchange, env, _ = bound_exchange
    path = Path(env['GRID_EXECUTION_HEARTBEAT'])
    heartbeat = json.loads(path.read_text()); heartbeat['updated_at_ms'] -= 120_001
    path.write_text(json.dumps(heartbeat))
    with pytest.raises(guard.GuardError, match='heartbeat'):
        await guard.inspect_grid(env, exchange=exchange)
    assert not exchange.cancel_calls and not exchange.place_calls


@pytest.mark.asyncio
async def test_readonly_probe_dates_evidence_before_a_slow_account_request(bound_exchange):
    _, exchange, env, _ = bound_exchange
    started = guard.time.time()
    async def delayed():
        await guard.asyncio.sleep(30)
        return dict(accountEquity='300', totalAvailableBalance='300')
    exchange.account_info = delayed
    proof = await guard.inspect_grid(env, exchange=exchange)
    assert proof['observed_at'] == started
    assert guard.time.time() - proof['observed_at'] == 30
