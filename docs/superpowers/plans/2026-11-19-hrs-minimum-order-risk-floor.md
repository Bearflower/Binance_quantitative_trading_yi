# HRS minimum executable floor implementation plan

> For agentic workers: use subagent-driven-development task by task, with independent spec and code reviews. User has authorized execution and deployment; no new approval gate.

Goal: minimum executable HRS entries fit the risk budget when other hard gates allow.
Architecture: versioned shared pure assessor; audited policy JSON enables it; same assessor in live sizing, locked reservation, replay and acceptance.
Tech stack: Python3.11/Decimal, pytest, PostgreSQL16, immutable GHCR images and scoped production rollout.

## 1. Pure assessor
- [x] Add red tests in tests/test_strategies/test_hrs_min_order_risk_floor.py for entry/TP minimum rounding, single+portfolio floors, fees/slippage, both sides and insufficient margin; strict legacy cases unchanged.
- [x] Extend strategies/hrs/risk_budget.py with explicit versions, policy reason parser, audit inputs, exact minimal executable quantity and dynamic floors. Use /tmp/tuner-fact-lag-venv/bin/python -m pytest -q on new and existing HRS risk tests.
- [x] Independent spec review then code review, resolve findings and commit only owned files.

## 2. Live and locked reservation
- [ ] Add red real-PG and wiring tests for policy7 opt-in, conservative actual account margin, concurrent minimum entries, stale allocation/revision, missing exposure and exact refined submission quantity.
- [ ] Wire strategy.py and shared/entry_risk_policy.py with mode/captured inputs, locked risk recomputation and downsize-only committed audit. Keep legacy path unchanged. Final authorization rejects forged/stale new decisions.
- [ ] Independent spec/code review and combined regression.

## 3. Replay and acceptance
- [x] Test captured policy version selection and shared assessor results against actual denied candidates; test unknown/tampered floor evidence rejection.
- [ ] Wire ai_tuner/replay/adapters/hrs.py and strategies/hrs/acceptance_report.py. Preserve legacy interpretation and evidence closure; add future preregistration without rewriting old windows.
- [ ] Run real-PG export/replay/CLI regression and independent review.

## 4. Controlled policy activation and rollout
- [x] Build reviewable HRS6->7 CAS candidate preserving all financial/margin flags and expiry; verify exact policy reason, existing permissions contract, active claims/reservations and original protection.
- [x] Add tested deployment metadata identifying the changed budget model. Check daily read-only inspector recognizes only the audited new revision. Reuse current immutable hub; no new exchange writes.
- [ ] Full freshPG/Python3.11 CI, immutable image/source/registry/config checks, code deployment then policy activation; actual new gate/writer and protection checks, rollback on initialization failure.
- [ ] Report actual offline minimum quantities/risks, deployment evidence and remaining natural/performance acceptance. Update knowledge/tracking; do not close financial optimization.

## Current execution status

- Pure assessor105 cases and independent exact-oracle review passed.
- Live/account-lock finance review identified a live mark versus older equity-parent mark issue. A new independent-writer inventory receipt is being bound; production is unchanged.
- Replay/acceptance implementation82b831d0 passed346 tests with isolated PostgreSQL; source-receipt followup and final review remain.
- Restricted policy helper tested against PostgreSQL, exact expiry/caps and rollback preserved. Current production permission audit:1328 objects, zero issues, audit only.
- Nine authentic denied signal prices with captured current exchange filters now fit isolated floor sizing; these are feasibility checks, not historical returns.
- Future v5 research is preregistered for November20CST00:00; old windows remain immutable.
- No minimum-order budget production activation or release has occurred yet.
