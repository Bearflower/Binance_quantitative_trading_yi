# Allocation freeze and capped entry remediation

Automatic allocation publication is disabled at the first step of monthly execution,
daily refresh, and direct `AllocationConfigUpdater.update_all`. The shared freeze
helper deliberately remains true regardless of expiry or DB availability. Monthly
execution does not initialize tables or collect metrics; daily execution does not
fetch NAV. Existing allocated amounts, ratios, configuration and holdings limits
remain unchanged. Released shares remain unavailable cash budget, never redistributed.
The previous completed month and first-three-day retry scheduling are preserved.
Failed or nonfinite/nonpositive account equity cannot fall back to configured capital.

The collector uses one canonical published report for the exact completed CST month,
account, `monthly:{start_ms}` key and `REPORT_VERSION`, with `current=False`. Publication,
coverage, attribution, identity and finite strategy net values must all verify. Net
values include signed commission and funding from the common ledger. Missing evidence
raises `AllocationInputUnavailable`, never fake zero returns or partial rankings.
Even a valid report cannot prove historical strategy capital constancy or owned-fill
activity: the collector currently always rejects ranking with `missing_capital_evidence`
and `missing_activity_evidence`; report ID and verified net diagnostics are inspectable
but are not eligible scores. Old trade-record, HRS special-source, future snapshot and
open-margin denominator fallbacks are removed.

BTC/ETH original and new-coin entry gates require a current `capped` policy and fresh
finite positive `totalMarginBalance`. Their real clients supply the equity provider.
The entry ceiling is the minimum of existing effective allocation, frozen absolute
policy ceiling and live equity times policy ratio cap (activation candidates: 20% and
25%). Equity is fetched before the final policy read, so a policy expiring during the
network wait cannot authorize entry. Missing inputs, DB errors, invalid numbers or
expired policy deny entries. NAV growth cannot increase the frozen absolute ceiling.
The overlay is confined to `can_open_within_limit`; holdings enforcement and its close
callbacks still use the original allocation. Policy `allow_add` is not consumed by
these two strategies: their existing within-budget add rules are unchanged. HRS and
aggressive no-add enforcement is separate.

This phase does not retrofit durable reservations or final optimized-price checks to
original/new-coin execution. Concurrent pending entries and price movement after the
legacy check remain limits of that integration; no strict account-wide atomic risk
claim is made. Legacy float sizing can slightly exceed an exact Decimal budget at a
boundary; such orders are conservatively rejected, never granted an epsilon allowance.
Frozen exposure ceilings are not statements of withdrawable cash.

Reenable requires proven historical capital and owned-fill activity contracts plus a
reviewed candidate/verified/active publication CAS tied to report ID and policy revision.
Removing the freeze alone is not an authorized or safe reenable operation.

Validation uses unit tests with zero-writer assertions and isolated local PostgreSQL
`allocation_tests`. `ALLOCATION_TEST_DSN` must explicitly name that local database;
without it the two PG tests skip. Neither production credentials nor risk/ledger test
DBs are fallback targets. PG exercises actual published-report reader and actual
policy reads under `trading_user` NOSUPERUSER, including expiry while awaiting equity.
Old downstream orchestration tests inject explicit synthetic verified prerequisites;
production freeze/error tests do not bypass the real gate.
