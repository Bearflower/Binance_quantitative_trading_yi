"""Bounded maintenance selects only frozen, contract-owned protected relations."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/database_maintenance.py'


def module():
    assert SCRIPT.exists(), 'bounded protected-table maintenance script is missing'
    spec = importlib.util.spec_from_file_location('maintenance', SCRIPT)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def stats(**changes):
    row = dict(dead=0, modified=0, inserted=0, vacuum_threshold=50,
               analyze_threshold=50, insert_threshold=1000, xid_age=10,
               mxid_age=1, xid_limit=100000000, mxid_limit=100000000)
    row.update(changes)
    return row


def test_thresholds_and_toast_debt_are_not_silently_missed():
    m = module()
    assert m.reasons(stats()) == []
    assert m.reasons(stats(dead=50)) == []
    assert m.reasons(stats(dead=51)) == ['dead']
    assert m.reasons(stats(modified=51)) == ['modified']
    assert m.reasons(stats(inserted=1001)) == ['inserted']
    assert m.reasons(stats(inserted=100000, insert_threshold=-1)) == []
    assert m.reasons(stats(xid_age=100000000)) == ['xid_age']
    assert m.reasons(stats(mxid_age=100000000)) == ['mxid_age']
    row = dict(schema='app', name='evidence', stats=stats(), toast=stats(dead=100))
    assert m.table_reasons(row) == ['toast:dead']
    # TOAST tables are not analyzed: this counter never resets and must not
    # cause an expensive false maintenance trigger on every timer tick.
    row['toast'] = stats(modified=1000000)
    assert m.table_reasons(row) == []


def test_scope_cannot_expand_to_unknown_or_runtime_owned_tables():
    m = module()
    manifest = {'roles': {'trading_user': {}}, 'objects': [
        {'schema': 'app', 'name': 'safe', 'kind': 'table', 'owner': 'admin'},
        {'schema': 'app', 'name': 'legacy', 'kind': 'table', 'owner': 'trading_user'}]}
    assert m.resolve_scope(manifest, [{'schema': 'app', 'name': 'safe'}])[0]['owner'] == 'admin'
    for name in ['unknown', 'legacy']:
        with pytest.raises(ValueError):
            m.resolve_scope(manifest, [{'schema': 'app', 'name': name}])
    with pytest.raises(ValueError):
        m.resolve_scope(manifest, [{'schema': 'app', 'name': 'safe'}] * 2)


@pytest.fixture(scope='module')
def isolated():
    if not all(shutil.which(x) for x in ('initdb', 'pg_ctl', 'psql')):
        pytest.skip('local PostgreSQL binaries required')
    with tempfile.TemporaryDirectory(prefix='maint-') as tmp:
        data = Path(tmp) / 'data'
        subprocess.run(['initdb', '-D', str(data), '-U', 'trading_user', '-A', 'trust', '--no-locale', '-E', 'UTF8'], check=True, capture_output=True)
        subprocess.run(['pg_ctl', '-D', str(data), '-l', str(Path(tmp)/'log'), '-o', f"-F -k {tmp} -h '' -p 55441 -c autovacuum=off", '-w', 'start'], check=True, capture_output=True)
        env = dict(os.environ, PGHOST=tmp, PGPORT='55441', PGDATABASE='postgres', PGUSER='trading_user')
        def sql(query):
            return subprocess.run(['psql', '-XAt', '-v', 'ON_ERROR_STOP=1'], input=query, env=env, text=True, capture_output=True)
        assert sql('CREATE ROLE trading_migration_admin LOGIN SUPERUSER; ALTER DATABASE postgres OWNER TO trading_migration_admin;').returncode == 0
        env['PGUSER'] = 'trading_migration_admin'
        # PG16 guards ALTER ROLE on OID 10; recreate the PG15 incident only in this disposable cluster.
        assert sql('UPDATE pg_authid SET rolsuper=false,rolcreatedb=false,rolcreaterole=false,rolreplication=false,rolbypassrls=false WHERE oid=10; CREATE SCHEMA app; GRANT USAGE ON SCHEMA app TO trading_user; CREATE TABLE app.evidence(id int); CREATE TABLE app.other(id int); GRANT SELECT ON ALL TABLES IN SCHEMA app TO trading_user; INSERT INTO app.evidence SELECT generate_series(1,120); INSERT INTO app.other SELECT generate_series(1,120); SELECT pg_stat_force_next_flush();').returncode == 0
        manifest = {'version': 1, 'schemas': ['app'], 'roles': {'trading_user': {'required': True, 'schemas': {'app': ['USAGE']}}}, 'profiles': {'none': [], 'read': ['SELECT']}, 'objects': [{'schema': 'app', 'name': name, 'kind': 'table', 'owner': 'trading_migration_admin', 'required': True, 'grants': {'trading_user': 'read'}} for name in ['evidence', 'other']]}
        path = Path(tmp)
        (path/'manifest.json').write_text(json.dumps(manifest))
        (path/'scope.json').write_text(json.dumps([{'schema': 'app', 'name': 'evidence'}]))
        def cli(*args):
            return subprocess.run([sys.executable, str(SCRIPT), '--local', '--manifest', str(path/'manifest.json'), '--scope', str(path/'scope.json'), '--lock-file', str(path/'maintenance.lock'), *args], env=env, text=True, capture_output=True)
        yield sql, cli, path
        subprocess.run(['pg_ctl', '-D', str(data), '-m', 'immediate', '-w', 'stop'], check=True, capture_output=True)


def test_real_postgres_plan_and_run_keep_other_tables_and_runtime_acl_untouched(isolated):
    module()
    sql, cli, _ = isolated
    result = cli()
    assert result.returncode == 0, result.stdout + result.stderr
    plan = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(r['event'] == 'plan' and r['selected'] == 1 for r in plan)
    assert sql("SELECT last_vacuum IS NULL FROM pg_stat_user_tables WHERE relname='evidence';").stdout.strip() == 't'
    result = cli('--execute')
    assert result.returncode == 0, result.stdout + result.stderr
    assert sql("SELECT last_vacuum IS NOT NULL AND last_analyze IS NOT NULL FROM pg_stat_user_tables WHERE relname='evidence';").stdout.strip() == 't'
    assert sql("SELECT last_vacuum IS NULL FROM pg_stat_user_tables WHERE relname='other';").stdout.strip() == 't'
    assert sql("SELECT NOT rolsuper FROM pg_roles WHERE rolname='trading_user';").stdout.strip() == 't'
    assert sql('SET ROLE trading_user; INSERT INTO app.evidence VALUES (999);').returncode != 0
    result = cli('--execute')
    assert result.returncode == 0
    assert any(json.loads(line).get('selected') == 0 for line in result.stdout.splitlines())


def test_owner_drift_and_acl_drift_block_before_maintenance(isolated):
    module()
    sql, cli, _ = isolated
    assert sql('GRANT INSERT ON app.evidence TO trading_user;').returncode == 0
    result = cli('--execute')
    assert result.returncode != 0
    assert 'permission_audit_failed' in result.stdout
    assert sql('REVOKE INSERT ON app.evidence FROM trading_user;').returncode == 0
    assert sql('ALTER TABLE app.evidence OWNER TO trading_user;').returncode == 0
    assert cli('--execute').returncode != 0
    assert sql('ALTER TABLE app.evidence OWNER TO trading_migration_admin; GRANT SELECT ON app.evidence TO trading_user;').returncode == 0


def test_host_lock_prevents_overlapping_runs(isolated):
    import fcntl
    module()
    _, cli, path = isolated
    with (path/'maintenance.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = cli('--execute')
        assert result.returncode == 0
        assert 'already_running' in result.stdout


def test_locked_table_is_reported_and_does_not_block_application(isolated):
    import time
    module()
    sql, cli, path = isolated
    assert sql('INSERT INTO app.evidence SELECT generate_series(1000,1200); SELECT pg_stat_force_next_flush();').returncode == 0
    env = dict(os.environ, PGHOST=str(path), PGPORT='55441', PGDATABASE='postgres', PGUSER='trading_migration_admin')
    locker = subprocess.Popen(['psql', '-XqAt', '-v', 'ON_ERROR_STOP=1'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
    try:
        locker.stdin.write("BEGIN; LOCK app.evidence IN ACCESS EXCLUSIVE MODE; SELECT 'ready'; SELECT pg_sleep(20); ROLLBACK;\n")
        locker.stdin.flush()
        assert locker.stdout.readline().strip() == 'ready'
        started = time.monotonic()
        result = cli('--execute')
        assert time.monotonic()-started < 5
        assert result.returncode != 0
        assert 'failed_or_skipped' in result.stdout
    finally:
        locker.terminate()
        locker.wait(timeout=5)
        # Disconnecting the client need not interrupt pg_sleep immediately.
        sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND query LIKE '%pg_sleep(20)%';")
    result = cli('--execute')
    assert result.returncode == 0, result.stdout + result.stderr
