# AI Tuner Reduced Trial Evaluation Implementation Plan

> **For agentic workers:** Execute inline in this session; tests precede each production change.

**Goal:** Run daily and weekly evidence-based assessments for HRS and aggressive MTPCS inside AI Tuner without automatic risk changes.

**Architecture:** A focused monitor reads active policy, reservations, exchange positions and protection orders, and canonical exchange-income facts. Pure assessment functions distinguish observed failures from missing evidence. APScheduler invokes daily and weekly jobs; notification state is persisted on the existing AI Tuner logs volume.

**Tech Stack:** Python 3, asyncpg-backed DatabaseManager, ReadOnlyBinanceClient, APScheduler, pytest.

---

### Task 1: Evidence and decision contracts

**Files:** `ai_tuner/monitor/reduced_trial.py`, `ai_tuner/tests/test_reduced_trial.py`.

- [x] Write failing pure tests for policy expiry, missing policy, unresolved reservations, missing exchange evidence, incomplete income attribution, and missing drawdown/thresholds.
- [x] Run `python3 -m pytest ai_tuner/tests/test_reduced_trial.py -q` and verify expected failures.
- [x] Implement evidence-only classification. `REVIEW_CANDIDATE` stays unreachable until the separate versioned performance contract is implemented.
- [x] Re-run tests and inspect full output.

### Task 2: Read-only evidence collection

**Files:** `ai_tuner/monitor/reduced_trial.py`, `ai_tuner/tests/test_reduced_trial.py`.

- [x] Write failing tests with fake DB/exchange for policy, reservation, signal classification, owned positions and STOP coverage, and canonical income completeness.
- [x] Run the focused tests red.
- [x] Implement SELECT-only repository queries, a ReadOnlyBinanceClient dependency, and `read_monthly_economic_report` for daily and weekly fixed windows. Do not use `trade_records.realized_pnl` as net profit.
- [x] Re-run tests green, including missing API/DB evidence fail-closed cases.

### Task 3: Scheduling and notification

**Files:** `ai_tuner/main.py`, `ai_tuner/config.yaml`, `ai_tuner/monitor/reduced_trial.py`, `ai_tuner/tests/test_reduced_trial.py`.

- [x] Write failing tests for daily/weekly schedule registration, weekly Monday window, repeat-send deduplication, and no trading/config/risk mutation calls.
- [x] Add jobs after PnL reconciliation watermarks, use existing logs volume for durable alert state, and send only changed daily alerts plus one weekly report.
- [x] Re-run relevant AI Tuner tests.

### Task 4: Production handoff and proof

**Files:** `docs/reports/strategy-remediation-tracker.md`, `docs/reports/ai-tuner-reduced-trial-release-2026-11-10.md`, scoped AI Tuner release files if the active pin requires them.

- [x] Run full relevant unit tests, syntax check, and diff check; live database contract validation remains pending.
- [x] Read the current production pin and predecessor completion state before any release.
- [ ] Use the scoped CLCI path if an active release pin blocks generic deployment; verify exact image, scheduler jobs, health, one manual dry run, report provenance, and no policy or allocation change.
- [ ] Keep existing Codex expiry patrol until the new AI Tuner alert is proven delivered, then update tracker and handoff.
