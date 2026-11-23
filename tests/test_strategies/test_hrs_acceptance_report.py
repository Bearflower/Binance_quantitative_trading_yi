"""Acceptance is evidence quality, never invented trading performance."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
import pytest_asyncio

from strategies.hrs.acceptance_report import (
    build_report, reconcile_lifecycles, summarize_evidence, write_report_atomic,
    _apply_verified_funding,
    _compact_record,
)
from strategies.hrs.decision_capture import canonical_payload

UTC = timezone.utc
NOW = datetime(2026, 11, 18, 0, 30, tzinfo=UTC)
START = NOW.replace(minute=0) - timedelta(hours=24)
ACCOUNT = 'acceptance-test'


@pytest_asyncio.fixture
async def initialized_report_schema():
    """Install this reader's dependencies without relying on other test modules."""
    import asyncpg
    connection = await asyncpg.connect(os.environ['HRS_TEST_POSTGRES_DSN'])
    root = Path(__file__).resolve().parents[2]
    try:
        await connection.execute('CREATE SCHEMA IF NOT EXISTS trading; CREATE SCHEMA IF NOT EXISTS hrs')
        # Match the base source-table columns used by the equity-service fixture.
        # The facts themselves use their real initialization source, not a mock.
        await connection.execute('''CREATE TABLE IF NOT EXISTS trading.trade_records
            (id bigserial PRIMARY KEY,account_id text,symbol text,order_id text,strategy text,
             side text,status text,order_type text,quantity numeric,realized_pnl numeric,
             executed_at timestamp);
            CREATE TABLE IF NOT EXISTS trading.income_ingestion_attempt
            (attempt_id text PRIMARY KEY,account_id text,observations jsonb,state text DEFAULT 'pending',
             created_at timestamptz DEFAULT now(),finished_at timestamptz);
            CREATE TABLE IF NOT EXISTS public.capital_allocation
            (id serial PRIMARY KEY,month text,entries jsonb,status text,created_at timestamp,total_capital numeric);
            CREATE TABLE IF NOT EXISTS public.condition_orders (id bigserial PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS hrs.hrs_positions (id bigserial PRIMARY KEY)''')
        for name in ('11-execution-facts.sql', '16-reviewed-income-attribution.sql'):
            source = (root/'database/postgres/init-scripts'/name).read_text()
            # Isolated clusters need not contain the deployment-only app role.
            source = '\n'.join(line for line in source.splitlines()
                               if not line.strip().startswith('GRANT '))
            await connection.execute(source)
        for name in ('2026-11-17-hrs-risk-ledger-loss-gates.sql',
                     '2026-11-17-hrs-equity-service.sql',
                     '2026-11-17-hrs-allocation-event-audit.sql',
                     '2026-11-17-hrs-equity-reconciliation.sql',
                     '2026-11-17-hrs-funding-publication.sql',
                     '2026-11-17-hrs-decision-evidence.sql',
                     '2026-11-17-hrs-frozen-exit-plan.sql',
                     '2026-11-17-hrs-condition-account-scope.sql'):
            await connection.execute((root/'database/postgres/migrations'/name).read_text())
        yield
    finally:
        await connection.close()


def event(run, kind, at, payload, symbol=None):
    encoded, digest = canonical_payload(payload)
    return dict(evidence_id=uuid.uuid4().hex, run_id=run, kind=kind, symbol=symbol,
                event_time=at, available_at=at, payload=json.loads(encoded),
                payload_sha256=digest)


def evidence():
    rows = []
    for hour in range(24):
        at = START + timedelta(hours=hour, minutes=5)
        run = str(hour)
        snapshot = {'financial_context': {'entry_policy': {'account_id': ACCOUNT}}}
        batch = dict(requested_symbols=['BTCUSDT', 'ETHUSDT'],
                     coverage_scope='requested_scan_batch', captured_at=at,
                     universe_audit=dict(raw_count=3, eligible_count=2,
                         counts={'crypto': 2, 'inapplicable_tradfi': 1},
                         rows={'BTCUSDT': 'crypto', 'ETHUSDT': 'crypto', 'XAUUSDT': 'inapplicable_tradfi'},
                         observed_at=at), quotes={})
        for symbol in batch['requested_symbols']:
            batch['quotes'][symbol] = dict(value=100, source='coingecko', provider='coingecko',
                provider_asset_id=symbol, as_of=at.timestamp()-30,
                available_at=at.timestamp(), is_proxy=False, missing_reason=None)
        rows += [event(run, 'scan_start', at, snapshot), event(run, 'market_cap_batch', at, batch),
                 event(run, 'standard_scan_scope', at, {'symbols': ['BTCUSDT']}),
                 event(run, 'analysis', at, {'result': {'short': {'should_entry': False,
                    'score_result': {'entry_mode': 'standard', 'trend_filter_passed': False}}}}, 'BTCUSDT'),
                 event(run, 'standard_candidate_outcome', at, {'reason': 'scored'}, 'BTCUSDT'),
                 event(run, 'lv_rm_scan_scope', at, {'symbols': ['ETHUSDT']}),
                 event(run, 'lv_rm_candidate_outcome', at, {'reason': 'insufficient_klines'}, 'ETHUSDT'),
                 event(run, 'scan_end', at+timedelta(seconds=2), {'status': 'complete', 'state': {}})]
    return rows


def changed(rows, kind, change, run='0'):
    row = next(r for r in rows if r['kind'] == kind and r['run_id'] == run)
    change(row['payload'])
    encoded, row['payload_sha256'] = canonical_payload(row['payload'])
    row['payload'] = json.loads(encoded)


def technical(rows=None, **kwargs):
    return summarize_evidence(evidence() if rows is None else rows, as_of=NOW,
                              account_id=ACCOUNT, **kwargs)


def test_complete_consecutive_closed_hours_keep_all_denominators():
    report = technical()
    assert report['status'] == 'PASS'
    assert report['consecutive_complete_hours'] == 24
    slot = report['hourly_slots'][0]
    assert slot['start_cst'] == '2026-11-17T08:00:00+08:00'
    assert slot['market_cap_batches'][0]['raw_universe_count'] == 3
    assert slot['market_cap_batches'][0]['requested_count'] == 2
    assert slot['market_cap_batches'][0]['fresh_coverage'] == 1.0
    assert slot['market_cap_batches'][0]['full_ticker_cap_coverage'] is None
    assert slot['modes']['standard']['trend_denials'] == 1
    assert slot['modes']['lv_rm']['data_failures'] == 1
    assert slot['modes']['emm']['eligible'] == 0


def test_background_equity_and_candidate_refresh_are_not_phantom_scans():
    rows = evidence()
    for hour in range(24):
        at = START + timedelta(hours=hour, minutes=10)
        rows.append(event('startup-collector', 'equity_gate', at,
                          {'ready': False, 'allowed': False, 'reasons': ['daily_origin_missing']}))
        batch = next(r['payload'] for r in rows
                     if r['kind'] == 'market_cap_batch' and r['run_id'] == str(hour))
        rows.append(event('startup-candidate-timer', 'market_cap_batch', at, batch))
    report = technical(rows)
    assert report['status'] == 'PASS'
    assert report['consecutive_complete_hours'] == 24
    assert all(slot['scan_count'] == 1 for slot in report['hourly_slots'])
    # Heartbeats and background refreshes cannot manufacture a trading scan.
    only_background = [r for r in rows if r['run_id'].startswith('startup-')]
    assert technical(only_background)['status'] == 'NOT_READY'
    assert all(slot['scan_count'] == 0 for slot in technical(only_background)['hourly_slots'])


@pytest.mark.parametrize('mutation,reason', [
    (lambda row: row['payload'].update(ready='tampered'), 'payload_hash_mismatch'),
    (lambda row: row.update(available_at=row['event_time']-timedelta(seconds=1)), 'noncausal_evidence'),
])
def test_background_filter_preserves_integrity_failures(mutation, reason):
    row = event('startup-collector', 'equity_gate', START+timedelta(minutes=10), {'ready': True})
    mutation(row)
    report = technical(evidence()+[row])
    assert report['status'] == 'NOT_READY'
    assert reason in report['hourly_slots'][0]['reasons']


def test_partial_scan_and_context_failure_still_invalidate_an_hour():
    at = START + timedelta(minutes=15)
    for kind, payload in [('scan_end', {'status': 'failed'}),
                          ('standard_scan_scope', {'symbols': ['BTCUSDT']}),
                          ('scan_context_failure', {'error_type': 'TimeoutError'})]:
        rows = evidence() + [event('broken-trading-scan', kind, at, payload)]
        report = technical(rows)
        assert report['status'] == 'NOT_READY'
        assert 'scan_start_unproven' in report['hourly_slots'][0]['reasons']


@pytest.mark.parametrize('mutation,reason', [
    (lambda p: p['quotes'].pop('ETHUSDT'), 'quote_inventory_incomplete'),
    (lambda p: p['quotes']['ETHUSDT'].update(as_of=START.timestamp()-7200), 'fresh_coverage_below_95_percent'),
    (lambda p: p['quotes']['ETHUSDT'].update(is_proxy=True), 'fresh_coverage_below_95_percent'),
    (lambda p: p['quotes']['ETHUSDT'].update(provider_asset_id=None), 'fresh_coverage_below_95_percent'),
    (lambda p: p['universe_audit'].update(raw_count=2), 'raw_universe_unproven'),
])
def test_missing_or_stale_provenance_cannot_pass(mutation, reason):
    rows = evidence()
    changed(rows, 'market_cap_batch', mutation)
    report = technical(rows)
    assert report['status'] == 'NOT_READY'
    assert reason in report['hourly_slots'][0]['reasons']
    if reason == 'quote_inventory_incomplete':
        assert report['hourly_slots'][0]['market_cap_batches'][0]['fresh_coverage'] is None


def test_exact_95_percent_retains_explicit_missing_denominator():
    rows = evidence()
    def update(p):
        good = p['quotes']['BTCUSDT']
        p['requested_symbols'] = [f'S{i}USDT' for i in range(20)]
        p['quotes'] = {s: copy.deepcopy(good) for s in p['requested_symbols']}
        p['quotes']['S19USDT'] = dict(value=None, source='missing', is_proxy=False,
                                    missing_reason='id_unmapped')
        p['universe_audit'] = dict(raw_count=20, eligible_count=20, counts={'crypto': 20},
            rows={s: 'crypto' for s in p['requested_symbols']}, observed_at=START.timestamp())
    changed(rows, 'market_cap_batch', update)
    report = technical(rows)
    assert report['status'] == 'PASS'
    assert report['hourly_slots'][0]['market_cap_batches'][0]['fresh_coverage'] == .95


def test_actual_epoch_metadata_timestamp_and_crypto_scope_are_checked():
    rows = evidence()
    changed(rows, 'market_cap_batch', lambda p: p['universe_audit'].update(observed_at=START.timestamp()))
    assert technical(rows)['status'] == 'PASS'
    changed(rows, 'market_cap_batch', lambda p: p['universe_audit']['rows'].update(ETHUSDT='metadata_unverified'))
    changed(rows, 'market_cap_batch', lambda p: p['universe_audit'].update(
        eligible_count=1, counts={'crypto': 1, 'inapplicable_tradfi': 1, 'metadata_unverified': 1}))
    assert technical(rows)['status'] == 'NOT_READY'


def test_scored_terminal_without_actual_analysis_is_not_a_complete_funnel():
    rows = [r for r in evidence() if not (r['run_id'] == '0' and r['kind'] == 'analysis')]
    assert technical(rows)['status'] == 'NOT_READY'


def test_malformed_payload_returns_not_ready_instead_of_breaking_cycle_finally():
    rows = evidence()
    changed(rows, 'market_cap_batch', lambda p: p.update(universe_audit=['malformed']))
    assert technical(rows)['status'] == 'NOT_READY'


def test_metadata_failure_preserves_known_raw_denominator_without_qualifying_it():
    rows = evidence()
    changed(rows, 'market_cap_batch', lambda p: p.update(universe_audit=dict(raw_count=915,
        eligible_count=0, counts={'metadata_unverified': 915}, rows={}, observed_at=START.timestamp())))
    batch = technical(rows)['hourly_slots'][0]['market_cap_batches'][0]
    assert batch['raw_universe_count'] == 915
    assert batch['raw_universe_inventory_proven'] is False
    assert technical(rows)['status'] == 'NOT_READY'


def test_raw_bars_are_hash_verified_before_compacting_without_changing_source():
    at = START+timedelta(minutes=5)
    row = event('0', 'analysis', at, {'result': {'short': {'should_entry': False}},
        'klines_1h': [{'close': 100}]*100, 'klines_4h': [{'close': 100}]*100}, 'BTCUSDT')
    compact = _compact_record(row)
    assert 'klines_1h' not in compact['payload']
    assert compact['source_payload_sha256'] == row['payload_sha256']
    assert canonical_payload(compact['payload'])[1] == compact['payload_sha256']
    assert len(row['payload']['klines_1h']) == 100
    row['payload']['klines_1h'][0]['close'] = 999
    assert _compact_record(row)['payload_sha256'] != canonical_payload(compact['payload'])[1]



@pytest.mark.parametrize('kind', ['scan_start', 'scan_end'])
def test_scan_snapshot_compaction_verifies_discarded_history_and_preserves_consumers(kind):
    rows = evidence()
    for row in rows:
        if row['kind'] == kind:
            bulky = {'equity_source_archives': [{'payload': {'history': ['x' * 1024] * 100}}]}
            if kind == 'scan_start':
                row['payload']['financial_context'].update(bulky)
                row['payload']['candidate_scan'] = bulky
            else:
                row['payload']['state'] = bulky
            row['payload_sha256'] = canonical_payload(row['payload'])[1]
    compact = [_compact_record(row) for row in rows]
    assert technical(compact) == technical(rows)
    selected = next(row for row in compact if row['kind'] == kind)
    assert len(json.dumps(selected['payload'])) < 1024
    assert selected['source_payload_sha256'] == next(row for row in rows if row['kind'] == kind)['payload_sha256']
    source = next(row for row in rows if row['kind'] == kind)
    container = source['payload']['financial_context'] if kind == 'scan_start' else source['payload']['state']
    container['equity_source_archives'][0]['payload']['history'][0] = 'tampered'
    assert technical([_compact_record(row) for row in rows])['status'] == 'NOT_READY'


def test_compact_scan_start_preserves_full_policy_for_engineering_checks():
    captures = engineering_evidence(5)
    baseline = financial(5, decision_evidence=captures)
    for row in captures:
        if row['kind'] == 'scan_start':
            row['payload']['financial_context']['equity_source_archives'] = [{'unused': 'x' * 10000}]
            row['payload_sha256'] = canonical_payload(row['payload'])[1]
    assert financial(5, decision_evidence=[_compact_record(row) for row in captures]) == baseline


class EvidenceCursorConnection:
    def __init__(self, rows):
        self.rows, self.prefetches, self.queries, self.fetches = rows, [], [], 0

    def cursor(self, sql, *args, prefetch):
        self.prefetches.append(prefetch)
        self.queries.append(sql)
        async def values():
            for row in self.rows:
                yield row
        return values()

    async def fetch(self, sql, *args):
        self.fetches += 1
        return self.rows


@pytest.mark.asyncio
async def test_evidence_cursor_uses_single_raw_row_and_rejects_oversize_before_decode(monkeypatch):
    from strategies.hrs import acceptance_report as reporter
    row = event('0', 'scan_start', START, {'financial_context': {'entry_policy': {'account_id': ACCOUNT}}})
    row.update(payload=None, source_payload_bytes=129)
    connection = EvidenceCursorConnection([row])
    monkeypatch.setattr(reporter, 'MAX_SOURCE_PAYLOAD_BYTES', 128, raising=False)
    with pytest.raises(ValueError, match='report_source_payload_budget_exceeded'):
        await reporter._read_evidence(connection, 'SELECT source')
    assert connection.prefetches == [1]
    assert 'octet_length' in connection.queries[0] and 'CASE WHEN' in connection.queries[0]


@pytest.mark.asyncio
async def test_evidence_retained_budget_overflow_is_fail_closed(monkeypatch):
    from strategies.hrs import acceptance_report as reporter
    connection = EvidenceCursorConnection(evidence())
    monkeypatch.setattr(reporter, 'MAX_RETAINED_EVIDENCE_BYTES', 1, raising=False)
    with pytest.raises(ValueError, match='report_retained_evidence_budget_exceeded'):
        await reporter._read_evidence(connection, 'SELECT source')


@pytest.mark.asyncio
async def test_budget_parent_sources_use_bounded_cursor_without_compacting_hash_bound_payload():
    from strategies.hrs import acceptance_report as reporter
    from strategies.hrs.risk_budget import FLOOR
    client, digest = 'client', 'a' * 64
    captures = [event('0', 'order_execution_result', START, {'outcome': {'order': {'orderId': 1, 'clientOrderId': client}}}, 'S0USDT'),
                event('0', 'entry_budget_reserved', START, {'decision': {'client_order_id': client, 'budget_version': FLOOR}}, 'S0USDT'),
                event('0', 'entry_stage_inputs', START, {'stage': 'reservation_success', 'client_order_id': client, 'budget_context': {'evidence_sha256': digest}}, 'S0USDT')]
    parent = {'payload': json.dumps({'capture': {'local': {}}, 'history': ['original']}), 'evidence_sha256': digest}
    connection = EvidenceCursorConnection([parent])
    rows = await reporter._read_budget_sources(connection, {'samples': [{'symbol': 'S0USDT', 'order_ids': ['1']}]}, captures, ACCOUNT, NOW)
    assert rows == [parent]
    assert connection.fetches == 0
    assert connection.prefetches == [1]
    assert 'octet_length' in connection.queries[0] and 'CASE WHEN' in connection.queries[0]


@pytest.mark.asyncio
async def test_many_large_scan_sources_are_compacted_with_bounded_peak_memory():
    import tracemalloc
    from strategies.hrs import acceptance_report as reporter
    body = 'x' * 1_700_000
    def sources():
        for index in range(40):
            row = event(str(index), 'scan_start', START, {
                'financial_context': {'entry_policy': {'account_id': ACCOUNT},
                                      'equity_source_archives': [{'history': body}]}})
            row['payload'] = json.dumps(row['payload'])
            row['source_payload_bytes'] = len(row['payload'].encode('utf-8'))
            yield row
    connection = EvidenceCursorConnection(sources())
    tracemalloc.start()
    try:
        rows = await reporter._read_evidence(connection, 'SELECT source')
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(rows) == 40
    assert sum(len(json.dumps(row['payload'])) for row in rows) < 4096
    assert peak < 32 * 1024 * 1024


@pytest.mark.asyncio
async def test_oversized_report_source_returns_explicit_pending_without_coherent_snapshot(monkeypatch):
    from contextlib import asynccontextmanager
    from strategies.hrs import acceptance_report as reporter
    row = event('0', 'scan_start', START, {})
    row.update(payload=None, source_payload_bytes=129)
    connection = EvidenceCursorConnection([row])
    @asynccontextmanager
    async def transaction(**kwargs):
        assert kwargs == {'isolation': 'repeatable_read', 'readonly': True}
        yield
    @asynccontextmanager
    async def acquire():
        yield connection
    async def integrity(*args):
        return 0
    connection.transaction = transaction
    connection.fetchval = integrity
    monkeypatch.setattr(reporter, 'MAX_SOURCE_PAYLOAD_BYTES', 128)
    report = await build_report(SimpleNamespace(pool=SimpleNamespace(acquire=acquire)), as_of=NOW, account_id=ACCOUNT)
    assert report['source_error_type'] == 'ReportEvidenceLimitExceeded'
    assert report['database_snapshot_coherent'] is False
    assert report['status'] == 'PENDING'
    assert report['technical']['status'] == 'NOT_READY'
    assert report['financial']['status'] == 'PENDING'
    assert report['performance_accepted'] is False


def test_entry_stage_receipt_preserves_all_original_payload_fields():
    row = event('0', 'entry_stage_inputs', START, {'stage': 'reservation_inventory',
        'slot_inputs': {'local': {}}, 'klines_1h': ['unexpected but hash-bound']})
    compact = _compact_record(row)
    assert compact['payload'] == row['payload']
    assert compact['payload_sha256'] == row['payload_sha256']


@pytest.mark.parametrize('kind', ['kline_repair', 'execution_batch'])
def test_unused_operational_payload_is_hash_verified_then_compacted_without_changing_summary(kind):
    rows = evidence()
    source = event('0', kind, START+timedelta(minutes=5), {
        'nested': {'history': ['x' * 1024] * 2048}, 'status': 'actual_source'})
    rows.append(source)
    baseline = technical(rows)
    assert baseline['status'] == 'PASS'
    compact = _compact_record(source)
    assert compact['payload'] == {}
    assert compact['source_payload_sha256'] == source['payload_sha256']
    assert compact['payload_sha256'] == canonical_payload({})[1]
    assert compact['report_payload_bytes'] == 2
    assert {k: compact[k] for k in ('evidence_id', 'run_id', 'kind', 'symbol', 'event_time', 'available_at')} == {
        k: source[k] for k in ('evidence_id', 'run_id', 'kind', 'symbol', 'event_time', 'available_at')}
    assert technical([_compact_record(row) for row in rows]) == baseline
    assert len(source['payload']['nested']['history']) == 2048
    source['payload']['nested']['history'][0] = 'tampered'
    changed = technical([_compact_record(row) for row in rows])
    assert changed['status'] == 'NOT_READY'
    assert 'payload_hash_mismatch' in changed['hourly_slots'][0]['reasons']

def test_explicit_other_account_runs_are_excluded_without_mixing_counters():
    rows = evidence()
    at = START+timedelta(minutes=5)
    rows += [event('other', 'scan_start', at, {'financial_context': {'entry_policy': {'account_id': 'other'}}}),
             event('other', 'scan_end', at, {'status': 'incomplete'})]
    assert technical(rows)['status'] == 'PASS'


def test_current_hour_does_not_fill_a_missing_closed_hour():
    rows = [r for r in evidence() if r['run_id'] != '0']
    rows += [event('current', 'scan_start', NOW, {}),
             event('current', 'scan_end', NOW, {'status': 'complete'})]
    assert technical(rows)['consecutive_complete_hours'] == 23
    assert technical(rows)['status'] == 'NOT_READY'


@pytest.mark.parametrize('kind', ['scan_end', 'standard_candidate_outcome', 'lv_rm_candidate_outcome'])
def test_missing_terminal_evidence_fails_closed(kind):
    rows = [r for r in evidence() if not (r['run_id'] == '0' and r['kind'] == kind)]
    assert technical(rows)['status'] == 'NOT_READY'


def test_account_mismatch_incomplete_scan_and_tampering_fail_closed():
    rows = evidence()
    changed(rows, 'scan_start', lambda p: p['financial_context']['entry_policy'].update(account_id='other'))
    assert technical(rows)['status'] == 'NOT_READY'
    rows = evidence()
    changed(rows, 'scan_end', lambda p: p.update(status='incomplete'))
    assert technical(rows)['status'] == 'NOT_READY'
    rows = evidence()
    rows[0]['payload']['tampered'] = True
    assert technical(rows)['status'] == 'NOT_READY'


def test_budget_denial_order_response_and_exchange_fill_are_distinct():
    rows = evidence()
    at = START+timedelta(minutes=5)
    signal = dict(symbol='BTCUSDT', direction='short', entry_mode='emm')
    rows += [event('0', 'signal_pre_execution', at, signal, 'BTCUSDT'),
        event('0', 'signal_execution_outcome', at, dict(result=False,
            signal={**signal, '_audit_stage': 'entry_risk_budget', '_audit_denial': 'below_min_qty'}), 'BTCUSDT'),
        event('0', 'order_execution_result', at, dict(operation='execute_short', outcome={
            'order_filled': True, 'filled_quantity': 3, 'order': {'orderId': 10}}), 'BTCUSDT')]
    report = technical(rows)
    mode = report['hourly_slots'][0]['modes']['emm']
    assert mode['budget_denials'] == 1
    assert mode['submitted_orders'] == 1
    assert mode['exchange_fills'] == 0
    fill = dict(account_id=ACCOUNT, symbol='BTCUSDT', trade_id=50, order_id=10,
                trade_time_utc=at)
    mode = technical(rows, fills=[fill, fill])['hourly_slots'][0]['modes']['emm']
    assert mode['exchange_fills'] == 1


def lifecycle_sources(count=1):
    fills, owners, incomes, events = [], [], [], []
    for n in range(count):
        symbol = f'S{n}USDT'
        # partial TP legs must remain one actual lifecycle
        for leg, (side, qty, pnl) in enumerate([('BUY', '10', '0'), ('SELL', '4', '4'), ('SELL', '6', '6')]):
            trade_id = n*10+leg+1
            at = START+timedelta(minutes=n*10+leg)
            fills.append(dict(account_id=ACCOUNT, symbol=symbol, trade_id=trade_id,
                order_id=n*10+1 if leg == 0 else n*10+2, side=side, qty=Decimal(qty), realized_pnl=Decimal(pnl),
                price=Decimal('100')+leg,
                commission=Decimal('.1'), commission_asset='USDT', trade_time_utc=at))
            owners.append(dict(account_id=ACCOUNT, symbol=symbol, order_id=str(trade_id), strategy='hrs'))
            for kind, amount in [('COMMISSION', '-.1'), ('REALIZED_PNL', pnl)]:
                if Decimal(amount) == 0:
                    continue
                income = dict(account_id=ACCOUNT, symbol=symbol, trade_id=trade_id, tran_id=len(incomes)+1,
                    income_type=kind, income=Decimal(amount), asset='USDT', event_time_utc=at)
                incomes.append(income)
                events.append(dict(account_id=ACCOUNT, tran_id=str(income['tran_id']), income_type=kind,
                    symbol=symbol, asset='USDT', amount=amount, strategy='hrs', reason=None,
                    event_time_utc=at.isoformat()))
    economic = dict(id_match_and_coverage_complete=True, events=events, report_id='proof',
                    coverage_gaps=[], missing_income_fills=[], unattributed=[])
    return fills, owners, incomes, economic


def financial(count=1, **kwargs):
    fills, owners, incomes, economic = lifecycle_sources(count)
    return reconcile_lifecycles(fills, owners, incomes, economic, account_id=ACCOUNT,
        inception=START-timedelta(seconds=1), through=NOW, anchor_certified=True, **kwargs)


def test_five_natural_lifecycles_use_income_and_keep_partial_closes_together():
    report = financial(5)
    assert report['status'] == 'READY'
    assert report['observed_closed_lifecycles'] == 5
    assert len(report['samples']) == 5
    assert report['samples'][0]['net_income_usdt'] == '9.7'
    assert report['samples'][0]['commission_usdt'] == '-0.3'
    assert report['samples'][0]['funding_usdt'] == '0'
    assert report['samples'][0]['fill_count'] == 3
    assert report['samples'][0]['fill_legs'][1]['quantity'] == '4'
    assert report['samples'][0]['engineering_status'] == 'PENDING'
    assert report['samples'][0]['engineering_checks']['budget']['status'] == 'NOT_CAPTURED'
    assert report['engineering_status'] == 'PENDING'
    assert report['performance_accepted'] is False


def test_absent_closures_are_pending_not_zero_profit():
    report = financial(0)
    assert report['status'] == 'PENDING'
    assert len(report['samples']) == 5
    assert all(s['net_income_usdt'] is None for s in report['samples'])


def engineering_evidence(count=1, *, lineage=True):
    rows = []
    for n in range(count):
        symbol, order = f'S{n}USDT', n*10+1
        at = START+timedelta(minutes=n*10)
        client = f'risk_{n}'
        decision = dict(allowed=True, quantity='10', worst_loss='12', risk_limit='20',
            policy_revision=1, client_order_id=client, effective_allocation='2000',
            risk_allocation='2000', exit_ratios=['1', '0'])
        rows += [event(str(n), 'scan_start', at-timedelta(seconds=3), {
            'financial_context': {'entry_policy': {'account_id': ACCOUNT}}}),
            event(str(n), 'entry_budget_reserved', at-timedelta(seconds=2), dict(
            decision=decision, entry_price='100', stop_price='99',
            take_profit_prices=['101', '102'], account_equity='3000', occupied_margin='0', entry_mode='standard'), symbol),
            event(str(n), 'order_execution_result', at+timedelta(seconds=1), dict(
                outcome={'order': {'symbol': symbol, 'orderId': order, 'clientOrderId': client}}), symbol)]
        for role, qty, price in [('sl', '10', '99'), ('tp1', '10', '101')]:
            request = dict(symbol=symbol, side='SELL', stop_price=price, quantity=qty,
                close_position=False, reduce_only=True, client_id=client+'_'+role)
            response = dict(symbol=symbol, side='SELL', quantity=qty, triggerPrice=price,
                reduceOnly=True, clientAlgoId=request['client_id'], algoId=f'{order}-{role}')
            if lineage and role == 'tp1':
                response['actualOrderId'] = order+1
            rows.append(event(str(n), 'protection_order_ack', at+timedelta(seconds=2),
                dict(position_id=f'hrs:{symbol}:{order}', role=role, request=request,
                     response=response, algo_id=response['algoId']), symbol))
    return rows


def test_real_acknowledgments_and_reserved_client_identity_close_engineering_checks():
    result = financial(5, decision_evidence=engineering_evidence(5))
    assert result['engineering_status'] == 'READY'
    assert result['samples'][0]['engineering_checks']['budget']['status'] == 'RECONCILED'
    assert result['samples'][0]['engineering_checks']['protection_quantity_and_price']['status'] == 'RECONCILED'
    assert result['samples'][0]['engineering_checks']['exit_role_reconciliation']['status'] == 'RECONCILED'


def test_unlinked_exit_fills_do_not_prove_tp_execution_from_algo_id_alone():
    result = financial(5, decision_evidence=engineering_evidence(5, lineage=False))
    assert result['engineering_status'] == 'PENDING'


@pytest.mark.parametrize('limit,expected', [('.5231','RECONCILED'), ('.5231001','PENDING')])
def test_budget_one_percent_uses_explicit_risk_allocation_not_reduced_margin(limit,expected):
    rows = engineering_evidence(5)
    changed(rows, 'entry_budget_reserved', lambda p: p['decision'].update(
        risk_allocation='52.31', effective_allocation='26.155', risk_limit=limit, worst_loss='.5'))
    result = financial(5, decision_evidence=rows)
    check = result['samples'][0]['engineering_checks']['budget']
    assert check['status'] == expected
    if expected == 'RECONCILED':
        assert check['risk_allocation'] == '52.31'
        assert check['effective_allocation'] == '26.155'


def test_missing_original_risk_allocation_source_remains_pending():
    rows = engineering_evidence(5)
    changed(rows, 'entry_budget_reserved', lambda p: p['decision'].pop('risk_allocation'))
    result = financial(5, decision_evidence=rows)
    check = result['samples'][0]['engineering_checks']['budget']
    assert check['status'] == 'PENDING'
    assert check['reason'] == 'risk_allocation_not_captured'


@pytest.mark.parametrize('problem', ['missing_exchange_fields', 'wrong_client', 'oversized', 'request_price_only', 'hash', 'wrong_account'])
def test_budget_and_protection_requests_are_not_exchange_acknowledgment_proof(problem):
    rows = engineering_evidence(5)
    ack = next(r for r in rows if r['kind'] == 'protection_order_ack')
    if problem == 'missing_exchange_fields':
        changed(rows, 'protection_order_ack', lambda p: p.update(response={'algoId': '1'}))
    elif problem == 'request_price_only':
        changed(rows, 'protection_order_ack', lambda p: p['response'].pop('triggerPrice'))
    elif problem == 'wrong_client':
        changed(rows, 'order_execution_result', lambda p: p['outcome']['order'].update(clientOrderId='other'))
    elif problem == 'oversized':
        changed(rows, 'entry_budget_reserved', lambda p: p['decision'].update(quantity='1'))
    elif problem == 'wrong_account':
        changed(rows, 'scan_start', lambda p: p['financial_context']['entry_policy'].update(account_id='other'))
    else:
        ack['payload']['response']['quantity'] = '999'
    result = financial(5, decision_evidence=rows)
    assert result['engineering_status'] == 'PENDING'
    assert result['samples'][0]['engineering_status'] == 'PENDING'


@pytest.mark.parametrize('problem', [None, 'tampered', 'late_fill','missing_forecast_source','valid_forecast_reference'])
def test_funding_uses_same_verified_inventory_interval_and_rejects_changed_facts(problem):
    from strategies.hrs.equity_evidence import canonical
    fills, owners, incomes, economic = lifecycle_sources()
    at = START+timedelta(seconds=30)
    funding = dict(account_id=ACCOUNT, tran_id=999, income_type='FUNDING_FEE', symbol='S0USDT',
                   asset='USDT', income=Decimal('-.5'), trade_id=None, event_time_utc=at)
    incomes.append(funding)
    income_event = dict(account_id=ACCOUNT, tran_id='999', income_type='FUNDING_FEE', symbol='S0USDT',
        asset='USDT', amount='-.5', strategy=None, reason='funding_owner_unproven', event_time_utc=at.isoformat())
    economic.update(id_match_and_coverage_complete=False, account_coverage_complete=True,
                    unattributed=[income_event])
    economic['events'].append(income_event)
    payload = dict(account_id=ACCOUNT, segment_id='segment', as_of=NOW,
        capture=dict(captured_at=NOW, inventory=[], independent=[], local={}, marks={}),
        fills=copy.deepcopy(fills), result={'ledger_version': 1}, source_collection={'gaps': []})
    reference_args={}
    if problem in ('missing_forecast_source','valid_forecast_reference'):
        from strategies.hrs.funding_reference import build_forecasts_ref
        source_payload=dict(forecasts=[dict(symbol='S0USDT',source_time=START,received_at=START,
            next_funding_time=NOW+timedelta(hours=1),source='authenticated_premium_index')])
        encoded,source_digest=canonical(source_payload)
        sources=[dict(account_id=ACCOUNT,evidence_sha256=source_digest,available_at=START,payload=json.loads(encoded))]
        ref,_=build_forecasts_ref(ACCOUNT,NOW,sources,expected_count=1)
        payload['available_at']=NOW
        payload['capture']['funding_publication']=dict(forecasts_ref=ref)
        if problem=='valid_forecast_reference':
            reference_args=dict(funding_source_rows=sources,funding_source_count=1)
    archive = dict(payload=json.loads(canonical(payload)[0]), evidence_sha256=canonical(payload)[1])
    if problem == 'tampered':
        archive['payload']['capture']['inventory'] = [{'symbol': 'fake'}]
    elif problem == 'late_fill':
        fills[0]['qty'] += Decimal(1)
    attributed = _apply_verified_funding(economic, archive, account_id=ACCOUNT,
        inception=START-timedelta(seconds=1), through=NOW, fills=fills, orders=owners,
        opening_inventory=(),**reference_args)
    assert attributed['id_match_and_coverage_complete'] is (problem in (None,'valid_forecast_reference'))
    if problem in (None,'valid_forecast_reference'):
        result = reconcile_lifecycles(fills, owners, incomes, attributed, account_id=ACCOUNT,
            inception=START-timedelta(seconds=1), through=NOW, anchor_certified=True)
        assert result['samples'][0]['net_income_usdt'] == '9.2'
        assert result['samples'][0]['funding_usdt'] == '-0.5'


def test_missing_fee_facts_and_non_usdt_fees_cannot_qualify_as_zero():
    for commission, asset in [(None, 'USDT'), (Decimal('.1'), 'BNB')]:
        fills, owners, incomes, economic = lifecycle_sources(5)
        fills[0].update(commission=commission, commission_asset=asset)
        result = reconcile_lifecycles(fills, owners, incomes, economic, account_id=ACCOUNT,
            inception=START-timedelta(seconds=1), through=NOW, anchor_certified=True)
        assert result['status'] == 'PENDING'
        assert result['samples'][0]['net_income_usdt'] is None


@pytest.mark.parametrize('problem', ['coverage', 'owner', 'reversal', 'income', 'anchor'])
def test_uncertain_financial_facts_never_turn_into_profit(problem):
    fills, owners, incomes, economic = lifecycle_sources(5)
    certified = True
    if problem == 'coverage':
        economic['id_match_and_coverage_complete'] = False
    elif problem == 'owner':
        owners.append({**owners[0], 'strategy': 'mtpc'})
    elif problem == 'reversal':
        fills[1]['qty'] = Decimal('14')
    elif problem == 'income':
        incomes.pop(0)
    else:
        certified = False
    result = reconcile_lifecycles(fills, owners, incomes, economic, account_id=ACCOUNT,
        inception=START-timedelta(seconds=1), through=NOW, anchor_certified=certified)
    assert result['status'] == 'PENDING'
    assert all(s['net_income_usdt'] is None for s in result['samples'])


def test_writer_is_atomic_and_does_not_leave_partial_json(tmp_path):
    path = tmp_path/'acceptance.json'
    write_report_atomic({'status': 'PENDING', 'amount': None}, path)
    assert json.loads(path.read_text())['amount'] is None
    with pytest.raises(ValueError):
        write_report_atomic({'invalid': float('nan')}, path)
    assert json.loads(path.read_text())['status'] == 'PENDING'
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.asyncio
async def test_missing_pool_or_account_is_structured_not_ready(monkeypatch):
    monkeypatch.delenv('TRADE_ACCOUNT_ID', raising=False)
    report = await build_report(SimpleNamespace(pool=None), as_of=NOW)
    assert report['technical']['status'] == 'NOT_READY'
    assert report['financial']['status'] == 'PENDING'
    assert report['performance_accepted'] is False


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv('HRS_TEST_POSTGRES_DSN'), reason='isolated PostgreSQL required')
async def test_real_postgres_reader_is_read_only_and_absent_samples_pending(initialized_report_schema):
    import asyncpg
    pool = await asyncpg.create_pool(os.environ['HRS_TEST_POSTGRES_DSN'], min_size=1, max_size=1)
    try:
        report = await build_report(SimpleNamespace(pool=pool), as_of=NOW,
                                    account_id='acceptance-nonexistent-'+uuid.uuid4().hex)
        assert report['database_snapshot_coherent'] is True
        assert report['technical']['status'] == 'NOT_READY'
        assert report['financial']['status'] == 'PENDING'
        assert all(s['net_income_usdt'] is None for s in report['financial']['samples'])
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv('HRS_TEST_POSTGRES_DSN'), reason='isolated PostgreSQL required')
async def test_acceptance_snapshot_does_not_block_income_ingestion(initialized_report_schema, monkeypatch):
    import asyncpg
    from shared.accounting.income_integrity import income_account_lock, require_income_integrity
    from strategies.hrs import acceptance_report as reporter

    account = 'acceptance-concurrent-' + uuid.uuid4().hex
    opened = asyncio.Event()
    resume = asyncio.Event()
    read_evidence = reporter._read_evidence

    async def pause_snapshot(connection, sql, *args):
        assert await connection.fetchval('SHOW transaction_isolation') == 'repeatable read'
        assert await connection.fetchval('SHOW transaction_read_only') == 'on'
        opened.set()
        await resume.wait()
        await require_income_integrity(connection, account)
        return await read_evidence(connection, sql, *args)

    monkeypatch.setattr(reporter, '_read_evidence', pause_snapshot)
    dsn = os.environ['HRS_TEST_POSTGRES_DSN']
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    writer = await asyncpg.connect(dsn)
    running = asyncio.create_task(build_report(SimpleNamespace(pool=pool), as_of=NOW, account_id=account))
    try:
        await asyncio.wait_for(opened.wait(), timeout=5)
        # Bound the writer only in the test so a regression cannot hang it.
        await writer.execute("SET lock_timeout = '100ms'")
        async with income_account_lock(writer, account):
            await writer.execute('INSERT INTO trading.income_ingestion_attempt(attempt_id,account_id) VALUES($1,$2)',
                                 account, account)
            resume.set()
            prior = await asyncio.wait_for(running, timeout=5)
            assert prior['database_snapshot_coherent'] is True
            # The earlier snapshot stays coherent. A fresh read sees the durable
            # pending row even while the writer still owns its session lock.
            blocked = await asyncio.wait_for(
                build_report(SimpleNamespace(pool=pool), as_of=NOW, account_id=account), timeout=5,
            )
            assert blocked['database_snapshot_coherent'] is False
            assert blocked['source_error_type'] == 'IncomeIntegrityBlocked'
    finally:
        resume.set()
        await asyncio.gather(running, return_exceptions=True)
        try:
            await writer.execute('DELETE FROM trading.income_ingestion_attempt WHERE attempt_id=$1', account)
        finally:
            await writer.close()
            await pool.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv('HRS_TEST_POSTGRES_DSN'), reason='isolated PostgreSQL required')
async def test_real_postgres_full_financial_contract_and_integrity_latch(initialized_report_schema):
    import asyncpg
    account = 'acceptance-positive-'+uuid.uuid4().hex
    pool = await asyncpg.create_pool(os.environ['HRS_TEST_POSTGRES_DSN'], min_size=1, max_size=1)
    fills, owners, incomes, _ = lifecycle_sources(5)
    captures = engineering_evidence(5)
    for n in range(5):
        changed(captures, 'scan_start', lambda p: p['financial_context']['entry_policy'].update(account_id=account), run=str(n))
    inception = START-timedelta(seconds=10)
    attempt = uuid.uuid4().hex
    for row in [*fills, *owners, *incomes]:
        row['account_id'] = account
    try:
        async with pool.acquire() as c:
            async with c.transaction():
                await c.execute('''INSERT INTO trading.hrs_equity_segment
                    (account_id,segment_id,inception,opening_cash,opening_allocation_sha256,opening_evidence)
                    VALUES($1,$1,$2,1000,$3,$4::jsonb)''', account, inception, '0'*64,
                    json.dumps({'account_flat_anchor_certified': True, 'opening_inventory': []}))
                await c.execute('''INSERT INTO trading.hrs_loss_gate_state
                    (account_id,segment_id,version,ledger_state,gate_state) VALUES($1,$1,1,$2::jsonb,'{}')''',
                    account, json.dumps({'as_of': NOW.isoformat()}))
                for f in fills:
                    await c.execute('''INSERT INTO trading.execution_fact_fill
                        (account_id,symbol,trade_id,order_id,side,qty,price,commission,commission_asset,realized_pnl,trade_time_utc)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)''',
                        *(f[k] for k in ('account_id','symbol','trade_id','order_id','side','qty','price','commission','commission_asset','realized_pnl','trade_time_utc')))
                for i in incomes:
                    await c.execute('''INSERT INTO trading.execution_fact_income
                        (account_id,symbol,trade_id,tran_id,income_type,income,asset,event_time_utc)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8)''',
                        *(i[k] for k in ('account_id','symbol','trade_id','tran_id','income_type','income','asset','event_time_utc')))
                for owner in owners:
                    await c.execute('INSERT INTO trading.trade_records(account_id,symbol,order_id,strategy) VALUES($1,$2,$3,$4)',
                                    *(owner[k] for k in ('account_id','symbol','order_id','strategy')))
                for source, kind, symbol in ([('um/income', k, None) for k in ('COMMISSION','REALIZED_PNL','FUNDING_FEE')]
                                            +[('userTrades', None, f'S{n}USDT') for n in range(5)]):
                    await c.execute('''INSERT INTO trading.execution_fact_cursor
                        (source,account_id,income_type,symbol,window_start_ms,covered_until_ms,page_complete,request_status)
                        VALUES($1,$2,$3,$4,$5,$6,true,'ok')''', source, account, kind, symbol,
                        int(inception.timestamp()*1000), int(NOW.timestamp()*1000)+1)
                for row in captures:
                    await c.execute('''INSERT INTO hrs.hrs_decision_evidence
                        (evidence_id,run_id,kind,symbol,event_time,available_at,payload_sha256,payload)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)''', row['evidence_id'], account,
                        row['kind'], row['symbol'], row['event_time'], row['available_at'], row['payload_sha256'], json.dumps(row['payload']))
            await c.execute('SET default_transaction_read_only=on')
        report = await build_report(SimpleNamespace(pool=pool), account_id=account, as_of=NOW)
        assert report['database_snapshot_coherent'] is True, report.get('source_error_type')
        assert report['financial']['status'] == 'READY'
        assert report['financial']['engineering_status'] == 'READY'
        assert Decimal(report['financial']['samples'][0]['net_income_usdt']) == Decimal('9.7')
        async with pool.acquire() as c:
            await c.execute('SET default_transaction_read_only=off')
            await c.execute("INSERT INTO trading.income_ingestion_attempt(attempt_id,account_id,state) VALUES($1,$2,'pending')", attempt, account)
        blocked = await build_report(SimpleNamespace(pool=pool), account_id=account, as_of=NOW)
        assert blocked['financial']['status'] == 'PENDING'
        assert blocked['technical']['status'] == 'NOT_READY'
        assert all(s['net_income_usdt'] is None for s in blocked['financial']['samples'])
    finally:
        async with pool.acquire() as c:
            await c.execute('SET default_transaction_read_only=off')
            # Keep the UUID-account segment: production makes this proof immutable.
            for table in ('hrs_loss_gate_state','execution_fact_fill','execution_fact_income',
                          'execution_fact_cursor','trade_records','income_ingestion_attempt'):
                await c.execute(f'DELETE FROM trading.{table} WHERE account_id=$1', account)
            await c.execute('DELETE FROM hrs.hrs_decision_evidence WHERE evidence_id=ANY($1::text[])', [r['evidence_id'] for r in captures])
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interval, cycles", [(0, 1), (900, 2)])
async def test_main_logs_each_successful_report(monkeypatch, tmp_path, capsys, interval, cycles):
    import asyncpg
    from unittest.mock import AsyncMock

    from strategies.hrs import acceptance_report as reporter

    output = tmp_path / "report.json"
    pool = SimpleNamespace(close=AsyncMock())
    monkeypatch.setenv("DATABASE_URL", "unused")
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(reporter, "build_report", AsyncMock(return_value={
        "status": "PENDING",
        "private_detail": "not-for-stdout",
    }))

    sleeps = 0

    async def sleep(seconds):
        nonlocal sleeps
        assert seconds == interval
        sleeps += 1
        if sleeps == cycles:
            raise reporter.asyncio.CancelledError

    monkeypatch.setattr(reporter.asyncio, "sleep", sleep)
    args = SimpleNamespace(output=output, interval=interval)
    if interval:
        with pytest.raises(reporter.asyncio.CancelledError):
            await reporter._main(args)
    else:
        await reporter._main(args)

    assert json.loads(output.read_text())["status"] == "PENDING"
    assert capsys.readouterr().out.splitlines() == [
        "event=hrs_acceptance_report_written status=PENDING",
    ] * cycles
    pool.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_does_not_log_success_on_write_failure(monkeypatch, tmp_path, capsys):
    import asyncpg
    from unittest.mock import AsyncMock

    from strategies.hrs import acceptance_report as reporter

    pool = SimpleNamespace(close=AsyncMock())
    monkeypatch.setenv("DATABASE_URL", "unused")
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(reporter, "build_report", AsyncMock(return_value={"status": "PENDING"}))

    with pytest.raises(OSError):
        await reporter._main(SimpleNamespace(output=tmp_path, interval=0))

    assert capsys.readouterr().out == ""
    pool.close.assert_awaited_once()


def test_full_funding_inventory_heartbeats_do_not_create_spurious_scan_failures():
    rows=evidence()
    rows.append(event('funding-inventory-only','funding_schedule_inventory',START+timedelta(minutes=3),
                      dict(account_id=ACCOUNT,source='prospective_exchange_forecast')))
    report=summarize_evidence(rows,as_of=NOW,account_id=ACCOUNT)
    baseline=summarize_evidence(evidence(),as_of=NOW,account_id=ACCOUNT)
    assert report==baseline
