# AI Tuner MVP Automation Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the weekly decision loop in a 12–16 week time machine, then enable fully automatic tuning for exactly one strategy with verified activation and safe fallback.

**Architecture:** Immutable overlay versions, scorecards, qualification runs, and decisions are stored with code/config/schema/data identities. A deterministic selector filters incompatible or unqualified versions, uses market-regime similarity only among eligible versions, and switches only when the advantage threshold is met. Activation is a two-phase operation: write and point to the version, then wait for the strategy's runtime acknowledgement; failure restores the former pointer.

**Tech Stack:** Python 3.10, PostgreSQL JSONB, existing `ConfigOperator` and `shared.config_loader`, aiohttp scheduler service, pytest/pytest-asyncio.

---

## File map

- Create `database/postgres/migrations/2026-10-17-ai-tuner-mvp.sql`: immutable version, scorecard, run, decision, LLM cache, and activation tables.
- Create `ai_tuner/mvp/repository.py`: parameterized persistence API.
- Create `ai_tuner/mvp/models.py`: version, scorecard, decision, and state-machine models.
- Create `ai_tuner/mvp/llm_cache.py`: deterministic request identity and saved raw response.
- Create `ai_tuner/mvp/selector.py`: compatibility, regime matching, and conservative ranking.
- Create `ai_tuner/mvp/state_machine.py`: keep/switch/pause/resume decisions and cooldown.
- Create `ai_tuner/mvp/time_machine.py`: weekly historical orchestration and baseline comparison.
- Create `ai_tuner/deploy/activation.py`: transactional pointer change and acknowledgement rollback.
- Create `shared/runtime_config.py`: cycle-boundary reload and runtime identity acknowledgement.
- Modify `strategies/btc_eth_aggressive/main.py` and `strategies/hrs/main.py`: use runtime reload only for the selected strategy's manifest paths.
- Create `ai_tuner/scheduler/purpose_driven_job.py`: production weekly loop.
- Create `ai_tuner/monitor/mvp_daily_monitor.py`: hard-breach, activation, pause, and recovery evaluation.
- Modify `ai_tuner/main.py`: schedule and API status for the MVP job.
- Extend `scripts/ai_tuner_mvp.py`: `time-machine`, `activate`, and `status` commands.
- Create `tests/test_ai_tuner_mvp/automation/`.

### Task 1: Persist immutable identities, scorecards, decisions, and cached LLM calls

**Files:**
- Create: `database/postgres/migrations/2026-10-17-ai-tuner-mvp.sql`
- Create: `ai_tuner/mvp/__init__.py`
- Create: `ai_tuner/mvp/models.py`
- Create: `ai_tuner/mvp/repository.py`
- Test: `tests/test_ai_tuner_mvp/automation/test_repository.py`

- [ ] **Step 1: Write failing repository tests**

```python
import pytest
from ai_tuner.mvp.repository import MVPRepository


@pytest.mark.asyncio
async def test_overlay_identity_is_immutable(fake_db):
    repo = MVPRepository(fake_db)
    await repo.insert_overlay_version({
        "strategy_id": "btc_eth_aggressive", "overlay_version": "V20261017",
        "overlay_content_hash": "a", "base_config_hash": "b",
        "parameter_schema_version": "1", "strategy_code_revision": "c"})
    with pytest.raises(ValueError):
        await repo.insert_overlay_version({
            "strategy_id": "btc_eth_aggressive", "overlay_version": "V20261017",
            "overlay_content_hash": "changed", "base_config_hash": "b",
            "parameter_schema_version": "1", "strategy_code_revision": "c"})


@pytest.mark.asyncio
async def test_decision_saves_every_rejected_version_reason(fake_db):
    repo = MVPRepository(fake_db)
    decision_id = await repo.insert_decision(
        selected_version="V2", action="SWITCH",
        candidates=[{"version": "V1", "eligible": True, "reason": "lower confidence score"},
                    {"version": "V3", "eligible": False, "reason": "drawdown cap"}])
    row = await repo.get_decision(decision_id)
    assert len(row["candidates"]) == 2
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/automation/test_repository.py -q`

Expected: FAIL because the MVP repository is missing.

- [ ] **Step 3: Add the database migration**

```sql
CREATE TABLE IF NOT EXISTS public.ai_tuner_overlay_versions (
    strategy_id VARCHAR(32) NOT NULL,
    overlay_version VARCHAR(32) NOT NULL,
    overlay_content_hash CHAR(64) NOT NULL,
    base_config_hash CHAR(64) NOT NULL,
    parameter_schema_version VARCHAR(20) NOT NULL,
    strategy_code_revision VARCHAR(64) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    status VARCHAR(20) NOT NULL DEFAULT 'candidate',
    PRIMARY KEY (strategy_id, overlay_version)
);

CREATE TABLE IF NOT EXISTS public.ai_tuner_scorecards (
    id BIGSERIAL PRIMARY KEY,
    strategy_id VARCHAR(32) NOT NULL,
    overlay_version VARCHAR(32) NOT NULL,
    evaluation_as_of TIMESTAMP NOT NULL,
    data_fingerprint CHAR(64) NOT NULL,
    market_regime VARCHAR(40) NOT NULL,
    regime_features JSONB NOT NULL,
    metrics JSONB NOT NULL,
    gates JSONB NOT NULL,
    conservative_score DOUBLE PRECISION,
    qualification_status VARCHAR(20) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(strategy_id, overlay_version, evaluation_as_of, data_fingerprint)
);

CREATE TABLE IF NOT EXISTS public.ai_tuner_decisions (
    id BIGSERIAL PRIMARY KEY,
    strategy_id VARCHAR(32) NOT NULL,
    decision_time TIMESTAMP NOT NULL,
    action VARCHAR(20) NOT NULL,
    current_version VARCHAR(32),
    selected_version VARCHAR(32),
    market_regime VARCHAR(40) NOT NULL,
    candidates JSONB NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.ai_tuner_llm_cache (
    request_hash CHAR(64) PRIMARY KEY,
    model VARCHAR(80) NOT NULL,
    model_parameters JSONB NOT NULL,
    prompt_hash CHAR(64) NOT NULL,
    raw_response TEXT NOT NULL,
    parsed_response JSONB NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.ai_tuner_activation_attempts (
    id BIGSERIAL PRIMARY KEY,
    strategy_id VARCHAR(32) NOT NULL,
    requested_version VARCHAR(32) NOT NULL,
    previous_version VARCHAR(32),
    expected_params_hash CHAR(64) NOT NULL,
    loaded_version VARCHAR(32),
    loaded_params_hash CHAR(64),
    status VARCHAR(24) NOT NULL,
    reason TEXT,
    requested_at TIMESTAMP NOT NULL DEFAULT NOW(),
    verified_at TIMESTAMP
);

ALTER TABLE trading.trade_records
    ADD COLUMN IF NOT EXISTS overlay_version VARCHAR(32),
    ADD COLUMN IF NOT EXISTS config_hash CHAR(64);
```

Add foreign-key-compatible indexes by `(strategy_id, overlay_version)` and decision time. Grant the existing trading user read/write permissions and sequence usage.

- [ ] **Step 4: Implement typed persistence methods**

Use parameterized SQL only. Reject an insert when the same `(strategy_id, overlay_version)` has different identity fields; identical retries are idempotent. Provide insert/get/list methods for versions, scorecards, decisions, LLM cache, and activation attempts.

```python
async def insert_overlay_version(self, item):
    existing = await self.get_overlay_version(item["strategy_id"], item["overlay_version"])
    identity = ("overlay_content_hash", "base_config_hash", "parameter_schema_version", "strategy_code_revision")
    if existing:
        if any(existing[key] != item[key] for key in identity):
            raise ValueError("overlay version identity is immutable")
        return existing
    return await self.db.fetch_one(self.INSERT_OVERLAY_SQL, *(item[key] for key in self.OVERLAY_COLUMNS))
```

- [ ] **Step 5: Run migration/repository tests and commit**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/automation/test_repository.py -q`

Expected: all tests pass.

```bash
git add database/postgres/migrations/2026-10-17-ai-tuner-mvp.sql ai_tuner/mvp tests/test_ai_tuner_mvp/automation/test_repository.py
git commit -m "feat(ai-tuner): persist immutable mvp evidence"
```

### Task 2: Cache LLM proposals by exact historical request identity

**Files:**
- Create: `ai_tuner/mvp/llm_cache.py`
- Modify: `ai_tuner/engine/llm_client.py`
- Test: `tests/test_ai_tuner_mvp/automation/test_llm_cache.py`

- [ ] **Step 1: Write failing replay-cache tests**

```python
import pytest
from ai_tuner.mvp.llm_cache import CachedProposalClient


@pytest.mark.asyncio
async def test_identical_request_never_calls_model_twice(repo, raw_llm):
    client = CachedProposalClient(repo, raw_llm, model="deepseek-reasoner", parameters={"temperature": 0})
    first = await client.propose("system", "user")
    second = await client.propose("system", "user")
    assert first == second
    assert raw_llm.call_count == 1


@pytest.mark.asyncio
async def test_changed_prompt_or_model_parameters_changes_request_hash(repo, raw_llm):
    a = CachedProposalClient(repo, raw_llm, model="m1", parameters={"temperature": 0})
    b = CachedProposalClient(repo, raw_llm, model="m1", parameters={"temperature": 0.1})
    assert a.request_hash("s", "u") != b.request_hash("s", "u")
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/automation/test_llm_cache.py -q`

Expected: FAIL because `CachedProposalClient` is missing.

- [ ] **Step 3: Implement canonical request hashing**

```python
def request_hash(self, system_prompt, user_prompt):
    payload = {
        "model": self.model,
        "parameters": self.parameters,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "response_schema": "purpose-driven-v1",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

`propose()` reads the cache first, calls the existing `LLMClient` only on a miss, parses once with `ResponseParser`, and stores both raw and parsed response. A cached parse error is replayed as the same `DATA_ERROR`; it is not silently retried during an acceptance experiment.

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/automation/test_llm_cache.py ai_tuner/tests/test_llm_client.py -q`

Expected: all tests pass.

```bash
git add ai_tuner/mvp/llm_cache.py ai_tuner/engine/llm_client.py tests/test_ai_tuner_mvp/automation/test_llm_cache.py
git commit -m "feat(ai-tuner): cache reproducible llm proposals"
```

### Task 3: Select only compatible and qualified versions by regime-aware score

**Files:**
- Create: `ai_tuner/mvp/selector.py`
- Test: `tests/test_ai_tuner_mvp/automation/test_selector.py`

- [ ] **Step 1: Write failing selector tests**

```python
def test_incompatible_high_score_cannot_win(selector, candidate_factory):
    candidates = [
        candidate_factory("current", score=60, compatible=True, eligible=True),
        candidate_factory("bad", score=99, compatible=False, eligible=True),
        candidate_factory("good", score=68, compatible=True, eligible=True),
    ]
    assert selector.select(candidates, current="current").selected_version == "good"


def test_no_advantage_keeps_current(selector, candidate_factory):
    candidates = [candidate_factory("current", score=60), candidate_factory("new", score=63)]
    result = selector.select(candidates, current="current", min_advantage=5)
    assert result.action == "KEEP"


def test_same_regime_precedes_adjacent_regime_when_confidence_is_equal(selector, candidate_factory):
    candidates = [candidate_factory("same", score=70, regime_distance=0),
                  candidate_factory("adjacent", score=72, regime_distance=1)]
    assert selector.select(candidates, current="current").selected_version == "same"
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/automation/test_selector.py -q`

Expected: FAIL because selector logic is missing.

- [ ] **Step 3: Implement filter and ranking order**

1. Remove code/config/schema-incompatible versions.
2. Remove versions with failed gates or insufficient samples.
3. Limit history to three versions: exact regime first, then same direction/adjacent volatility, then same volatility/adjacent direction.
4. Rank by regime feature distance, conservative confidence score, max drawdown, and stable version key.
5. Switch only when the candidate beats current by the configured five-point advantage.

`STRESS` never uses ordinary ranking. It returns `PAUSE` unless a separately marked stress-safe version exists and passes the drawdown/cost gates.

```python
def select(self, candidates, current, min_advantage=5.0):
    eligible = [c for c in candidates if c.compatible and c.eligible]
    if not eligible:
        return Selection("NO_ELIGIBLE", None, [])
    ranked = sorted(eligible, key=lambda c: (c.regime_distance, -c.conservative_score, c.max_drawdown, c.version))
    winner = ranked[0]
    current_item = next((c for c in eligible if c.version == current), None)
    if current_item and winner.conservative_score < current_item.conservative_score + min_advantage:
        return Selection("KEEP", current, ranked)
    return Selection("SWITCH", winner.version, ranked)
```

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/automation/test_selector.py -q`

Expected: all tests pass.

```bash
git add ai_tuner/mvp/selector.py tests/test_ai_tuner_mvp/automation/test_selector.py
git commit -m "feat(ai-tuner): select qualified regime versions"
```

### Task 4: Implement keep, switch, emergency fallback, pause, and resume states

**Files:**
- Create: `ai_tuner/mvp/state_machine.py`
- Test: `tests/test_ai_tuner_mvp/automation/test_state_machine.py`

- [ ] **Step 1: Write failing transition tests**

```python
from datetime import datetime, timedelta, timezone


def test_normal_switch_is_blocked_inside_seven_days(machine, state, decision):
    state.last_switch_at = datetime.now(timezone.utc) - timedelta(days=2)
    assert machine.transition(state, decision(action="SWITCH"), hard_breach=False).action == "KEEP"


def test_hard_breach_can_select_safe_history_inside_cooldown(machine, state, decision):
    state.last_switch_at = datetime.now(timezone.utc) - timedelta(days=2)
    result = machine.transition(state, decision(action="SWITCH", selected="safe-history"), hard_breach=True)
    assert result.action == "EMERGENCY_SWITCH"


def test_no_qualified_version_pauses_new_entries(machine, state, decision):
    result = machine.transition(state, decision(action="NO_ELIGIBLE"), hard_breach=True)
    assert result.action == "PAUSE_NEW_ENTRIES"


def test_pause_resumes_only_with_qualified_version(machine, paused_state, decision):
    assert machine.transition(paused_state, decision(action="NO_ELIGIBLE"), hard_breach=False).action == "STAY_PAUSED"
    assert machine.transition(paused_state, decision(action="SWITCH", selected="qualified"), hard_breach=False).action == "RESUME"
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/automation/test_state_machine.py -q`

Expected: FAIL because the state machine is missing.

- [ ] **Step 3: Implement explicit transitions**

State contains active version, paused flag, last switch time, last decision ID, loaded identity, and hard-breach reason. A pause changes only the new-entry permission; open positions remain under the active version's existing exit logic. Daily re-evaluation can resume only to a qualified version. Every transition emits a reason and candidate rejection list for persistence.

```python
def transition(self, state, decision, hard_breach, now=None):
    now = now or datetime.now(timezone.utc)
    if decision.action == "NO_ELIGIBLE":
        return Transition("PAUSE_NEW_ENTRIES" if hard_breach else ("STAY_PAUSED" if state.paused else "KEEP"))
    if state.paused and decision.action == "SWITCH":
        return Transition("RESUME", decision.selected_version)
    inside_cooldown = state.last_switch_at and now - state.last_switch_at < timedelta(days=7)
    if decision.action == "SWITCH" and inside_cooldown and not hard_breach:
        return Transition("KEEP", state.active_version)
    if decision.action == "SWITCH":
        return Transition("EMERGENCY_SWITCH" if hard_breach else "SWITCH", decision.selected_version)
    return Transition("KEEP", state.active_version)
```

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/automation/test_state_machine.py -q`

Expected: all tests pass.

```bash
git add ai_tuner/mvp/state_machine.py tests/test_ai_tuner_mvp/automation/test_state_machine.py
git commit -m "feat(ai-tuner): add guarded tuning state machine"
```

### Task 5: Add transactional activation with runtime acknowledgement

**Files:**
- Create: `shared/runtime_config.py`
- Create: `ai_tuner/deploy/activation.py`
- Modify: `ai_tuner/deploy/config_operator.py:78-246`
- Modify: `strategies/btc_eth_aggressive/main.py:20-56,136-220`
- Modify: `strategies/hrs/main.py:11-55`
- Modify: `shared/trade_logger.py`
- Test: `tests/test_ai_tuner_mvp/automation/test_activation.py`
- Test: `tests/test_strategies/test_btc_eth_aggressive_ai_tuner.py`
- Test: `tests/test_strategies/test_hrs_ai_tuner.py`

- [ ] **Step 1: Write failing activation tests**

```python
import pytest


@pytest.mark.asyncio
async def test_activation_verifies_requested_loaded_and_hash(activator, runtime_repo):
    runtime_repo.acknowledge(version="V2", effective_params_hash="hash-v2")
    result = await activator.activate("btc_eth_aggressive", "V2", "hash-v2")
    assert result.status == "verified"


@pytest.mark.asyncio
async def test_missing_ack_restores_previous_pointer(activator, active_file):
    active_file.write_text("V1\n")
    result = await activator.activate("btc_eth_aggressive", "V2", "hash-v2", timeout_seconds=0)
    assert result.status == "rolled_back"
    assert active_file.read_text().strip() == "V1"


def test_runtime_reload_only_changes_manifest_paths(runtime_reloader, effective_configs):
    old, new = effective_configs
    applied = runtime_reloader.diff_allowed(old, new, strategy_id="btc_eth_aggressive")
    assert set(applied) <= set(runtime_reloader.manifest.parameters)
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/automation/test_activation.py -q`

Expected: FAIL because acknowledgement activation does not exist.

- [ ] **Step 3: Return version identity from overlay creation**

Change `ConfigOperator.apply_overrides()` to return an `OverlayWriteResult` containing success, new version, previous version, overlay path, overlay hash, and expected merged config hash. Keep boolean compatibility through `__bool__` during migration.

```python
@dataclass(frozen=True)
class OverlayWriteResult:
    success: bool
    version: str
    previous_version: str | None
    overlay_path: str
    overlay_hash: str
    expected_params_hash: str

    def __bool__(self):
        return self.success
```

- [ ] **Step 4: Reload only at a safe cycle boundary**

`RuntimeConfigReloader` reads `load_strategy_config_with_metadata()`, validates the diff against the selected manifest, and applies it between strategy cycles. For MTPCS aggressive it replaces `strategy.config` and `strategy.scoring_config`; for HRS it replaces `strategy.config`, `strategy.scoring_engine.config`, and the cached scoring sub-configs through a single `apply_runtime_config()` method. If any non-manifest path changed, refuse hot reload and retain the former configuration.

After applying, save `strategy_states` with `state_key="tuning_config"` and:

```json
{
  "requested_version": "V2",
  "loaded_version": "V2",
  "effective_params_hash": "...",
  "loaded_at": "...",
  "manifest_schema_version": "1"
}
```

Pass `overlay_version` and `config_hash` to `TradeLogger`, which writes them on every new trade record.

```python
async def reload_if_changed(self, strategy):
    candidate, metadata = load_strategy_config_with_metadata(self.strategy_dir)
    params_hash = compute_manifest_params_hash(candidate, self.manifest)
    if params_hash == self.loaded_params_hash:
        return None
    self.validate_allowed_diff(strategy.config, candidate)
    strategy.apply_runtime_config(candidate)
    self.loaded_params_hash = params_hash
    await self.acknowledge(metadata, effective_params_hash=params_hash)
    return {**metadata, "effective_params_hash": params_hash}
```

- [ ] **Step 5: Implement two-phase activation**

`OverlayActivator` captures the previous `.active`, atomically writes the new pointer, creates an activation attempt, polls the `tuning_config` acknowledgement up to a configured two strategy cycles, verifies all three identities, and marks verified. Timeout, wrong version, wrong hash, or loader error restores the previous pointer atomically and waits for the previous version acknowledgement before returning `rolled_back`.

```python
async def activate(self, strategy_id, version, expected_params_hash, timeout_seconds=750):
    previous = self.pointer.read(strategy_id)
    attempt = await self.repository.start_activation(strategy_id, version, previous, expected_params_hash)
    self.pointer.write_atomic(strategy_id, version)
    ack = await self.ack_reader.wait(strategy_id, version, timeout_seconds)
    if ack and ack.loaded_version == version and ack.effective_params_hash == expected_params_hash:
        return await self.repository.mark_activation_verified(attempt.id, ack)
    self.pointer.write_atomic(strategy_id, previous)
    await self.ack_reader.wait(strategy_id, previous, timeout_seconds)
    return await self.repository.mark_activation_rolled_back(attempt.id, "runtime acknowledgement mismatch")
```

- [ ] **Step 6: Run activation and strategy tests**

Run:

```bash
python3 -m pytest \
  tests/test_ai_tuner_mvp/automation/test_activation.py \
  tests/test_strategies/test_btc_eth_aggressive_ai_tuner.py \
  tests/test_strategies/test_hrs_ai_tuner.py \
  ai_tuner/tests/test_config_operator.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add shared/runtime_config.py shared/trade_logger.py ai_tuner/deploy strategies/btc_eth_aggressive/main.py strategies/hrs/main.py tests/test_ai_tuner_mvp/automation/test_activation.py tests/test_strategies
git commit -m "feat(ai-tuner): verify runtime overlay activation"
```

### Task 6: Run the 12–16 week historical time machine

**Files:**
- Create: `ai_tuner/mvp/time_machine.py`
- Modify: `scripts/ai_tuner_mvp.py`
- Test: `tests/test_ai_tuner_mvp/automation/test_time_machine.py`

- [ ] **Step 1: Write failing time-machine tests**

```python
def test_each_week_uses_only_information_available_at_decision_time(time_machine, experiment):
    report = time_machine.run(experiment)
    assert report.future_access_attempts == 0
    assert all(run.max_read_time <= run.decision_time for run in report.weekly_runs)


def test_acceptance_compares_against_fixed_baseline(time_machine, experiment):
    report = time_machine.run(experiment)
    assert report.fixed_baseline.version == experiment.baseline_version
    assert report.decision in {"GO", "REVISE", "STOP"}


def test_repeated_experiment_is_identical_with_cached_llm(time_machine, experiment):
    first = time_machine.run(experiment).canonical_json()
    second = time_machine.run(experiment).canonical_json()
    assert first == second
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/automation/test_time_machine.py -q`

Expected: FAIL because the orchestrator is missing.

- [ ] **Step 3: Implement weekly historical orchestration**

For each historical Sunday `T`: build an 8-week diagnosis ending before the 2-week blind window; classify regime as of `T`; request or replay one cached LLM response; validate the proposal; qualify current, proposal, and at most three historical versions using only data available at `T`; persist the decision; then evaluate the selected version on the unseen week after `T`. Advance exactly seven days.

Track proposal/HOLD/DATA_ERROR counts, rejection reasons, posterior weekly performance, history-match outcomes, keep/switch/emergency/pause counts, oscillation count, and every maximum read timestamp.

```python
for decision_time in experiment.weekly_decision_times():
    portal = self.portal.at(decision_time)
    diagnosis = self.diagnose(portal, weeks=8, blind_weeks=2)
    proposal = self.proposals.propose_cached(diagnosis, decision_time)
    qualification = self.qualifier.run_at(decision_time, proposal, experiment.version_pool)
    selection = self.selector.select(qualification.candidates, current=state.active_version)
    transition = self.machine.transition(state, selection, qualification.current_hard_breach, decision_time)
    posterior = self.evaluate_next_week(decision_time, transition.selected_version)
    report.record(decision_time, portal.max_read_time, proposal, qualification, transition, posterior)
```

- [ ] **Step 4: Define automatic experiment result**

- `GO`: parity passed; zero future access; automated risk-adjusted return is no worse than fixed baseline; automated max drawdown is no higher; switching limit respected; no unqualified version selected; activation simulation passed.
- `REVISE`: replay is reliable, but proposal quality, selector quality, regime match, or switching behavior misses acceptance.
- `STOP`: parity is unreliable, future data was accessed, required history is absent, or automated drawdown is materially worse.

```python
if not report.parity_passed or report.future_access_attempts or report.required_history_missing:
    report.decision = "STOP"
elif report.max_drawdown > report.fixed_baseline.max_drawdown or report.risk_adjusted_return < report.fixed_baseline.risk_adjusted_return:
    report.decision = "REVISE"
else:
    report.decision = "GO"
```

- [ ] **Step 5: Add CLI and run tests**

Command:

```bash
python3 scripts/ai_tuner_mvp.py time-machine \
  --strategy btc_eth_aggressive \
  --start 2026-06-28T00:00:00Z \
  --weeks 16 \
  --baseline-version V20260910 \
  --output var/ai_tuner/time-machine.json
```

Run: `python3 -m pytest tests/test_ai_tuner_mvp/automation/test_time_machine.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add ai_tuner/mvp/time_machine.py scripts/ai_tuner_mvp.py tests/test_ai_tuner_mvp/automation/test_time_machine.py
git commit -m "feat(ai-tuner): add historical time machine"
```

### Task 7: Integrate the qualified weekly job and daily safety monitor

**Files:**
- Create: `ai_tuner/scheduler/purpose_driven_job.py`
- Create: `ai_tuner/monitor/mvp_daily_monitor.py`
- Modify: `ai_tuner/main.py:332-472,545-625,850-900`
- Modify: `ai_tuner/config.yaml`
- Test: `tests/test_ai_tuner_mvp/automation/test_purpose_driven_job.py`
- Test: `tests/test_ai_tuner_mvp/automation/test_daily_monitor.py`

- [ ] **Step 1: Write failing end-to-end scheduler tests**

```python
@pytest.mark.asyncio
async def test_weekly_job_activates_only_selector_winner(job, qualified_candidate):
    result = await job.run(as_of=qualified_candidate.as_of)
    assert result.action == "SWITCH"
    job.activator.activate.assert_awaited_once_with(
        qualified_candidate.strategy_id,
        qualified_candidate.version,
        qualified_candidate.expected_params_hash)


@pytest.mark.asyncio
async def test_all_candidates_fail_enters_pause_on_hard_breach(job, failing_candidates):
    job.current_health.hard_breach = True
    result = await job.run(candidates=failing_candidates)
    assert result.action == "PAUSE_NEW_ENTRIES"
    job.activator.activate.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_monitor_does_not_rollback_on_noise(monitor, healthy_state):
    healthy_state.one_day_return = -0.01
    result = await monitor.evaluate(healthy_state)
    assert result.action == "KEEP"
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/automation/test_purpose_driven_job.py tests/test_ai_tuner_mvp/automation/test_daily_monitor.py -q`

Expected: FAIL because production orchestration is missing.

- [ ] **Step 3: Implement the purpose-driven weekly job**

The job executes only when `purpose_driven_mvp.mode == live`, `selected_strategy` is non-null, the latest time-machine report for current identities says `GO`, and parity is current. It gathers data, produces at most one LLM candidate, qualifies versions, persists all scorecards and the decision, runs the state machine, and calls the activator only for switch/resume actions.

```python
async def run(self, as_of):
    self.preconditions.require_live_go(self.config, as_of)
    proposal = await self.proposal_service.create_one(as_of)
    qualification = await self.qualification_service.evaluate(as_of, proposal)
    selection = self.selector.select(qualification.candidates, qualification.current_version)
    transition = self.machine.transition(await self.state.load(), selection, qualification.current_hard_breach, as_of)
    decision_id = await self.repository.persist_decision(transition, qualification)
    if transition.action in {"SWITCH", "EMERGENCY_SWITCH", "RESUME"}:
        await self.activator.activate(self.strategy_id, transition.selected_version, qualification.expected_params_hash)
    return transition.with_decision_id(decision_id)
```

- [ ] **Step 4: Implement daily hard-breach monitoring**

The monitor evaluates realized live scorecard updates, activation identity, data freshness, strategy drawdown cap, and `STRESS`. It does not infer a parameter direction. On breach it reruns eligibility for compatible historical versions; if one qualifies, perform emergency switch; otherwise pause new entries. It reevaluates paused state daily and resumes only to a qualified version.

```python
async def evaluate(self, health):
    hard_breach = health.drawdown > self.drawdown_cap or not health.identity_matches or health.regime == "STRESS"
    if not hard_breach and not health.paused:
        return Transition("KEEP", health.active_version)
    qualification = await self.qualifier.evaluate_existing_versions(health.as_of)
    selection = self.selector.select(qualification.candidates, health.active_version)
    return self.machine.transition(health.state, selection, hard_breach, health.as_of)
```

- [ ] **Step 5: Wire scheduling without changing other strategies**

Keep the existing `WeeklyTuningJob` for non-MVP strategies. Add a separate scheduled `PurposeDrivenTuningJob` and daily monitor. `/api/v1/trigger` routes the selected MVP strategy to the new job and returns its decision ID; `/health` includes mode, selected strategy, active/loaded version, config hash agreement, latest qualification, and paused status.

```python
if requested_ids and self.mvp_job.strategy_id in requested_ids:
    mvp_result = await self.mvp_job.run(as_of=datetime.now(BEIJING_TZ))
legacy_ids = [sid for sid in (requested_ids or self.legacy_strategy_ids) if sid != self.mvp_job.strategy_id]
if legacy_ids:
    await self.weekly_job.run_weekly_tuning(strategy_ids=legacy_ids, force=force)
```

- [ ] **Step 6: Run tests and commit**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/automation/test_purpose_driven_job.py tests/test_ai_tuner_mvp/automation/test_daily_monitor.py ai_tuner/tests/test_auto_apply.py -q`

Expected: all tests pass and legacy strategies retain their existing scheduling behavior.

```bash
git add ai_tuner/scheduler/purpose_driven_job.py ai_tuner/monitor/mvp_daily_monitor.py ai_tuner/main.py ai_tuner/config.yaml tests/test_ai_tuner_mvp/automation
git commit -m "feat(ai-tuner): schedule qualified automatic tuning"
```

### Task 8: Execute final acceptance and produce the rollout evidence

**Files:**
- Create: `docs/runbooks/ai-tuner-mvp-rollout.md`
- Create: `docs/reports/ai-tuner-mvp-acceptance.example.json`
- Modify: `README.md`

- [ ] **Step 1: Run the complete automated suite**

Run:

```bash
python3 -m pytest \
  ai_tuner/tests \
  tests/test_ai_tuner_mvp \
  tests/test_strategies/test_btc_eth_aggressive_ai_tuner.py \
  tests/test_strategies/test_hrs_ai_tuner.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run determinism and future-access acceptance twice**

Run the identical `time-machine` command twice against the same database snapshot and cached model responses. SHA-256 the two canonical reports.

Expected: hashes match; `future_access_attempts` is `0`.

- [ ] **Step 3: Perform an activation failure drill in shadow mode**

Request a candidate activation while suppressing the runtime acknowledgement in the test environment.

Expected: the new `.active` pointer is restored to the former version; activation status is `rolled_back`; the unacknowledged version receives no live score attribution.

- [ ] **Step 4: Write the rollout runbook**

The runbook must list:

- prerequisites and current identity checks;
- the exact audit, parity, qualification, time-machine, status, activation, pause, and resume commands;
- interpretation of `GO`, `REVISE`, and `STOP`;
- database queries for latest decisions and activation attempts;
- recovery steps that restore a named prior `.active` version;
- evidence required before changing mode from `shadow` to `live`;
- evidence required before adding another parameter group or strategy.

```markdown
## Live enablement checklist

- Readiness report selected exactly one strategy.
- Golden parity report passed for current code/config/schema identities.
- Time-machine report decision is `GO` and has zero future-data access.
- Shadow activation failure drill restored the previous pointer.
- Runtime status shows requested version = loaded version and expected hash = loaded hash.
```

- [ ] **Step 5: Change to live only after a real `GO` report**

Update these two keys only:

```yaml
purpose_driven_mvp:
  mode: live
  selected_strategy: btc_eth_aggressive
```

Use `hrs` instead only when the persisted readiness report selected HRS. If the real report is `REVISE` or `STOP`, retain `shadow` and do not enable production activation.

- [ ] **Step 6: Commit final acceptance documentation**

```bash
git add README.md docs/runbooks/ai-tuner-mvp-rollout.md docs/reports/ai-tuner-mvp-acceptance.example.json ai_tuner/config.yaml
git commit -m "docs(ai-tuner): add mvp rollout and acceptance"
```
