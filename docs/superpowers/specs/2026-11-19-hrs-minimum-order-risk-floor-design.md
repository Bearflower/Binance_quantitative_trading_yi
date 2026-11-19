# HRS minimum executable order risk floor

User authorization: change and relax HRS risk budgets so that an exchange minimum executable order fits. Prior authorization covers offline verification followed by direct production rollout, no shadow stage and no routine approval interruption. This replaces the former hard 1% single/3% portfolio ceilings for minimum-size exceptions. Other entry gates remain effective. No artificial orders, cancellations, position changes, allocation edits or guessed historical evidence.

## Budget rule

A new explicit budget version `hrs_minimum_executable_v1` is enabled only by an audited HRS policy whose JSON reason declares `hrs_risk_budget_version`. Unstructured historical policy reasons retain strict percent behavior. Unknown declared versions fail closed. Activation creates HRS revision7 from revision6, preserves multiplier/ratio_cap/entry_ceiling/allow_add/freeze_allocation/expires_at and other strategies, and appends the existing immutable policy audit. The current account lock and revision CAS protect activation.

For allocation A and known existing pressure exposure R, the baseline budgets remain B1=A*1%, B3=A*3%. Qmin is the least exchange-lattice quantity satisfying entry minQty/step/minNotional and a fully executable TP1; existing small-order full-TP1 fallback is preserved. Fees and adverse stop execution are included in per-unit loss L. M=Qmin*L. Effective single S=max(B1,M), effective portfolio P=max(B3,R+M), and the entry risk bound is min(S,P-R). This only raises exposure enough for one minimum executable order when ordinary budgets do not fit. Preserve old behavior by default; do not reinterpret old records.

Inputs must be finite, protective and executable. Insufficient actual remaining margin, unknown risk/protection, invalid metadata, ownership, slot, funding, cooldown or loss gates still deny. Do not manufacture an order or raise account margin allocation. New audit evidence records budget version, original/effective limits, Qmin/M, actual pressure loss and full assessor inputs. Do not describe exceptions as still capped at1%/3%.

## Lock and submission

Before insertion, the existing account advisory transaction lock re-reads active policy, full allocation, full HRS pending reservations and verified local positions. Existing risk counts the maximum local/reserved risk per symbol, not duplicate exposure, and includes all pending symbols. Unknown pressure risk blocks the floor path. Existing full-account margin plus durable margins remains conservative. Re-run the same assessor using actual remaining account cap and a quantity ceiling equal to the pre-lock plan; refinement can decrease quantity but never increase it. Persist refined quantity and pressure risk, return the committed refined decision/audit to live execution, and use that exact decision for the final HTTP guard. Legacy consumers remain unchanged.

## Replay, acceptance and rollout

Live and replay derive mode from the captured full policy reason and share the same assessor. Acceptance re-computes new-version decisions from original captured inputs; legacy1% checks remain for old records. New minimum quantity/filter/exit geometry and reservation context must be source-bound, and tampering or unknown versions must block. Register a new prospective experiment revision before new-regime inputs; do not rewrite v1-v4 or infer Aug-Sep gains.

Offline verification includes the actual denied05:22 and09:21 inputs with authentic exchange filters, entry/TP quantity rounding, both directions, full TP1 fallback, insufficient margin, invalid metadata, pending reservations, allocation/policy races and final authorization. Re-run full CI with all market-cap provenance tests retained. Controlled deployment updates only needed HRS services, reuses the current verified hub, checks runtime permissions without grants, activates audited revision7 after code is available, refreshes actual equity evidence and verifies gate/writer/capture/protection. Update the read-only daily inspector's expected audited HRS revision without sending a manual message. Keep performance acceptance incomplete.
