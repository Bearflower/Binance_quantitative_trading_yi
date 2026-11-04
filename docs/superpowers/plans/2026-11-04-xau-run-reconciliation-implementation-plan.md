# XAU Run Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a fail-closed, auditable local XAU calibrated replay from all equivalent physical runs, while tracking registration through the calibrated observation end.

**Architecture:** Keep production collector and single-run export unchanged. A local reconciliation module validates frozen calibration evidence and single-run exports, deduplicates identical hourly events, and emits a coverage manifest. Extend only the offline replay engine/report to accept a calibrated profile and to block admission when decision coverage is incomplete.

**Tech Stack:** Python 3, pytest, PyYAML, JSON Lines, SHA-256.

**2026-11-04 implementation status:** Tasks 1–3 implemented in the isolated worktree; targeted replay/export suite passed. Task 4 documentation and four-hour automation reminder updated. Production was queried read-only through 05:00 UTC: 166 rows, 160 distinct hours, 6 duplicates, 4 missing hours (2026-11-02 04:00–07:00 UTC). Historical production exports have not yet been copied to the local machine for a real-data dry run; no online deployment, renewal, restart or database write was performed.

---

### Task 1: Reconcile local run exports

**Files:** Create `backtest/btc_eth/replay/reconcile_runs.py`; create `tests/test_shadow_replay/test_reconcile_runs.py`.

- [ ] Write a failing test using two temporary run directories, one shared hour with different `run_id`/`evaluated_at` but identical decision fields, and one missing hour. Assert one retained event, one duplicate, and the exact missing hour.
- [ ] Run `python3 -m pytest -q -p no:cacheprovider tests/test_shadow_replay/test_reconcile_runs.py` and confirm failure because `reconcile_run_exports` does not exist.
- [ ] Implement `reconcile_run_exports(run_dirs, profile_config_path, calibration_manifest_path, calibration_snapshot_path, start, end, output_dir)` with explicit input validation, SHA-256 checks, canonical JSON comparison excluding only `run_id`, `evaluated_at`, `git_commit`, deterministic earliest-event selection, and half-open hourly coverage. Write `decision_events.jsonl`, `run_manifest.json`, and `coverage.json`; refuse conflicting duplicates.
- [ ] Add failing tests for conflicting duplicate, wrong config hash, wrong snapshot hash, missing event source, and repeated invocation byte-for-byte determinism; implement only the needed checks, then rerun the file to green.
- [ ] Verify that the legacy single-run export tests still pass with `python3 -m pytest -q -p no:cacheprovider tests/test_strategies/test_shadow_export.py`.

### Task 2: Accept calibrated profile in offline engine

**Files:** Modify `backtest/btc_eth/replay/engine.py`, `backtest/btc_eth/replay/main.py`; create `tests/test_shadow_replay/test_calibrated_replay.py`.

- [ ] Write a failing engine test with a calibrated-only run manifest, frozen `xau_calibrated.yaml`, and a candidate event. Assert `xau_calibrated` appears in engine results and unrelated profiles do not.
- [ ] Run that test and confirm failure on hard-coded original/aggressive manifest keys.
- [ ] Add optional `profile_config_path_calibrated` to the engine; build profile config, pilot, state, order, and sequence maps from manifest-declared profile IDs. Reject unknown profile IDs and missing/hash-mismatched YAML. Keep existing original/aggressive constructor calls valid.
- [ ] Change loops and profile risk lookup to use the configured profile map. Preserve original/aggressive same-timestamp comparison only when both profiles are present. Add CLI `--profile-calibrated`; retain existing arguments and semantics for old two-profile runs.
- [ ] Run calibrated test, then `python3 -m pytest -q -p no:cacheprovider tests/test_shadow_replay` to verify old behavior.

### Task 3: Report calibrated metrics and fail closed on decision gaps

**Files:** Modify `backtest/btc_eth/replay/report.py`, `backtest/btc_eth/replay/main.py`; extend `tests/test_shadow_replay/test_calibrated_replay.py`.

- [ ] Write a failing report test showing calibrated-only engine results are absent from JSON and markdown, and a decision gap does not force `insufficient_data`.
- [ ] Run it and confirm the observed failures.
- [ ] Compute `compute_metrics` for each manifest-declared profile; preserve legacy original/aggressive admission keys and add calibrated checks. When `coverage.json` reports missing hours, set admission to `insufficient_data` even if virtual PnL is positive. Include physical run IDs, input hashes, duplicate and gap counts in report metadata.
- [ ] Add CLI `--coverage` to read reconciliation coverage, check its hash/identity against the run manifest, and pass completeness to report; keep existing 1m snapshot gap behavior.
- [ ] Rerun calibrated and full replay tests, then `git diff --check`.

### Task 4: Registration coverage and documentation

**Files:** Modify `docs/superpowers/specs/2026-10-18-xau-mtcps-shadow-pilot-design.md`; modify `docs/superpowers/specs/2026-10-27-xau-mtcps-calibrated-shadow-design.md`; add or update the XAU operator note under `docs/operations/` if present.

- [ ] Replace the one-renewal claim with the actual `max(now + 30 days, existing expiry)` behavior. Document that renewal is repeated until `expires_at >= 2026-12-23 09:00 UTC` and always requires human authorization.
- [ ] Verify the existing four-hour `xau` automation checks renewed expiration against the calibrated end; no automatic registration is added.
- [ ] Read final diff, run `python3 -m pytest -q -p no:cacheprovider tests/test_shadow_replay tests/test_strategies/test_shadow_export.py`, and verify local fixture counts and gap/duplicate identities.
