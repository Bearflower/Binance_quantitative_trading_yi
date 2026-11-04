# Grid Queue Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve queue overflow evidence across restarts and report whether a 72-hour shadow window has complete queue audit coverage.

**Architecture:** Migrate the realtime SQLite schema to add process, overflow episode and periodic coverage tables. Record the first drop immediately, flush episode totals and coverage on ticks, and mark interrupted episodes uncertain. A read-only CLI reports coverage gaps and affected periods. Archive grid journald logs before container recreation as secondary evidence.

**Tech Stack:** Python 3, SQLite WAL, pytest, Bash, Docker Compose.

---

### Task 1: Durable schema and recorder

**Files:** `strategies/grid/realtime/reference_store.py`, `strategies/grid/realtime/queue_audit.py`, `tests/test_grid_realtime/test_queue_audit.py`.

- [x] Write tests for v1 migration, first-drop persistence, tick checkpoint, recovery, restart.
- [x] Run `python3 -m pytest -q tests/test_grid_realtime/test_queue_audit.py` and confirm failure.
- [x] Add v2 tables and idempotent migration; implement recorder with short SQLite transactions.
- [x] Run the same test and confirm pass.

### Task 2: Runtime integration and read-only window report

**Files:** `strategies/grid/realtime/main.py`, `strategies/grid/realtime/queue_audit.py`, `scripts/audit_grid_queue_window.py`, `tests/test_grid_realtime/test_main.py`, `tests/test_grid_realtime/test_queue_audit.py`.

- [x] Write tests proving overflow and warmup persist, and missing checkpoint coverage rejects research eligibility.
- [x] Run targeted tests and confirm failure.
- [x] Hook recorder into queue eviction and tick; expose CLI JSON with window, gaps, episodes, and eligibility.
- [x] Run targeted tests and confirm pass.

### Task 3: Deploy log archive and documentation

**Files:** `scripts/archive_container_logs.sh`, `docs/deployment/log-retention.md`, `tests/test_archive_container_logs.py`.

- [x] Write a mocked Docker test for grid journald archival and manifest.
- [x] Run test and confirm failure.
- [x] Archive grid journald logs before Compose up, retain checksums, update runbook.
- [x] Run test and confirm pass.

### Task 4: End-to-end verification and deployment

**Files:** `docs/handoffs/grid_realtime_alert_2026-10-08.md`.

- [x] Run relevant pytest and shell syntax checks, plus the CLI against fixtures.
- [x] Review diff and confirm shadow guardrails unchanged.
- [ ] Commit, push, create PR and merge via the authorized deployment path; verify production migration, audit continuity, and mode.
- [ ] Start a fresh 72-hour research evidence window; retain the earlier interval as incomplete.
