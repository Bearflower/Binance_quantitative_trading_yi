import hashlib
import json
import os
from datetime import timedelta
from decimal import Decimal

import pytest

from strategies.hrs.acceptance_report import MODES, reconcile_lifecycles
from strategies.hrs.decision_capture import canonical_payload
from strategies.hrs.mode_report import ENTRY_SOURCES, SIGNALS, entry_sources, summarize
from .test_hrs_acceptance_report import ACCOUNT, NOW, START, lifecycle_sources


def all_closed(count=1, *, complete=True):
    fills, owners, incomes, economic = lifecycle_sources(count)
    economic['id_match_and_coverage_complete'] = complete
    return reconcile_lifecycles(fills + fills, owners, incomes, economic, account_id=ACCOUNT,
            inception=START-timedelta(seconds=60), through=NOW, anchor_certified=True,
            acceptance_samples=False)['samples']


def receipt(mode='lv_rm', stop='99'):
    client = 'risk_' + hashlib.sha256(f'{ACCOUNT}:hrs:100'.encode()).hexdigest()[:30]
    execution = dict(operation='execute_long', outcome=dict(order_filled=True,
                     order=dict(orderId=1, symbol='S0USDT', clientOrderId=client)))
    budget = dict(stop_price=stop, entry_mode=mode, decision=dict(allowed=True, client_order_id=client))
    return dict(symbol='S0USDT', signal_id=100, entry_mode=mode, direction='long',
                resolution='executed', triggered_at=START-timedelta(seconds=30),
                observed_at=START+timedelta(seconds=1), execution=execution,
                execution_hash=canonical_payload(execution)[1], client_order_id=client,
                budget=budget, budget_at=START-timedelta(seconds=1), budget_hash=canonical_payload(budget)[1])


def test_all_flat_to_flat_closes_and_costs_including_after_five():
    samples = all_closed(7)
    report = summarize([], samples, entry_sources([receipt()], ACCOUNT))
    assert len(samples) == 7
    assert set(report) == set(MODES) | {'unknown'}
    live = report['lv_rm']['live']
    assert live['observed_closed'] == live['verified_net_count'] == live['net_wins'] == 1
    assert live['net_usdt'] == live['expectancy_usdt'] == Decimal('9.7')
    assert live['known_r_count'] == 1 and live['expectancy_r'] == Decimal('.97')
    assert live['lifecycles'][0]['fill_count'] == 3
    assert report['unknown']['live']['verified_net_count'] == 6
    assert report['unknown']['live']['known_r_count'] == 0
    assert report['emm']['live']['win_rate'] is None


@pytest.mark.parametrize('issue', ['incomplete', 'inconsistent_gross', 'inconsistent_fee'])
def test_unknown_net_excluded_from_every_performance_denominator(issue):
    samples = all_closed(complete=issue != 'incomplete')
    if issue == 'inconsistent_gross':
        samples[0]['realized_pnl_usdt'] = '11'
    if issue == 'inconsistent_fee':
        samples[0]['commission_usdt'] = '0'
    live = summarize([], samples, entry_sources([receipt()], ACCOUNT))['lv_rm']['live']
    assert live['observed_closed'] == live['missing_net_count'] == 1
    assert live['net_usdt'] is live['expectancy_usdt'] is live['expectancy_r'] is None
    assert live['net_wins'] == live['known_r_count'] == 0


@pytest.mark.parametrize('issue', ['hash', 'wrong_account', 'wrong_client', 'shadow', 'ambiguous'])
def test_entry_attribution_requires_unique_account_order_signal_evidence(issue):
    row = receipt()
    account = ACCOUNT
    if issue == 'hash':
        row['execution_hash'] = 'invalid'
    elif issue == 'wrong_account':
        account = 'other'
    elif issue == 'wrong_client':
        row['client_order_id'] = 'wrong'
    elif issue == 'shadow':
        row['resolution'] = 'shadow'
    rows = [row, receipt(mode='emm')] if issue == 'ambiguous' else [row]
    live = summarize([], all_closed(), entry_sources(rows, account))['unknown']['live']
    assert live['verified_net_count'] == 1 and live['known_r_count'] == 0


@pytest.mark.parametrize('issue', ['invalid_budget_hash', 'missing_stop', 'stop_above', 'future_budget', 'missing_fill_price'])
def test_known_mode_and_net_survive_missing_risk(issue):
    row = receipt(stop='101' if issue == 'stop_above' else '99')
    samples = all_closed()
    if issue == 'invalid_budget_hash':
        row['budget_hash'] = ''
    elif issue == 'missing_stop':
        row['budget'] = None
    elif issue == 'future_budget':
        row['budget_at'] = NOW
    elif issue == 'missing_fill_price':
        samples[0]['fill_legs'][0]['price'] = None
    live = summarize([], samples, entry_sources([row], ACCOUNT))['lv_rm']['live']
    assert live['net_wins'] == 1 and live['known_r_count'] == 0 and live['expectancy_r'] is None


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv('HRS_TEST_POSTGRES_DSN'), reason='isolated PostgreSQL required')
async def test_signal_sql_historical_routes_cutoff_and_no_fill_inference():
    import asyncpg
    connection = await asyncpg.connect(os.environ['HRS_TEST_POSTGRES_DSN'])
    try:
        # Isolated temporary tables allow this exact SQL to run alongside other DB fixtures.
        await connection.execute("""
            CREATE TEMP TABLE hrs_signals (id bigint, symbol text, direction text, entry_mode text,
              signal_origin text, triggered_at timestamptz, resolved_at timestamptz, resolution text);
            CREATE TEMP TABLE hrs_decision_evidence (kind text, symbol text, run_id text, event_time timestamptz,
              available_at timestamptz, payload jsonb, payload_sha256 text);
            CREATE TEMP TABLE entry_risk_reservation (account_id text, strategy_id text, symbol text,
              client_order_id text, created_at timestamptz, geometry jsonb);""")
        for id_, mode, origin, resolution, when in [
                (10, 'emm', 'shadow_scoring', 'executed', START),
                (11, 'emm', 'shadow_scoring', 'shadow', START),
                (12, 'lv_rm', 'empty_pool', 'pool_circuit_breaker', START),
                (13, 'standard', None, None, None),
                (14, None, None, 'executed', NOW),
                (15, 'semi_emm', 'shadow_scoring', None, None)]:
            await connection.execute('INSERT INTO hrs_signals VALUES($1,$2,$3,$4,$5,$6,$7,$8)',
                id_, 'X', 'long', mode, origin, START, when, resolution)
        for kind, data in [('pool_circuit_preflight_denial', dict(signal_id=12, direction='long',
                reason='pool_circuit_breaker', entry_mode='lv_rm', is_shadow=True)),
                ('signal_execution_outcome', dict(result=False, signal=dict(signal_id=13,
                direction='long', entry_mode='standard', skip_reason='entry risk policy')))]:
            await connection.execute('INSERT INTO hrs_decision_evidence VALUES($1,$2,$3,$4,$4,$5,$6)',
                kind, 'X', 'run', START, json.dumps(data), canonical_payload(data)[1])
        rows = await connection.fetch(SIGNALS.replace('hrs.hrs_', 'hrs_').replace('trading.entry_', 'entry_'),
                                      START, NOW, ACCOUNT)
        modes = summarize(rows, [], {})
        assert modes['emm']['signals']['live']['outcomes'] == {'executed': 1}
        assert modes['emm']['signals']['shadow']['outcomes'] == {'shadow': 1}
        assert modes['lv_rm']['signals']['shadow']['outcomes'] == {'pool_circuit_breaker': 1}
        assert modes['standard']['signals']['live']['outcomes'] == {'execution_declined': 1}
        assert modes['unknown']['signals']['unknown']['outcomes'] == {'unresolved': 1}
        assert modes['semi_emm']['signals']['unknown']['outcomes'] == {'unresolved': 1}
        assert modes['emm']['live']['verified_net_count'] == 0
        # Exercise the two exact account/order/signal/receipt SQL joins as well.
        row = receipt()
        await connection.execute('INSERT INTO hrs_signals VALUES(100,$1,$2,$3,NULL,$4,$4,$5)',
            row['symbol'], row['direction'], row['entry_mode'], row['triggered_at'], row['resolution'])
        await connection.execute('INSERT INTO entry_risk_reservation VALUES($1,$2,$3,$4,$5,$6)',
            ACCOUNT, 'hrs', row['symbol'], row['client_order_id'], START, json.dumps({'signal_id':100}))
        for kind, data, at in [('order_execution_result', row['execution'], row['observed_at']),
                ('entry_budget_reserved', row['budget'], row['budget_at'])]:
            await connection.execute('INSERT INTO hrs_decision_evidence VALUES($1,$2,$3,$4,$4,$5,$6)',
                kind, row['symbol'], 'trade', at, json.dumps(data), canonical_payload(data)[1])
        query = ENTRY_SOURCES.replace('hrs.hrs_', 'hrs_').replace('trading.entry_', 'entry_')
        sources = entry_sources(await connection.fetch(query, ACCOUNT, NOW, ['1']), ACCOUNT)
        assert summarize([], all_closed(), sources)['lv_rm']['live']['known_r_count'] == 1
    finally:
        await connection.close()
