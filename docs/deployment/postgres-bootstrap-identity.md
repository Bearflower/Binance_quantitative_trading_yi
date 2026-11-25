# PostgreSQL 15 bootstrap identity repair

## Result and boundary

The target is **OID 10 = `trading_bootstrap_admin`, SUPERUSER, NOLOGIN, no password or role memberships**. The application name remains `trading_user`, with a new OID, the same password/settings and the same reviewed business privileges. `trading_user` remains NOSUPERUSER/NOCREATEDB/NOCREATEROLE/NOREPLICATION; its existing BYPASSRLS exception is preserved. No system catalog is edited directly. PostgreSQL and all services need not be restarted.

The preparer only inventories catalogs, runs the existing permission audit, and writes SQL files. **It never executes gate, disconnect, migration, rollback, login reopening, probe creation, or timer changes.** Production execution belongs to the coordinated maintenance window.

`database/postgres/permissions.json` remains the sole contract. `bootstrap_identity` separately describes the administrator, so no superuser exception is added to managed runtime roles. The audit checks bootstrap OID/name/NOLOGIN/no-password/no-membership and checks the new application's ordinary ACLs. The application also explicitly requires CONNECT/TEMPORARY on the business database, without grant option; the existing `database_permissions.py apply` supplies these only if required. The archived migration candidate used `allow_legacy_demoted: true` solely for preflight/rollback. Production native autovacuum and autoanalyze were verified on 2026-11-25 at 21:31:17 Asia/Shanghai; the current contract now sets this flag **false**. A later explicit legacy rollback must use the preserved transitional contract and generated SQL; the normal audit rejects the old broken layout.

## Production inventory and expected window

The 2026-11-25 inventory found:

- `trading_platform`: 660 application-owned tables, 635 sequences and seven application-owned schemas, **1,302 explicit owner moves**. Associated indexes, TOAST and row/array types follow the table/type ownership change.
- 25 custom functions are already owned by the migration administrator, including SECURITY DEFINER functions. None belongs to OID 10; there are no application views, role-specific RLS policies, standalone application types, custom extensions, foreign objects, publications/subscriptions or large objects requiring migration. Unexpected unsupported dependencies block preparation/execution.
- Six exact default-ACL groups: application-created research tables/sequences self-grants, and migration-admin-created research/hrs tables/sequences grants to the application. They are reviewed in the contract, not blindly copied from observed ACLs.
- `postgres` and `template1` contain no non-system relations. Preparation connects to each and rejects business objects/dependencies. `template0` stays closed and untouched, retaining the same bootstrap OID. **OID 10 is pinned: absence of `pg_shdepend` rows is not evidence of no ownership.**
- The business DB is owned by `trading_migration_admin`; PUBLIC and the application currently have CONNECT/TEMPORARY. Only builtin `pg_default`/`pg_global` tablespaces exist with null ACLs; business relations use the database's default tablespace. There are no application parameter ACLs. Non-default business tablespace use and parameter grants are rejected for separate review.
- The original role has SCRAM credentials and only a global `search_path` setting. No password verifier is returned by inventory. Settings are hash-pinned before and after copying.
- Initially 38 application connections were observed, chiefly execution-facts, K-line, data-backend, grid, HRS and ai-tuner. Addresses/container IDs are ephemeral; discover consumers again at the window. The OID-10 logical replication launcher is an internal process and is deliberately not terminated.

The independent PG15.20 schema-only restoration used the full production schema with the same 100 max connections / 64 locks per transaction. The final gate/drain/rollback/reopen/forward/reopen rehearsal passed. Forward took **2.944 s including a 1 s local authentication grace**, rollback 3.153 s, reopening/audit approximately 0.4 s. These are local metadata timings, not a production latency guarantee.

Production `authentication_timeout` is **60 seconds**. The SQL itself requires a committed NOLOGIN gate and then waits one complete authentication timeout before touching identity, eliminating old in-flight authentication before promotion. Do not add a second manual 60-second wait. Budget **90 seconds** for quiescence, one authentication horizon, metadata migration and checks. All owner-changing statements have a 1-second lock timeout; contention aborts and rolls back the complete identity transaction. Do not extend lock waits into an indefinite trading outage.

## Prepare and review (read-only)

On the server, use a reviewed copy of the repository files in the independent candidate directory:

```bash
DB_REPAIR_DIR=/root/trading_system/.releases/pm-window-89160896/db-root-repair/candidate
cd "$DB_REPAIR_DIR"
python3 scripts/database_permissions.py audit
python3 scripts/database_bootstrap_identity.py --output artifacts
sha256sum scripts/database_bootstrap_identity.py scripts/database_permissions.py database/postgres/permissions.json artifacts/*.sql
```

Preparation inventories every connectable database and emits `plan.json`, `gate.sql`, `drain-idle.sql`, `forward.sql`, `rollback.sql`, and `reopen.sql` with private permissions. Review the role/default-ACL/owner inventory and generated SQL. Re-run preparation just before the window so an unexpected new object or role-setting change is rejected instead of silently migrated. Do not reuse old SQL if the contract, ownership inventory or settings changed.

Before execution require a verified restorable backup and a business-safe point. Pause new-entry writers/DDL creators and retain exchange-side protective orders. A long logical dump can hold ACCESS SHARE locks and block application initialization DDL; the root incident used a separately verified physical backup instead. Do not launch another long full logical dump as part of this sequence.

## Controlled production sequence

Only the coordinator executes the following after the backup, plan and business safe point are accepted. Use the existing local-container migration administrator connection; no password is passed as an argument.

```bash
docker exec -i trading_system-postgres psql -X -v ON_ERROR_STOP=1 -U trading_migration_admin -d trading_platform < artifacts/gate.sql
docker exec -i trading_system-postgres psql -X -v ON_ERROR_STOP=1 -U trading_migration_admin -d trading_platform < artifacts/drain-idle.sql
```

`gate.sql` commits NOLOGIN. A lost client response does **not** prove the gate failed: query actual `rolcanlogin` before any service restart or cleanup. `drain-idle.sql` only disconnects idle client connections; it does not cancel an active business transaction, backup administrator, autovacuum or logical launcher. If active transactions remain, keep the gate closed, wait for them to finish safely and repeat the drain. Do not promote OID 10 while any application/parallel/walsender session remains.

```bash
docker exec -i trading_system-postgres psql -X -v ON_ERROR_STOP=1 -U trading_migration_admin -d trading_platform < artifacts/forward.sql
python3 scripts/database_permissions.py audit
```

The forward SQL waits the authentication horizon, verifies scope/settings/defaults and drained sessions, renames OID 10, creates the new NOLOGIN application, transfers reviewed business ownership and exact default ACLs, copies role settings and credentials entirely inside the server, rechecks sessions, then promotes the isolated NOLOGIN bootstrap. It uses the existing permission tool's guarded apply and post-audit inside the same transaction. Any unexpected ACL or owner drift aborts the entire identity transaction. MD5 is also handled correctly: the verifier is captured before a rename clears it and assigned only after the target application name exists.

Keep the trading writers quiescent while checking identity and runtime SQL. Both roles remain NOLOGIN for external authentication until the explicitly audited reopening step:

```bash
docker exec -i trading_system-postgres psql -X -v ON_ERROR_STOP=1 -U trading_migration_admin -d trading_platform < artifacts/reopen.sql
```

Reopening audits in the same transaction and only commits LOGIN if it passes. Verify the application's unchanged real credential, basic reads, permitted append/row-lock SQL and denied immutable updates using the normal application path. The isolated SCRAM and MD5 tests both authenticated before/after forward and rollback; an existing asyncpg pool reconnected to the new OID without replacing the pool. This does **not** replay an interrupted SQL transaction. Callers that terminate a background task after a connection exception may require targeted recovery. Check each service's fresh DB operations/logs before resuming writers; do not assume all processes recovered simply because a pool test passed.

The new application intentionally loses the bootstrap's ownership of system databases, catalogs and builtin tablespaces. It retains CONNECT/TEMP, schema CREATE and business ownership required by the contract. In the full rehearsal both ordinary default-tablespace CREATE TABLE and CREATE TEMP TABLE succeeded as the new application, while business-database CREATE and builtin tablespace administration remained false. No tablespace-management privilege is granted to a runtime role.

## Native maintenance acceptance

Use only the registered disposable administrator-owned probe. Never insert test rows into business or immutable tables.

```bash
docker exec -i trading_system-postgres psql -X -v ON_ERROR_STOP=1 -U trading_migration_admin -d trading_platform < database/postgres/migrations/2026-11-25-bootstrap-autovacuum-probe.sql
python3 scripts/database_permissions.py audit
```

Poll `pg_stat_user_tables` for `trading.bootstrap_autovacuum_probe` until **both `autovacuum_count > 0` and `autoanalyze_count > 0` with fresh timestamps**. Production naptime is 60 s; observe at least two cycles if needed. Do not substitute an administrator's manual VACUUM or count old logs as proof. No global autovacuum option changes are required.

The probe's optional contract entry gives the application no table privileges. Its cleanup requires the expected owner, purpose marker and successful native-maintenance evidence:

```bash
docker exec -i trading_system-postgres psql -X -v ON_ERROR_STOP=1 -U trading_migration_admin -d trading_platform < database/postgres/migrations/2026-11-25-bootstrap-autovacuum-probe-cleanup.sql
python3 scripts/database_permissions.py audit
```

The full restored-schema rehearsal recorded native autovacuum and autoanalyze counts of 1 each, zero dead tuples, passed a 1,344-object permission audit while the probe existed, and passed guarded cleanup. The coordinator may retire the compensating timer **only after** equivalent production evidence; preparation does not change it. A pinned older compensation bundle may reject the new optional probe/identity until that transition is coordinated; never relax its audit to force it to run.

## Rollback and ambiguous outcomes

A pre-commit failure automatically restores all role names, owners, default ACLs, settings and credentials. The separately committed NOLOGIN gate remains closed. Inspect actual OIDs/role flags and run the appropriate audit; then use the audited `reopen.sql` to restore logins. Never infer state solely from an SSH exit code.

For a committed split that must be reversed before full business resumption:

```bash
docker exec -i trading_system-postgres psql -X -v ON_ERROR_STOP=1 -U trading_migration_admin -d trading_platform < artifacts/gate.sql
docker exec -i trading_system-postgres psql -X -v ON_ERROR_STOP=1 -U trading_migration_admin -d trading_platform < artifacts/drain-idle.sql
docker exec -i trading_system-postgres psql -X -v ON_ERROR_STOP=1 -U trading_migration_admin -d trading_platform < artifacts/rollback.sql
python3 scripts/database_permissions.py audit
docker exec -i trading_system-postgres psql -X -v ON_ERROR_STOP=1 -U trading_migration_admin -d trading_platform < artifacts/reopen.sql
```

Rollback rechecks the original inventory, waits the authentication horizon and requires the new application sessions drained. It explicitly moves reviewed owners back, restores defaults/settings, verifies that the disposable new role owns **no objects and no cross-database dependencies**, removes only its remaining grants, drops that new role, and renames/demotes OID 10 back to the original restricted application. The original credential is assigned after the MD5-sensitive rename. No blanket ownership reassignment is used.

If new DDL or settings appeared after reopening, the frozen rollback inventory deliberately refuses to proceed. Keep the login gate controlled and obtain a reviewed recovery plan; never bypass the guard or run an unguarded DROP OWNED. Once the separated state is accepted, prefer forward fixes rather than returning to the known broken bootstrap design.

## Tests and evidence

```bash
/private/tmp/tuner-fact-lag-venv/bin/python -m pytest tests/test_database_permissions tests/test_scripts -q
```

The PG15 identity fixtures use SQL-only setup/demotion and no system-catalog writes. Tests cover ungated/refused and drifted changes, production-shaped self default ACLs, custom SECURITY DEFINER/type ownership, role-setting preservation, CONNECT/TEMP repair, credential preservation under SCRAM and MD5, complete rollback on postflight ACL violation, forbidden bootstrap login/password, pool reconnection and actual native maintenance.

Production inventory and candidate artifacts: `/root/trading_system/.releases/pm-window-89160896/db-root-repair/`. Private local full-schema rehearsal: `/Users/yl/.codex/pm-cutover/20261125-89160896/db-root-repair/schema-rehearsal/`. These contain no exported application password verifier; the separate private backup is managed by the coordinator.

Final preparation at **2026-11-25 13:03 UTC** performed only read-only audit/inventory: **1,347 objects audited, zero issues; 1,306 owner objects; six default-ACL groups; 38 application sessions still online**. Four additional runtime relations appeared after the earlier full-schema snapshot and are covered by the existing contract and the fresh frozen plan. The final production artifact files remain gated; no production identity SQL was executed by preparation. The required suites passed **96 tests**.
