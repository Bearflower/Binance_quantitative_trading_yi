# Second-stage recovery after HRS startup

This is a separate default-dry-run recovery entrypoint, not a modification or replay of the first manual recovery. It is narrowly bound to d77c2328, the original manifest digest, and the first recovery's exact failed / ReleaseBlocked state with changed services `[execution-facts-collector, hrs-strategy]`. Parent confirmed the first process exited after the Unicode startup-marker timeout. Policy activation, collector and HRS recreation are not repeated.

Run the reviewed checkout's `python scripts/recover_after_hrs.py --root /root/trading_system --bundle ORIGINAL_BUNDLE --approval PRIVATE_APPROVAL.json` to validate. Only explicit `--execute` continues. Both hold the existing deployment lock. No production execution was performed by the implementer.

## Approval and audit

Approval JSON has `reason`, `artifacts`, `code`, `pin_sha256`, `containers`, `signed_evidence_path`, and `order_review`. Keep it private (0600). `artifacts` binds raw SHA256 for the following original-release-relative files:

- state.json, manifest.json, images.json, preflight-containers.json, preflight-proof.json, schema-proof.json
- manual-recovery/state.json, manual-recovery/policy-activation.json, manual-recovery/policy-proof.json, manual-recovery/collector-proof.json
- Each of the eight `<service>-effective.json` files

`code` binds raw SHA256 of recover_after_hrs.py, recover_ledger_risk_release.py, scoped_ledger_risk_release.py, ledger_risk_manifest.py, ledger_risk_runtime_config.py and verify_ledger_risk_release.py in the separate reviewed code directory. Do not replace the original bundle. `containers` maps every original project service, including excluded services, to `{id,image_id,started_at,config_sha256}`; config_sha256 is the canonical digest of `{Config,HostConfig}`. Collect this snapshot read-only and review it before approving. Current HRS and collector must be healthy and use their mapped reviewed images; all other containers must retain original identities. The tuner must remain stopped. Compose files and each effective release configuration must remain unchanged.

The script reads active policies and schema through the existing policy probe, then freshly queries signed Binance positions/ordinary/algo orders from the exact HRS container. Approval does not substitute a local JSON file for that fresh query. Original signed position quantity/entry exposure and holdings allocation must remain unchanged. The fresh inventory hashes must agree with the fresh policy proof and approved order endpoint. Startup logs are parsed as JSON events, including escaped Unicode, Docker JSON envelopes and RFC3339 timestamp prefixes; marker text in unrelated fields is not accepted. Error events and tracebacks block. Other strategy startup checks poll for up to 300 seconds for the structured event.

## Explicit missing-old-raw evidence exception

Original preflight retained order hashes but not raw order rows. Therefore this recovery does **not** claim original-versus-current raw equality for MELI/PFE or the account. The independent operator supplied signed evidence captured at 2026-11-04 14:23:09 UTC; its private file raw SHA256 is `d1ebf9dacd301b707c03b506b939183d3d00b737b017fc32a312ffc4c07bf4ff`. `signed_evidence_path` points to its private server copy. The code binds this exact evidence file and checks its current protection and historical transition before refreshing live evidence.

`order_review` must contain:

- `evidence_mode: "missing_old_raw_full_review"`, `missing_old_raw: true`, a substantive `reason`, and `symbols: ["ARIAUSDT"]`
- `position_exposure`, `before_orders_sha256`, `before_algos_sha256` exactly from original preflight proof
- `after_orders_sha256`, `after_algos_sha256` as canonical hashes of the signed file's `ordinary` and `algos`
- `reviewed_position_symbols: ["ARIAUSDT","MELIUSDT","PFEUSDT"]`
- `aria_history` exactly the signed file's `algo_history.ARIAUSDT`

Historical checks allow only these ARIA replacements: canceled stop 2000001576528510 to new 2000001577898163, and canceled TP2 2000001576528538 to new 2000001577898176. They require unchanged symbol/BUY/BOTH/order type/quantity/trigger/market price/closePosition/reduceOnly/workingType/timeInForce/priceProtect/priceMatch/selfTradePreventionMode. Stop trigger is 0.12977 with closePosition true; TP2 is 16 at trigger 0.1068. Exactly those two ARIA orders may be open. The earlier 12-unit TP1 fill occurred before preflight; original exposure is already ARIA -30, MELI -0.06, PFE -3.28. No extra close or add is authorized.

Every current held position must have full stop coverage. All its current orders must have correct side, NEW state, reducing semantics and supported stop/TP type; TP prices must be on the profit side and aggregate remaining TP quantity cannot exceed held quantity. The signed evidence has zero ordinary orders and eight algo orders. MELI/PFE are described only as currently protected with operator-reviewed current order/history evidence; original raw equality is not asserted. The audit explicitly records `old_raw_comparison_performed: false`.

## Continuation and failures

Execution creates `after-hrs-recovery/` once and saves approval, fresh signed inventory and startup evidence privately. It verifies HRS startup without restart, then rolls out aggressive/BTC/new-coin, dashboard consumers, freeze checks and tuner. Final report/dashboard gates remain unchanged. Final order comparison uses only the explicitly approved transition endpoint, while original exposure/allocation remain strict; original capture files and prior states are never rewritten. Further order changes from later strategy startups block and require another independent transition review. No global order-check bypass, automatic rollback, activation replay, migration replay or blind second attempt exists.

Local validation: scoped suite 204 passed, including JSON parsing, exact failure/artifact/code/pin boundaries, new-container service-image mapping, history/price/quantity constraints, missing-raw declaration, and final-gate restoration of the original capture object on failure. The actual supplied signed evidence also passed the pure protection/history verifier locally. These are not claims of live deployment completion.
