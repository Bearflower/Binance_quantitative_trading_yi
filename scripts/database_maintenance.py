#!/usr/bin/env python3
"""Bounded compensating maintenance for the reviewed PG15 bootstrap-role incident.

Read-only plan by default; --execute runs ordinary cost-throttled VACUUM/ANALYZE.
Scope is frozen separately; owner/ACL authority remains permissions.json only.
No password arguments, grants, owner changes, role changes, or log suppression.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from database_permissions import validate, identifier  # noqa: E402


def emit(event, **values):
    print(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(), event=event, **values), sort_keys=True), flush=True)


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def quoted(value):
    identifier(value)
    return '"' + value + '"'


def resolve_scope(manifest, scope):
    if not isinstance(scope, list) or not scope:
        raise ValueError('scope must be a nonempty frozen table list')
    resolved, seen = [], set()
    for item in scope:
        if set(item) != {'schema', 'name'}:
            raise ValueError('scope entries may contain only schema and name')
        key = (item['schema'], item['name'])
        for value in key:
            identifier(value)
        if key in seen:
            raise ValueError('duplicate scope table')
        seen.add(key)
        rules = [r for r in manifest['objects'] if (r['schema'], r.get('name')) == key and r['kind'] == 'table']
        if len(rules) != 1 or not rules[0].get('owner') or rules[0]['owner'] in manifest['roles']:
            raise ValueError('scope table must have an explicit protected owner in the contract')
        resolved.append(dict(item, owner=rules[0]['owner']))
    return resolved


def reasons(row):
    result = []
    for field, threshold in [('dead', 'vacuum_threshold'), ('modified', 'analyze_threshold'), ('inserted', 'insert_threshold')]:
        if row[threshold] >= 0 and row[field] > row[threshold]:
            result.append(field)
    for field, limit in [('xid_age', 'xid_limit'), ('mxid_age', 'mxid_limit')]:
        if row[field] >= row[limit]:
            result.append(field)
    return result


def table_reasons(row):
    result = reasons(row['stats'])
    # PostgreSQL does not ANALYZE TOAST relations; their modification counter
    # therefore never resets. Only actual vacuum/freeze debt can trigger here.
    if row.get('toast'):
        result += ['toast:' + r for r in reasons(row['toast']) if r != 'modified']
    return result


def inventory_sql(scope):
    targets = ','.join('(' + ','.join(literal(r[k]) for k in ('schema', 'name', 'owner')) + ')' for r in scope)
    # reloptions override the global thresholds, including the associated TOAST table.
    return """SET lock_timeout='1s'; SET statement_timeout='15s';
WITH targets(schema,name,expected_owner) AS (VALUES """ + targets + """), stats AS (
 SELECT c.oid,c.relowner,c.reltoastrelid,c.relkind,n.nspname,c.relname,
 greatest(s.last_vacuum,s.last_autovacuum) AS last_vacuum,
 jsonb_build_object(
  'dead',s.n_dead_tup,'modified',s.n_mod_since_analyze,'inserted',s.n_ins_since_vacuum,
  'vacuum_threshold',coalesce((o.opts->>'autovacuum_vacuum_threshold')::float,current_setting('autovacuum_vacuum_threshold')::float)+coalesce((o.opts->>'autovacuum_vacuum_scale_factor')::float,current_setting('autovacuum_vacuum_scale_factor')::float)*greatest(c.reltuples,0),
  'analyze_threshold',coalesce((o.opts->>'autovacuum_analyze_threshold')::float,current_setting('autovacuum_analyze_threshold')::float)+coalesce((o.opts->>'autovacuum_analyze_scale_factor')::float,current_setting('autovacuum_analyze_scale_factor')::float)*greatest(c.reltuples,0),
  'insert_threshold',CASE WHEN coalesce((o.opts->>'autovacuum_vacuum_insert_threshold')::float,current_setting('autovacuum_vacuum_insert_threshold')::float)<0 THEN -1 ELSE coalesce((o.opts->>'autovacuum_vacuum_insert_threshold')::float,current_setting('autovacuum_vacuum_insert_threshold')::float)+coalesce((o.opts->>'autovacuum_vacuum_insert_scale_factor')::float,current_setting('autovacuum_vacuum_insert_scale_factor')::float)*greatest(c.reltuples,0) END,
  'xid_age',age(c.relfrozenxid),'mxid_age',mxid_age(c.relminmxid),
  'xid_limit',least(100000000,coalesce((o.opts->>'autovacuum_freeze_max_age')::float,current_setting('autovacuum_freeze_max_age')::float)/2),
  'mxid_limit',least(100000000,coalesce((o.opts->>'autovacuum_multixact_freeze_max_age')::float,current_setting('autovacuum_multixact_freeze_max_age')::float)/2)
 ) AS counters
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_stat_all_tables s ON s.relid=c.oid
 LEFT JOIN LATERAL (SELECT jsonb_object_agg(option_name,option_value) AS opts FROM pg_options_to_table(c.reloptions)) o ON true
)
SELECT jsonb_build_object('bootstrap', (SELECT jsonb_build_object('name',rolname,'superuser',rolsuper) FROM pg_roles WHERE oid=10),
 'admin_superuser',(SELECT rolsuper FROM pg_roles WHERE rolname=current_user),
 'database_owner',(SELECT datdba FROM pg_database WHERE datname=current_database()),
 'tables',coalesce((SELECT jsonb_agg(jsonb_build_object('schema',t.schema,'name',t.name,'expected_owner',t.expected_owner,'oid',r.oid,'owner',pg_get_userbyid(r.relowner),'kind',r.relkind,'last_vacuum',r.last_vacuum,'stats',r.counters,'toast',toast.counters)) FROM targets t LEFT JOIN stats r ON r.nspname=t.schema AND r.relname=t.name LEFT JOIN stats toast ON toast.oid=r.reltoastrelid),'[]'::jsonb));
"""


def command(args):
    psql = ['psql', '-XqAt', '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=sqlstate']
    return psql if args.local else ['docker', 'exec', '-i', args.container] + psql + ['-U', 'trading_migration_admin', '-d', args.database]


def query(args, sql, timeout=30):
    return subprocess.run(command(args), input=sql, text=True, capture_output=True, timeout=timeout)


def audit(args):
    cmd = [sys.executable, str(Path(__file__).with_name('database_permissions.py')), 'audit', '--manifest', str(args.manifest)]
    cmd += ['--local'] if args.local else ['--container', args.container, '--database', args.database]
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=185)
    report = json.loads(result.stdout) if result.stdout.strip() else {}
    if result.returncode or not report.get('ok'):
        emit('permission_audit_failed', issue_codes=sorted({i.get('code', 'unknown') for i in report.get('issues', [])}))
        return False
    emit('permission_audit', objects_checked=report['objects_checked'], manifest_sha256=report['manifest_sha256'])
    return True


def maintain_sql(row, timeout_ms=120000):
    name = quoted(row['schema']) + '.' + quoted(row['name'])
    # Recheck OID/owner in the maintenance connection, preventing a stale scope from
    # processing a recreated or re-owned table. SQL contains identifiers, no row data.
    return """SET application_name='protected-table-maintenance';
SET lock_timeout='1s'; SET statement_timeout='""" + str(int(timeout_ms)) + """ms';
SET vacuum_cost_delay='5ms'; SET vacuum_cost_limit=100; SET maintenance_work_mem='64MB';
DO $guard$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_class WHERE oid=""" + str(int(row['oid'])) + " AND oid=to_regclass(" + literal(name) + ") AND pg_get_userbyid(relowner)=" + literal(row['owner']) + """ ) THEN
 RAISE EXCEPTION 'maintenance target identity changed'; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE oid=10 AND rolname='trading_user' AND NOT rolsuper) THEN
 RAISE EXCEPTION 'bootstrap identity changed'; END IF;
END $guard$;
VACUUM (ANALYZE, SKIP_LOCKED, TRUNCATE FALSE, PARALLEL 0) """ + name + ';\n'


def run(args):
    manifest = validate(json.loads(args.manifest.read_text()))
    scope = resolve_scope(manifest, json.loads(args.scope.read_text()))
    with args.lock_file.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            emit('already_running')
            return 0
        if not audit(args):
            return 1
        result = query(args, inventory_sql(scope))
        if result.returncode:
            emit('inventory_failed')
            return 1
        inventory = json.loads(result.stdout)
        if inventory['bootstrap'] != {'name': 'trading_user', 'superuser': False} or not inventory['admin_superuser'] or inventory['database_owner'] == 10:
            emit('bootstrap_or_admin_precondition_changed')
            return 1
        rows = inventory['tables']
        if any(not r['oid'] or r['kind'] not in ('r', 'm') or r['owner'] != r['expected_owner'] for r in rows):
            emit('scope_identity_changed')
            return 1
        selected = [dict(r, reasons=table_reasons(r)) for r in rows if table_reasons(r)]
        selected.sort(key=lambda r: (not any('age' in x for x in r['reasons']), r['last_vacuum'] or '', r['schema'], r['name']))
        emit('plan', execute=args.execute, scope_count=len(rows), selected=len(selected), max_tables=8, run_budget_seconds=240,
             scope_sha256=hashlib.sha256(args.scope.read_bytes()).hexdigest(),
             tables=[{'schema': r['schema'], 'name': r['name'], 'reasons': r['reasons']} for r in selected])
        if not args.execute:
            return 0
        started, completed, failures = time.monotonic(), 0, 0
        for row in selected[:8]:
            remaining = 240 - (time.monotonic() - started)
            if remaining <= 0:
                break
            timeout_ms = min(120000, max(1, int(remaining * 1000)))
            tick = time.monotonic()
            try:
                result = query(args, maintain_sql(row, timeout_ms), timeout=timeout_ms / 1000 + 5)
                codes = re.findall(r'(?:ERROR|WARNING|FATAL):\s+([A-Z0-9]{5})', result.stderr)
                failed = result.returncode != 0 or bool(result.stderr.strip())
                status = 'failed_or_skipped' if failed else 'completed'
            except subprocess.TimeoutExpired:
                codes, failed, status = ['client_timeout'], True, 'failed_or_skipped'
            failures += int(failed)
            completed += int(not failed)
            emit('maintenance', schema=row['schema'], name=row['name'], reasons=row['reasons'], before=row['stats'], toast_before=row['toast'],
                 status=status, sqlstates=codes, elapsed_seconds=round(time.monotonic() - tick, 3))
        emit('finished', completed=completed, failures=failures, deferred=len(selected)-completed-failures, elapsed_seconds=round(time.monotonic()-started, 3))
        return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--manifest', type=Path, default=ROOT/'database/postgres/permissions.json')
    parser.add_argument('--scope', type=Path, default=ROOT/'database/postgres/maintenance_scope.json')
    parser.add_argument('--lock-file', type=Path, default=Path('/run/lock/trading-postgres-maintenance.lock'))
    parser.add_argument('--local', action='store_true', help='test/local libpq connection, no credential arguments')
    parser.add_argument('--container', default='trading_system-postgres')
    parser.add_argument('--database', default='trading_platform')
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
        # Intentionally omit arbitrary SQL/connection output; credentials or row values
        # may occur in server errors. The JSON event and journal exit status alert ops.
        emit('blocked', error_type=type(exc).__name__)
        return 2


if __name__ == '__main__':
    sys.exit(main())
