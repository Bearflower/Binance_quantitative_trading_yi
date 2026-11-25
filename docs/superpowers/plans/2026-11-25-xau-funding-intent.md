# XAU funding boundary and entry intent implementation

Approved in chat on 2026-11-25. Implement in an isolated worktree; preserve all unrelated dirty files. Use writing-plans, TDD and subagent-driven-development for isolated implementation/review tasks.

Goal: recover genuine prospective XAU evidence at settlement boundaries and classify all three XAU profiles without changing signal, risk, matching or admission behavior.

## Funding task
- [x] Reproduce stale nextFundingTime at receipt using collector and verifier.
- [x] Add bounded current-slot public GET retries. Preserve successful original required-symbol observations and their timestamps if refresh fails. Never save an expired XAU forecast as prospective evidence, fabricate forecasts, backfill or weaken verifier.
- [x] Tests: refresh success through real verifier; still expired; fetch failure; slot deadline; malformed response; other-symbol isolation; normal slot no retry; immutable hashes and original timing.
Files: ai_tuner/funding_schedule/collector.py, focused collector tests. No schema or permission changes.

## Classification task
- [x] Add explicit entry_intent (trend/mean_reversion) and entry_route_fallback at actual stationary strategy branch in both strategy classes; no decision changes. Original and calibrated share original class.
- [x] Save classification in existing feature_snapshot; old/missing/invalid fields become unknown, never infer from market_state.
- [x] Carry label through pending/rejected orders, filled positions and all exit legs; aggregate closed positions once.
- [x] Report per profile and intent: candidate, risk reject, virtual orders, fills, pending cutoff, fill rate, closed positions, net PNL, fee/funding/slippage. No independent 300 USDT accounts or subset admission. Overall metrics stay identical.
- [x] Test all three profiles, real fill/partial/full-close and pending paths, legacy unknown, immutable event handling and PNL/metric conservation. Render JSON and both markdown formats.
Files: strategies/btc_eth{,_aggressive}/strategy.py, strategies/btc_eth/shadow/collector.py, backtest/btc_eth/replay/{engine,report}.py, focused tests.

## Verification and delivery
- [x] Run focused tests before/after; broader shadow, funding and strategy stationary tests; diff check.
- [x] Independent specification and correctness review; fix substantiated findings.
- [x] Record exact results, historical gaps and deployment status. Classification does not reset observation windows; behavior changes require separate version.
- [x] Commit only scoped changes. Prepare reviewable release without touching unrelated production services or deploying unreviewed base changes.

Prior archived funding audit: first settlement matched, one boundary slot invalid (20/21 slots); historical evidence remains insufficient. Dates 12/15 and 12/23 are review windows, not automatic admission.
