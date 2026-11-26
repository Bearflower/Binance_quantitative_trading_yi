# Protected PostgreSQL table maintenance compensation

This compensates for the PG15 bootstrap-role incident described in [the incident report](../reports/2026-11-25-postgres-autovacuum-bootstrap.md). It controls maintenance debt without changing runtime privileges or stopping business services. Native autovacuum still cannot maintain these protected tables; the eventual identity repair remains separate.

## Scope and authority

- `database/postgres/maintenance_scope.json` freezes the **42 existing protected tables** observed on 2026-11-25. It contains only schema/table names and is not an ACL contract.
- `database/postgres/permissions.json` remains the sole owner/permissions authority. Every selected scope entry must be an explicit table with an independent owner in that contract. Newly introduced tables are never silently included.
- Every invocation first runs `scripts/database_permissions.py audit`. Any permission/owner drift or unregistered object blocks all maintenance. The tool never calls `apply`, changes owners/roles, or grants permissions.
- The current role with OID 10 must still be `trading_user NOSUPERUSER`, the database must not be owned by OID 10, and the maintenance connection must be an existing superuser. The checked target OID and owner are checked again in the maintenance connection.

## Selection and load bounds

The default invocation is a read-only plan. `--execute` authorizes the ordinary maintenance statements.

```bash
python3 scripts/database_maintenance.py
python3 scripts/database_maintenance.py --execute
```

It reads PostgreSQL statistics and current per-table/global autovacuum settings. It selects a scoped table only when dead tuples, modified tuples, or inserted tuples exceed their corresponding maintenance threshold; negative insert thresholds are respected. TOAST vacuum/freeze statistics are included and cause maintenance of the scoped parent. TOAST modification/analyze counts are deliberately excluded because PostgreSQL does not analyze TOAST relations and those counters do not reset. XID or multixact age also triggers at half the configured maximum age, capped at 100 million. Age-triggered work goes first; other work is ordered by oldest successful vacuum to avoid starvation.

One host file lock prevents concurrent runs. Each pass processes at most eight tables and has a 240-second maintenance budget; each statement gets the smaller of 120 seconds and the remaining budget, with five seconds of client connection/cleanup allowance. Inventory has a 15-second server timeout, and the preflight audit uses its own existing bounds. The systemd service has an outer 600-second limit covering all phases.

All maintenance uses `lock_timeout=1s`, `vacuum_cost_delay=5ms`, `vacuum_cost_limit=100`, `maintenance_work_mem=64MB`, and `VACUUM (ANALYZE, SKIP_LOCKED, TRUNCATE FALSE, PARALLEL 0)`. This avoids FULL/table rewrites and tail-truncation exclusive locks. A lock conflict, skipped relation, SQL error, or timeout is recorded as `failed_or_skipped`, returns a failed service status, and can retry on the next scheduled pass. Unrelated tables are never maintained. Autovacuum remains enabled; no warnings are suppressed.

## Production installation

The operational bundle lives at `/root/trading_system/.ops/postgres-maintenance/`, with the scripts and database subdirectories preserved. It contains this maintenance script, the unchanged permission-audit script, the reviewed permission contract, and the frozen maintenance scope. Root owns the bundle and it is not writable by runtime accounts. The bundle is independent of application container rebuilds.

`trading-postgres-maintenance.service` and `.timer` are installed from `scripts/systemd/`. The timer starts two minutes after boot and then five minutes after the previous pass finishes, plus up to twenty seconds of jitter. Oneshot service semantics and the file lock prevent overlap. Credentials use the existing `docker exec … psql -U trading_migration_admin` local-container connection, exactly as the permission-audit tool; no new credential files, role memberships, or password arguments are introduced.

```bash
systemctl status trading-postgres-maintenance.timer
systemctl status trading-postgres-maintenance.service
journalctl -u trading-postgres-maintenance.service --since '-1 hour' --no-pager
systemctl start trading-postgres-maintenance.service
```

JSON journal records include the permission-manifest hash, scope hash, selected table names and reasons, before-statistics, outcome, SQLSTATE codes, deferred count, and elapsed time. Arbitrary SQL/connection stderr and data rows are not logged. The normal system journal provides bounded rotation; there is no unbounded per-run file log.

Stop future compensation without touching trading/database services:

```bash
systemctl disable --now trading-postgres-maintenance.timer
```

A currently running bounded pass may finish. Do not disable compensation indefinitely while the bootstrap issue remains unresolved. When a later release changes the permissions contract or protected table inventory, review and update the bundle/scope alongside that release, then verify the plan/audit before resuming. A stale contract intentionally fails closed. After the native identity repair is verified, disable the timer.

## Verification

Run:

```bash
python -m pytest tests/test_database_permissions tests/test_scripts -q
```

The maintenance tests use an isolated temporary PostgreSQL cluster. They verify threshold and TOAST triggers, frozen scope rejection, actual VACUUM/ANALYZE without maintaining an unselected table, preserved runtime denial, ACL/owner drift blocking, host-lock exclusion, and nonblocking lock-conflict reporting with a successful retry. PostgreSQL 16 prevents SQL demotion of the bootstrap role; the test fixture recreates the legacy PostgreSQL 15 catalog flag state only inside its disposable local cluster. No such catalog change exists in the operational script.
