# Entry-only risk remediation (2026-11-04)

This release adds persisted risk policy and entry reservations. It never changes
`capital_allocation.entries.allocated_amount`, CapitalManager holdings enforcement,
or any holdings-close callback. Frozen exposure ceilings are not withdrawable cash.
The four HRS production hotfix sources were copied before changes; only the HRS
strategy subsequently received policy gates. Executor gates are an additional file.

## Activation and operational state

Apply `database/postgres/migrations/2026-11-04-entry-risk-policy.sql` as the migration
administrator, not trading_user. Existing claim table must already exist. The
migration grants trading_user read-only policy access and reservation DML, creates
no policy automatically, and preserves existing claims. Runtime never performs DDL.
Policy versions and activation audit rows reject UPDATE/DELETE. Activation performs
account-locked expected-revision CAS and commits all requested strategies together.

Run `scripts/activate_entry_risk_policy.py --verified-equity VALUE --reason TEXT
--expected-revision N` with administrator DSN in `RISK_POLICY_ADMIN_DSN`. It is a dry
run unless `--activate` is explicit. It derives ceilings from current active monthly
allocation and explicit verified equity; rechecks allocation during activation; and
makes no exchange calls. Aggressive defaults to monitor_only; `--enable-reduced`
explicitly selects reduced mode. HRS runtime remains monitor_only. Grid receives candidate metadata only; this
release adds no grid runtime gate. btc_eth and
new_coin candidates are capped at 20% and 25%; their CapitalManager entry-only
consumers are now implemented with live equity and frozen absolute ceilings; see
[allocation remediation](allocation-freeze-2026-11-04.md) for scope and limitations.

Default validity is 72 hours. Missing, expired, unreadable, or changed policies deny
new risk. Expiry never resumes trading. Renew by reviewing a new dry run and
activating the next expected revision. Protection and position monitoring continue
regardless of policy expiry. The allocation_frozen helper deliberately always
returns true: reenabling publication requires a separately reviewed verified-report
allocation CAS implementation, not changing a timed pause.

## Reduced aggressive entry

q0 is the original signal quantity, immutable in the reservation. Quantity is
floored to the real stepSize with a multiplier at most 0.5. Risk uses the exact
exchange tick-rounded optimized entry, actual initial stop with configured adverse
stop-limit offset, 10 bps round-trip fees, and 20 bps slippage. These are pressure
allowances, not guaranteed realized-loss bounds through gaps or stop-limit failure.
Actual TP limit prices and the residual stop leg must satisfy exchange minima; no
quantity is rounded up. Grade, stop/TP geometry, effective TP ratios, leverage,
filters, offsets, q0, q, and pressure risk R0/Rq are persisted/checked.

New margin is capped by min(current active allocation, 15% verified live equity,
frozen absolute policy ceiling). Current holdings plus all unclosed reservations
count. One unresolved intent per symbol and pending symbols count toward position
slots. Reservation accounting and activation share an account advisory lock. No
connection is held while snapshot helpers query their pools; current allocation is
read again with the locked connection.

`submitting` is durable before exchange submission. Final policy authorization holds
the activation lock through the entry write. Client IDs are deterministic for a
stable signal identity. Retrying a prepared signal retains q; retrying an already
submitted/unknown intent never resubmits it. Neither timeout nor a TTL frees budget.
A database trigger prevents legacy claim expiry/release writers from clearing
ownership while a reservation exists, including across process restarts.

## Ambiguous fills and conservative limits

Confirmed partial fills survive subsequent query/cancel failures and enter the
existing protection flow. Submission-response fills also survive the first failed
read. Reservation recovery runs independently every position-monitor cycle and at
startup, even when there is no local position or retry marker. Exact returned
symbol/client ID/side plus observed position quantity prove reserved ownership.
It uses the audited original stop and only supplements missing hard-stop quantity;
late additional fills receive additional coverage. Existing protection is retained.

Full original reservation margin remains counted alongside actual holdings until
an exact terminal order AND explicit matching-symbol zero-position response prove
flat. This deliberately overcounts filled and partially filled exposure and reduces
available capacity; it has no accounting gap. Empty/wrong-symbol responses, unknown
orders, transport errors, and unknown legacy pending orders fail closed. PM responses
that omit zero positions may therefore require a future reviewed reconciliation
procedure; there is no automatic TTL release or unsafe manual override here.

For timeout-before-response with no TradeLogger entry, reservation recovery repairs
hard-stop coverage but does not invent software ATR/TP/entry-time state or change
existing legacy adoption semantics. Existing verified recovery remains responsible
for full software adoption; unresolved ownership blocks new reduced entries. This
is a restricted recovery state, not a claim that all ambiguous positions are fully
adopted. Future automated adoption must prove the original execution/time metadata.

## Validation

148 focused tests passed: tests/test_risk plus existing aggressive adoption,
reduce-only, HRS ghost-position guard, and shared order-fill waiter suites.
Risk PostgreSQL tests require explicit `RISK_TEST_DSN=postgresql://yl@127.0.0.1:55439/risk_tests`;
no environment value means skip (34 unit tests pass, 50 PostgreSQL cases skip).
The fixture rejects non-loopback hosts and any database other than risk_tests,
recreates only dedicated risk tables from the current migration, and never uses ledger_tests,
including two-connection budget/same-symbol races, one-connection pool safety,
CAS/audit immutability, durable ambiguous/partial fills, original stop recovery with
later fill growth, runtime trading_user NOSUPERUSER permissions, direct entry gates,
and prepare/retry/final-submit quantity identity. Migration permissions were tested
with SET ROLE trading_user because the local test role is NOLOGIN; no role setting
was altered. No production activation, trade, deployment, or commit was performed.

The entry risk helper has 100% statement and branch coverage: 328 statements,
132 branches, zero missing lines or partial branches, with no coverage exclusions.
Tests include CLI default dry run with zero policy writes, explicit five-policy
atomic activation and mid-batch CAS rollback. Policy time checks use PostgreSQL
clock_timestamp after acquiring the lock: a real two-connection test verifies expiry
during the final authorization lock wait rejects the writer. Slow filter lookup is
completed before final authorization. Coverage execution emits an existing NumPy
module-reload warning; database error assertions check exact SQLSTATE values.
