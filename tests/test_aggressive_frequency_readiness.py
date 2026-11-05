from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from strategies.btc_eth_aggressive.strategy import FrequencyController, BTCEthStrategy
from shared.entry_risk_policy import submit_aggressive_entry, EntryDenied

CONFIG = {'max_daily_symbol_trades': 10, 'max_daily_loss_usdt': 100,
          'weekly_loss_pause_enabled': False}


def row(at, **extra):
    return dict(symbol='ETHUSDT', last_trade_time=at, trade_date=at.date(),
                daily_trade_count=1, daily_pnl=Decimal('0'), consecutive_losses=0,
                pause_until=None, weekly_pnl=Decimal('0'), weekly_pause_until=None, **extra)


@pytest.mark.asyncio
async def test_restore_latest_independent_of_row_order_and_reload():
    now = datetime.now().astimezone()
    old, recent = row(now-timedelta(days=3)), row(now)
    for rows in ([old, recent], [recent, old]):
        fc = FrequencyController(CONFIG, SimpleNamespace(fetch_all=AsyncMock(return_value=rows)))
        await fc.load_state()
        await fc.load_state()
        assert fc.symbol_last_trade_time['ETHUSDT'] == now
        assert fc.daily_trades[now.date().isoformat()] == 1
        assert fc.state_ready is True


@pytest.mark.asyncio
@pytest.mark.parametrize('rows', [None])
async def test_unknown_history_denies(rows):
    fc = FrequencyController(CONFIG, SimpleNamespace(fetch_all=AsyncMock(return_value=rows)))
    await fc.load_state()
    assert fc.can_trade('ETHUSDT', datetime.now())[0] is False


@pytest.mark.asyncio
async def test_read_failure_revokes_readiness_without_partial_publish():
    now = datetime.now().astimezone()
    db = SimpleNamespace(fetch_all=AsyncMock(return_value=[row(now)]))
    fc = FrequencyController(CONFIG, db)
    await fc.load_state()
    db.fetch_all.side_effect = RuntimeError('unavailable')
    await fc.load_state()
    assert fc.can_trade('ETHUSDT', datetime.now())[0] is False
    assert fc.symbol_last_trade_time == {'ETHUSDT': now}


@pytest.mark.asyncio
async def test_no_database_and_save_failure_deny():
    fc = FrequencyController(CONFIG)
    await fc.load_state()
    assert fc.can_trade('ETHUSDT', datetime.now())[0] is False
    now = datetime.now().astimezone()
    fc.db_manager = SimpleNamespace(fetch_all=AsyncMock(return_value=[row(now)]), execute=AsyncMock(side_effect=RuntimeError('write failed')))
    await fc.load_state()
    await fc.record_trade('ETHUSDT', now)
    assert fc.can_trade('ETHUSDT', datetime.now())[0] is False


@pytest.mark.asyncio
async def test_direct_entry_paths_deny_unready_without_writers():
    strategy = SimpleNamespace(frequency_controller=FrequencyController(CONFIG))
    assert await BTCEthStrategy.execute_signal(strategy, {}) is False
    assert await BTCEthStrategy._place_entry_order(strategy, 'ETHUSDT', {}) is None
    with pytest.raises(EntryDenied, match='frequency'):
        await submit_aggressive_entry(strategy, 'ETHUSDT', {})


@pytest.mark.asyncio
async def test_aware_pause_with_naive_runtime_clock_denies_without_typeerror():
    now = datetime.now().astimezone()
    saved = row(now)
    saved['pause_until'] = now + timedelta(hours=1)
    fc = FrequencyController(CONFIG, SimpleNamespace(fetch_all=AsyncMock(return_value=[saved])))
    await fc.load_state()
    assert fc.can_trade('ETHUSDT', datetime.now())[0] is False


@pytest.mark.asyncio
async def test_successful_empty_history_is_ready():
    fc = FrequencyController(CONFIG, SimpleNamespace(fetch_all=AsyncMock(return_value=[])))
    await fc.load_state()
    assert fc.state_ready is True
    assert fc.can_trade('ETHUSDT', datetime.now())[0] is True

@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    {'symbol': ''}, {'trade_date': None}, {'last_trade_time': datetime.now()},
    {'daily_trade_count': -1}, {'daily_trade_count': None}, {'consecutive_losses': -1},
    {'daily_pnl': 'NaN'}, {'weekly_pnl': 'Infinity'}, {'daily_pnl': None}, {'weekly_pnl': None}, {'pause_until': datetime.now()},
    {'weekly_pause_until': datetime.now()},
])
async def test_malformed_snapshot_never_partially_publishes(change):
    now = datetime.now().astimezone()
    good = row(now)
    bad = row(now-timedelta(days=1))
    bad.update(change)
    fc = FrequencyController(CONFIG, SimpleNamespace(fetch_all=AsyncMock(return_value=[good, bad])))
    await fc.load_state()
    assert fc.state_ready is False
    assert fc.symbol_last_trade_time == {}


@pytest.mark.asyncio
async def test_duplicate_identity_denies():
    saved = row(datetime.now().astimezone())
    fc = FrequencyController(CONFIG, SimpleNamespace(fetch_all=AsyncMock(return_value=[saved, saved])))
    await fc.load_state()
    assert not fc.state_ready


@pytest.mark.asyncio
async def test_weekly_and_pauses_are_order_independent():
    now = datetime.now().astimezone()
    a, b = row(now), row(now)
    a.update(symbol='AAA', weekly_pnl=Decimal('-4'), pause_until=now+timedelta(hours=2))
    b.update(symbol='BBB', weekly_pnl=Decimal('-2'), pause_until=now+timedelta(hours=1), weekly_pause_until=now+timedelta(days=1))
    for rows in ([a,b], [b,a]):
        fc = FrequencyController(CONFIG, SimpleNamespace(fetch_all=AsyncMock(return_value=rows)))
        await fc.load_state()
        assert fc.state_ready
        assert fc.weekly_pnl[fc._get_week_key(now)] == Decimal('-4')
        assert fc.pause_until == a['pause_until']
        assert fc.weekly_pause_until == b['weekly_pause_until']


@pytest.mark.asyncio
async def test_successful_record_persists_and_missing_db_revokes_readiness():
    db = SimpleNamespace(fetch_all=AsyncMock(return_value=[]), execute=AsyncMock())
    fc = FrequencyController(CONFIG, db)
    await fc.load_state()
    await fc.record_trade('AAA', datetime.now())
    assert fc.state_ready
    assert fc.symbol_last_trade_time['AAA'].tzinfo == timezone.utc
    db.execute.assert_awaited_once()
    fc.db_manager = None
    await fc._save_state('AAA')
    assert not fc.state_ready


def test_equivalent_offsets_retain_exact_elapsed_cooldown():
    utc = datetime(2026,11,4,0,tzinfo=timezone.utc)
    china = utc.astimezone(timezone(timedelta(hours=8)))
    assert FrequencyController.utc_time(china)-FrequencyController.utc_time(utc) == timedelta(0)


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke', [False, True])
async def test_final_submit_rechecks_readiness_after_lock_wait(monkeypatch, revoke):
    from contextlib import asynccontextmanager
    import shared.entry_risk_policy as risk
    fc = SimpleNamespace(state_ready=True)
    signal = {'_risk_client_id':'id','_risk_stop':'95','initial_stop_loss':95,'direction':'LONG'}
    strategy = SimpleNamespace(frequency_controller=fc, db_manager=None,
        _get_symbol_precision=AsyncMock(return_value={}), risk_config={},
        _get_grade_risk=lambda grade: {'partial_take_profit':{}},
        binance=SimpleNamespace(place_order=AsyncMock(return_value={'orderId':1})))
    @asynccontextmanager
    async def submitting(*args):
        if revoke:
            fc.state_ready=False
        yield {'symbol':'AAA','quantity':1,'price':100,'geometry':{}}
    monkeypatch.setattr(risk, 'RiskPolicyStore', lambda db:SimpleNamespace(submitting=submitting))
    monkeypatch.setattr(risk, 'entry_geometry', lambda *args:{})
    if revoke:
        with pytest.raises(EntryDenied, match='frequency'):
            await submit_aggressive_entry(strategy,'AAA',signal)
        strategy.binance.place_order.assert_not_awaited()
    else:
        assert await submit_aggressive_entry(strategy,'AAA',signal) == {'orderId':1}
        strategy.binance.place_order.assert_awaited_once()

@pytest.mark.asyncio
async def test_restored_weekly_loss_cannot_be_bypassed_by_restart():
    now = datetime.now().astimezone()
    saved = row(now)
    saved['weekly_pnl'] = Decimal('-100')
    fc = FrequencyController(dict(CONFIG, weekly_loss_pause_enabled=True), SimpleNamespace(fetch_all=AsyncMock(return_value=[saved])))
    await fc.load_state()
    assert fc.can_trade('AAA', datetime.now())[0] is False


@pytest.mark.asyncio
async def test_recorded_loss_pause_and_successful_reload_after_write_failure():
    config = dict(CONFIG, consecutive_loss_pause=1, pause_duration_hours=24)
    db = SimpleNamespace(fetch_all=AsyncMock(return_value=[]), execute=AsyncMock())
    fc = FrequencyController(config, db)
    await fc.load_state()
    await fc.record_trade('AAA', datetime.now(), Decimal('-1'))
    assert fc.can_trade('AAA', datetime.now())[0] is False
    db.execute.side_effect = RuntimeError('failed')
    await fc.record_trade('AAA', datetime.now())
    assert not fc.state_ready
    db.fetch_all.return_value = [dict(row(datetime.now().astimezone()), pause_until=fc.pause_until)]
    await fc.load_state()
    assert fc.state_ready
    assert fc.can_trade('AAA', datetime.now())[0] is False

@pytest.mark.asyncio
async def test_post_restore_new_loss_accumulates_on_restored_week():
    now = datetime.now().astimezone()
    saved = row(now)
    saved['weekly_pnl'] = Decimal('-70')
    config = dict(CONFIG, weekly_loss_pause_enabled=True, consecutive_loss_pause=99, pause_duration_hours=24)
    db = SimpleNamespace(fetch_all=AsyncMock(return_value=[saved]), execute=AsyncMock())
    fc = FrequencyController(config, db)
    await fc.load_state()
    assert fc.can_trade('AAA', datetime.now())[0]
    await fc.record_trade('AAA', datetime.now(), Decimal('-10'))
    assert fc._calculate_weekly_pnl(fc._get_week_key(datetime.now())) == Decimal('-80')
    assert not fc.can_trade('AAA', datetime.now())[0]

@pytest.mark.asyncio
async def test_ready_entry_paths_continue_to_policy_gate(monkeypatch):
    import strategies.btc_eth_aggressive.strategy as module
    policy = AsyncMock(return_value=False)
    monkeypatch.setattr(module, 'entry_allowed', policy)
    strategy = SimpleNamespace(frequency_controller=SimpleNamespace(state_ready=True))
    assert await BTCEthStrategy.execute_signal(strategy, {}) is False
    assert await BTCEthStrategy._place_entry_order(strategy, 'AAA', {}) is None
    assert policy.await_count == 2


@pytest.mark.asyncio
async def test_both_analysis_paths_use_absolute_cooldown_instants():
    fc = FrequencyController(CONFIG, SimpleNamespace(fetch_all=AsyncMock(return_value=[])))
    await fc.load_state()
    fc.symbol_last_trade_time['AAA'] = datetime.now(timezone.utc)-timedelta(hours=1)
    strategy = SimpleNamespace(frequency_controller=fc, symbol_config={}, positions={},
        _check_economic_calendar=lambda now:(True, ''), timeframes=['1h'],
        kline_service=SimpleNamespace(get_multi_timeframe_data=AsyncMock(return_value={'1h':[{}]})),
        _build_indicators=lambda klines:{}, risk_config={'market_state':{'enabled':False}})
    allowed, reason = await BTCEthStrategy._check_stateful_execution_eligibility(
        strategy, 'AAA', datetime.now(), market_state_name='RANGING', market_behavior={})
    assert not allowed and '冷却期' in reason
    result = await BTCEthStrategy.analyze(strategy,'AAA')
    assert '冷却期' in result['reason']


@pytest.mark.asyncio
async def test_known_row_without_trade_timestamp_is_valid():
    saved = row(datetime.now().astimezone())
    saved['last_trade_time'] = None
    fc = FrequencyController(CONFIG, SimpleNamespace(fetch_all=AsyncMock(return_value=[saved])))
    await fc.load_state()
    assert fc.state_ready
    assert fc.symbol_last_trade_time == {}
