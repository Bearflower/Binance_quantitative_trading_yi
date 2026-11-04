# Income ingestion integrity and HRS source preservation

Income identity remains `(account_id, income_type, tran_id)`; fill storage semantics
are unchanged. Existing normalized income facts and income raw observations become
immutable. Equivalent retries retain the first fact, raw body, hash and fetch time.
Known numeric wire fields compare numerically without rounding long identifiers;
unknown raw fields retain JSON semantics. Different income types may share a tran ID.
Substantive differences (including symbol, time, amount, asset, info, trade linkage or
unknown raw fields) reject the entire page. Exporter deduplication includes semantic
payload identity so same-key contradictions reach the sink rather than disappearing.

The writer takes an account-scoped PostgreSQL session advisory lock on one leased
connection. It commits a pending attempt containing all incoming normalized/raw
observations before starting the atomic fact/raw transaction. Success completes that
attempt in the fact transaction. P7501/P7502 conflicts roll back the page, then append
old/new database evidence and transition to conflicted in a separate transaction.
Infrastructure exceptions remain pending rather than being labeled economic conflicts.
Cancellation, connection death, or failed quarantine writing also leaves the durable
pending latch. Later successful pages or cursor writes cannot resolve older attempts.
Attempt transitions are audited; evidence and event history are append-only. There is
no automatic or operator resolution command in this release: investigation and an
explicit reviewed resolution workflow are future work.

The publisher takes the same session lock *before* starting its repeatable-read
transaction. It checks pending/conflicted attempts before either refreshing or serving
a fresh cached report. It cannot slip into the rollback-to-quarantine gap. A previously
verified matching report may be returned only with `publication_status=stale` and an
integrity error; without a prior report the error propagates. Missing gate tables or
permissions fail closed. Account totals and attribution logic are otherwise unchanged.
This guarantee depends on all income writers using the new sink; direct privileged
SQL is not an authorized ingestion path.

Migration: `database/postgres/migrations/2026-11-04-income-immutability.sql`, applied by
migration administrator after the existing execution-facts schema. Runtime requires
only the explicitly granted attempt/conflict operations; trigger audit insertion uses
a fixed-search-path security definer. Original fact table grants remain, with immutable
triggers preventing contradictory update/delete. Stop the old execution-facts collector
before applying the migration, then start the new collector from the same release.
Never roll back fact/audit tables or erase unresolved gates to roll back an image.
Deployment itself is outside this implementation task.

Preserved current production HRS deltas from the 2026-11-04 source extraction:
`shared/market_cap.py` adds exactly three post-wait rate-limit checks (six lines), and
`strategies/hrs/strategy.py` adds the three-line market-cap preload before LV-RM scan
semaphore creation. Existing entry-risk gates remain. Market-cap service source now
matches the extracted production file byte-for-byte; the strategy file was not replaced.
Collector shared/scripts source required no production-overlay preservation changes;
new ingestion edits are intentional remediation. Runtime logging/heartbeat preservation
belongs to the later deployment work.

Validation: 283 affected tests pass with explicit local INGESTION_TEST_DSN,
LEDGER_TEST_DSN and ALLOCATION_TEST_DSN, each naming its own isolated database. No
production environment fallback is used. New ingestion helpers and the complete
publisher reach 100% statement/branch coverage. Tests include real DatabaseManager,
NOSUPERUSER runtime role, migration rerun, rollback/quarantine race, cancellation,
connection loss, identical retry, differing content, same ID across types/accounts,
failed audit, failed pending insertion and frozen cached publication. HRS tests verify
post-wait HTTP suppression and preload-before-symbol processing.

An unrelated expanded regression, `test_new_coin_weekly_review_queries_beijing_monday_midnight`,
expects the current Monday whereas the unchanged strategy queries the previous week.
It remains a pre-existing failure; no weekly-review business logic was changed here.
