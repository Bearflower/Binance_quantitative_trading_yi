# HRS mark history recovery

Approved scope: repair missing nonflat risk observations using real closed Binance
MARK_PRICE 1m bars and the already complete authenticated execution/income source.
Do not reset the segment, rewrite old rows, change ACLs, change thresholds, or claim
30 days of qualified performance. Production activation remains outside this change.

The observed defect is a held WLD gap on 2026-11-19 04:20:44–14:53:00 UTC and
another on 2026-11-19 21:15:34–21:22:45 UTC. Effective original checkpoints cannot
certify these intervals. Existing reconstruction can value actual bar observations,
but its timeline contains only old checkpoints and economic boundaries.

## Design

Keep reconstruction in `equity_reconciliation.py` and collection in
`equity_service.py`. A pure planner identifies HRS held episodes inside checkpoint
gaps greater than 120 seconds, using the exact fill path and certified opening
inventory. Fetch only those supporting minute ranges, outside the locked commit,
using GET `/fapi/v1/markPriceKlines`, at most 10,000 unique bars within the existing
30-day risk window. A recovery-only second complete financial capture follows the historical GETs, so
fetch latency does not consume the live observation freshness budget. Responses
retain symbol, exact request, real received time,
raw response and its SHA256. Missing, duplicate, open, future, wrong-source or
wrong-request bars fail closed.

`ReconciliationSource.historical_mark_bars` carries explicit response pages.
Validated bar closes introduce new minute boundary observations. Saved original
captures take precedence. Authenticated flat fill points and exact Shanghai
midnight remain economic boundaries; derived positions use the same owned fill
path and genuine prior closed mark rule as existing boundary reconstruction.
Never construct placeholder original EquityCheckpoints.

When the normal current proofs pass and historical coverage is incomplete, the
existing locked commit reconstructs all observations and activates through the
existing append-only reconciliation receipt and version CAS. Original checkpoints,
income events, allocation flows, opening marked units and segment inception stay
intact; all old rolling and same-day daily latches are OR-preserved. Recovered
intermediate threshold breaches also latch. The receipt names historical recovery
and records bar hashes and original capture provenance. Persist the exact source
input parent before hashing it into the receipt, avoiding an unresolved pre-result
evidence hash. The final projection references that source and the receipt.

After activation, `effective_checkpoints` supplies the repaired timeline. A normal
refresh needs no second reconciliation or history fetch. Incomplete or oversized
source coverage keeps the normal gate closed. Source observations retain the
existing 120-second freshness limit; historical availability is always later and
does not alter when historical data was actually known.

Alternatives considered: a one-off operator reconstruction would recover this
incident but leave the normal service unable to repair future outages. Reusing
fake checkpoints would conceal provenance. The automatic explicit-source path
addresses the cause with the existing persistence contract.

## Implementation plan

- [x] Add failing pure tests for held gaps across midnight, terminal close,
  one-minute real bar recovery, bad/missing/duplicate sources and recovered latches.
  Run `python -m pytest tests/test_strategies/test_hrs_equity_reconciliation.py -q`.
- [x] Add the bounded pure held-gap planner and explicit closed-bar source
  validation/timeline. Reuse `historical_boundary_observation`; preserve original
  capture priority and emit an explicit reconstruction receipt.
- [x] Add failing PostgreSQL service tests proving automatic collection,
  append-only activation, original known-at views, no repeat on the next refresh,
  missing data denial and a resolvable source evidence parent.
- [x] Add service collection outside locks and recovery inside the existing
  complete-source/locking transaction; persist exact source input evidence.
- [x] Run pure/service/runtime-role tests and database permission tests against
  the isolated local UTF8 PostgreSQL DSN. Check CAS conflicts, immutable receipt
  guards and existing late-income compatibility. No schema or ACL changes.
- [x] Replay the exported genuine immutable WLD evidence offline against the
  implementation; preserve actual received times and report the real gate result.
- [x] Inspect the diff and commit only these implementation/tests/spec files;
  hand to root for independent specification/quality review and full CI.

## Verification evidence

The eight equity/ledger/lifecycle test modules passed 141 tests with the isolated
PostgreSQL DSN, including the restricted `trading_user` recovery/CAS/append-only
case. The scripts suite has 31 passes and three existing workflow assertions
failing identically on an untouched HEAD archive. The release branch does not
contain `tests/test_database_permissions`; no schema or permission changes were
made. `git diff --check` passes.

Offline replay against 2,293 authentic original captures plus 642 actual closed
WLD marks produced 2,937 effective points. Baseline reconstruction rejects the
held gap. The implementation reports ready/allowed with `inception_segment` and
`qualified_30_day=false`; marked units and final NAV are identical, and daily and
rolling latches are false before and after. Separate tests prove recovered
intermediate loss breaches and prior rolling/same-day daily latches persist.
This is reconstructed minute-cadence risk history, not observed intrabar or
30-day performance. No production mutations were executed for this work.
