"""Allocation decreases constrain entries without liquidating grandfathered holdings."""
from copy import deepcopy
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from shared.capital_manager import CapitalManager
from shared.accounting.history_tolerance import acceptance_digest

SCOPES = ('btc_eth', 'btc_eth_aggressive', 'hrs', 'new_coin')


def digest(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def receipt():
    p = dict(kind='historical_risk_acceptance_v1', account_id='default', revision=1,
        enabled=True, exchange_uid_sha256='a'*64, classic_cutover_ms=1795582464000,
        accepted_from='2026-10-31T16:00:00+00:00', accepted_through='2026-11-26T07:59:00+00:00',
        effective_at='2026-11-26T08:00:00+00:00', classic_flat_at='2026-11-25T05:32:58.143000+00:00',
        classic_flat_evidence_sha256='b'*64, classic_open_equity='391.65638581',
        risk_limit_ratio='0.05', hrs_daily_limit='0.03', hrs_rolling_limit='0.06',
        classic_risk_method='classic_fixed_capital_cashflow_adjusted_drawdown_v1',
        automatic_allocation=True, approval_ref='operator:2026-11-27',
        allowed_gap_codes=['pm_native_income_coverage', 'historical_nav_sampling'],
        allocation_ceilings_usdt={sid:'100' for sid in SCOPES}, allocation_anchor_id=7,
        allocation_anchor_entries_sha256=digest([dict(strategy_id=s,allocated_amount=100) for s in SCOPES]))
    return dict(account_id='default', revision=1, payload=p, receipt_sha256=acceptance_digest(p))


class AllocationDB:
    def __init__(self):
        self.policy = receipt()
        self.previous = dict(id=7, entries=[dict(strategy_id=s, allocated_amount=100) for s in SCOPES])
        self.current = dict(id=8, entries=[dict(strategy_id=s, allocated_amount=30,
            holdings_floor_usdt='100', publication_method='classic_history_acceptance_v1',
            previous_allocation_id=7, previous_entries_sha256=digest(self.previous['entries']),
            history_acceptance_sha256=self.policy['receipt_sha256']) for s in SCOPES])
        self.reads = []

    async def fetch_one(self, sql, *args):
        self.reads.append((sql, args))
        if 'historical_risk_acceptance' in sql:
            assert 'receipt_sha256' in sql
            if len(args) == 2:
                assert self.current['entries'][0]['history_acceptance_sha256'] in args
            return deepcopy(self.policy)
        if 'WHERE id' in sql:
            assert args == (7,)
            return deepcopy(self.previous)
        assert 'capital_allocation' in sql
        return deepcopy(self.current)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS', '1795582464000')
    path = tmp_path / 'config.yaml'
    path.write_text('{}')
    db = AllocationDB()
    return CapitalManager(str(path), db=db, strategy_id='hrs', require_current_allocation=True)


@pytest.mark.asyncio
async def test_reduced_allocation_preserves_holdings_but_still_blocks_new_entries(manager):
    close = AsyncMock(return_value=True)
    assert await manager.enforce_existing_positions({'ETHUSDT':80}, close)
    close.assert_not_awaited()
    assert await manager.get_effective_margin_limit() == (30, 'db')
    assert not (await manager.can_open_within_limit(80, 1))[0]


@pytest.mark.asyncio
async def test_legitimate_breach_above_holdings_floor_still_closes(manager):
    close = AsyncMock(return_value=True)
    assert await manager.enforce_existing_positions({'ETHUSDT':110}, close)
    close.assert_awaited_once_with('ETHUSDT')


@pytest.mark.asyncio
async def test_holdings_amount_and_floor_come_from_one_current_row(manager):
    close = AsyncMock(return_value=True)
    assert await manager.enforce_existing_positions({'ETHUSDT':80}, close)
    reads = [sql for sql, _ in manager.db.reads if 'left(month' in sql]
    assert len(reads) == 1
    close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['floor_nan', 'floor_negative', 'floor_inflated', 'parent_hash',
    'parent_id', 'current_id', 'missing_receipt', 'bad_receipt_hash', 'unknown_method',
    'missing_method', 'duplicate_strategy', 'missing_strategy', 'receipt_scope', 'entry_over_cap'])
async def test_bad_floor_provenance_never_forces_close(manager, corruption):
    db = manager.db
    entry = next(e for e in db.current['entries'] if e['strategy_id'] == 'hrs')
    if corruption == 'floor_nan': entry['holdings_floor_usdt'] = 'NaN'
    elif corruption == 'floor_negative': entry['holdings_floor_usdt'] = '-1'
    elif corruption == 'floor_inflated': entry['holdings_floor_usdt'] = '101'
    elif corruption == 'parent_hash': entry['previous_entries_sha256'] = 'c'*64
    elif corruption == 'parent_id': entry['previous_allocation_id'] = 6
    elif corruption == 'current_id': db.current['id'] = 7
    elif corruption == 'missing_receipt': db.policy = None
    elif corruption == 'bad_receipt_hash': db.policy['receipt_sha256'] = 'd'*64
    elif corruption == 'unknown_method': entry['publication_method'] = 'other'
    elif corruption == 'missing_method': del entry['publication_method']
    elif corruption == 'duplicate_strategy': db.current['entries'].append(dict(entry))
    elif corruption == 'missing_strategy': db.current['entries'].pop()
    elif corruption == 'receipt_scope':
        db.policy['payload']['allocation_ceilings_usdt'] = {'hrs':'100'}
        db.policy['receipt_sha256'] = acceptance_digest(db.policy['payload'])
        for e in db.current['entries']: e['history_acceptance_sha256'] = db.policy['receipt_sha256']
    elif corruption == 'entry_over_cap': entry['allocated_amount'] = '101'
    close = AsyncMock(return_value=True)
    assert not await manager.enforce_existing_positions({'ETHUSDT':80}, close)
    close.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_allocation_keeps_existing_enforcement(manager):
    manager.db.current = dict(id=8, entries=[dict(strategy_id='hrs', allocated_amount=30)])
    manager.db.policy = None
    close = AsyncMock(return_value=True)
    assert await manager.enforce_existing_positions({'ETHUSDT':80}, close)
    close.assert_awaited_once_with('ETHUSDT')
    assert len(manager.db.reads) == 2


@pytest.mark.asyncio
async def test_db_failure_cannot_fall_back_to_smaller_allocation_and_close(manager):
    manager.db.fetch_one = AsyncMock(side_effect=RuntimeError('unavailable'))
    close = AsyncMock(return_value=True)
    assert not await manager.enforce_existing_positions({'ETHUSDT':80}, close)
    close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['missing_parent', 'parent_extra_strategy', 'mixed_receipt',
    'mixed_parent', 'bad_entries', 'negative_legacy_amount', 'missing_current'])
async def test_partial_or_unavailable_provenance_does_not_close(manager, corruption):
    db = manager.db
    if corruption == 'missing_parent': db.previous = None
    elif corruption == 'parent_extra_strategy':
        db.previous['entries'].append({'strategy_id':'unknown', 'allocated_amount':1})
        for entry in db.current['entries']:
            entry['previous_entries_sha256'] = digest(db.previous['entries'])
    elif corruption == 'mixed_receipt': db.current['entries'][-1]['history_acceptance_sha256'] = 'f'*64
    elif corruption == 'mixed_parent': db.current['entries'][-1]['previous_allocation_id'] = 6
    elif corruption == 'bad_entries': db.current['entries'] = '{}'
    elif corruption == 'negative_legacy_amount':
        db.current['entries'] = [{'strategy_id':'hrs', 'allocated_amount':-1}]
    elif corruption == 'missing_current': db.current = None
    close = AsyncMock(return_value=True)
    assert not await manager.enforce_existing_positions({'ETHUSDT':80}, close)
    close.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonrepresentable_holdings_limit_is_not_unlimited(manager):
    for entry in manager.db.previous['entries']:
        entry['allocated_amount'] = '1e999'
    manager.db.policy['payload']['allocation_anchor_entries_sha256']=digest(manager.db.previous['entries'])
    manager.db.policy['receipt_sha256']=acceptance_digest(manager.db.policy['payload'])
    for entry in manager.db.current['entries']:
        entry['holdings_floor_usdt'] = '1e999'
        entry['previous_entries_sha256'] = digest(manager.db.previous['entries'])
        entry['history_acceptance_sha256'] = manager.db.policy['receipt_sha256']
    close = AsyncMock(return_value=True)
    assert not await manager.enforce_existing_positions({'ETHUSDT':80}, close)
    close.assert_not_awaited()


@pytest.mark.asyncio
async def test_provenance_query_timeout_does_not_close(manager):
    import asyncio
    async def wait(*args):
        await asyncio.sleep(1)
    manager.db.fetch_one = wait
    manager._get_db_query_timeout = lambda: 0.001
    close = AsyncMock(return_value=True)
    assert not await manager.enforce_existing_positions({'ETHUSDT':80}, close)
    close.assert_not_awaited()


@pytest.fixture
def postgres_holdings():
    """Real runtime SELECT permissions, isolated from any existing PostgreSQL."""
    from pathlib import Path
    import os
    import shutil
    import subprocess
    import tempfile
    if not all(shutil.which(binary) for binary in ('initdb', 'pg_ctl', 'psql')):
        pytest.skip('local PostgreSQL binaries required')
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='holdings-floor-') as tmp:
        data = Path(tmp)/'data'
        subprocess.run(['initdb', '-D', str(data), '-U', 'floor_admin', '-A', 'trust',
            '--no-locale', '-E', 'UTF8'], check=True, capture_output=True)
        subprocess.run(['pg_ctl', '-D', str(data), '-l', str(Path(tmp)/'log'), '-o',
            f"-F -k {tmp} -h '' -p 55449", '-w', 'start'], check=True, capture_output=True)
        env = dict(os.environ, PGHOST=tmp, PGPORT='55449', PGDATABASE='postgres', PGUSER='floor_admin')
        try:
            ddl = ('CREATE ROLE trading_migration_admin LOGIN; CREATE ROLE trading_user LOGIN; '
                'CREATE ROLE hrs_acceptance_reader LOGIN; CREATE SCHEMA trading AUTHORIZATION trading_migration_admin; '
                'CREATE TABLE public.capital_allocation(id bigint PRIMARY KEY, month date NOT NULL, '
                'entries jsonb NOT NULL, status text NOT NULL, created_at timestamptz DEFAULT now()); '
                'ALTER TABLE public.capital_allocation OWNER TO trading_user; '
                'SET ROLE trading_migration_admin;\n'
                + (root/'database/postgres/migrations/2026-11-27-historical-risk-acceptance.sql').read_text())
            subprocess.run(['psql', '-XAt', '-v', 'ON_ERROR_STOP=1'], input=ddl, env=env,
                text=True, capture_output=True, check=True)
            full = json.loads((root/'database/postgres/permissions.json').read_text())
            objects = [row for row in full['objects'] if
                (row.get('schema'), row.get('name')) in {
                    ('public', 'capital_allocation'), ('trading', 'historical_risk_acceptance'),
                    ('trading', 'classic_account_risk_point')}]
            manifest = dict(version=1, schemas=['trading', 'public'], profiles=full['profiles'],
                roles={role:dict(required=True, schemas={'trading':['USAGE'], 'public':['USAGE']})
                       for role in ['trading_user', 'hrs_acceptance_reader']}, objects=objects)
            file = Path(tmp)/'permissions.json'; file.write_text(json.dumps(manifest))
            applied = subprocess.run(['python3', str(root/'scripts/database_permissions.py'), 'apply',
                '--local', '--manifest', str(file)], env=env, text=True, capture_output=True)
            assert applied.returncode == 0, applied.stdout + applied.stderr
            yield tmp
        finally:
            subprocess.run(['pg_ctl', '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                check=True, capture_output=True)


@pytest.mark.asyncio
async def test_postgres_runtime_retains_original_floor_after_policy_revocation(postgres_holdings, manager):
    import asyncpg
    from datetime import date
    admin = await asyncpg.connect(host=postgres_holdings, port=55449, user='floor_admin', database='postgres')
    runtime = None
    try:
        db = manager.db
        for row in (db.previous, db.current):
            month = manager._current_month() if row is db.current else '2026-10'
            await admin.execute('INSERT INTO public.capital_allocation(id,month,entries,status) '
                'VALUES ($1,$2,$3::jsonb,$4)', row['id'], date.fromisoformat(month+'-01'),
                json.dumps(row['entries']), 'active')
        policy = db.policy
        await admin.execute('SET ROLE trading_migration_admin')
        await admin.execute('INSERT INTO trading.historical_risk_acceptance '
            '(account_id,revision,payload,receipt_sha256) VALUES ($1,$2,$3::jsonb,$4)',
            policy['account_id'], policy['revision'], json.dumps(policy['payload']), policy['receipt_sha256'])
        revoked = deepcopy(policy['payload']); revoked.update(revision=2, enabled=False)
        await admin.execute('INSERT INTO trading.historical_risk_acceptance '
            '(account_id,revision,payload,receipt_sha256) VALUES ($1,$2,$3::jsonb,$4)',
            'default', 2, json.dumps(revoked), acceptance_digest(revoked))
        runtime = await asyncpg.connect(host=postgres_holdings, port=55449, user='trading_user', database='postgres')
        class DB:
            fetch_one = runtime.fetchrow
        manager.db = DB()
        close = AsyncMock(return_value=True)
        assert await manager.enforce_existing_positions({'ETHUSDT':80}, close)
        close.assert_not_awaited()
        assert await manager.get_effective_margin_limit() == (30, 'db')
        assert not (await manager.can_open_within_limit(80, 1))[0]
        await runtime.execute("UPDATE public.capital_allocation SET entries=jsonb_set(entries,'{2,holdings_floor_usdt}','\"0\"') WHERE id=8")
        assert not await manager.enforce_existing_positions({'ETHUSDT':80}, close)
        close.assert_not_awaited()
        # Even after revision 2 revoked automatic allocation, deleting all current
        # protection metadata must not turn this reduced row into a legacy limit.
        stripped=[dict(strategy_id=e['strategy_id'],allocated_amount=e['allocated_amount'])
                  for e in db.current['entries']]
        await runtime.execute('UPDATE public.capital_allocation SET entries=$1::jsonb WHERE id=8',json.dumps(stripped))
        assert not await manager.enforce_existing_positions({'ETHUSDT':80}, close)
        close.assert_not_awaited()
    finally:
        if runtime is not None:
            await runtime.close()
        await admin.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'automatic_allocation':False}, {'enabled':False}])
async def test_original_receipt_must_have_authorized_automatic_allocation(manager, change):
    policy = manager.db.policy
    policy['payload'].update(change)
    policy['receipt_sha256'] = acceptance_digest(policy['payload'])
    for entry in manager.db.current['entries']:
        entry['history_acceptance_sha256'] = policy['receipt_sha256']
    close = AsyncMock(return_value=True)
    assert not await manager.enforce_existing_positions({'ETHUSDT':80}, close)
    close.assert_not_awaited()


@pytest.mark.asyncio
async def test_postgres_rehashed_corrupt_parent_floor_cannot_liquidate_holdings(postgres_holdings, manager):
    import asyncpg
    from datetime import date
    from shared.accounting.allocation_provenance import entries_digest
    admin = await asyncpg.connect(host=postgres_holdings, port=55449, user='floor_admin', database='postgres')
    runtime = None
    try:
        proof = manager.db.policy
        grandparent = dict(id=7, entries=[dict(strategy_id=sid, allocated_amount=40) for sid in SCOPES])
        proof['payload']['allocation_anchor_entries_sha256']=entries_digest(grandparent['entries'])
        proof['receipt_sha256']=acceptance_digest(proof['payload'])
        parent = dict(id=8, entries=[dict(strategy_id=sid, allocated_amount=30,
            holdings_floor_usdt='40', publication_method='classic_history_acceptance_v1',
            previous_allocation_id=7, previous_entries_sha256=entries_digest(grandparent['entries']),
            history_acceptance_sha256=proof['receipt_sha256']) for sid in SCOPES])
        # This damage used to be laundered by the next monthly writer, which
        # rehashed the parent and propagated max(30, 0) as the new holdings floor.
        for entry in parent['entries']:entry['holdings_floor_usdt']='0'
        current = dict(id=9, entries=[dict(strategy_id=sid, allocated_amount=20,
            holdings_floor_usdt='30', publication_method='classic_history_acceptance_v1',
            previous_allocation_id=8, previous_entries_sha256=entries_digest(parent['entries']),
            history_acceptance_sha256=proof['receipt_sha256']) for sid in SCOPES])
        for row in (grandparent,parent,current):
            month=manager._current_month() if row is current else ('2026-09' if row is grandparent else '2026-10')
            await admin.execute('INSERT INTO public.capital_allocation(id,month,entries,status) VALUES ($1,$2,$3::jsonb,$4)',
                row['id'],date.fromisoformat(month+'-01'),json.dumps(row['entries']),'active')
        await admin.execute('SET ROLE trading_migration_admin')
        await admin.execute('INSERT INTO trading.historical_risk_acceptance '
            '(account_id,revision,payload,receipt_sha256) VALUES ($1,$2,$3::jsonb,$4)',
            proof['account_id'],proof['revision'],json.dumps(proof['payload']),proof['receipt_sha256'])
        runtime=await asyncpg.connect(host=postgres_holdings,port=55449,user='trading_user',database='postgres')
        class DB:fetch_one=runtime.fetchrow
        manager.db=DB()
        close=AsyncMock(return_value=True)
        assert not await manager.enforce_existing_positions({'ETHUSDT':35},close)
        close.assert_not_awaited()
    finally:
        if runtime is not None:await runtime.close()
        await admin.close()


@pytest.mark.asyncio
async def test_removing_all_current_protection_metadata_cannot_impersonate_legacy(manager):
    manager.db.current['entries']=[dict(strategy_id=e['strategy_id'],allocated_amount=e['allocated_amount'])
        for e in manager.db.current['entries']]
    close=AsyncMock(return_value=True)
    assert not await manager.enforce_existing_positions({'ETHUSDT':80},close)
    close.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_row_without_this_strategy_never_authorizes_close(manager):
    manager.db.policy=None
    manager.db.current['entries']=[dict(strategy_id='btc_eth',allocated_amount=1)]
    close=AsyncMock(return_value=True)
    assert not await manager.enforce_existing_positions({'ETHUSDT':80},close)
    close.assert_not_awaited()
