"""The account observer must never turn missing evidence into healthy equity."""
from copy import deepcopy
from decimal import Decimal
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from shared.readonly_client import ReadOnlyBinanceClient

from ai_tuner.monitor.account_risk import assess_account_risk, collect_account_risk


T = 1_795_000_000_000
TYPES = ('MAIN_PORTFOLIO_MARGIN', 'PORTFOLIO_MARGIN_MAIN')


def budget():
    return {'account_id': 'default', 'account_identity': 'operator-bound-default',
            'budget_id': 'default-pm-5pct-20261117-v1',
            'max_drawdown_ratio': '0.05', 'decision_at_ms': T - 1000,
            'max_sample_gap_ms': 60_000, 'max_snapshot_age_ms': 120_000}


def evidence(values=('100', '102', '96.9')):
    points = [{'account_id': 'default', 'account_identity': budget()['account_identity'],
               'exchange_time_ms': T + n * 1000, 'equity_usd': value,
               'source': 'signed:GET /papi/v1/account', 'point_id': str(n)}
              for n, value in enumerate(values)]
    return {'account_id': 'default', 'account_identity': budget()['account_identity'],
            'baseline': points[0], 'equity_points': points, 'cashflows': [],
            'coverage': [{'account_id': 'default', 'account_identity': budget()['account_identity'],
                          'transfer_type': typ, 'start_ms': T + 1,
                          'end_ms': points[-1]['exchange_time_ms'], 'complete': True,
                          'total': 0, 'event_ids': []}
                         for typ in TYPES],
            'as_of_ms': points[-1]['exchange_time_ms'],
            'observed_at_ms': points[-1]['exchange_time_ms'] + 100,
            'source_gaps': []}


def test_exact_five_percent_from_historical_high_requires_review():
    result = assess_account_risk(evidence(), budget())
    assert result['status'] == 'SAFETY_REVIEW'
    assert Decimal(result['max_drawdown_ratio']) == Decimal('.05')
    assert Decimal(result['current_drawdown_ratio']) == Decimal('.05')
    assert result['known_breach'] is not None
    assert result['confirmed_drawdown_lower_bound_ratio'] == '0.05'


def test_complete_breach_assessment_uses_durable_breach_when_present():
    data = evidence()
    durable = {'account_id': 'default', 'account_identity': budget()['account_identity'],
               'source': 'persisted:account_risk_verified_breach',
               'baseline_time_ms': T, 'as_of_ms': T + 2000, 'max_drawdown_ratio': '0.05'}
    data['confirmed_breaches'] = [durable]
    result = assess_account_risk(data, budget())
    assert result['status'] == 'SAFETY_REVIEW'
    assert result['known_breach'] == durable
    assert result['source_gaps'] == []
    assert result['current_equity_usd'] == '96.9'


def test_exact_limit_does_not_round_down_after_multiple_equity_ratios():
    data = evidence(('4743.55', '5869.64', '9077.97', '8624.0715'))
    result = assess_account_risk(data, budget())
    assert result['status'] == 'SAFETY_REVIEW'
    assert Decimal(result['max_drawdown_ratio']) == Decimal('.05')
    assert result['known_breach'] is not None
    assert result['known_breach']['source'] == 'assessed:account_risk_verified_breach'


def test_signed_zero_equity_after_positive_baseline_is_full_drawdown():
    result = assess_account_risk(evidence(('100', '0')), budget())
    assert result['status'] == 'SAFETY_REVIEW'
    assert Decimal(result['max_drawdown_ratio']) == 1


def test_invalid_budget_and_malformed_facts_fail_closed_without_crashing():
    limits = budget()
    limits['max_sample_gap_ms'] = 'bad'
    assert assess_account_risk(evidence(), limits)['status'] == 'BLOCKED_DATA'
    data = evidence()
    data['equity_points'][0] = None
    assert assess_account_risk(data, budget())['status'] == 'BLOCKED_DATA'


def test_recovery_does_not_erase_maximum_drawdown_or_rebase():
    result = assess_account_risk(evidence(('100', '102', '96.9', '104')), budget())
    assert result['status'] == 'SAFETY_REVIEW'
    assert Decimal(result['current_drawdown_ratio']) == 0
    assert Decimal(result['max_drawdown_ratio']) == Decimal('.05')
    assert result['baseline']['equity_usd'] == '100'


def test_budget_renewal_registration_does_not_change_original_activation():
    data = evidence()
    data['baseline']['decision_at_ms'] = T - 1000
    limits = budget()
    limits.update(budget_id='renewal-v2', decision_at_ms=T + 1500)
    result = assess_account_risk(data, limits)
    assert result['status'] == 'SAFETY_REVIEW'
    assert result['activation_gap']['start_ms'] == T - 1000


def test_external_baseline_pin_rejects_a_different_epoch():
    limits = {**budget(), 'expected_baseline_time_ms': T - 1000}
    result = assess_account_risk(evidence(), limits)
    assert result['status'] == 'BLOCKED_DATA'
    assert 'baseline_pin_mismatch' in result['source_gaps']


def test_zero_cashflow_complete_signed_points_form_unit_nav():
    result = assess_account_risk(evidence(('100', '102', '101')), budget())
    assert result['status'] == 'VERIFIED'
    assert Decimal(result['unit_nav']) == Decimal('1.01')
    assert result['activation_gap']['start_ms'] == T - 1000
    assert result['activation_gap']['end_ms'] == T


@pytest.mark.parametrize('broken,reason', [
    ('coverage', 'cashflow_coverage_gap'), ('baseline', 'baseline_missing'),
    ('identity', 'account_identity_mismatch'), ('sampling', 'equity_sampling_gap'),
    ('stale', 'equity_snapshot_stale'), ('order', 'equity_order_invalid'),
    ('future', 'equity_snapshot_future'),
])
def test_missing_or_invalid_facts_never_become_healthy(broken, reason):
    data = evidence(('100', '100', '100'))
    if broken == 'coverage': data['coverage'] = []
    if broken == 'baseline': data['baseline'] = None
    if broken == 'identity': data['equity_points'][1]['account_identity'] = 'grid'
    if broken == 'sampling': data['equity_points'][1]['exchange_time_ms'] -= 100_000
    if broken == 'stale': data['observed_at_ms'] += 200_000
    if broken == 'order': data['equity_points'][1]['exchange_time_ms'] = T
    if broken == 'future': data['observed_at_ms'] = T
    result = assess_account_risk(data, budget())
    assert result['status'] == 'BLOCKED_DATA'
    assert result['max_drawdown_ratio'] is None
    assert any(reason in gap for gap in result['source_gaps'])


def with_cashflow(*, asset='USDT', anchors=True):
    data = evidence(('100', '102', '152'))
    event = {'account_id': 'default', 'account_identity': budget()['account_identity'],
             'transfer_type': TYPES[0], 'event_id': 'deposit-1',
             'exchange_time_ms': T + 1500, 'asset': asset, 'amount': '50',
             'status': 'CONFIRMED', 'source': 'signed:GET /sapi/v1/asset/transfer'}
    if anchors:
        event.update(value_usd='50', valuation_id='event-valuation-1',
                     pre_equity_usd='102', post_equity_usd='152',
                     pre_valuation_time_ms=T + 1500, post_valuation_time_ms=T + 1500)
    data['cashflows'] = [event]
    data['coverage'][0].update(total=1, event_ids=['deposit-1'])
    return data


def test_real_cashflow_without_event_anchors_stays_blocked_on_later_runs():
    data = with_cashflow(anchors=False)
    for _ in range(2):
        result = assess_account_risk(data, budget())
        assert result['status'] == 'BLOCKED_DATA'
        assert 'cashflow_event_equity_unverified:deposit-1' in result['source_gaps']


def test_cashflow_is_neutralized_only_with_event_time_valuations():
    result = assess_account_risk(with_cashflow(), budget())
    assert result['status'] == 'VERIFIED'
    assert Decimal(result['unit_nav']) == Decimal('1.02')
    assert Decimal(result['max_drawdown_ratio']) == 0


def test_multiple_deposit_and_withdrawal_events_preserve_exact_five_percent_drawdown():
    data = evidence(('100', '153', '50.35'))
    deposit = with_cashflow()['cashflows'][0]
    deposit.update(exchange_time_ms=T + 500, pre_valuation_time_ms=T + 500,
                   post_valuation_time_ms=T + 500, pre_equity_usd='100', post_equity_usd='150')
    withdrawal = {**deposit, 'transfer_type': TYPES[1], 'event_id': 'withdrawal-1',
                  'exchange_time_ms': T + 1500, 'amount': '100', 'value_usd': '100',
                  'pre_equity_usd': '153', 'post_equity_usd': '53',
                  'pre_valuation_time_ms': T + 1500, 'post_valuation_time_ms': T + 1500}
    data['cashflows'] = [deposit, withdrawal]
    data['coverage'][0].update(total=1, event_ids=['deposit-1'])
    data['coverage'][1].update(total=1, event_ids=['withdrawal-1'])
    result = assess_account_risk(data, budget())
    assert result['status'] == 'SAFETY_REVIEW'
    assert Decimal(result['max_drawdown_ratio']) == Decimal('.05')
    assert Decimal(result['unit_nav']) == Decimal('.969')


def test_transfer_inside_snapshot_request_interval_is_ambiguous():
    data = with_cashflow()
    data['equity_points'][-1].update(request_start_ms=T + 1400, request_end_ms=T + 2000)
    result = assess_account_risk(data, budget())
    assert result['status'] == 'BLOCKED_DATA'
    assert 'cashflow_snapshot_time_ambiguous:deposit-1' in result['source_gaps']


def test_same_timestamp_cashflows_cannot_invent_an_event_order():
    data = with_cashflow()
    second = {**data['cashflows'][0], 'event_id': 'deposit-2'}
    data['cashflows'].append(second)
    data['coverage'][0].update(total=2, event_ids=['deposit-1', 'deposit-2'])
    assert assess_account_risk(data, budget())['status'] == 'BLOCKED_DATA'


def test_non_usdt_missing_event_value_is_unknown():
    data = with_cashflow(asset='BTC')
    del data['cashflows'][0]['value_usd']
    result = assess_account_risk(data, budget())
    assert 'cashflow_value_unverified:deposit-1' in result['source_gaps']
    assert result['status'] == 'BLOCKED_DATA'


def test_coverage_cannot_survive_lost_cashflow_fact():
    data = with_cashflow()
    data['cashflows'] = []
    result = assess_account_risk(data, budget())
    assert result['status'] == 'BLOCKED_DATA'
    assert any('coverage_event_identity_mismatch' in gap for gap in result['source_gaps'])


def test_grid_points_cannot_be_merged_into_main_account():
    data = evidence()
    data['equity_points'][1]['account_id'] = 'grid'
    assert assess_account_risk(data, budget())['status'] == 'BLOCKED_DATA'


def test_assessment_does_not_modify_facts_or_budget():
    data, limits = evidence(), budget()
    original = deepcopy((data, limits))
    assess_account_risk(data, limits)
    assert (data, limits) == original


def test_prior_verified_breach_is_not_downgraded_by_later_data_gap():
    data = evidence()
    data['coverage'] = []
    data['confirmed_breaches'] = [{'account_id': 'default',
        'account_identity': budget()['account_identity'],
        'source': 'persisted:account_risk_verified_breach',
        'baseline_time_ms': T, 'as_of_ms': T + 2000, 'max_drawdown_ratio': '0.05'}]
    result = assess_account_risk(data, budget())
    assert result['status'] == 'SAFETY_REVIEW'
    assert result['max_drawdown_ratio'] is None
    assert result['current_drawdown_ratio'] is None
    assert result['confirmed_drawdown_lower_bound_ratio'] == '0.05'
    assert result['known_breach']


@pytest.mark.asyncio
async def test_collector_rejects_non_readonly_clients_before_network_or_database():
    result = await collect_account_risk(None, object(), object(), budget())
    assert result['status'] == 'BLOCKED_DATA'
    assert 'readonly_client_required' in result['source_gaps']


DSN = os.environ.get('ACCOUNT_RISK_TEST_DSN')
if DSN:
    parsed = urlsplit(DSN)
    if parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.path != '/account_risk_tests':
        raise ValueError('ACCOUNT_RISK_TEST_DSN requires local isolated account_risk_tests')


@pytest_asyncio.fixture
async def risk_db():
    if not DSN:
        pytest.skip('set ACCOUNT_RISK_TEST_DSN to local isolated account_risk_tests')
    import asyncpg
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute('DROP TABLE IF EXISTS trading.account_risk_baseline, '
                           'trading.account_risk_equity_point, trading.account_risk_transfer, '
                           'trading.account_risk_coverage, trading.account_risk_breach CASCADE')
        migration = Path(__file__).resolve().parents[2] / 'database/postgres/migrations/2026-11-17-account-risk-facts.sql'
        await conn.execute(migration.read_text())
        await conn.execute(migration.read_text())
    try:
        yield SimpleNamespace(pool=pool)
    finally:
        await pool.close()


class Exchange(ReadOnlyBinanceClient):
    def __init__(self, *, wallet=False):
        super().__init__('test-risk-key', 'test-risk-secret',
                         base_url='https://api.binance.com' if wallet else 'https://papi.binance.com',
                         use_unified_account=not wallet)
        self.stamp, self.equity = T, '100'
        self.calls = []
        self.history = lambda params: {'total': 0}

    async def _request(self, method, endpoint, params=None, signed=True, **kwargs):
        assert method == 'GET' and signed is True
        self.calls.append((endpoint, params))
        if endpoint == '/papi/v1/account':
            return {'accountEquity': self.equity, 'actualEquity': self.equity, 'updateTime': self.stamp}
        assert endpoint == '/sapi/v1/asset/transfer'
        assert params['size'] == 100 and params['type'] in TYPES
        return self.history(params)


@pytest.mark.asyncio
async def test_atomic_postgres_facts_and_persistent_baseline_across_restart_and_renewal(risk_db, monkeypatch):
    import ai_tuner.monitor.account_risk as mod
    pm, wallet = Exchange(), Exchange(wallet=True)
    monkeypatch.setattr(mod.time, 'time', lambda: (pm.stamp + 100) / 1000)
    limits = budget()
    for stamp, value in [(T, '100'), (T + 1000, '102'), (T + 2000, '96.9')]:
        pm.stamp, pm.equity = stamp, value
        result = await collect_account_risk(risk_db, pm, wallet, limits)
    assert result['status'] == 'SAFETY_REVIEW'
    assert Decimal(result['max_drawdown_ratio']) == Decimal('.05')
    assert result['known_breach'] is not None
    assert result['known_breach']['source'] == 'persisted:account_risk_verified_breach'
    limits['budget_id'] = 'renewed-policy-same-account'
    result = await collect_account_risk(risk_db, pm, wallet, limits)
    assert result['baseline']['equity_usd'] == '100'
    assert result['baseline']['budget_id'] == budget()['budget_id']
    assert result['status'] == 'SAFETY_REVIEW'
    async with risk_db.pool.acquire() as conn:
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_baseline') == 1
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_equity_point') == 3
        with pytest.raises(Exception, match='append.only'):
            await conn.execute("UPDATE trading.account_risk_equity_point SET payload='{}'::jsonb")
        with pytest.raises(Exception, match='append.only'):
            await conn.execute('DELETE FROM trading.account_risk_baseline')
    wallet.history = lambda p: {'rows': []}
    pm.stamp += 1000
    result = await collect_account_risk(risk_db, pm, wallet, limits)
    assert result['status'] == 'SAFETY_REVIEW'
    assert result['max_drawdown_ratio'] is None
    assert result['known_breach'] is not None
    assert Decimal(result['confirmed_drawdown_lower_bound_ratio']) == Decimal('.05')
    assert all(endpoint in ('/papi/v1/account', '/sapi/v1/asset/transfer')
               for endpoint, _ in pm.calls + wallet.calls)


@pytest.mark.asyncio
async def test_concurrent_first_samples_fix_one_baseline_and_do_not_overwrite_points(risk_db, monkeypatch):
    import ai_tuner.monitor.account_risk as mod
    pm, wallet = Exchange(), Exchange(wallet=True)
    monkeypatch.setattr(mod.time, 'time', lambda: (T + 100) / 1000)
    results = await asyncio.gather(*(collect_account_risk(risk_db, pm, wallet, budget()) for _ in range(3)))
    assert all(result['status'] == 'VERIFIED' for result in results)
    async with risk_db.pool.acquire() as conn:
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_baseline') == 1
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_equity_point') == 1
    pm.equity = '200'
    conflict = await collect_account_risk(risk_db, pm, wallet, budget())
    assert conflict['status'] == 'BLOCKED_DATA'
    assert 'equity_fact_conflict' in conflict['source_gaps']
    assert conflict['baseline']['equity_usd'] == '100'


@pytest.mark.asyncio
async def test_failed_initial_transfer_window_cannot_fix_or_poison_first_valid_baseline(risk_db, monkeypatch):
    import ai_tuner.monitor.account_risk as mod
    pm, wallet = Exchange(), Exchange(wallet=True)
    monkeypatch.setattr(mod.time, 'time', lambda: (pm.stamp + 100) / 1000)
    wallet.history = lambda p: {'rows': []} if p['type'] == TYPES[0] else {'total': 0}
    first = await collect_account_risk(risk_db, pm, wallet, budget())
    assert first['status'] == 'BLOCKED_DATA'
    assert first['baseline'] is None
    async with risk_db.pool.acquire() as conn:
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_baseline') == 0
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_equity_point') == 0
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_coverage') == 2
    wallet.history = lambda p: {'total': 0}
    pm.stamp += 1000
    second = await collect_account_risk(risk_db, pm, wallet, budget())
    assert second['status'] == 'VERIFIED'
    assert second['baseline']['exchange_time_ms'] == T + 1100
    assert second['activation_gap']['end_ms'] == T + 1100


@pytest.mark.asyncio
async def test_transfer_inside_initial_request_defers_baseline_and_keeps_raw_facts(risk_db, monkeypatch):
    import ai_tuner.monitor.account_risk as mod
    pm, wallet = Exchange(), Exchange(wallet=True)
    monkeypatch.setattr(mod.time, 'time', lambda: (pm.stamp + 100) / 1000)
    event = {'asset': 'USDT', 'amount': '50', 'type': TYPES[0], 'status': 'CONFIRMED',
             'tranId': 7, 'timestamp': T + 100}
    wallet.history = lambda p: {'total': 1, 'rows': [event]} if p['type'] == TYPES[0] and p['startTime'] <= T + 100 else {'total': 0}
    first = await collect_account_risk(risk_db, pm, wallet, budget())
    assert first['baseline'] is None
    async with risk_db.pool.acquire() as conn:
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_equity_point') == 0
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_transfer') == 1
    pm.stamp += 1000
    pm.equity = '150'
    second = await collect_account_risk(risk_db, pm, wallet, budget())
    assert second['status'] == 'VERIFIED'
    assert second['baseline']['exchange_time_ms'] == T + 1100
    assert second['evidence']['cashflows'] == []


@pytest.mark.asyncio
async def test_cashflow_missing_anchors_remains_unknown_even_after_zero_transfer_window(risk_db, monkeypatch):
    import ai_tuner.monitor.account_risk as mod
    pm, wallet = Exchange(), Exchange(wallet=True)
    monkeypatch.setattr(mod.time, 'time', lambda: (pm.stamp + 100) / 1000)
    await collect_account_risk(risk_db, pm, wallet, budget())
    event = {'asset': 'USDT', 'amount': '50', 'type': TYPES[0], 'status': 'CONFIRMED',
             'tranId': 7, 'timestamp': T + 500}
    wallet.history = lambda p: {'total': 1, 'rows': [event]} if p['type'] == TYPES[0] and p['startTime'] <= T + 500 else {'total': 0}
    pm.stamp, pm.equity = T + 1000, '150'
    first = await collect_account_risk(risk_db, pm, wallet, budget())
    pm.stamp += 1000
    second = await collect_account_risk(risk_db, pm, wallet, budget())
    assert first['status'] == second['status'] == 'BLOCKED_DATA'
    assert 'cashflow_event_equity_unverified:7' in second['source_gaps']
    assert len(second['evidence']['cashflows']) == 1


@pytest.mark.asyncio
async def test_pagination_missing_page_and_changing_total_never_mark_coverage_complete(risk_db, monkeypatch):
    import ai_tuner.monitor.account_risk as mod
    pm, wallet = Exchange(), Exchange(wallet=True)
    monkeypatch.setattr(mod.time, 'time', lambda: (pm.stamp + 100) / 1000)
    await collect_account_risk(risk_db, pm, wallet, budget())
    pm.stamp += 1000
    rows = [{'asset': 'USDT', 'amount': '1', 'type': TYPES[0], 'status': 'CONFIRMED',
             'tranId': i, 'timestamp': T + i + 200} for i in range(100)]
    wallet.history = lambda p: ({'total': 101, 'rows': rows} if p['current'] == 1
                                else {'total': 101, 'rows': []}) if p['type'] == TYPES[0] else {'total': 0}
    result = await collect_account_risk(risk_db, pm, wallet, budget())
    assert result['status'] == 'BLOCKED_DATA'
    assert any('transfer_page_incomplete' in gap for gap in result['source_gaps'])
    assert not any(c['complete'] for c in result['evidence']['coverage']
                   if c['transfer_type'] == TYPES[0] and c['end_ms'] > T + 100)
    wallet.history = lambda p: ({'total': 101, 'rows': rows} if p['current'] == 1
                                else {'total': 100, 'rows': []}) if p['type'] == TYPES[0] else {'total': 0}
    result = await collect_account_risk(risk_db, pm, wallet, budget())
    assert any('transfer_total_changed' in gap for gap in result['source_gaps'])


@pytest.mark.asyncio
async def test_transaction_rolls_back_baseline_and_point_together(risk_db, monkeypatch):
    import ai_tuner.monitor.account_risk as mod
    pm, wallet = Exchange(), Exchange(wallet=True)
    monkeypatch.setattr(mod.time, 'time', lambda: (pm.stamp + 100) / 1000)
    async with risk_db.pool.acquire() as conn:
        await conn.execute("""CREATE OR REPLACE FUNCTION trading.account_risk_test_fail()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected risk write failure'; END $$""")
        await conn.execute('CREATE TRIGGER fail_risk_point BEFORE INSERT ON trading.account_risk_equity_point '
                           'FOR EACH ROW EXECUTE FUNCTION trading.account_risk_test_fail()')
    result = await collect_account_risk(risk_db, pm, wallet, budget())
    assert result['status'] == 'BLOCKED_DATA'
    assert 'account_risk_storage_unavailable' in result['source_gaps']
    async with risk_db.pool.acquire() as conn:
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_baseline') == 0
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_equity_point') == 0


@pytest.mark.asyncio
async def test_failed_commit_cannot_publish_an_uncommitted_persisted_breach(risk_db, monkeypatch):
    import ai_tuner.monitor.account_risk as mod
    pm, wallet = Exchange(), Exchange(wallet=True)
    monkeypatch.setattr(mod.time, 'time', lambda: (pm.stamp + 100) / 1000)
    await collect_account_risk(risk_db, pm, wallet, budget())
    async with risk_db.pool.acquire() as conn:
        await conn.execute("""CREATE OR REPLACE FUNCTION trading.account_risk_test_commit_fail()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected commit failure'; END $$""")
        await conn.execute('CREATE CONSTRAINT TRIGGER risk_commit_fail AFTER INSERT ON trading.account_risk_breach '
                           'DEFERRABLE INITIALLY DEFERRED FOR EACH ROW '
                           'EXECUTE FUNCTION trading.account_risk_test_commit_fail()')
    pm.stamp += 1000
    pm.equity = '95'
    result = await collect_account_risk(risk_db, pm, wallet, budget())
    assert result['status'] == 'BLOCKED_DATA'
    assert 'account_risk_storage_unavailable' in result['source_gaps']
    assert result['known_breach'] is None
    async with risk_db.pool.acquire() as conn:
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_breach') == 0
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_equity_point') == 1


@pytest.mark.asyncio
async def test_old_exchange_update_time_is_a_fresh_signed_observation(risk_db, monkeypatch):
    import ai_tuner.monitor.account_risk as mod
    pm, wallet = Exchange(), Exchange(wallet=True)
    # updateTime is the last account change; observation time belongs to the GET.
    monkeypatch.setattr(mod.time, 'time', lambda: (T + 10_000_000) / 1000)
    result = await collect_account_risk(risk_db, pm, wallet, budget())
    assert result['status'] == 'VERIFIED'
    assert result['as_of_ms'] == T + 10_000_000
    assert result['evidence']['equity_points'][0]['exchange_update_time_ms'] == T
    assert result['current_equity_usd'] == '100'


@pytest.mark.asyncio
async def test_account_isolation_and_explicit_identity_binding(risk_db, monkeypatch):
    import ai_tuner.monitor.account_risk as mod
    pm, wallet = Exchange(), Exchange(wallet=True)
    monkeypatch.setattr(mod.time, 'time', lambda: (pm.stamp + 100) / 1000)
    await collect_account_risk(risk_db, pm, wallet, budget())
    other = {**budget(), 'account_id': 'grid', 'account_identity': 'grid-bound-credentials'}
    pm.equity = '800'
    grid = await collect_account_risk(risk_db, pm, wallet, other)
    assert grid['current_equity_usd'] == '800'
    pm.stamp += 1000
    pm.equity = '99'
    main = await collect_account_risk(risk_db, pm, wallet, budget())
    assert main['baseline']['equity_usd'] == '100'
    assert main['current_equity_usd'] == '99'
    assert main['status'] == 'VERIFIED'
    mismatched = await collect_account_risk(risk_db, pm, wallet, {**budget(), 'account_identity': 'wrong'})
    assert mismatched['status'] == 'BLOCKED_DATA'
    assert 'account_risk_baseline_identity_mismatch' in mismatched['source_gaps']


@pytest.mark.asyncio
async def test_first_baseline_and_coverage_loss_never_rebase_old_history(risk_db, monkeypatch):
    import ai_tuner.monitor.account_risk as mod
    pm, wallet = Exchange(), Exchange(wallet=True)
    monkeypatch.setattr(mod.time, 'time', lambda: (pm.stamp + 100) / 1000)
    await collect_account_risk(risk_db, pm, wallet, budget())
    # Simulate an operator restoring only part of the fact backup in isolated DB.
    async with risk_db.pool.acquire() as conn:
        await conn.execute('ALTER TABLE trading.account_risk_baseline DISABLE TRIGGER USER')
        await conn.execute('DELETE FROM trading.account_risk_baseline')
        await conn.execute('ALTER TABLE trading.account_risk_baseline ENABLE TRIGGER USER')
        await conn.execute('ALTER TABLE trading.account_risk_coverage DISABLE TRIGGER USER')
        await conn.execute('DELETE FROM trading.account_risk_coverage')
        await conn.execute('ALTER TABLE trading.account_risk_coverage ENABLE TRIGGER USER')
    pm.stamp += 1000
    pm.equity = '90'
    result = await collect_account_risk(risk_db, pm, wallet, budget())
    assert result['status'] == 'BLOCKED_DATA'
    assert 'baseline_missing_with_existing_history' in result['source_gaps']
    assert result['current_equity_usd'] is None
    async with risk_db.pool.acquire() as conn:
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_baseline') == 0


@pytest.mark.asyncio
async def test_external_pin_prevents_rebootstrap_after_total_fact_loss(risk_db, monkeypatch):
    import ai_tuner.monitor.account_risk as mod
    pm, wallet = Exchange(), Exchange(wallet=True)
    monkeypatch.setattr(mod.time, 'time', lambda: (T + 1000) / 1000)
    result = await collect_account_risk(risk_db, pm, wallet,
                                        {**budget(), 'expected_baseline_time_ms': T})
    assert result['status'] == 'BLOCKED_DATA'
    assert 'baseline_pin_missing' in result['source_gaps']
    assert not pm.calls and not wallet.calls
    async with risk_db.pool.acquire() as conn:
        assert await conn.fetchval('SELECT count(*) FROM trading.account_risk_baseline') == 0


@pytest.mark.asyncio
async def test_complete_two_page_transfer_history_is_recorded_and_duplicate_id_is_not_complete(risk_db, monkeypatch):
    import ai_tuner.monitor.account_risk as mod
    pm, wallet = Exchange(), Exchange(wallet=True)
    monkeypatch.setattr(mod.time, 'time', lambda: (pm.stamp + 100) / 1000)
    await collect_account_risk(risk_db, pm, wallet, budget())
    pm.stamp += 1000
    rows = [{'asset': 'USDT', 'amount': '1', 'type': TYPES[0], 'status': 'CONFIRMED',
             'tranId': i, 'timestamp': T + i + 200} for i in range(101)]
    wallet.history = lambda p: {'total': 101, 'rows': rows[(p['current'] - 1) * 100:p['current'] * 100]} if p['type'] == TYPES[0] else {'total': 0}
    rows[-1]['tranId'] = 0
    incomplete = await collect_account_risk(risk_db, pm, wallet, budget())
    assert incomplete['status'] == 'BLOCKED_DATA'
    assert any('transfer_identity_or_time_invalid' in gap for gap in incomplete['source_gaps'])
    rows[-1]['tranId'] = 100
    result = await collect_account_risk(risk_db, pm, wallet, budget())
    windows = [w for w in result['evidence']['coverage']
               if w['transfer_type'] == TYPES[0] and w['end_ms'] > T + 100 and w['complete']]
    assert len(windows) == 1 and windows[0]['complete'] is True
    assert len(windows[0]['raw_pages']) == 2
    assert len(windows[0]['event_ids']) == len(result['evidence']['cashflows']) == 101
    assert result['status'] == 'BLOCKED_DATA'  # event valuations remain absent
