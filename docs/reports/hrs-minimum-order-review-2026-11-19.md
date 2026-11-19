# HRS minimum executable budget review

Scope: changes after54c4643c through561f986f; integration candidate f144eb073c14ae3fdeb7f2d4239eced97aec97e1 has identical runtime source bytes. User explicitly authorizes minimum executable order risk to override baseline single1%/portfolio3%; this is an economic risk-model change, not a claim of equivalence or improved returns.

Independent review checkpoints:

- Pure assessor: specification and code-quality PASS; exact Decimal cancellation and ratio precision defects fixed.105 focused cases plus independent exact-rational comparisons; old STRICT arithmetic retained.
- Live lock/submission: actual monthly allocation, all pending source rows, protected local risk, independent writer financial receipt and current policy/gate are bound. Real mark changes with unchanged inventory quantities/basis are accepted. Final HTTP boundary repeats authorization on the same locked connection.
- Receipt integration SPEC:8/8 changed source/test files manually covered,189 tests passed. Acceptance receipt protection-field mismatch was reproduced then corrected in3450a7fd.
- Integration QUALITY:6 source/5 new integration test files reviewed,100% scoped coverage.107 focused tests passed; latest legacy contract/HTTP/scope/acceptance/capture bundle102 passed.
- Full selected local inventory exposed one legacy decision compatibility regression after1651 passing tests.561f986f restores STRICT default for old decision objects while preserving the original durable geometry guard. Original regression and adjacent live/HTTP tests passed; full local and fresh CI are rerunning. The old incomplete65988564 CI was cancelled, not represented as passing.
- Controlled rollout: audited6->7 CAS preserves exact expiry and all old margin/financial flags; existing policies are immutable and rollback only changes active pointer plus appends audit. Missing-pointer rollback defect reproduced and fixed. Restricted rollout only audits SHA-bound existing permissions; no schema/ACL modifications.53 independent rollout tests plus mocked success/failure orchestration passed.
- Production readonly pre-audit:1328 objects checked,zero issues,manifest57a484abe1826f408ccd3cfc73d134ed18d57c8709c4d3e57bdd2a9a158322fb. Actual existing server tool and contract match the released source bytes.

OCR delegation was used for deterministic file selection and rule resolution; excluded tests/Dockerfile were explicitly reviewed. Latest wrapper/native mismatch was resolved by directly using the already-installed v1.13.13 native binary; no external review LLM endpoint or global install was used.

Unrelated optional legacy risk/script tests have four failures independently reproduced using54c4643c source bytes. They are outside both the former and current scoped CI inventory and have not been deleted or silently added to passing counts. Required original permission/script checks in the original workspace passed54 tests without edits.

Engineering test fixtures, captured-price execution feasibility and future experiment registration do not establish historical performance or financial acceptance. Aug–Sep original snapshot gaps, SL/TP selection,24hour/five natural closed lifecycle acceptance remain open.
