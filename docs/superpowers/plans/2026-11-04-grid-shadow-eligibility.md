# Grid Shadow Eligibility Evidence Implementation Plan

> **For agentic workers:** Implement inline with tests first; keep production in shadow mode.

**Goal:** Make reference freshness and research-eligible time measurable without treating missing history as valid evidence.

**Architecture:** Extend the existing one-second queue checkpoint with reference and feed state, retain it for the prospective 30-day validation window, and add a read-only report. Existing checkpoints receive an explicit UNKNOWN state during migration. Update the hourly monitor to check effective reference age and distinguish expected STALE from synchronization failure.

**Tech Stack:** Python, SQLite, pytest, Codex heartbeat automation.

---

### Task 1: Persist prospective eligibility evidence

**Files:** `strategies/grid/realtime/reference_store.py`, `strategies/grid/realtime/queue_audit.py`, `strategies/grid/realtime/main.py`, `tests/test_grid_realtime/test_queue_audit.py`

- [x] Add a failing migration test: v2 checkpoints become UNKNOWN, and a fresh checkpoint stores VALID/STALE plus feed and storage health.
- [x] Run the targeted test and confirm the expected failure.
- [x] Upgrade schema to v3 through the existing migration path; add status and health columns to the checkpoint. Keep existing rows UNKNOWN.
- [x] Pass the reference status and health flags from the realtime tick into the checkpoint; retain checkpoints for 35 days.
- [x] Run the targeted tests and verify the write path.

### Task 2: Read-only eligibility report

**Files:** `strategies/grid/realtime/queue_audit.py`, `scripts/audit_grid_shadow_window.py`, `tests/test_grid_realtime/test_queue_audit.py`

- [x] Add failing tests for VALID, STALE, NO_ACTIVE_REFERENCE, SYNC_UNCERTAIN, missing checkpoint, queue overflow, and pre-migration UNKNOWN intervals.
- [x] Run the tests and confirm the expected failures.
- [x] Implement streaming interval accounting and count eligible risk events by persisted decision time. Mark incomplete coverage or missing market evidence as insufficient.
- [x] Add CLI requiring both the grid and collector DB, opened read-only; report sample continuity and aggregate-trade ID continuity from retained data or prospective persisted attestations.
- [x] Run targeted tests and CLI smoke tests.

### Task 3: Acceptance protocol and monitor

**Files:** `docs/research/grid_realtime/`, heartbeat automation `shadow`.

- [x] Document the current data inventory, the new evidence start time, and that old status history cannot be recreated.
- [x] Prepare a preregistration form for alert burden, usable coverage, false-positive limits, and regime event counts. Do not mark it frozen without a user decision.
- [x] Update the existing heartbeat to compare `effective_at_ms` with `reference.max_age_seconds`, report status transitions and only treat VALID healthy periods as research-eligible.
- [x] Verify the saved automation and its notification policy.

### Task 4: Release verification

- [x] Run focused tests, schema migration test, and read-only report against a fixture.
- [x] Review diff and capacity impact; confirm mode remains shadow and research reminders remain disabled.
- [ ] Commit isolated changes; if released, check deployment logs, migration version, active checkpoint, and outbox boundary before considering work complete.
