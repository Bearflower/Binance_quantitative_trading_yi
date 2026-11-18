# HRS funding forecast reference v1

## Scope and observed problem

Each successful prospective refresh previously copied every retained forecast into
`trading.hrs_equity_evidence.payload.capture.funding_publication.forecasts`.
The source table already retains those observations once, append-only. Production
read-only observations on 2026-11-18 showed 150 equity rows, 97,486,528 PostgreSQL
JSON-text logical bytes, 16,440,825 compressed payload bytes and 45,813 embedded
forecast copies, versus 388 independent source rows and 319,488 total relation
bytes. A later snapshot grew from 243 to 395 forecasts with the oldest observation
still dated November 2. These are point-in-time measurements, not a growth or disk
exhaustion forecast. HRS local inventory was one position; the two three-position
exchange inventories describe the whole account.

This change removes the cumulative forecast array from **new equity table rows**.
It does not alter old inline payloads, their canonical hashes, allocation, income,
loss thresholds, ownership, pending settlement checks or decision availability.
The once-only immutable initial segment evidence remains as originally captured.
Other repeated capture fields, public responses and monthly income/fill histories
are outside this bounded fix. Full-prefix source reads and hashing remain O(n)
per refresh; this is a storage fix, not a claimed CPU complexity fix.

## Exact reference

`capture.funding_publication.forecasts_ref` has exactly these fields:

```json
{
  "version": "hrs-funding-forecasts-ref-v1",
  "table": "trading.hrs_funding_observation",
  "account_id": "<one exact account>",
  "available_through": "<actual source-read cutoff, UTC aware ISO timestamp>",
  "row_count": 0,
  "forecast_count": 0,
  "order": "available_at,evidence_sha256",
  "ordered_sha256": "<64 lowercase hex characters>",
  "first_key": null,
  "last_key": null,
  "canonical_contract": "hrs-equity-canonical-v1+length-framed-source-row-v1"
}
```

The prefix contains **every** row for this account with
`available_at <= available_through`, with no lower bound. This cutoff is retained
at the real source read after observation persistence; it is not a replacement for
`source_time`, `received_at`, parent `as_of` or source receipt `available_at`.
Nonempty first/last keys are `[UTC available_at ISO, evidence_sha256]`.
COUNT and rows are read within one consistent database snapshot, using fixed SQL
rather than SQL/table names supplied by a reference. Parent availability must be
at least the retained source cutoff.

For every source row, the original complete `payload` must be an object containing
a nonempty `forecasts` list. Its original equity-canonical SHA256 must equal the
source `evidence_sha256`. Every original forecast field remains intact, including
raw source proofs and all clocks. Source, received and durable availability must
be causal. Rows must be strictly ordered by `(UTC available_at, evidence_sha256)`;
conflicting/duplicate identities, wrong accounts and out-of-cutoff rows fail.

For each complete row, equity-canonical encode this object:

```
{account_id, evidence_sha256, available_at: UTC_ISO, payload: ORIGINAL_JSON}
```

Append its UTF8 byte length as eight unsigned big-endian bytes and then its UTF8
bytes to a SHA256 stream. The resulting digest, source row count, sum of original
forecast list lengths and first/last keys must exactly match the reference.
No cumulative digest/ID list is stored. The reference stays constant size except
for decimal count digits. Distinct original receipt/source clocks are retained;
existing source PK deduplication is not changed. In particular, the independently
recorded public timetable and the service's selected/durable source receipts are
different source families and cannot substitute for each other.

## Producer and runtime

The collector first persists individual observations using the existing table,
then independently COUNTs and reads the exact durable prefix in a read-only
repeatable-read transaction. Only that verified source set supplies runtime
forecasts. The existing funding publication evaluator continues receiving full
forecast objects with original clocks. At projection publication, the existing
locked connection rechecks COUNT, original source hashes and the whole prefix
closure, then compares the same evaluated runtime set. The persisted parent
replaces the array with the reference; the original runtime object is not mutated.
Projection expiry still uses the full verified forecasts and is capped by the
next independently observed held-position funding deadline as well as 120 seconds.
A missing reference/array or failed source check never acts like an empty forecast.
Denial evidence without any capture continues to deny new risk normally.

The existing source table is append-only. No migration or main-role grant is
needed. The release reader allowlist adds SELECT on this exact table; the reader
continues to have no source mutation privileges or exchange credentials.

## Consumer contract

`strategies.hrs.funding_reference` supplies:

- `build_forecasts_ref(account_id, cutoff, rows, expected_count=...)` returns the
  reference and a separate full forecast view.
- `resolve_forecasts_ref(ref, rows, account_id=..., expected_count=...,
  collect=True)` validates the complete exact prefix. `collect=False` streams
  validation and returns only counts/digest, without accumulating forecast lists.
- `resolve_evidence_forecasts(parent_row, source_rows, account_id=...,
  expected_count=..., collect=True)` first verifies the **raw original parent**
  canonical hash, then validates/resolves the reference. An inline legacy parent
  remains supported without replacing its stored payload/hash.
- `evidence_forecasts_ref` validates the full raw parent and returns only its
  compact reference, or None for a validated legacy inline parent.
- `ForecastPrefixState.add` / `.reference` implement the same contract in a
  reusable incremental pass; `verify_forecasts_prefixes` validates all ordered
  cutoff checkpoints and the independent maximum-prefix count. Historical
  consumers share this implementation instead of reimplementing the digest.
- `verify_stored_prefixes` reads the maximum prefix once for all already validated
  raw parent references; empty reference sets perform no source query.
- `read_forecasts_prefix` / `load_forecasts_ref` / `verify_stored_evidence` use
  the caller's existing consistent connection, never acquire another lease.

A parent is `{account_id?, evidence_sha256, payload}`. Source rows have exactly
`{account_id,evidence_sha256,available_at,payload}`. External source collections
containing multiple parent prefixes must first select the exact account/cutoff
prefix for each parent and independently count it. Extra rows are not silently
ignored by the resolver. Unknown reference versions/contracts/fields fail closed.
An expanded view must never be passed off as the original parent for hash checks.

Reconciliation streams historical raw parents through a cursor (prefetch 10).
Every consumed parent's full original hash and reference availability is checked
before any source-prefix read. Only compact references and the reconstruction's
actual fields (`inventory`, `independent`, `local`, `marks`,
`historical_mark_bars`) plus original as_of/evidence hash remain in memory. These
are evaluated views, never replacements for stored parents. All referenced
cutoffs are then verified by one shared incremental `ForecastPrefixState`: one
independent COUNT and one fetch of the maximum source prefix, one validation/hash
per source row, and exact count/forecast-count/digest/first/last checkpoints at
every parent cutoff. It does not COUNT/fetch or hash the whole source prefix
again for each historical parent. Single-parent projection semantics are
unchanged. It still reconstructs positions from retained inventory/marks and
actual fills, without changing historical marks or clocks. The acceptance reader
validates the raw parent and closure in its existing read-only snapshot before
using its inventory-based funding proof; missing closure cannot promote unknown
funding ownership. The offline exporter must add a separate
`hrs_funding_observations` source family, with independent COUNT and exact scope,
covering the union of referenced prefixes, and verify each parent prefix against
that family. Existing public `funding_observations` data is not this closure.
The exporter integration is independently owned by the replay agent.

## Validation and limitations

Tests exercise legacy hash preservation, independently mutable derived views,
same body with distinct receipt clocks, constant-size references, exact count,
missing/extra/duplicate/conflicting source rows, bad hashes/order/first key,
wrong account/table/version/contract and parent/source availability conflicts.
Real isolated PostgreSQL tests execute `SET ROLE trading_user` with NOSUPERUSER,
full service refresh/restart, idempotent migrations and no new grants. They also
apply the actual release reader allowlist to a restricted role and prove source
SELECT succeeds while INSERT/UPDATE/DELETE/TRUNCATE privileges remain absent.
Funding expiry stays at its actual deadline; late reconciliation retains normal
advance/restart behavior and refuses damaged parent/ref histories. Instrumented
real-PG late reconciliation proves exactly one historical COUNT/fetch regardless
of parent count (plus the unchanged separate single-projection validation), and
proves a damaged oldest parent's unused field is rejected before any source
query, even when newer valid parents were encountered first. Iterator-counting
pure tests verify each source row is consumed once across many cutoff references,
including repeated cutoffs, and that an invalid early prefix cannot be hidden by
a valid final prefix.

Missing historical source receipts cannot be manufactured. A deleted or changed
source prefix fails closed, and append-only rows referenced by an equity parent
must remain available for audit, backup and export. The fix does not prune old
inline copies, compress arbitrary public responses or erase receipt times.
