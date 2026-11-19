# HRS natural-entry denial followup

Current production is releasef144eb07/r7. User asks whether entries work and all prior gaps are closed. Previous authorization covers fixing/testing/deploying the approved budget path; it does not change financial margin semantics without the pending user choice.

Observed15:55CST: first12:21 natural PIPPIN326 and EVAA1.4 both passed FLOOR budgets and durable reservations but sent no orders. They remainreserved; subsequent scans fail equity gate outstanding_hrs_reservation. No legacy consecutive-loss/drawdown pause is active. Latest requested market-cap batches are100% covered.

Root cause reproduced from original source: nested planned_budget_decision in the account-lock context is a RiskDecision string. _hrs_budget_json normalizes dataclasses only when top-level; actual strategy passes a raw dataclass nested in geometry, whereas tests passed a pre-normalized mapping. Context revalidation correctly rejects that invalid shape. Fix recursive dataclass normalization without parsing or rewriting old repr/history.

Also fix cleanup for an explicit denied pre-submit attempt: only its matching durable row in reserved state can be released under the account advisory lock; pending claim releases only if no other active reservation exists. Submitting, unknown HTTP, fills and unrelated strategy rows remain untouched. No TTL/blind cleanup or artificial orders. Preserve full final HTTP authorization and source integrity checks. Add meaningful raw-dataclass real-PG and both-side denial cleanup regressions, including race/state-negative cases. Log bounded reason codes rather than silently losing the cause.

Separate financial constraint: current all-account position margin64.85953804557USDT includes ETN49.8626, exceeding HRS26.18 threshold. Account availablebalance448.86029153 does not override this policy. User choice on HRS-owned quota plus account-level available-margin protection is pending; do not infer approval from elapsed time.

Plan: TDDsource/cleanup fixes; independent SPEC thenqualityreview; selected1652+newtests freshPGCI; restricted newimage deployment preservingpolicy7/expiry andallriskparameters; one-time exactintent recovery onlyafter immutable state+two inventories+no workingorders proofs; refreshgate/equity/report and classify any remaining blockers, without forcing a trade. KeepAug–Sepmissing original snapshots, SL/TPwinner and24h/fiveclosed-sample acceptance open.
