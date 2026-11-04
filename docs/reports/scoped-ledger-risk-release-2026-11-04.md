# Scoped ledger/risk release — review draft, activation blocked

This change implements a separate branch inside the existing deployment workflow/runtime. It has not deployed anything. Production evidence is intentionally absent in the checked-in manifest draft. The parent must rebase onto the current main baseline, refresh the draft and independently review complete production evidence before activation.

## Scope and order

Seven `remediation-<full SHA>` images are built or reused by digest; `latest` is not published by this mode. Eight services are selected: collector, HRS, aggressive, original BTC/ETH, new coin, data backend, dashboard API and tuner. Grid has no policy consumer in this scope: its candidate was removed, and its runtime remains excluded and unchanged.

The existing remote deployment lock covers preflight, pulls, backup, activation and gates. The scoped branch exits before funding secret preparation, broad cleanup, generic init SQL or broad Compose up. Live base/override/logging files are never uploaded in this mode. Reviewed per-service stacks and an image-only overlay preserve runtime settings. Exact collector command/health/journald restoration is the only approved non-image difference from its running container. Full running container Config/HostConfig fingerprints and the complete Compose project container inventory are required.

Before stopping anything: verify configuration/source/container identities, pull and inspect all seven images, capture full database dump and logs, preserve old image tags, observe exchange protection and allocations, and persist the active release pin. Stop only old tuner and collector. Apply publication, policy and income-immutability migrations in that order. Activate four policies with CAS. Start compatible collector and require fresh successful heartbeat/integrity; start strategies individually with no dependencies; then consumers; tuner only after freeze verification. Final publication checks Beijing August/September/October windows against independently audited net amounts −115.45319304, −6.65665904 and +5.80965338 USDT, then checks actual dashboard HTTP monthly responses against those same report IDs/net values. Unknown strategy attribution remains explicit and allocation ranking stays frozen.

## Safety gates

- Every image and running target is checked for VERSION full SHA and selected module hashes. Running consumer imports must resolve DataService to the canonical ledger reader. Strategy gates require successful startup log markers and fresh exchange protective coverage; a trivial Docker healthcheck is not sufficient. HRS initialization includes its state/order restoration. Startup logs are preserved for independent review.
- PM equity uses `accountEquity`, with `totalMarginBalance` only for non-PM compatibility; available balance is never capital evidence. Activation uses the lower of current/approved equity and each reviewed absolute ceiling. A rise cannot expand the frozen ceiling. The existing private 600 migration password is URL encoded into a subprocess environment; passwords/DSNs are not printed or persisted.
- Split protective stops count distinct remaining quantities, with full-position close stops accepted. Pre/post positions and order hashes are recorded. Any unexplained change blocks completion for independent event/restoration review; there is no automatic cancellation, liquidation or permissive rollback.
- The final gate is **not read-only for report metadata**: it invokes the canonical publisher, which writes verified report rows/active pointers only when its coverage/integrity gates permit. It never writes exchange facts or places/cancels orders. Missing, stale, conflicted or mismatched reports fail the gate.
- Active/activating pins block later generic deployments both before live Compose upload and in the locked runtime. A failed or completed existing release state cannot blindly replay. Recovery requires a reviewed successor/recovery procedure; automatic resume/rollback is deliberately unavailable. The old collector must never restart against strict immutability schema.

## Required remaining preflight evidence

1. Rebase/fast-forward main baseline, then run `python3 scripts/prepare_ledger_risk_manifest.py --base <full-reviewed-base-SHA>` in the final source tree. This produces only a blocked draft; review its exact changed-file allowlist/checksums before commit. Do not use stale hashes after further edits.
2. Full current production base/override/logging hashes; explicit per-service stack/effective config hashes; **every** Compose project container ID/image/StartedAt/Config+HostConfig fingerprint and required source hashes. The November 4 07:05 full deploy and 07:21 HRS-only restoration invalidate older observations. HRS restored image is `sha256:8d01db084443a8a5cd5d98efb1c8fbd75df84fbff6244faa4a6f82a56c5073db`; refresh it rather than assuming it still holds.
3. Collector exact before fields and approved after restoration; full disk/backup capacity, NOSUPERUSER application role and existing private migration-admin secret. No new undefined env file is required.
4. Verified account ID, four reviewed policy candidates/revision/expiry/reason/absolute ceilings and contemporaneous equity; historical account/window/source-proof SHA256 for all three independent exchange reconciliations. No guessed account or evidence hash is present in the draft.
5. Clean-checkout seven-image build/import proof and independent review. No local Docker builds or live activation were performed here. Collector health may wait up to 30 minutes for first successful collection.
6. Live postflight review of startup logs/protection and any legitimate order or position transitions. A changed order ID/hash can conservatively block despite successful protection restoration; the runtime will not silently approve that difference. The separate 48-hour reliability observation remains outstanding.

BTC/new coin still have their documented legacy final-price/concurrency limits; this deployment does not add reservations or alter their existing add rules/holdings enforcement. Frozen exposure ceilings are not withdrawable cash.

## Local verification

- `python3 -m pytest tests/test_scoped_release tests/test_scripts/test_render_remote_deploy.py tests/test_scripts/test_execution_facts_deployment.py tests/test_scripts/test_allocation_reconcile_deployment.py tests/test_allocation_remediation -q --tb=short`: 121 passed, 2 explicitly skipped PostgreSQL allocation tests without `ALLOCATION_TEST_DSN`.
- `RISK_TEST_DSN=postgresql://yl@127.0.0.1:55439/risk_tests python3 -m pytest tests/test_risk tests/test_allocation_remediation/test_entry_caps.py tests/test_scoped_release -q --tb=short`: 143 passed, including real PostgreSQL four-candidate CAS/ceiling checks.
- Python compilation and `bash -n scripts/remote_deploy_runtime.sh` passed.

These are local unit/integration/command-contract tests, not a Docker/production release simulation or proof that all external failure branches are covered. All ordered release steps have injected-prefix failure assertions; command-level tests verify target/no-deps/migration checksum behavior and active/partial replay blocking. Full seven-image and live application verification remain required.

Review follow-up: image proof now explicitly inserts `/app` before shared imports, covering dashboard's `/app/dashboard/backend` WORKDIR without PYTHONPATH. A subprocess regression reproduces that runtime shape. The production `main_docker.app` ASGI test confirms `/api/trend` resolves to `api.routes_docker.get_trend`, returns `data.trends`, preserves ledger identity/coverage/unknown fields and ignores the obsolete mutable cache. The unrelated legacy `routes.py` was not changed. The updated affected suite passed 121 tests with 2 explicit DB skips; a sandbox-only pytest cache-write warning did not affect results.
