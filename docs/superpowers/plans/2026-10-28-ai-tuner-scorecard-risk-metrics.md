# AI Tuner Risk Metrics and Cost-Stress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved daily-equity Sortino, diagnostic Calmar, and 1.5× pathwise 30% capital-loss gate for the offline AI Tuner replay, without enabling production application.

**Architecture:** First make replay equity marks correct at each event timestamp, including all symbols and funding. Then derive complete UTC daily marks and risk ratios from immutable `ReplayResult` values. Finally migrate the cost gate to an unambiguous maximum-capital-loss contract, run three cost tiers from identical frozen inputs, and expose a fail-closed scorecard to the later S6 runner/CLI work.

**Tech Stack:** Python 3.9-compatible syntax, `decimal.Decimal`, existing `ai_tuner/replay` models and engine, pytest. No server backtest, DB write, overlay write, push, or deploy.

---

## Prerequisites and boundaries

- Approved spec: `docs/superpowers/specs/2026-10-28-ai-tuner-scorecard-risk-metrics-design.md` (`725a841`).
- Existing S6 plan: `docs/superpowers/plans/2026-10-17-ai-tuner-mvp-replay-qualification.md`, Tasks 8–10. This plan covers the **risk-metric and cost-gate slice** of Task 8. It does not declare Task 9/10 complete or issue a real `PASS` for either strategy.
- Branch: `codex/ai-tuner-stage2-readiness-plan`. Use local commits only. Keep the primary checkout untouched.
- The current `ai_tuner/qualification/gates.py` uses the ambiguous old `cost_1_5x_capital_preservation_pct` key. Do not run formal qualification with it; migrate in Task 3.
- Real golden trades, HRS historical state, and other uncalibrated thresholds remain blocking. Synthetic tests are framework evidence, not strategy qualification evidence.

## File ownership map

| File | Responsibility |
|---|---|
| `ai_tuner/replay/engine.py` | One equity mark after **all** events at a timestamp; include funding-only timestamps and deterministic start/end marks. |
| `ai_tuner/replay/clock.py` | Add deterministic UTC midnight events after funding, snapshots, and bars. |
| `ai_tuner/qualification/metrics.py` | Pure daily mark extraction, Sortino, Calmar, maximum drawdown, and capital-loss functions. No IO, strategy logic, or thresholds. |
| `ai_tuner/qualification/gates.py` | Ten hard gates; new cost key and strict `< 30` path-loss comparison. |
| `ai_tuner/config.yaml` | Explicit offline-only cost redline value; no runtime fallback. |
| `ai_tuner/qualification/scorecard.py` | Assemble typed metric evidence and BLOCKED_DATA reasons from three replay results; no candidate selection. |
| `docs/superpowers/plans/2026-10-22-ai-tuner-stage2-replay-contracts.md` | Align §5.4 and §8.4 with the approved spec; preserve historical note explaining the former key. |
| `tests/test_ai_tuner_mvp/replay/test_clock_engine_determinism.py` | Timestamp-group and funding-mark regression. |
| `tests/test_ai_tuner_mvp/qualification/test_metrics.py` | Decimal math, daily coverage, ratio edge cases. |
| `tests/test_ai_tuner_mvp/qualification/test_gates.py` | New key, strict redline, missing old-key fail-closed. |
| `tests/test_ai_tuner_mvp/qualification/test_scorecard.py` | Same-input cost tiers, 2× diagnostic, reason propagation. |

## Task 1: Correct timestamp-level replay equity evidence

**Files:** Modify `ai_tuner/replay/clock.py` and `ai_tuner/replay/engine.py`; test `tests/test_ai_tuner_mvp/replay/test_clock_engine_determinism.py`.

- [x] **Step 1: Write failing tests.** Extend the existing replay fixture with two symbols closing at the same timestamp and a funding event between bars. Assert the mark at the shared timestamp equals `broker.mark(ts)` *after both symbols have advanced*, and that a funding-only timestamp has an `EquityPoint`. Assert each UTC midnight in a multi-day window is marked, even without an organic event at exactly midnight. If a position is open without a bar closing at that midnight, assert `DataBlocked` with `daily_equity_gap:<symbol>:<timestamp>`. Assert the first point is the initial equity at `portal.window_start` and the final point is at `portal.as_of`.

```python
def test_shared_timestamp_mark_follows_all_symbol_bars(two_symbol_portal):
    result = run_replay(two_symbol_portal, adapter, broker)
    point = next(p for p in result.equity_curve if p.ts == shared_close)
    assert point.marked_equity == expected_after_both_symbols

def test_funding_only_timestamp_is_marked(funding_only_portal):
    result = run_replay(funding_only_portal, adapter, broker)
    assert funding_time in {p.ts for p in result.equity_curve}
```

- [x] **Step 2: Verify red.** Run `python3 -m pytest -q -p no:cacheprovider tests/test_ai_tuner_mvp/replay/test_clock_engine_determinism.py`; expect new tests to fail against current “first bar wins” / funding-not-marked / missing-midnight behavior.
- [x] **Step 3: Add UTC midnight events.** Define `DAILY_MARK = "daily_mark"` with priority `3` (after `BAR_CLOSE=2`) and `collect_daily_mark_events(window_start, as_of)`. Generate one event at each UTC `00:00` satisfying `window_start < midnight <= as_of`; never read system time. In engine, record the last close time per symbol. On a daily event with open positions, require each open symbol to have a bar closing exactly at that midnight; otherwise raise `DataBlocked` with `daily_equity_gap:<symbol>:<timestamp>`. A cash-only portfolio may mark without a bar.

```python
from datetime import timedelta

DAILY_MARK = "daily_mark"
EVENT_PRIORITY[DAILY_MARK] = 3

def collect_daily_mark_events(window_start, as_of):
    midnight = window_start.replace(hour=0, minute=0, second=0, microsecond=0)
    if midnight <= window_start:
        midnight += timedelta(days=1)
    events = []
    while midnight <= as_of:
        events.append(Event(midnight, DAILY_MARK, "portfolio", len(events), None))
        midnight += timedelta(days=1)
    return events
```

- [x] **Step 4: Implement one mark per complete timestamp group.** Keep `build_schedule()` ordering; group adjacent events by `event.time`, process every event at that time before `broker.mark(ts)`. Add the generated daily events to `events` before `build_schedule()`; they create boundaries but must not call `adapter.intents()`. Do not reorder funding before bar logic. Start with `EquityPoint(window_start, initial, initial, False)`; replace it if actual events occur exactly at start. The end mark at `as_of` replaces any existing mark for that timestamp, never appends a duplicate.

```python
from itertools import groupby

events.extend(collect_daily_mark_events(portal.window_start, portal.as_of))
schedule = build_schedule(events)
last_bar_close_by_symbol = {}
equity = {portal.window_start: EquityPoint(
    portal.window_start, broker.initial_equity, broker.initial_equity, False
)}
for ts, grouped in groupby(schedule, key=lambda event: event.time):
    for event in grouped:
        if event.kind == FUNDING:
            broker.settle_funding(event.subject, event.payload, event.time)
        elif event.kind == SNAPSHOT:
            ctx.portfolio = broker.view()
            adapter.on_snapshot(event.payload, ctx)
        elif event.kind == BAR_CLOSE:
            bar = event.payload
            broker.on_bar(bar.symbol, bar, bar.close_time)
            last_bar_close_by_symbol[bar.symbol] = bar.close_time
            ctx.portfolio = broker.view()
            adapter.on_bar(bar, ctx)
            intents = adapter.intents(ctx)
            if intents:
                broker.submit(intents, bar, bar.close_time)
            _apply_protective_prices(broker, adapter, bar.symbol)
        elif event.kind == DAILY_MARK:
            for symbol in sorted(broker.positions):
                if broker.positions[symbol].is_open() and last_bar_close_by_symbol.get(symbol) != ts:
                    raise DataBlocked(["daily_equity_gap:{0}:{1}".format(symbol, to_utc_z(ts))])
        _drain_broker_audit(broker, audit, event.time)
    equity[ts] = broker.mark(ts)
equity[portal.as_of] = broker.mark(portal.as_of)
```

- [x] **Step 5: Verify green and no replay drift.** Run `python3 -m pytest -q -p no:cacheprovider tests/test_ai_tuner_mvp/replay`; expect all tests to pass. Compare existing deterministic audit sequence assertions; change only equity points, not order intent behavior.
- [x] **Step 6: Local commit.** `git add ai_tuner/replay/clock.py ai_tuner/replay/engine.py tests/test_ai_tuner_mvp/replay/test_clock_engine_determinism.py && git commit -m 'fix(ai-tuner): mark equity after complete replay timestamps'`.

## Task 2: Compute complete UTC daily marks and risk ratios

**Files:** Create `ai_tuner/qualification/metrics.py`; create `tests/test_ai_tuner_mvp/qualification/test_metrics.py`.

- [x] **Step 1: Write failing daily-mark tests.** Use frozen `EquityPoint` fixtures for UTC `00:00` on three consecutive days. Assert `daily_returns()` yields exactly `E_d / E_prev - 1` as `Decimal`; a missing day, duplicate boundary, negative/zero equity, or naive (timezone-less) input returns structured `DataBlocked` reasons. A bar at `23:59` must not stand in for a missing `00:00` mark.

```python
def test_missing_utc_day_is_not_a_zero_return():
    points = (equity_at("2026-10-01T00:00:00Z", "100"),
              equity_at("2026-10-03T00:00:00Z", "101"))
    with pytest.raises(DataBlocked, match="daily_equity_gap"):
        daily_returns(points, start=utc("2026-10-01T00:00:00Z"),
                      end=utc("2026-10-03T00:00:00Z"))
```

- [x] **Step 2: Verify red.** Run `python3 -m pytest -q -p no:cacheprovider tests/test_ai_tuner_mvp/qualification/test_metrics.py`; expect import failure before adding `metrics.py`.
- [x] **Step 3: Implement pure daily-return extraction.** Build exact UTC midnight keys only, require every midnight from the first selected complete day to the last, reject duplicate boundary points and `marked_equity <= 0`, and return `(returns, first_day, last_day)`. Use `timedelta(days=1)` and `Decimal`, never float or implicit forward fill.

```python
return tuple(
    marks[day] / marks[day - timedelta(days=1)] - Decimal("1")
    for day in days[1:]
)
```

- [x] **Step 4: Write failing Sortino / Calmar tests.** Assert `(0.01, -0.02, 0.03)` uses all three days in downside deviation (not just one loss day); `MAR=0`; `sqrt(365)` annualization; zero downside yields `None` plus `no_downside_observation`. Assert max drawdown starts at initial equity and uses **intraday** marks; Calmar with zero drawdown is `None`, while an 8-week value carries `short_window=True` and `diagnostic_only`. The 30-return minimum belongs to the scorecard assembly in Task 4, not the standalone `sortino()` math function.

```python
downside = (Decimal("0.02") ** 2 / Decimal("3")).sqrt()
expected = ((Decimal("0.01") - Decimal("0.02") + Decimal("0.03"))
            / Decimal("3") / downside * Decimal("365").sqrt())
value, reason = sortino((Decimal("0.01"), Decimal("-0.02"), Decimal("0.03")))
assert reason is None
assert value == expected
```

- [x] **Step 5: Implement the metric functions.** Expose `daily_returns(points, start, end) -> (returns, first_day, last_day)`, `sortino(returns) -> (value_or_none, reason_or_none)`, `max_drawdown_pct(points, initial_equity) -> Decimal`, `max_capital_loss_pct(points, initial_equity) -> Decimal`, and `calmar_diagnostic(points, start, end, initial_equity) -> dict` as pure functions. Set a local decimal context precision of 28, serialize only finite `Decimal` values, and do not quantize before comparisons. Calmar uses geometric `(end/start) ** (365/elapsed_days) - 1`; `elapsed_days` comes from the supplied UTC timestamps. Return `None` with explicit reason for undefined denominators.
- [x] **Step 6: Verify green.** Run `python3 -m pytest -q -p no:cacheprovider tests/test_ai_tuner_mvp/qualification/test_metrics.py tests/test_ai_tuner_mvp/replay`; expect all pass.
- [x] **Step 7: Local commit.** `git add ai_tuner/qualification/metrics.py tests/test_ai_tuner_mvp/qualification/test_metrics.py && git commit -m 'feat(ai-tuner): compute daily Sortino and diagnostic Calmar'`.

## Task 3: Migrate the 1.5× cost hard gate

**Files:** Modify `ai_tuner/qualification/gates.py`, `ai_tuner/config.yaml`, and `tests/test_ai_tuner_mvp/qualification/test_gates.py`; modify `docs/superpowers/plans/2026-10-22-ai-tuner-stage2-replay-contracts.md`.

- [x] **Step 1: Write failing boundary tests.** A maximum path loss of `29.999` with threshold `30` passes; exactly `30.000` or more fails; missing new threshold or actual blocks. Supplying only the old `cost_1_5x_capital_preservation_pct` key must not pass. Base `max_drawdown_pct=15` test remains unchanged.

```python
thresholds["max_cost_1_5x_capital_loss_pct"] = "30"
actuals["max_capital_loss_pct_1_5x"] = "30.000"
assert evaluate_gates(actuals, thresholds)["status"] == "FAIL"
actuals["max_capital_loss_pct_1_5x"] = "29.999"
assert evaluate_gates(actuals, thresholds)["status"] == "PASS"
```

- [x] **Step 2: Verify red.** Run `python3 -m pytest -q -p no:cacheprovider tests/test_ai_tuner_mvp/qualification/test_gates.py`; expect new-key test to block under old gate spec.
- [x] **Step 3: Replace the gate contract.** Use `("cost_1_5x", "max_cost_1_5x_capital_loss_pct", "max_capital_loss_pct_1_5x", "strict_max", "fail")`. For `strict_max`, reject `actual >= threshold` on raw `Decimal` values. Validate threshold in `(0,100)` and actual in `[0,100]`; malformed values block. Keep the `FutureDataAccess`-overrides-block precedence.
- [x] **Step 4: Set the explicit cost redline and update the contract.** Add `purpose_driven_mvp.qualification.gates.max_cost_1_5x_capital_loss_pct: 30` to `ai_tuner/config.yaml`, without adding invented values for other uncalibrated gates. In the contract document §5.4 make `sortino` and `calmar` nullable with reason and short-window metadata. In §8.4 replace the ambiguous old key with `max_cost_1_5x_capital_loss_pct=30` and define `max_capital_loss_pct_1_5x < 30` using intraday marked equity; record the old key as a deprecated historical name, not an accepted alias.

```yaml
purpose_driven_mvp:
  qualification:
    gates:
      max_cost_1_5x_capital_loss_pct: 30
```
- [x] **Step 5: Verify green.** Run `python3 -m pytest -q -p no:cacheprovider tests/test_ai_tuner_mvp/qualification/test_gates.py`; expect all pass. Run `rg -n 'cost_1_5x_capital_preservation_pct' ai_tuner/qualification docs/superpowers/plans/2026-10-22-ai-tuner-stage2-replay-contracts.md`; expect references only in deprecation text, not executable gate specs.
- [x] **Step 6: Local commit.** `git add ai_tuner/qualification/gates.py ai_tuner/config.yaml tests/test_ai_tuner_mvp/qualification/test_gates.py docs/superpowers/plans/2026-10-22-ai-tuner-stage2-replay-contracts.md && git commit -m 'fix(ai-tuner): define strict 30 percent cost-stress loss gate'`.

## Task 4: Assemble a fail-closed cost-tier scorecard

**Files:** Create `ai_tuner/qualification/scorecard.py`; create `tests/test_ai_tuner_mvp/qualification/test_scorecard.py`; modify `ai_tuner/qualification/__init__.py`.

- [x] **Step 1: Write failing pathwise loss tests.** Supply `ReplayResult`-style 1.5× equity points `100 → 69 → 105` and assert `max_capital_loss_pct_1_5x == 31`, even though final equity recovered. With `100 → 70`, assert boundary failure. With `100 → 70.001`, assert pass under a 30% threshold. Include funding-only and open-position mark points.

```python
assert max_capital_loss_pct((Decimal("100"), Decimal("69"), Decimal("105")),
                            Decimal("100")) == Decimal("31")
```

- [x] **Step 2: Verify red.** Run `python3 -m pytest -q -p no:cacheprovider tests/test_ai_tuner_mvp/qualification/test_scorecard.py`; expect missing `scorecard.py` or function failure.
- [x] **Step 3: Implement pure capital-loss and tier evidence.** In `scorecard.py`, accept exactly three already-executed `ReplayResult`s keyed `baseline`, `1.5x`, `2.0x`, plus initial equity and matching frozen-input identity. Reject missing tiers, nonpositive initial equity, differing `manifest_ref`, or non-finite equity with structured `BLOCKED_DATA`. Compute the path minimum over *all* `EquityPoint.marked_equity` values, including explicit start and end. Compute `100*(E0-min)/E0`, clamped below at zero. Do not infer 1.5× results from baseline PnL.
- [x] **Step 4: Write and satisfy cost monotonicity tests.** If final equities are `100, 98, 96`, `cost_monotonicity_violation=False`; if `100, 101, 96`, flag `True` and `FAIL` rather than silently ranking the candidate. Keep the 2× tier diagnostic, not a second 30% gate.
- [x] **Step 5: Assemble scorecard evidence.** Combine Task 2 metrics (diagnostic 8 weeks only), baseline intraday max drawdown, Task 3 gate actual `max_capital_loss_pct_1_5x`, and per-tier final/minimum equity. Require at least 30 continuous daily returns; 29 returns, unavailable Sortino, or a missing tier yields an explicit `BLOCKED_DATA` reason. Do not emit formal `PASS` in this module; candidate admission stays in the later S6 runner.
- [x] **Step 6: Verify green.** Run `python3 -m pytest -q -p no:cacheprovider tests/test_ai_tuner_mvp/qualification/test_scorecard.py tests/test_ai_tuner_mvp/qualification/test_metrics.py tests/test_ai_tuner_mvp/qualification/test_gates.py`; expect all pass.
- [x] **Step 7: Local commit.** `git add ai_tuner/qualification/scorecard.py ai_tuner/qualification/__init__.py tests/test_ai_tuner_mvp/qualification/test_scorecard.py && git commit -m 'feat(ai-tuner): build fail-closed three-tier scorecard evidence'`.

## Task 5: Commit-level review and S6 handoff

**Files:** Tests and documentation from Tasks 1–4 only; no production strategy code or overlay changes.

- [x] **Step 1: Run focused and wider regression.** Run `python3 -m pytest -q -p no:cacheprovider tests/test_ai_tuner_mvp tests/test_strategies/test_btc_eth_aggressive_core_equivalence.py`. Record counts and any skips; compare any failures to the pre-change baseline, not to an assumed clean suite.
- [x] **Step 2: Check repeatability and syntax.** Run a deterministic synthetic replay twice and compare canonical scorecard bytes. Run `PYTHONPYCACHEPREFIX=/private/tmp/ai-tuner-scorecard-pycache python3 -m compileall -q ai_tuner/qualification ai_tuner/replay`. Run `git diff-tree --check HEAD~4 HEAD` and `git status --short`.
- [x] **Step 3: Review the Task 1–4 local commit series against the approved spec.** Verify: no float in money math; same-snapshot three-tier input; no future data or missing-day fill; strict `<30` comparison; base 15% gate retained; `2.0x` diagnostic only; no server replay or production writes. Report any unresolved defect with file/line and stop before merge.
- [x] **Step 4: Handoff remaining S6 tasks.** The existing S6 plan still requires true goldens, complete MTPCS/HRS decision wiring, the full same-data runner, CLI, and six immutable reports. Until those and calibrated thresholds exist, any local demo remains `BLOCKED_DATA`/`NOT_QUALIFICATION_EVIDENCE`. Do not push or deploy this plan's commits without separate review.

### Task 5 local verification and handoff (2026-10-28)

- Task 1–4 were completed as a local commit series from `0d2b014` through `49c259f`, with spec and code-quality review at each task boundary. The canonical scorecard regression in Task 5 uses frozen synthetic tier results and separately runs the same 70-day synthetic snapshot through all three cost tiers twice. This checks deterministic bytes, not real-strategy performance; the flat-cash replay correctly remains `BLOCKED_DATA` because Sortino has no downside observation.
- Wider regression: `python3 -m pytest -q -p no:cacheprovider tests/test_ai_tuner_mvp tests/test_strategies/test_btc_eth_aggressive_core_equivalence.py` → **586 passed, 4 skipped**. Syntax check: `PYTHONPYCACHEPREFIX=/private/tmp/ai-tuner-scorecard-pycache python3 -m compileall -q ai_tuner/qualification ai_tuner/replay` → exit 0. `git diff-tree --check 442818c HEAD` and `git diff --check` → exit 0. No strategy production code, overlay, server replay, or production data writes are in this plan's diff.
- This slice computes risk evidence only. The runner must verify full strategy/candidate/parameter/overlay identity and a fresh flat broker for every tier, independently replay fees/slippage against one frozen snapshot, then feed actuals to the existing hard gates. A matching `manifest_ref` in `scorecard.py` alone proves snapshot identity, not full candidate identity.
- Remaining S6 blockers: true MTPCS/HRS golden parity; complete MTPCS market-state, filters, sizing and HRS decision orchestration; same-data three-tier runner and CLI; six immutable reports; real historical coverage and calibration of the other gate thresholds. Neither `AVAILABLE` evidence nor synthetic repeatability is a formal qualification `PASS`. No merge, push, deploy, parameter or overlay activation is authorized by this handoff.
