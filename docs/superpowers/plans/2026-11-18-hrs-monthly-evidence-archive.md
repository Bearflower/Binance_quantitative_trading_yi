# HRS Monthly Evidence Archive Implementation Plan

> **For agentic workers:** Execute task-by-task in the existing authorized worktree. Archive implementation and exporter integration have separate assigned owners; do not spawn additional agents. Use TDD and verification-before-completion.

**Goal:** Convert complete prospective monthly HRS evidence without the legacy whole-window 50k-row/64MiB ceiling, retaining every receipt, original payload hash and actual availability.

**Architecture:** A verified, lossless CAS archive stores bounded receipt/object shards with independently counted source inventories. A private SQLite index provides repeated streaming and per-run queries without reconstructing a whole-window Python list. Existing snapshot sealing, shared scoring, execution assumptions, risk clocks and formal qualification remain authoritative.

**Tech Stack:** Python stdlib JSON/SHA-256/SQLite, asyncpg readonly repeatable-read cursors, existing DataPortal/snapshot_sealer/replay engine.

## Evidence and fixed limits

Root's authorized production read-only measurement for 04:00–04:26 UTC: 870 public-response rows, 48,838,527 bytes of JSONB text (4.4 MB compressed/TOAST). Collector public responses are not filtered or deduplicated before legacy limits. The legacy parser remains capped at 50,000 rows/64MiB.

Archive limits are fixed: 2,000,000 receipts, 256GiB reconstructed logical bytes, 32GiB physical bytes, 8,192 shards; each receipt shard remains at most 50,000 rows and 64MiB logical bytes. No row is omitted to meet these budgets. Repeated JSON subtrees may share objects only if original canonical hashes reconstruct exactly. Receipt identity, run, event/availability time and original payload hashes remain distinct.

## Task 1: Shared archive contract (archive owner)

Files: `ai_tuner/replay/hrs_evidence_archive.py`, independent archive tests.

- [x] Write failing tests for receipt/object shard boundaries, independent expected counts, duplicate identity conflicts, missing/tampered references, global budgets and exact reconstruction.
- [x] Implement `EvidenceArchiveWriter(root, source_metadata=...)`, `write_row(family,row)`, `finish()` and `EvidenceArchive.open(root, expected_manifest_sha256=...)`.
- [x] Implement `iter_records(family, kind=None, run_id=None, path=None, symbol=None, event_start=None, event_end=None, available_before=None)`, `get_record`, `iter_runs`, `counts`, `descriptor` using private temporary SQLite indexes. Do not flatten.
- [x] Use globally unique shard basenames and an inventory filename other than `manifest.json`, preserving current sealer validation.
- [x] Verify archive tests and commit owned files.

## Task 2: Stream the authorized database export

Files: `ai_tuner/hrs_evidence_export.py`, `tests/test_ai_tuner_mvp/test_hrs_evidence_export.py`.

- [x] Add a failing async connection fixture whose `fetch` raises for full source reads and whose cursor yields more than 50k distinct receipts. Independently return the exact COUNT inside the same readonly repeatable-read snapshot.
- [x] Add an optional archive destination to `read_evidence_sources`; keep the legacy branch and its caps unchanged.
- [x] For each unchanged parameterized source scope, execute independent `SELECT count(*) FROM (...)` and stream `connection.cursor(...)` rows into `write_row`. Record query identity, account, true lower/upper bounds and independent count. Source metadata must not derive expected counts from emitted rows.
- [x] Retain bounded reference-hash, scanned-symbol and ledger-point indexes needed by later scoped SQL. Fail and discard an unfinished archive on inventory mismatch or budget error.
- [x] Test late endpoint evidence uses the nominal end through the bounded actual endpoint horizon, without modifying economic or availability timestamps.

## Task 3: Convert records without a whole-window list

Files: `ai_tuner/hrs_evidence_export.py`, integration tests.

- [x] Introduce a verified archive source view alongside legacy JSON, with restartable family iterators, normalized decision records, indexed per-run access and source-hash/time equity lookups.
- [x] Iterate only real scan runs for `_cycles`; background startup/collector runs are never materialized as one month-long group. Funding/metadata receipt queries still read the complete retained source inventory through the verified archive.
- [x] Use a private SQLite bar spool to validate identity/OHLCV conflicts and write the existing ordered `klines/*.jsonl` files. Keep exact per-analysis captured arrays for scoring.
- [x] Validate funding observations per symbol and stream observation output, preserving independent schedule evidence and earliest valid publication receipt.
- [x] Keep source hashes in archive inventory rather than rebuilding a multi-million-entry receipt dictionary in the export receipt. Seal the small archive descriptor plus every archive shard with the existing manifest builder.

## Task 4: CLI raw-boundary verification

Files: `ai_tuner/hrs_exit_experiment.py`, CLI tests.

- [x] Teach raw boundary verification to open the manifest-bound descriptor and verify every archived source/shard before querying candidate starts.
- [x] Recompute full candidate inventory and earliest scan independently, including earliest incomplete/nonflat rejection. Do not accept a boundary list supplied by the writer without raw reconstruction.
- [x] Include archive reader implementation in `engine_identity`; preserve strict official research/holdout identity equality.
- [x] Keep legacy raw JSON snapshots and all existing start/end/28-day/shared-boundary checks working.

## Task 5: Derived execution budgets and monthly shape

Files: exporter, replay data/engine only if necessary; tests.

- [x] Count derived bars/events before preflight and reject above an explicit global execution budget, with actual count in the gap report.
- [x] Set a budget that includes 31 days × 52 symbols × 1,440 one-minute bars = 2,321,280 execution bars, plus trend and decision events. Do not claim arbitrary-universe or constant-memory engine support.
- [x] Avoid redundant monthly raw evidence loads during candidate/tier verification. A streaming archive does not license rebuilding a giant source JSON list.
- [x] If existing event materialization exceeds the supported resource envelope, implement HRS-only iterator/heap scheduling behind a capability, preserving MTPC behavior, or report the precise remaining resource limitation before claiming completion.

## Task 6: End-to-end red/green and documentation

- [x] Generate engineering evidence with more than 50k receipts and more than 64MiB logical source bytes; repeated payloads have distinct true receipt times. Export through archive, seal, run the actual engine, and verify count/hash/time retention. Never label this engineering fixture production evidence.
- [x] Test missing shards, changed candidate inventory, hash mismatches, earliest invalid starts across shard boundaries, funding conflicts and global budget rejection; legacy JSON remains capped.
- [x] Verify v2 preregistered economic baseline/candidates/costs are unchanged. Document archive as a storage/runtime revision, not a retrospective economic or start-selection change; preserve earlier registration/report artifacts.
- [x] Run focused archive/export/CLI tests and full replay/core/scoring/pattern domain regression, `git diff --check`, then commit owned files for independent review. No production API calls, live strategy changes, deployment or service restart.

Verification command:

```bash
/tmp/hrs-test-venv/bin/python -m pytest tests/test_ai_tuner_mvp/test_hrs_evidence_export.py tests/test_ai_tuner_mvp/test_hrs_exit_experiment.py tests/test_ai_tuner_mvp/replay tests/test_strategies/test_hrs_replay_core.py strategies/hrs/tests/test_scoring_engine.py strategies/hrs/tests/test_pattern.py -q
```

## Completion evidence

Archive owner committed `17c3411a`; integration exercised >50,000 receipts and >64MiB logical bytes through actual exporter/sealer/preflight/engine and six CLI cost runs. The 52-symbol, three-interval cursor test verifies 156 guarded source queries, independent nested bar counts and chunking. Generic MTPC and legacy list funding tests prohibit importing HRS modules, first reproduce the regression and then pass. Final full domain command: **416 passed**, including 24 archive tests.

No HRS heap/event iterator was added: the existing engine remains explicitly materialized and bounded to 3,000,000 execution bars / 4,000,000 events. These budgets accommodate the stated 31-day/52-symbol event count; they are not a constant-RAM or wall-clock benchmark claim. Future incomplete source facts and unsupported nonflat openings still block. No production changes or deployment are part of this work.
