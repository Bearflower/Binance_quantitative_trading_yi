"""Capital is known only over an audited interval; never infer old allocations."""
from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

from ai_tuner.allocation.capital_evidence import project_capital_evidence, read_capital_evidence


def ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


START = ms('2026-11-01T00:00:00+08:00')
END = ms('2026-11-02T00:00:00+08:00')
MID = (START + END) // 2


def allocation(amount='20', month='2026-11', **overrides):
    row = dict(id=1, month=month, status='active', created_at='2026-08-31T12:00:00',
               entries=[dict(strategy_id='hrs', allocated_amount=amount,
                             open_margin=9999, entry_ceiling=1)])
    row.update(overrides)
    return row


def event(event_id, observed_ms, rows=None, operation='UPDATE'):
    return dict(id=event_id, account_id='default',
                observed_at=datetime.fromtimestamp(observed_ms / 1000, timezone.utc),
                operation=operation, allocations=rows if rows is not None else [allocation()])


def project(events, start=START, end=END, strategies=('hrs',), account='default', **kwargs):
    return project_capital_evidence(events, account, start, end, strategies, **kwargs)


def test_time_weighted_allocated_capital_is_30_for_half_windows():
    result = project([event(1, START - 1, operation='BASELINE'),
                      event(2, MID, [allocation('40')])])['hrs']
    assert result['average_capital_usdt'] == Decimal('30')
    assert result['coverage_complete'] is True
    assert result['reasons'] == []
    assert result['account_id'] == 'default'
    assert (result['start_ms'], result['end_ms']) == (START, END)
    assert '1,2' in result['source_id']


def test_source_identity_tracks_event_contents_and_window():
    first = project([event(1, START)])['hrs']['source_id']
    assert first != project([event(1, START, [allocation('40')])])['hrs']['source_id']
    assert first != project([event(1, START)], end=END - 1)['hrs']['source_id']


def test_future_allocation_waits_for_beijing_month_boundary():
    start = ms('2026-11-30T12:00:00+08:00')
    end = ms('2026-12-01T12:00:00+08:00')
    rows = [allocation('20'), allocation('40', '2026-12')]
    assert project([event(1, start - 1, rows)], start, end)['hrs']['average_capital_usdt'] == Decimal('30')


def test_future_month_does_not_override_latest_active_past_month():
    rows = [allocation('10', '2026-09'), allocation('20', '2026-10'),
            allocation('99', '2026-12'), allocation('500', '2026-11', status='draft')]
    assert project([event(1, START, rows)])['hrs']['average_capital_usdt'] == Decimal('20')


def test_baseline_time_does_not_backdate_old_created_at():
    start = ms('2026-08-01T00:00:00+08:00')
    end = ms('2026-09-01T00:00:00+08:00')
    result = project([event(1, end - 1000, [allocation(month='2026-08')], 'BASELINE')], start, end)['hrs']
    assert result['average_capital_usdt'] is None
    assert result['coverage_complete'] is False
    assert 'missing_start_coverage' in result['reasons']


@pytest.mark.parametrize('amount', ['0', '-1', 'NaN', 'Infinity', None, True, 'invalid'])
def test_invalid_capital_is_unknown_not_zero(amount):
    result = project([event(1, START, [allocation(amount)])])['hrs']
    assert result['average_capital_usdt'] is None
    assert not result['coverage_complete']
    assert 'invalid_allocated_amount' in result['reasons']


@pytest.mark.parametrize('rows,reason', [
    ([], 'no_active_allocation'),
    ([allocation('20'), allocation('40')], 'duplicate_active_month'),
    ([allocation(entries=[])], 'missing_strategy'),
])
def test_absent_deleted_or_conflicting_allocations_invalidate_whole_window(rows, reason):
    result = project([event(1, START), event(2, MID, rows, 'DELETE')])['hrs']
    assert result['average_capital_usdt'] is None
    assert not result['coverage_complete']
    assert reason in result['reasons']


def test_missing_strategy_does_not_invalidate_an_independent_present_strategy():
    results = project([event(1, START)], strategies=('hrs', 'btc_eth'))
    assert results['hrs']['coverage_complete']
    assert results['btc_eth']['average_capital_usdt'] is None
    assert results['btc_eth']['reasons'] == ['missing_strategy']


def test_same_timestamp_events_follow_id_and_end_is_exclusive():
    result = project([event(3, END, []), event(2, START, [allocation('40')]), event(1, START)])['hrs']
    assert result['average_capital_usdt'] == Decimal('40')
    assert result['coverage_complete']


def test_json_encoded_snapshots_and_entries_are_accepted():
    row = allocation()
    row['entries'] = json.dumps(row['entries'])
    observation = event(1, START, [row])
    observation['allocations'] = json.dumps(observation['allocations'])
    assert project([observation])['hrs']['average_capital_usdt'] == Decimal('20')


@pytest.mark.parametrize('account,events,kwargs,reason', [
    ('grid', [], {}, 'unsupported_account'),
    ('default', [], {}, 'missing_start_coverage'),
    ('default', [event(1, START)], {'audit_healthy': False}, 'capital_audit_unhealthy'),
    ('default', [dict(event(1, START), account_id='grid')], {}, 'capital_account_mismatch'),
    ('default', [dict(event(1, START), observed_at=datetime(2026, 11, 1))], {}, 'invalid_history_event'),
])
def test_untrusted_evidence_fails_closed(account, events, kwargs, reason):
    result = project(events, account=account, **kwargs)['hrs']
    assert result['average_capital_usdt'] is None
    assert result['coverage_complete'] is False
    assert reason in result['reasons']


class Database:
    def __init__(self, healthy=True, fail=False):
        self.healthy, self.fail, self.calls = healthy, fail, []

    async def fetch_all(self, query, *args):
        self.calls.append((query, args))
        if self.fail:
            raise RuntimeError('database unavailable')
        return [dict(event(1, START), audit_healthy=self.healthy)]


@pytest.mark.asyncio
async def test_reader_verifies_audit_and_history_in_one_database_snapshot():
    db = Database()
    result = (await read_capital_evidence(db, 'default', START, END, ['hrs']))['hrs']
    assert result['average_capital_usdt'] == Decimal('20')
    assert len(db.calls) == 1
    assert 'pg_catalog.pg_trigger' in db.calls[0][0]
    assert 'capital_allocation_history' in db.calls[0][0]
    assert db.calls[0][1] == ('default', START, END)


@pytest.mark.asyncio
@pytest.mark.parametrize('healthy,fail,reason', [
    (False, False, 'capital_audit_unhealthy'), (True, True, 'capital_history_unavailable')])
async def test_reader_rejects_disabled_trigger_or_database_failure(healthy, fail, reason):
    result = (await read_capital_evidence(Database(healthy, fail), 'default', START, END, ['hrs']))['hrs']
    assert result['average_capital_usdt'] is None
    assert reason in result['reasons']


def test_sub_millisecond_baseline_after_window_start_is_not_rounded_into_coverage():
    observation = event(1, START)
    observation['observed_at'] = observation['observed_at'].replace(microsecond=1)
    result = project([observation])['hrs']
    assert not result['coverage_complete']
    assert 'missing_start_coverage' in result['reasons']


def test_non_mapping_history_event_is_rejected_without_exception():
    result = project([None])['hrs']
    assert not result['coverage_complete']
    assert result['average_capital_usdt'] is None
    assert result['reasons'] == ['invalid_history_event']


def test_duplicate_strategy_entry_is_not_added_twice():
    rows = [allocation()]
    rows[0]['entries'].append(dict(strategy_id='hrs', allocated_amount='30'))
    result = project([event(1, START, rows)])['hrs']
    assert not result['coverage_complete']
    assert result['reasons'] == ['duplicate_strategy_allocation']
