"""Real readonly SQL source closure; engineering fixtures are not trading evidence."""
from datetime import datetime, timedelta, timezone
import json
import os
import uuid

import pytest
import pytest_asyncio

from strategies.hrs.decision_capture import canonical_payload
from strategies.hrs.funding_reference import build_forecasts_ref

AT = datetime(2026, 11, 20, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def source_database():
    import asyncpg
    dsn = os.environ.get('HRS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('isolated PostgreSQL administrator DSN required')
    control = await asyncpg.connect(dsn)
    name = 'hrs_bridge_' + uuid.uuid4().hex
    await control.execute('CREATE DATABASE ' + name + " TEMPLATE template0 ENCODING 'UTF8'")
    db = await asyncpg.connect(dsn, database=name)
    try:
        await db.execute('''CREATE SCHEMA hrs; CREATE SCHEMA trading;
            CREATE TABLE hrs.hrs_decision_evidence(evidence_id text,run_id text,kind text,symbol text,event_time timestamptz,available_at timestamptz,payload_sha256 text,payload jsonb);
            CREATE TABLE trading.hrs_equity_reconciliation(account_id text,segment_id text,after_state_version bigint,as_of timestamptz,available_at timestamptz,receipt_sha256 text,receipt jsonb,after_state jsonb);
            CREATE TABLE trading.hrs_equity_evidence(account_id text,evidence_sha256 text,segment_id text,as_of timestamptz,available_at timestamptz,payload jsonb);
            CREATE TABLE trading.hrs_risk_equity_checkpoints(account_id text,segment_id text,as_of timestamptz,equity numeric,units numeric,nav numeric,created_at timestamptz);
            CREATE TABLE public.ai_tuner_funding_schedule_observations(symbol text,source_time timestamptz,received_at timestamptz,next_funding_time timestamptz,funding_interval_hours int,payload_hash text);
            CREATE TABLE trading.hrs_funding_observation(account_id text,evidence_sha256 text,available_at timestamptz,payload jsonb);''')
        yield db
    finally:
        await db.close()
        await control.execute('DROP DATABASE ' + name)
        await control.close()


@pytest.mark.parametrize('archive', [False, True])
@pytest.mark.parametrize('cash_age', [0, 30])
async def test_nearest_complete_bridge_keeps_original_availability_and_forecast_closure(source_database, tmp_path, archive, cash_age):
    from ai_tuner.hrs_evidence_export import read_evidence_sources, _ArchiveSources, verify_forecast_source_closure, _records
    from tests.test_ai_tuner_mvp.test_hrs_forecast_closure import observation
    db = source_database
    policy = dict(account_id='test', reason=json.dumps(dict(
        hrs_risk_budget_version='hrs_minimum_executable_v1', hrs_margin_scope='hrs_owned_quota_account_free_v1')))
    financial = dict(entry_policy=policy, replay_bootstrap_ready=True,
        account_cash_observed_at=(AT-timedelta(seconds=cash_age)).isoformat(),
        ledger_state=dict(ledger_state=dict(as_of=(AT-timedelta(seconds=30)).isoformat())))
    payload = dict(financial_context=financial, positions={})
    await db.execute('INSERT INTO hrs.hrs_decision_evidence VALUES($1,$2,$3,NULL,$4,$5,$6,$7::jsonb)',
        'scan', 'run', 'scan_start', AT, AT+timedelta(seconds=.5), canonical_payload(payload)[1], json.dumps(payload))
    forecast = observation(AT+timedelta(seconds=35))
    await db.execute('INSERT INTO trading.hrs_funding_observation VALUES($1,$2,$3,$4::jsonb)',
        'test', forecast['evidence_sha256'], AT+timedelta(seconds=35), json.dumps(forecast['payload']))
    ref, _ = build_forecasts_ref('test', AT+timedelta(seconds=40), [forecast], expected_count=1)
    parents = {}
    # The closer invalid rows cannot replace the earliest complete flat proof.
    for label, event, available, local, complete in [
        ('nonflat', 10, 20, {'A': {}}, True), ('incomplete', 20, 30, {}, False),
        ('nearest', 70, 100, {}, True), ('late', 90, 121, {}, True), ('later', 80, 110, {}, True)]:
        p = dict(account_id='test', as_of=(AT+timedelta(seconds=event)).isoformat(),
            available_at=(AT+timedelta(seconds=available)).isoformat(),
            capture=dict(local=local), source_collection=dict(complete=complete, gaps=[]),
            income_report=dict(id_match_and_coverage_complete=True, coverage_gaps=[]))
        if label == 'nearest':
            p['capture']['funding_publication'] = dict(forecasts_ref=ref)
        digest = canonical_payload(p)[1]
        parents[label] = dict(payload=p, evidence_sha256=digest)
        await db.execute('INSERT INTO trading.hrs_equity_evidence VALUES($1,$2,$3,$4,$5,$6::jsonb)',
            'test', digest, 'segment', AT+timedelta(seconds=event), AT+timedelta(seconds=available), json.dumps(p))
    await db.execute('SET default_transaction_read_only=on')
    sources = await read_evidence_sources(db, 'test', window_start=AT, as_of=AT+timedelta(hours=3),
        archive_root=tmp_path/'archive' if archive else None)
    expected = 1 if cash_age == 0 else 0
    if archive:
        with _ArchiveSources(sources) as view:
            rows = list(view.archive.iter_records('equity_evidence'))
            assert view.archive.source_metadata['equity_evidence']['expected_count'] == expected
            assert view.forecast_closure['source_count'] == expected
    else:
        rows = sources['equity_evidence']
        assert len(rows) == expected
        assert verify_forecast_source_closure(sources, _records(sources['decision_evidence']))['source_count'] == expected
    assert len(rows) == expected
    if not expected:
        return
    assert rows[0]['evidence_sha256'] == parents['nearest']['evidence_sha256']
    retained = rows[0]['payload']
    retained = json.loads(retained) if isinstance(retained, str) else retained
    assert retained == parents['nearest']['payload']
    assert rows[0]['available_at'] == (AT+timedelta(seconds=100)).isoformat()


@pytest.mark.parametrize('change', ['fill', 'income', 'allocation'])
def test_retained_bridge_parent_does_not_bless_changed_economics(change):
    from ai_tuner.hrs_evidence_export import _equity, _flat_unchanged_proof
    from ai_tuner.replay.models import DataBlocked
    previous = AT-timedelta(seconds=30)
    changed_at = AT-timedelta(seconds=10)
    parents = []
    for at in (previous, AT+timedelta(seconds=10)):
        p = dict(account_id='test', segment_id='segment', as_of=at.isoformat(), available_at=at.isoformat(),
            capture=dict(local={}, inventory=[], independent=[]), fills=[], order_owners=[],
            source_collection=dict(complete=True),
            income_report=dict(id_match_and_coverage_complete=True, coverage_gaps=[], events=[]),
            allocation_audit=dict(journal=[]))
        if at > AT:
            if change == 'fill':
                for trade, side in ((1, 'BUY'), (2, 'SELL')):
                    p['fills'].append(dict(account_id='test', symbol='A', trade_id=trade, order_id=trade,
                        qty='1', price='100', side=side, trade_time_utc=changed_at.isoformat()))
                    p['order_owners'].append(dict(account_id='test', symbol='A', order_id=trade, strategy='hrs'))
            elif change == 'income':
                p['income_report']['events'].append(dict(strategy='hrs', event_time_utc=changed_at.isoformat()))
            else:
                p['allocation_audit']['journal'].append(dict(statement_at=changed_at.isoformat()))
        parents.append(dict(payload=p, evidence_sha256=canonical_payload(p)[1]))
    with pytest.raises(DataBlocked, match=dict(fill='bridge_hrs_fills', income='bridge_hrs_income',
                                              allocation='allocation_changes_unsettled')[change]):
        _flat_unchanged_proof(_equity(parents), previous, AT, [])
