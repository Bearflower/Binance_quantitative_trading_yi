# Recovery after independently authorized MELI close

## Scope and evidence

The user separately authorized flattening MELIUSDT. This recovery does not submit or cancel trades. It verifies that specific completed intervention and permits only its recorded economic transition while resuming the still-unfinished scoped release.

The binding chain is: original preflight exposure and order hashes → reviewed HRS ARIA protection reconstruction → independently authorized MELI close → fresh signed current exchange inventory. Original preflight raw orders were not retained. That original evidence gap remains explicit; no claim of original raw-row equality is added. However, complete raw rows exist for both post-HRS and post-MELI snapshots, and this transition requires exact equality for every non-MELI ordinary/algo row and unchanged ARIA/PFE exposure.

The post-HRS private signed file remains bound to SHA256 `d1ebf9dacd301b707c03b506b939183d3d00b737b017fc32a312ffc4c07bf4ff`. The independently acquired post-MELI file, captured 2026-11-04 14:58:00 UTC, is bound to SHA256 `0dbad1540bdd398fcccf19453603c2215bcf1d0735360ff5f19592067bef06cb`. Raw snapshots are private operator evidence and are not committed.

Required exact intervention:

- Original MELI short exposure is -0.06, BOTH. Only this position may disappear; all remaining original position fields remain equal.
- STOP algo 2000001555621647 is FINISHED, linked through actualOrderId 1785285, with zero actual fill, trigger 1719.31 and limit 1721.03 for 0.06.
- LIMIT order 1785285 is BUY/BOTH/reduce-only, CANCELED, executed zero, original quantity 0.06, price 1721.03.
- MARKET order 1803995 has clientOrderId `codex-meli-stopclose-20261104-1`, is BUY/BOTH/reduce-only FILLED for exactly 0.06 at average 1749.30.
- User trade 38450 proves quantity 0.06, price 1749.30, realized PnL -6.7116, and commission 0.04198320 USDT. Duplicate IDs, other trade IDs, unknown/nonfinite numbers and incomplete fills reject recovery.
- Both stale TP algos 2000001555621787 and 2000001555621677 are CANCELED. No MELI ordinary/algo order remains.

The supplied snapshots verify these facts locally. Current remaining positions are ARIA -30 and PFE -3.28, with zero ordinary orders and five algo orders. Each remaining position must still have full stop coverage, correctly sided reducing NEW protection and feasible TP quantities. The original allocation hash remains unchanged. This close's released budget is not redistributed or treated as permission to increase entry ceilings.

## Entry point and approval

Use the separately reviewed code directory:

```sh
python scripts/recover_after_meli.py --root /root/trading_system --bundle ORIGINAL_BUNDLE --approval PRIVATE_MELI_APPROVAL.json
```

This defaults to read-only validation and creates no recovery artifact directory. `--execute` is the separate explicit continuation action. Both modes hold the existing Compose deployment lock and require the prior exact failed state with changed services `[execution-facts-collector, hrs-strategy]`, activating pin, unchanged original manifest/images/Compose and complete reviewed container identities. `after-hrs-recovery/` must not exist (the prior attempt was dry-run only), and `after-meli-recovery/` must not already exist. It cannot replay another partial rollout.

The new private approval object contains four fields:

- `reason`: review/authorization explanation for this specific completed close and continuation
- `recovery_code_sha256`: raw SHA256 of reviewed scripts/recover_after_meli.py
- `signed_post_meli_path`: private server path of the bound post-MELI signed snapshot
- `pre_meli_approval`: complete previous after-HRS approval, still bound to original failed state/artifacts and original signed post-HRS snapshot

Regenerate/review the nested `pre_meli_approval.code` hashes from the new reviewed code set, because recover_after_hrs.py now extracts its unchanged economic-validation block into an overrideable method. Its original CLI behavior and checks remain unchanged. Do not rewrite original release manifests, images or evidence to obtain matching hashes.

Validation uses the existing read-only policy/schema proof plus a fresh signed account-wide inventory and fresh signed GET queries for limit 1785285, market 1803995, order-filtered userTrades, and MELI algo history. Fresh proof and inventory must match the approved post-close endpoint; current fill/cancel evidence must match the audited intervention. API/parse/missing-evidence errors block; no write API is called. A local stored snapshot alone never satisfies the live check.

## Continuation and failure

Execution creates private `after-meli-recovery/` once, records the full nested approval and fresh evidence, then verifies HRS startup and continues the remaining strategies/consumers/freeze/tuner/final gates. It never reactivates policy, restarts the already-upgraded collector/HRS, repeats migrations or places/cancels an order.

Final verification records the original exposure/order hashes, previous HRS review and authorized MELI transition before using the explicitly approved endpoint for comparison. Original artifacts stay immutable; original allocation hash is retained. The in-memory original capture is restored even when the final gate fails. Any other position/order change blocks and requires independent review. No automatic rollback or permissive retry exists.

## Local verification

TDD red tests preceded the verifier, transition and new constraints. The supplied private before/after signed files passed the pure transition verifier locally. All nested exchange-code references were checked against the actual client: get_order, get_user_trades(order_id=..., limit=100), and the existing signed PM GET algo-history route. No credentials or raw evidence were added to source control.

New-module tests include exact fill/cancel/history/cost validation, numeric errors, original exposure limits, code/file/prior-state binding, live allocation/exposure/order/fill drift, read-only GET construction, default dry-run versus explicit execution, final evidence preservation and failures. The new module achieved 100% measured statement and branch coverage (114 statements, 10 branches); external Docker/exchange operations were mocked. This coverage is not live deployment evidence. The inherited scoped/recovery regression suite is run separately in the delivery report. Independent review and operator-controlled live validation remain required.
