# Exchange Income Auto-Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attribute exchange income to a strategy automatically only when order or verified position evidence is unique, while keeping uncertain amounts visible as unknown.

**Architecture:** Keep exchange income/fills immutable. Persist exchange-to-strategy order evidence and a derived attribution table; replay signed fills from a verified flat baseline to identify the sole owner at each funding timestamp. The dashboard shows observed strategy subtotals with an explicit partial label until all facts are covered and attributed.

**Tech Stack:** Python 3.11, asyncpg/PostgreSQL, FastAPI dashboard, vanilla JS, pytest.

---

## File map

- `database/postgres/init-scripts/19-derived-income-attribution.sql`: schema and privileges for immutable order evidence, flat baselines, and derived income attribution.
- `database/postgres/migrations/2026-11-09-derived-income-attribution.sql`: same change for existing production databases.
- `shared/accounting/order_evidence.py`: exact account/symbol/order-to-strategy validation and condition-order-link resolution.
- `shared/trade_logger.py`, `shared/condition_orders.py`: verify existing ordinary/conditional order IDs and account/strategy identity are persisted at order acknowledgement; add missing durable fields at these existing central write points rather than changing every strategy.
- `shared/accounting/position_replay.py`: signed-fill replay from verified flat baseline; funding-time unique-owner query with fail-closed reasons.
- `shared/accounting/derived_attribution.py`: choose income attribution from exact order evidence or verified funding owner, preserving exchange amount.
- `scripts/reconcile_income_attribution.py`: read-only shadow and gated idempotent writer CLI, with machine-readable counts and conflicts.
- `shared/accounting/report_reader.py` and `shared/accounting/economic_ledger.py`: load derived rows and apply identity-checked manual-over-derived precedence.
- `dashboard/backend/services/exchange_ledger.py` and `dashboard/frontend/js/main.js`: expose/display partial strategy subtotal and unknown bucket without calling partial amounts final net profit.
- `tests/test_accounting/test_order_evidence.py`, `test_position_replay.py`, `test_derived_attribution.py`, `test_economic_ledger.py`, `tests/test_dashboard_partial_ledger.py`: focused acceptance tests.

### Task 1: Durable evidence schema

- [ ] **Step 1: Write the failing schema contract test.** In `tests/test_accounting/test_derived_schema.py`, assert that both SQL files contain the three table names, composite income key, unique `(account_id,symbol,order_id)` evidence key, `evidence JSONB NOT NULL`, and that `trading_user` has no `UPDATE/DELETE` grant on derived rows.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_accounting/test_derived_schema.py`; expect missing-file failure.
- [ ] **Step 3: Create both SQL files.** `order_strategy_evidence` stores account, symbol, order_id, strategy, source_kind, source_id, payload SHA, observed_at; `position_flat_baseline` stores account, symbol, flat_at, exchange_qty=0, evidence JSON; `derived_income_attribution` stores account, income_type, tran_id, strategy, symbol, income, asset, event_time_utc, method, evidence, algorithm_version, created_at. Use primary/unique constraints and `INSERT`/`SELECT` grants only for the dedicated reconciliation role; `trading_user` reads them. No `ON CONFLICT DO UPDATE` on economic evidence.
- [ ] **Step 4: Rerun the schema test; expect pass. Commit** `feat(accounting): add immutable derived attribution schema`.

### Task 2: Unique order evidence

- [ ] **Step 1: Write failing `test_order_evidence.py` cases:** a single `(account,symbol,order_id)` owner succeeds; two strategies, missing account, mismatched PM `algoId → actualOrderId`, or amount mismatch return a typed rejection reason. Use the 2026-11-09 split sample shape (6 original/6 aggressive) to prevent symbol fallback.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_accounting/test_order_evidence.py`; expect import failure.
- [ ] **Step 3: Implement pure `resolve_order_owner(account_id,symbol,order_id,trade_records,algo_links,condition_orders)` returning `(strategy,evidence)` or `(None,reason)`. Require exact account match for trade records; for PM links require exactly one algo ID and one local condition strategy. Preserve original exchange IDs as strings and payload SHA. Reuse the strict PM history pagination from `scripts/audit_algo_attribution.py` in the job rather than symbol guessing. Inspect `shared/trade_logger.py` and `shared/condition_orders.py` write paths; persist account, strategy and order/algo ID at their existing acknowledgement points wherever an ID is currently omitted, with tests for each changed path.
- [ ] **Step 4: Rerun focused tests; expect pass. Commit** `feat(accounting): resolve unique exchange order owners`.

### Task 3: Verified position replay for funding

- [ ] **Step 1: Write failing `test_position_replay.py` cases:** flat→owned entry→funding succeeds; partial reduce retains owner; flat ends ownership; opposite-side cross opens a new interval only with new order evidence; unknown opening order, multiple strategies, missing fill coverage, equal funding/fill timestamp, and quantity mismatch all return `unverified` without a strategy.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_accounting/test_position_replay.py`; expect import failure.
- [ ] **Step 3: Implement `replay_position(baseline,fills,order_evidence,coverage_end,exchange_qty)` using `Decimal`, sorted `(trade_time_utc,trade_id)` and signed BUY/SELL quantity. Require a zero exchange baseline, continuous verified fill cursor and final net-quantity equality to the exchange position snapshot. Return immutable owner intervals or named failures. `owner_at(intervals,funding_time)` returns a strategy only for one verified nonzero interval strictly enclosing the funding timestamp.
- [ ] **Step 4: Rerun focused tests; expect pass. Commit** `feat(accounting): replay verified position ownership intervals`.

### Task 4: Income derivation and precedence

- [ ] **Step 1: Write failing `test_derived_attribution.py` and extend `test_economic_ledger.py`:** exact fill/income amount yields order attribution; funding uses `owner_at`; wrong amount, unknown position or non-USDT stays unknown; manual row wins only on matching income identity; contradictory manual/derived strategies raise a conflict and prevent a complete report.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_accounting/test_derived_attribution.py tests/test_accounting/test_economic_ledger.py`; expect failure.
- [ ] **Step 3: Implement pure derivation and reader wiring.** Add `_DERIVED_SQL` in `report_reader.py`, fetch by `(account_id,income_type,tran_id)`, and pass rows to `build_economic_report`. In the report, validate account, symbol, type, amount, asset and event time before using either derived or manual source. Keep account income conservation and existing `strategy_attribution_complete` gate. Raise/report explicit owner conflict rather than silently preferring a contradictory row.
- [ ] **Step 4: Run** `python3 -m pytest -q tests/test_accounting`; expect all pass. Commit `feat(accounting): use identity-checked derived income owners`.

### Task 5: Shadow job and gated writer

- [ ] **Step 1: Write failing CLI tests:** default invocation performs only `SELECT`; `--apply` requires full cursor coverage and a previously verified zero baseline; rerun is idempotent; manual row is never updated/deleted; contradictory existing derived row aborts the whole transaction.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_accounting/test_reconcile_income_attribution.py`; expect failure.
- [ ] **Step 3: Implement the CLI.** Read exchange facts in one repeatable-read transaction; collect exact PM algo links and order evidence; replay by symbol; compute proposed derived rows; print JSON preview with accepted/rejected reasons. First establish a `position_flat_baseline` only when PM position quantity is exactly zero at a captured exchange observation and fill cursors are complete through that observation; active positions wait until their next verified flat. Before publishing a funding owner, reconcile replayed current net quantity against a fresh PM position snapshot. `--apply` inserts only rows whose underlying fact identity and evidence SHA still match, in one transaction. Do not schedule writer yet.
- [ ] **Step 4: Rerun tests; expect pass. Commit** `feat(accounting): add shadow-first income attribution reconciler`.

### Task 6: Honest partial dashboard

- [ ] **Step 1: Write failing dashboard tests:** when `strategy_attribution_complete=false`, response keeps `total_pnl=None`, includes `observed_pnl` and unknown amount; frontend renders `已归属部分` and the observed value, not `期间交易所净收益`; when complete, it renders final net profit. Win rate, Sharpe and drawdown remain null.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_dashboard_partial_ledger.py`; expect failure.
- [ ] **Step 3: Modify only the strategy PnL card label/value in `main.js` and schema/projection as needed.** Keep the account total, unknown count/net/absolute, report ID and stale marker. Avoid replacing null statistics with zero.
- [ ] **Step 4: Run focused tests and existing dashboard tests; expect pass. Commit** `feat(dashboard): show labeled partial strategy income`.

### Task 7: Production shadow and release gate

- [ ] **Step 1: Run** `python3 -m pytest -q -p no:cacheprovider tests/test_accounting tests/test_dashboard_partial_ledger.py`; expect no regression. Run `git diff --check` and check schema migration in an isolated PostgreSQL database.
- [ ] **Step 2: Deploy schema and shadow mode only.** Compare all existing 127 manual rows, 12 proven algo rows and 11 funding rows by exact income key; assert 0 auto/manual conflicts, 0 account-income delta, and report conservation. Export a private reason list for unmatched current events.
- [ ] **Step 3: Activate the writer only after Step 2 passes.** Confirm a second run inserts 0 rows, no manual row changed, and ambiguous funding remains unknown. Check API day/week/month for account total, partial label and completeness flag; leave the trade path untouched.
- [ ] **Step 4: Record counts, before/after report IDs, conservation sums, rejected reasons and rollback command in `docs/reports/exchange-auto-attribution-release-2026-11-09.md`. Commit and push the tested branch.
