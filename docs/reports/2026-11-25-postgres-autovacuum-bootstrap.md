# PostgreSQL autovacuum bootstrap identity incident — 2026-11-25

## Root cause

Production PostgreSQL 15.19 was initialized with `trading_user` as the cluster bootstrap role (OID 10). The permissions contract correctly requires that application account to be `NOSUPERUSER`. Protected tables are owned by `trading_migration_admin` or dedicated NOLOGIN owner roles, and `trading_platform` is owned by `trading_migration_admin`.

PostgreSQL 15 autovacuum initializes its session identity using the fixed bootstrap role OID. Its VACUUM/ANALYZE ownership check uses that role's catalog privileges. Demoting the bootstrap account therefore leaves automatic maintenance unable to process protected objects. This is an identity-design conflict, not missing runtime table grants. The warning's blank user/application fields and once-per-minute batch match the autovacuum worker, not a strategy's SQL.

Primary source evidence:

- [PostgreSQL 15 miscinit.c, InitializeSessionUserIdStandalone](https://github.com/postgres/postgres/blob/REL_15_STABLE/src/backend/utils/init/miscinit.c): bootstrap OID is used for autovacuum startup.
- [PostgreSQL 15 vacuum.c, vacuum_is_relation_owner](https://github.com/postgres/postgres/blob/REL_15_STABLE/src/backend/commands/vacuum.c): table/database ownership or superuser privilege is checked.
- [PostgreSQL 15 VACUUM documentation](https://www.postgresql.org/docs/15/sql-vacuum.html): maintenance privileges and non-FULL vacuum behavior.

Before intervention, protected tables had zero recorded autovacuum/autoanalyze completions. `trading.income_ingestion_attempt` had approximately 131,000 dead tuples. Larger affected relations, including TOAST/index storage, were `hrs.hrs_decision_evidence` (2,655 MB), `trading.hrs_equity_evidence` (1,694 MB), and `trading.income_ingestion_attempt` (1,388 MB). Database frozen-XID age was approximately 17.7 million; this incident had not reached the configured normal 200-million anti-wraparound threshold.

## Authorized non-disruptive mitigation

Use the existing migration administrator for bounded ordinary maintenance. Do not change application capabilities, table owners, catalog OIDs, or database identity; do not restart the database.

The initial permissions audit passed for 1,343 objects, with zero issues and manifest SHA256 `e9066387dc6080a667f80b01176e665e33a46abe650c9e1f01a170b6550d0809`.

The saved maintenance SQL selects existing non-bootstrap-owned user tables with pending modification/dead/insertion statistics, smallest first, and executes:

```sql
SET application_name = 'postgres-maintenance-20261125';
SET lock_timeout = '1s';
SET statement_timeout = '120s';
SET vacuum_cost_delay = '2ms';
SET vacuum_cost_limit = 200;
SET maintenance_work_mem = '64MB';
VACUUM (ANALYZE, SKIP_LOCKED, TRUNCATE FALSE, PARALLEL 0) schema.table;
```

`TRUNCATE FALSE` avoids the tail-truncation exclusive lock. Ordinary vacuum reclaims space for reuse; it does not promise host disk space reclamation. Lock conflicts are skipped/bounded and statement time is capped. This reduces accumulated maintenance debt but does not repair the bootstrap identity; native autovacuum failures can recur when thresholds are crossed again. No autovacuum disabling or log suppression is appropriate.

Evidence directory on the production host:
`/root/trading_system/.releases/pm-window-89160896/server-log-review/`

Evidence includes `postgres-permissions-before.json`, `postgres-maintenance-before.csv`, the exact generated `postgres-maintenance.sql`, execution log, and timestamps. Final observations are appended after execution and the post-maintenance audit.

## Durable repair proposal — requires controlled maintenance window

Separate the cluster bootstrap identity from every application identity. Keep OID 10 as a dedicated `NOLOGIN SUPERUSER` with no password, and give the existing `trading_user` application name a new non-superuser OID with the exact reviewed contract. Do not directly update `pg_authid`/OID values or grant application membership in the migration administrator.

This is **not safe as an uncoordinated live rename**: existing sessions remain bound to their original role OID. Promoting OID 10 while old application sessions exist would promote those sessions too, even after renaming the role. `NOLOGIN` alone does not revoke existing sessions.

Before implementation:

1. Inventory every database, login consumer, role setting, ownership, explicit/default ACL, function owner, RLS policy, and grantor dependency of OID 10. Capture protected backups and prove restoration. Do not print password verifiers or secrets.
2. Rehearse a transaction-based identity migration on an isolated restored cluster, with application and immutable-table regression tests. Explicitly transfer legacy application-owned objects; do not blindly `REASSIGN OWNED`, which also affects cluster/bootstrap dependencies. Reconcile the sole permissions contract and migrations together.
3. Drain/stop all services and pools that can authenticate as OID 10 across the cluster. Close admission and terminate any remaining OID-10 sessions. Prove none remain before granting bootstrap superuser capabilities.
4. In the rehearsed migration, rename the old bootstrap identity, create the application login with a new OID and required credentials/settings, explicitly transfer reviewed application ownership/ACL dependencies, and leave protected owners unchanged. The bootstrap role must be NOLOGIN with password removed. New `trading_user` must retain only the contract's permitted flags and no role memberships.
5. Run `scripts/database_permissions.py audit`, and only contract-permitted missing grants may be filled with `apply`. Test application SQL and immutable boundaries, then reconnect services and observe an actual successful autovacuum/autoanalyze on protected relations before accepting the repair.
6. Rollback must also be rehearsed with sessions drained; role-name rollback alone is insufficient after connections attach to the new OID.

An alternative is a new cluster initialized with a dedicated bootstrap administrator followed by a verified logical restore and service cutover. It has a clearer identity boundary but requires storage headroom and a planned database cutover. Neither option was executed during the non-disruptive mitigation.

## Execution verification

The maintenance batch completed at **2026-11-25 09:24:58 UTC**, with **37 requested VACUUM/ANALYZE commands and 37 successful completions**, zero warnings/errors, and no statement timeout. The three largest relations completed in approximately 108, 72, and 99 seconds respectively.

At 09:25:30 UTC, `income_ingestion_attempt` had 5 dead tuples (concurrent writes after maintenance), down from approximately 131,065. Its pending analyze modifications were 6 rather than 262,130. Both large evidence tables had zero dead tuples and fresh manual vacuum/analyze timestamps. All protected tables still correctly reported no automatic maintenance success; the operation was explicitly manual.

The post-maintenance permission audit passed at 09:25:29 UTC: 1,343 checked objects, zero issues, and exactly the same manifest hash. OID 10 remained `trading_user`, `NOSUPERUSER`, LOGIN; the migration administrator remained unchanged. No sessions were waiting on database locks at the post-check. Saved post-check evidence: `postgres-permissions-after.json`, `postgres-maintenance-after.csv`, `postgres-bootstrap-identity-evidence.txt`, and `postgres-after-maintenance.log`.

This mitigates accumulated maintenance debt. It must not be reported as a completed native-autovacuum fix or a guaranteed permanent end to warnings. Until a controlled identity migration is performed, administrator maintenance must be repeated when protected-table dead/insertion/modification statistics grow; it is not safe to “fix” the warning by re-promoting the application role or suppressing the messages. The initial intervention made no permanent table/storage configuration change. A subsequently authorized, bounded recurring compensation is documented in [the maintenance runbook](../deployment/postgres-maintenance.md); it controls debt while leaving native bootstrap identity repair outstanding.

## Recurring compensation installed and verified

At **2026-11-25 09:38 UTC**, the bounded compensation bundle was installed under `/root/trading_system/.ops/postgres-maintenance/`. `trading-postgres-maintenance.timer` is enabled and active; the oneshot service exited successfully with `Result=success` / `ExecMainStatus=0` and is inactive between runs. The first manual service invocation and immediate initial timer invocation both passed permission audit for 1,343 objects and selected **0 of the 42 scoped tables**, consistent with the completed backlog cleanup. Both recorded zero failures and zero deferred targets. The next scheduled run was 09:43:27 UTC.

The installed script, unchanged permissions-audit tool, permission contract, scope, and both systemd units exactly matched local SHA256 hashes. Script SHA256: `6cdde41345d0e64f76c341ad81e9c63abe626c73184c1fc0d2b4fb9a4b344b64`; scope SHA256: `28414f0399add3d28de153e0a8f0fb53c3e40687758f825a495827700d96ae1f`. The required test suites completed with **85 passed**, including six maintenance tests using actual isolated PostgreSQL.

The production read-only preflight also caught and eliminated a false trigger before timer activation: PostgreSQL does not ANALYZE TOAST tables, so their modification counter cannot be treated like an ordinary table's analyze debt. A regression assertion now excludes that counter while retaining TOAST dead/insertion/age triggers, consistent with [PostgreSQL 15 autovacuum source](https://github.com/postgres/postgres/blob/REL_15_STABLE/src/backend/postmaster/autovacuum.c).

Between the initial maintenance completion (09:24:58 UTC) and the final installation check (approximately 09:38:34 UTC), PostgreSQL emitted **zero** new `only table or database owner can …` warnings. This is a measured observation window, not a claim that native autovacuum is fixed: the timer controls future debt, and warnings may recur briefly before a compensation pass.

Additional evidence in the same directory: `postgres-maintenance-first-plan.jsonl`, `postgres-maintenance-first-service.log`, `postgres-maintenance-first-service-status.txt`, `postgres-maintenance-timer-status.txt`, and `postgres-maintenance-installed-sha256.txt`. No role identities, application privileges, protected owners, database settings, or trading service processes changed.
