"""Regressions for the independent HRS safety review on 2026-11-17."""
from contextlib import asynccontextmanager
from decimal import Decimal as D
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from shared.entry_risk_policy import EntryDenied, RiskPolicyStore, executable_exit_ratios
from strategies.hrs.decision_capture import canonical_payload
from strategies.hrs.executor import TradingExecutor
from strategies.hrs.position_exit_plan import PARAMETERS, validate_exit_plan
from strategies.hrs.position_manager import PositionManager
from strategies.hrs.strategy import HRSStrategy


def frozen_plan():
    return dict(version=1, reference_price=100, atr=2, params=dict(
        atr_multiplier=3, emergency_percent=.1, min_absolute_percent=.01,
        target1_atr_multiplier=2, target1_close_percent=.3,
        target2_atr_multiplier=3, target2_close_percent=.4,
        trailing_atr_multiplier=.5, max_holding_hours=5))


@pytest.mark.parametrize('field', ['reference_price', 'atr', *sorted(PARAMETERS)])
def test_positive_decimal_underflow_cannot_silently_change_frozen_geometry(field):
    plan = frozen_plan()
    target = plan if field in ('reference_price', 'atr') else plan['params']
    target[field] = '1e-1000'
    with pytest.raises(ValueError, match='invalid_hrs_exit_plan'):
        validate_exit_plan(plan)


@pytest.mark.parametrize('first', ['.5', '1'])
def test_explicit_zero_second_target_remains_a_supported_frozen_plan(first):
    plan = frozen_plan()
    plan['params'].update(target1_close_percent=first, target2_close_percent='0')
    validated = validate_exit_plan(plan)
    assert validated['params']['target1_close_percent'] == float(first)
    assert validated['params']['target2_close_percent'] == 0


@pytest.mark.parametrize('ratios', [('.5', '0'), ('1', '0'), ('.3', '.4'), ('.4', '.6')])
def test_legal_restored_ratios_survive_actual_executor_validation(ratios):
    strategy = object.__new__(HRSStrategy)
    strategy.position_manager = PositionManager({}, AsyncMock())
    strategy._apply_position_row(dict(symbol='TESTUSDT', direction='long', entry_price=1000,
        quantity=.02, opened_at=1794918147895, entry_mode='standard', exit_ratios=json.dumps(ratios)))
    actual = strategy.position_manager.get_position('TESTUSDT')['exit_ratios']
    assert [D(value) for value in actual] == [D(value) for value in ratios]
    params = TradingExecutor._params_for_exit_ratios({}, actual)
    assert params['target1_close_percent'] == D(ratios[0])
    assert params['target2_close_percent'] == D(ratios[1])


def test_actual_small_order_two_leg_fallback_can_be_restored_and_replenished():
    ratios = executable_exit_ratios(D('.02'), D('.01'), D('.01'), D('5'),
                                   [D('1000')]*3, [D('.3'), D('.4')])
    assert ratios == (D('.5'), D('0'))
    params = TradingExecutor._params_for_exit_ratios({}, ratios)
    assert params['target1_close_percent'] == D('.5')
    assert params['target2_close_percent'] == 0


@pytest.mark.parametrize('ratios', [('0', '0'), ('-.1', '.4'), ('.5', '-.1'),
                                   ('.7', '.4'), ('NaN', '0'), ('.5', 'Infinity')])
def test_invalid_exit_ratios_remain_rejected(ratios):
    with pytest.raises(ValueError, match='HRS exit geometry invalid'):
        TradingExecutor._params_for_exit_ratios({}, ratios)


@pytest.mark.parametrize('key', ['app_secret', 'secret_key', 'APP-SECRET', 'access token',
    'access_token', 'apiKey', 'API-KEY', 'binance_api_secret', 'private_key',
    'X-CMC-PRO_API_KEY', 'feishu-webhook-url', 'service_password'])
def test_capture_redacts_known_credential_names_without_opaque_yaml(key):
    marker = 'synthetic-sensitive-value'
    payload, _ = canonical_payload({'parsed_document': {key: marker}})
    assert marker not in payload
    assert json.loads(payload)['parsed_document'][key] == '[REDACTED]'


def test_redaction_preserves_public_provider_identity_and_audit_fields():
    public = dict(provider='coingecko', provider_asset_id='bitcoin', source='cache_hit',
                  payload_sha256='f'*64, policy_revision=6, client_order_id='risk_test')
    payload, _ = canonical_payload(public)
    assert json.loads(payload) == public


@pytest.mark.asyncio
@pytest.mark.parametrize('state,owner,strategy,other_uncertain,released', [
    ('reserved', 'audit', 'hrs', False, True),
    ('submitting', 'audit', 'hrs', False, False),
    ('reserved', 'another-account', 'hrs', False, False),
    ('reserved', 'audit', 'mtpc', False, False),
    ('reserved', 'audit', 'hrs', True, True),
])
async def test_real_pg_revision_changed_before_submission_commits_only_proven_unsent_release(
        state, owner, strategy, other_uncertain, released):
    import asyncpg
    dsn = os.getenv('HRS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('isolated PostgreSQL DSN required')
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    schema = 'hrs_unsent_review_'+uuid.uuid4().hex
    class Connection:
        def __init__(self, connection): self.connection = connection
        def transaction(self): return self.connection.transaction()
        async def execute(self, sql, *args):
            return await self.connection.execute(sql.replace('trading.', schema+'.'), *args)
        async def fetchrow(self, sql, *args):
            return await self.connection.fetchrow(sql.replace('trading.', schema+'.'), *args)
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            async with pool.acquire() as connection:
                yield Connection(connection)
    try:
        async with pool.acquire() as connection:
            await connection.execute(f'''CREATE SCHEMA {schema};
                CREATE TABLE {schema}.entry_risk_reservation(client_order_id text PRIMARY KEY,
                    account_id text,strategy_id text,state text,symbol text,revision bigint,
                    updated_at timestamptz,geometry jsonb DEFAULT '{{}}');
                CREATE TABLE {schema}.position_claims(symbol text,strategy text,claim_state text,
                    released_at timestamptz,reason text);''')
            await connection.execute(f'''INSERT INTO {schema}.entry_risk_reservation
                (client_order_id,account_id,strategy_id,state,symbol,revision)
                VALUES('intent',$1,$2,$3,'TESTUSDT',6)''', owner, strategy, state)
            await connection.execute(f"INSERT INTO {schema}.position_claims VALUES('TESTUSDT','hrs','PENDING',NULL,NULL)")
            if other_uncertain:
                await connection.execute(f'''INSERT INTO {schema}.entry_risk_reservation
                    (client_order_id,account_id,strategy_id,state,symbol,revision)
                    VALUES('other-uncertain','audit','hrs','submitting','TESTUSDT',6)''')
        store = RiskPolicyStore(SimpleNamespace(pool=Pool()), account='audit')
        async def policy(*args):
            return dict(revision=7, mode='reduced')
        store.policy = policy
        with pytest.raises(EntryDenied, match='submission is not authorized'):
            async with store.submitting('hrs', 'intent'):
                pytest.fail('no submission body or HTTP is authorized')
        # This fresh acquisition observes committed state, not an uncommitted
        # transaction that will roll back when EntryDenied escapes the context.
        async with pool.acquire() as connection:
            row = await connection.fetchrow(f"SELECT * FROM {schema}.entry_risk_reservation WHERE client_order_id='intent'")
            assert row['state'] == ('released' if released else state)
            assert row['geometry'] == '{}'
            claim = await connection.fetchval(f'SELECT claim_state FROM {schema}.position_claims')
            assert claim == ('RELEASED' if released and not other_uncertain else 'PENDING')
            if other_uncertain:
                assert await connection.fetchval(f"SELECT state FROM {schema}.entry_risk_reservation WHERE client_order_id='other-uncertain'") == 'submitting'
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        await pool.close()
