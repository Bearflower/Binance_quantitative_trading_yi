# HRS offline slot scope and daily origin audit

This audit follows the November 18 release `441559ae`. It does not change an
existing evidence row, registration, trading parameter, ledger segment or risk
policy expiry. Historical experiment results remain unavailable.

## Slot scope compatibility

The old `hrs_account_occupancy_v1` replay contract counted all account symbols
and retained HRS reservations against `max_positions`. Its observed foreign
holdings also contribute marked margin to the final account margin cap. Those
two calculations are independent.

The authorized replacement counts proved HRS holdings and unreleased HRS
reservations for HRS slots. Foreign holdings still contribute account margin;
unknown ownership must deny entry. A captured runtime scope and original
ownership evidence must select this policy, rather than the replay interpreter's
current code version. Old evidence without that declaration retains its old
semantics. Counterfactual HRS holdings come from the virtual portfolio, while
the real captured ownership proof identifies exogenous account holdings.

The implementation binds the new proof to the original scan/reservation-stage
and claim receipts, the captured policy account, and the original hashed equity
parent. The portal reproduces this proof before replay entry. Original full
account inventory remains available for margin validation. A change of runtime
slot scope requires a separate exported window; old captures are not relabelled.

New prospective registrations can explicitly set
`runtime_admission_scope: hrs_owned_and_unreleased_reservations_v2`. The runner
then requires the opening runtime and every input's occupancy/proof scope to
match before any research or holdout engine run. Its formal report records that
scope and each tier's checked input count. This is a newly approved admission
model; the old baseline commit still identifies its original production
configuration source and does not assert that v2 was deployed at that time.
Existing registrations without the field remain unchanged and report the
admission constraint as `NOT_REGISTERED`. Candidate economics are unchanged.

Kline repair receipts additionally bind the complete old arrays and each actual
public request body. The offline consumer rebuilds the full span with the shared
normalizer and four-hour aggregator, verifies the full-span hashes, and only then
checks the normal final scoring windows. Removing a derived receipt reference
does not bypass the original raw inventory. All availability remains causal.

## Daily origin diagnosis

`loss_gate.evaluate_loss_gate` requires a checkpoint at exactly 00:00 Asia/Shanghai
and complete NAV history from that boundary. The immutable segment's opening
marked equity establishes NAV 1 at its actual activation; it makes no claim about
returns before activation. A current balance or complete month-to-date income
report alone does not establish the missing same-segment midnight NAV.

The checked rollout evidence reports only `daily_origin_missing`, with inception
protection enabled and full 30-day qualification false. It does not provide an
authenticated pre-activation midnight allocation/unit balance, owned inventory
with actual marks, and the full allocation/income bridge into the existing
segment. This audit therefore supplies no basis to insert today's midnight
checkpoint or change the segment inception. No production reads or writes were
performed for this audit.

The existing service processes midnights crossed after the committed state. It
reconstructs inventory from complete owned fills and the certified opening
inventory, includes actual income and allocation flows, and requires real closed
historical mark-price bars for held positions. The enclosing evidence retains
the actual later receipt availability. A flat boundary still requires proof of
the whole fill/income path. The next midnight can resolve this particular gate
only when those facts and every other gate are genuinely satisfied; this is not
a promise of entry eligibility at midnight.

Existing regressions cover refusal to substitute points just before/after
midnight, flat midnight reconstruction with actual receipt availability, and
nonflat historical boundaries requiring actual closed mark bars. No risk limit,
policy lifetime, or natural sample threshold is relaxed.

## Future data conditions

The prospective experiment still requires its deterministic earliest registered
scan boundary, complete causal inputs and funding receipts, and an opening with
the exported cost/protection/MAE proof. Nonflat openings lacking those facts
remain blocked. The original registrations and discovery reports retain their
original source identity. A later discovery report must record the current
runtime identity and cannot turn absent August–September inputs or future
December/January observations into measured performance.

## Verification

The offline/replay domain suite passed **477 tests** on November 18, including
the archive size regressions and generic MTPC paths that forbid HRS imports.
An isolated local UTF-8 PostgreSQL database exercised a `NOSUPERUSER` role with
read-only transactions through actual COUNT/cursor export, archive verification,
and the real engine/experiment CLI for both slot scopes. Those are engineering
fixtures, with six engine runs per scope; they are not production return samples.
The daily-gate/equity-evidence pure regression suite also passed 41 tests. No
production operation was performed by this offline task.
