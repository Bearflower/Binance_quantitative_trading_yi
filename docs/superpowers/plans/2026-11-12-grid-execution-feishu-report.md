# Grid Execution Feishu Report Implementation Plan

> **For agentic workers:** Implement inline in this session. The production executor remains independent of notification delivery.

**Goal:** Send one additional Feishu message for each confirmed grid fill, showing the trade, current net position, current grid epoch PnL, and cumulative executor grid PnL on the existing grid webhook.

**Architecture:** A separate read-only notification worker polls the durable execution journal. It matches each owned fill by order and trade ID against PM `userTrades`, reads the PM position and funding history, stores a durable notification record, and reuses `WebhookSingleSender`. No signal text, grid-rule, or order-execution code changes.

**Tech Stack:** Python 3.11, SQLite, existing `GridExchange` and `WebhookSingleSender`, pytest, Docker.

---

### Task 1: Exact report model

**Files:** Create `strategies/grid/execution/notification_report.py`; test `tests/test_grid_execution/test_notification_report.py`.

- [ ] Write a failing test for signed realized PnL, negative commission expense, funding, net position and phase/cumulative totals. A fee with a non-USDT asset must fail closed instead of silently converting.
- [ ] Run `python3 -m pytest -q tests/test_grid_execution/test_notification_report.py` and confirm the missing module failure.
- [ ] Implement pure `Decimal` parsing, totals and a Chinese text renderer. Label the position as the snapshot at notification time; show a flat position explicitly.
- [ ] Run the focused test and commit the model.

### Task 2: Durable PM fact matching and notification cursor

**Files:** Create `strategies/grid/execution/notification_worker.py`; test `tests/test_grid_execution/test_notification_worker.py`.

- [ ] Write failing tests proving only journal-owned `fill` rows qualify, matching uses both exchange order ID and trade ID, stale/missing PM facts do not produce a report, and a successful send cannot repeat after restart.
- [ ] Implement a separate SQLite notification database under `/app/data`, with one row per `(order_id,trade_id)`, persisted PM realized PnL and commission, and a `SENT` marker. Do not alter execution schema 5.
- [ ] Reuse `GridExchange` read methods and `WebhookSingleSender`. Query PM position and `FUNDING_FEE` income in complete bounded windows. Treat incomplete PM income as unknown PnL and retain a pending report.
- [ ] Run focused tests and commit the worker.

### Task 3: Deployment and verification

**Files:** Update `docs/deployment/grid-order-executor.md`; add a scoped notification deployment record.

- [ ] Run `python3 -m pytest -q tests/test_grid_execution` and inspect the code diff.
- [ ] Build a notification image from the reviewed commit and verify its `VERSION` and key source hashes.
- [ ] Check the current production pin and existing executor account/health before adding one isolated notifier container; keep all existing containers unchanged.
- [ ] Configure the same `FEISHU_WEBHOOK_GRID` via root-only secrets. Start the notifier and check health, durable cursor, and absence of fabricated historical grid fills.
- [ ] Recheck executor health, positions and orders. Record exactly which live notifications could be observed; do not claim a grid fill notification until an owned fill occurs.
