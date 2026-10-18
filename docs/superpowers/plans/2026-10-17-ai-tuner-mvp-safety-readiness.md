# AI Tuner MVP Safety and Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AI proposals mechanically safe and select the first replayable MVP strategy using a reproducible 90-day audit.

**Architecture:** A typed `ParameterManifest` becomes the only authority for tunable paths, values, ranges, change limits, groups, and invariants. The LLM emits a strict decision envelope, but deterministic code validates it against the effective runtime configuration. The old weekly path is placed behind an explicit per-strategy mode so it cannot auto-apply to the MVP strategy before qualification exists.

**Tech Stack:** Python 3.10 dataclasses/enums, PyYAML, PostgreSQL via the existing `DatabaseManager`, pytest and pytest-asyncio.

---

## 实现偏差回标（2026-10-18，阶段一收口）

> 本节只回标「计划 vs 最终实现」的差异，**不改写上方计划正文**。事实基线以代码与 `ai_tuner/config.yaml` 为准（配套设计见 [spec（2026-10-16）](../specs/2026-10-16-purpose-driven-ai-tuner-design.md)，其文末亦有对应回标）。

| 编号 | 计划位置 | 计划描述 | 最终实现（偏差） |
|---|---|---|---|
| D1 | File map / Task 7 | 未列 `ai_tuner/Dockerfile` 变更 | 实际补 `COPY strategies/btc_eth_aggressive/config.yaml /app/strategies/btc_eth_aggressive/config.yaml`，使 `adapter._read_effective_config()` 在容器内可读到激进版策略配置（否则 manifest 校验拿不到 `from` 真实值） |
| D2 | Task 6 Step 5 / CLI | CLI 仅定义 exit 0=SELECT、2=STOP | 实际新增 **exit 1=运行时错误**（DB 连接失败 / `--as-of` 解析失败）；`--as-of` 额外支持 `Z` 后缀；报告为**原子写**（先写 `.tmp` 再 `os.replace`）；额外打印人类可读摘要 |
| D3 | Task 5 Step 4 | MVP 候选在非 live 模式下 `return "mvp_managed"`，**跳过旧调优路径** | **语义改为「完整跑流水线、仅拦截写入」**：`_tune_single_strategy()` 对 MVP 候选照常执行「采集→LLM→校验→diff→飞书推送」，仅在**步骤12 `auto_apply`** 处拦截（不写覆盖层 / 不动 `.active` / 不 `mark_applied`）；返回值仍为流水线结果（success/skip/error），**不出现 `mvp_managed`**；`run_weekly_tuning()` 的 `mvp_managed` 计数逻辑未采用。判定式：`is_mvp_blocked = strategy_id in candidates and (mode != "live" or strategy_id != selected_strategy)` |
| D4 | Task 5 Step 3 | 「删除 `build_learning_instructions()` 中的 L4 调用与约束段落」；「删除 `config.yaml` 的 `stale_unchanged_count` / `stale_adjustment_ratio`」 | `_apply_l4_stale_trigger()` 保留且**恒返回 `None`** ✓，但 `build_learning_instructions()` **仍保留 L4 调用点**（改由返回值 `None` 关闭行为，未删调用）；`config.yaml` 的 `stale_unchanged_count` / `stale_adjustment_ratio` **保留未删**（兼容字段，行为已失效） |
| D5 | Task 6 Step 3 | `ReadinessAuditor(repo)`（单参数）、`run(as_of)`；`CandidateReadiness` 无 `gaps` 字段 | 实际 `ReadinessAuditor(db_manager, config)`（**两参数**，config 提供候选顺序与阈值）；`CandidateReadiness` **新增 `gaps: List[str]`** 数据缺口说明字段，字段顺序调整（`regime_count` 前移） |
| D6 | Task 6 Step 3 阈值 | `min_external_snapshot_coverage: 0.95` | 初版实际 **全局 `0.0`**（数据库无任何外部时点快照表 → coverage=0，降级为报告项）——**该全局降级已被 D11 修正为策略级**；另新增 `identity_ratio_baseline`（默认 0.90）供 `config_identity_ratio` 使用 |
| D7 | Task 6 Step 4 | 用固定表直接查询；config identity 由身份列动态计算 | 实际全部动态：用 `information_schema` 探测 K 线表 / `trade_signals` 的 schema、表名、时间戳列（`_KNOWN_SCHEMAS` 白名单）；`regime_count` 为 `COUNT(DISTINCT symbol)` 的 **proxy**（非真实市场状态数，标 TODO 阶段二）；`config_identity_ratio` 取 `identity_ratio_baseline` 常量（inferred），非动态计算 |
| D8 | Task 4 Step 3 | `REQUIRED_FIELDS` + `_validate_envelope()` | 实际命名 `NEW_REQUIRED_FIELDS` + `_validate_new_envelope()`；双契约由 `ResponseParser.parse_response(raw, contract_mode)` 分流（`"legacy"` 默认 / `"new"`），`WeeklyTuningJob` 依适配器是否声明 `parameter_manifest` 决定 `contract_mode` |
| D9 | Task 7 / 测试 | 列举 6 个测试文件 | 实际 `tests/test_ai_tuner_mvp/` 共 **11 个文件**（含 `test_db_hallucination_guard.py`、`test_h_severity_regressions.py`、`test_dual_contract.py`、`test_cli.py` 等），**227 用例** |
| D10 | Task 7 Step 3 | 更新根 `README.md`、创建报告样例 | 本回标完成：根 README 增补「Purpose-driven AI Tuner MVP readiness」段；报告样例落 `docs/reports/ai-tuner-mvp-readiness-report.example.json` |
| D11 | Task 6 Step 3 阈值 / 用户复核 | 外部快照门禁为单一全局阈值 | **改为策略级门禁**（用户拍板）：全局默认 `min_external_snapshot_coverage: 0.95` + `external_snapshot_requirement: required`；`readiness.per_strategy` 按策略覆盖——`btc_eth_aggressive` 设 `0.0` + `not_required_for_current_manifest`（入场逻辑不依赖 OI/市值/资金费/情绪快照，报告须写「不要求」而非「合格」），`hrs` 保持 `0.95` + `required`（OI/市值/资金费/候选池历史状态直接影响评分与可交易集合，缺失即 failure，原因码 `required_historical_external_snapshots_missing`）。`CandidateReadiness` 新增 `external_snapshot_requirement` / `external_snapshot_reason` 两字段承载该语义。**不得**在全局把外部快照阈值统一降为零 |
| D12 | Task 6 Step 3/4 审计读取 + config | 计划未规定审计读取层的列类型绑定与表/列探测口径；`window_days` 走 `get` 兜底、无 `identity_ratio_baseline` 强制键 | 经**生产 DB 取证**修复审计读取层三处口径缺陷（回归 227 用例全绿）：**(1) 时区绑定**——新增 `_fetch_column_data_type` + `_bind_window`，先查 `information_schema` 的列 `data_type`，naive 列（`timestamp without time zone`）去 tzinfo、aware 列（`timestamp with time zone`）保留 UTC 后再绑定，覆盖 `trading.trade_records.executed_at`(naive) / kline `open_time`(naive) / `hrs.hrs_signals.triggered_at`(aware)，消除 asyncpg `can't subtract offset-naive and offset-aware datetimes` 致窗口查询全败；**(2) 信号表探测重写**——schema 直接取 `strategy_id`（信号按 schema 隔离），表名用 `ILIKE '%signal%'` 探测（覆盖 `trade_signals`/`hrs_signals`/`short_signals`），时间列按 `['created_at','signal_time','triggered_at','executed_at']` 优先级探测并取其类型（原硬编码 `signal_time` + 要求 `strategy_id` 列 → 生产无该列 → 恒报「trade_signals 表缺失」假故障）；**(3) K 线覆盖度按主力合约**——先取该策略窗口内 `trade_records` 中 symbol 出现次数最多者，再按 `kline_<symbol小写>_%h`（优先 1h）定位表，覆盖度口径为「主力合约抽样」，非全候选池覆盖（原为任意取第一张 K 线表，常取到无关小币）。配置强制化：`ai_tuner/config.yaml` 的 `purpose_driven_mvp` 新增 `identity_ratio_baseline: 0.90`；`auditor.py` 新增 `_REQUIRED_MVP_KEYS`（`candidates`/`identity_ratio_baseline`）并把 `window_days` 纳入 `_REQUIRED_READINESS_KEYS`，去除原硬编码兜底（缺失即 `ValueError`） |

> 另：`scripts/ai_tuner_mvp.py` 需显式把仓库根注入 `sys.path[0]`（以文件方式运行 `python3 scripts/ai_tuner_mvp.py` 时 `sys.path[0]=scripts/`，`shared/`、`ai_tuner/` 会导入失败）——计划未提，属实际实现补充。

---

## File map

- Create `ai_tuner/parameters/models.py`: manifest enums and typed parameter definitions.
- Create `ai_tuner/parameters/manifest.py`: manifest loading, runtime-path resolution, invariants, proposal validation.
- Create `ai_tuner/parameters/manifests/btc_eth_aggressive.yaml`: MTPCS aggressive `entry_quality` contract.
- Create `ai_tuner/parameters/manifests/hrs.yaml`: HRS fallback `entry_quality` contract.
- Modify `ai_tuner/adapters/base_adapter.py`: delegate proposal validation and current-value lookup to the manifest.
- Modify `ai_tuner/engine/response_parser.py`: enforce the new `PROPOSE|HOLD|DATA_ERROR` response schema.
- Modify `ai_tuner/feedback/learning_signal.py`: remove forced tuning after repeated `HOLD`.
- Modify `ai_tuner/prompts/common_rules.txt`: state objective, hard constraints, blind-data boundary, and `HOLD` behavior.
- Modify `ai_tuner/prompts/btc_eth_aggressive_system.txt`, `btc_eth_aggressive_user.txt`, `hrs_system.txt`, `hrs_user.txt`: expose one manifest group and the new output schema.
- Modify `ai_tuner/config.yaml`: introduce MVP mode and replace the MVP candidates' broad legacy whitelists with manifest references.
- Modify `ai_tuner/scheduler/weekly_job.py`: route MVP candidates away from legacy auto-apply.
- Create `ai_tuner/readiness/models.py`: audit result and gate definitions.
- Create `ai_tuner/readiness/auditor.py`: 90-day data/trade/config audit and deterministic selection.
- Create `scripts/ai_tuner_mvp.py`: `audit` command that writes a JSON evidence report.
- Create `tests/test_ai_tuner_mvp/`: focused unit and integration tests.

### Task 1: Define the parameter-manifest contract

**Files:**
- Create: `ai_tuner/parameters/__init__.py`
- Create: `ai_tuner/parameters/models.py`
- Test: `tests/test_ai_tuner_mvp/test_parameter_models.py`

- [ ] **Step 1: Write the failing model tests**

```python
from ai_tuner.parameters.models import ParameterSpec, ParameterStatus, ValueType


def test_parameter_spec_rejects_non_tunable_adjustment():
    spec = ParameterSpec(
        path="strategy.scoring.min_score",
        status=ParameterStatus.REDLINE,
        value_type=ValueType.FLOAT,
        min_value=55.0,
        max_value=75.0,
        max_change_pct=0.10,
        group="entry_quality",
        live_usage_probe="BTCEthStrategy.analyze",
    )
    assert spec.can_adjust is False


def test_parameter_spec_coerces_numeric_value_without_coercing_bool():
    spec = ParameterSpec(
        path="strategy.scoring.min_score",
        status=ParameterStatus.TUNABLE,
        value_type=ValueType.FLOAT,
        min_value=55.0,
        max_value=75.0,
        max_change_pct=0.10,
        group="entry_quality",
        live_usage_probe="BTCEthStrategy.analyze",
    )
    assert spec.coerce(61) == 61.0
    try:
        spec.coerce(True)
    except ValueError as exc:
        assert "boolean" in str(exc)
    else:
        raise AssertionError("boolean must not be accepted as a numeric parameter")
```

- [ ] **Step 2: Run the tests and verify the import fails**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_parameter_models.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'ai_tuner.parameters'`.

- [ ] **Step 3: Implement the typed manifest models**

```python
# ai_tuner/parameters/models.py
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ParameterStatus(str, Enum):
    TUNABLE = "tunable"
    REDLINE = "redline"
    SHADOW = "shadow"
    DEAD = "dead"
    DERIVED = "derived"


class ValueType(str, Enum):
    FLOAT = "float"
    INT = "int"


@dataclass(frozen=True)
class ParameterSpec:
    path: str
    status: ParameterStatus
    value_type: ValueType
    min_value: float
    max_value: float
    max_change_pct: float
    group: str
    live_usage_probe: str
    affects: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def can_adjust(self) -> bool:
        return self.status is ParameterStatus.TUNABLE

    def coerce(self, value: Any) -> float | int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{self.path}: boolean or non-numeric value is invalid")
        if self.value_type is ValueType.INT:
            if int(value) != value:
                raise ValueError(f"{self.path}: integer value required")
            return int(value)
        return float(value)


@dataclass(frozen=True)
class ParameterManifest:
    strategy_id: str
    schema_version: str
    max_adjustments_per_run: int
    parameters: Dict[str, ParameterSpec]
    invariants: List[Dict[str, Any]]


@dataclass(frozen=True)
class ValidatedProposal:
    decision: str
    adjustments: Dict[str, Dict[str, float | int]]
    errors: List[str]
    warnings: List[str]

    @property
    def valid(self) -> bool:
        return not self.errors
```

Export these symbols from `ai_tuner/parameters/__init__.py`.

- [ ] **Step 4: Run the focused tests**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_parameter_models.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add ai_tuner/parameters tests/test_ai_tuner_mvp/test_parameter_models.py
git commit -m "feat(ai-tuner): define parameter manifest models"
```

### Task 2: Add live-path manifests for both MVP candidates

**Files:**
- Create: `ai_tuner/parameters/manifests/btc_eth_aggressive.yaml`
- Create: `ai_tuner/parameters/manifests/hrs.yaml`
- Create: `ai_tuner/parameters/manifest.py`
- Test: `tests/test_ai_tuner_mvp/test_parameter_manifest.py`

- [ ] **Step 1: Write failing tests for real paths and invariants**

```python
from pathlib import Path
import yaml

from ai_tuner.parameters.manifest import ManifestRegistry, get_path


ROOT = Path(__file__).resolve().parents[2]


def test_aggressive_manifest_paths_exist_in_effective_config():
    registry = ManifestRegistry(ROOT / "ai_tuner/parameters/manifests")
    manifest = registry.load("btc_eth_aggressive")
    base = yaml.safe_load((ROOT / "strategies/btc_eth_aggressive/config.yaml").read_text())
    assert 3 <= len(manifest.parameters) <= 5
    assert all(get_path(base, path) is not None for path in manifest.parameters)
    assert all(path.startswith("strategy.scoring.") for path in manifest.parameters)


def test_hrs_manifest_contains_only_entry_quality_parameters():
    registry = ManifestRegistry(ROOT / "ai_tuner/parameters/manifests")
    manifest = registry.load("hrs")
    assert 3 <= len(manifest.parameters) <= 5
    assert {spec.group for spec in manifest.parameters.values()} == {"entry_quality"}
    assert all(spec.max_change_pct == 0.10 for spec in manifest.parameters.values())
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_parameter_manifest.py -q`

Expected: FAIL because `ManifestRegistry` and the YAML manifests do not exist.

- [ ] **Step 3: Add exact manifest data**

```yaml
# ai_tuner/parameters/manifests/btc_eth_aggressive.yaml
strategy_id: btc_eth_aggressive
schema_version: "1"
max_adjustments_per_run: 2
parameters:
  strategy.scoring.min_score:
    status: tunable
    value_type: float
    min_value: 55.0
    max_value: 70.0
    max_change_pct: 0.10
    group: entry_quality
    live_usage_probe: BTCEthStrategy.analyze
    affects: {primary: [trade_count, expectancy], secondary: [max_drawdown]}
  strategy.scoring.trend_strength.adx_strong_threshold:
    status: tunable
    value_type: float
    min_value: 22.0
    max_value: 32.0
    max_change_pct: 0.10
    group: entry_quality
    live_usage_probe: BTCEthStrategy._calculate_trend_strength_score
  strategy.scoring.trend_strength.adx_medium_threshold:
    status: tunable
    value_type: float
    min_value: 17.0
    max_value: 24.0
    max_change_pct: 0.10
    group: entry_quality
    live_usage_probe: BTCEthStrategy._calculate_trend_strength_score
  strategy.scoring.breakout_pullback.proximity_pct:
    status: tunable
    value_type: float
    min_value: 0.0075
    max_value: 0.0125
    max_change_pct: 0.10
    group: entry_quality
    live_usage_probe: BTCEthStrategy._detect_breakout_pullback
invariants:
  - kind: greater_than
    left: strategy.scoring.trend_strength.adx_strong_threshold
    right: strategy.scoring.trend_strength.adx_medium_threshold
```

```yaml
# ai_tuner/parameters/manifests/hrs.yaml
strategy_id: hrs
schema_version: "1"
max_adjustments_per_run: 2
parameters:
  scoring.entry_threshold:
    status: tunable
    value_type: float
    min_value: 5.0
    max_value: 7.0
    max_change_pct: 0.10
    group: entry_quality
    live_usage_probe: ScoringEngine.should_entry
  scoring.short_entry_threshold:
    status: tunable
    value_type: float
    min_value: 4.5
    max_value: 6.5
    max_change_pct: 0.10
    group: entry_quality
    live_usage_probe: ScoringEngine.should_entry
  scoring.technical.min_total_score:
    status: tunable
    value_type: float
    min_value: 3.0
    max_value: 5.0
    max_change_pct: 0.10
    group: entry_quality
    live_usage_probe: ScoringEngine.should_entry
  scoring.technical.short_min_total_score:
    status: tunable
    value_type: float
    min_value: 1.5
    max_value: 3.0
    max_change_pct: 0.10
    group: entry_quality
    live_usage_probe: ScoringEngine.should_entry
invariants:
  - kind: greater_than_or_equal
    left: scoring.entry_threshold
    right: scoring.short_entry_threshold
  - kind: greater_than_or_equal
    left: scoring.technical.min_total_score
    right: scoring.technical.short_min_total_score
```

- [ ] **Step 4: Implement manifest loading and path access**

```python
# ai_tuner/parameters/manifest.py
from pathlib import Path
from typing import Any, Mapping
import yaml

from .models import ParameterManifest, ParameterSpec, ParameterStatus, ValueType


def get_path(config: Mapping[str, Any], path: str) -> Any:
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


class ManifestRegistry:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def load(self, strategy_id: str) -> ParameterManifest:
        raw = yaml.safe_load((self.directory / f"{strategy_id}.yaml").read_text("utf-8"))
        specs = {
            path: ParameterSpec(
                path=path,
                status=ParameterStatus(data["status"]),
                value_type=ValueType(data["value_type"]),
                min_value=float(data["min_value"]),
                max_value=float(data["max_value"]),
                max_change_pct=float(data["max_change_pct"]),
                group=data["group"],
                live_usage_probe=data["live_usage_probe"],
                affects=data.get("affects", {}),
            )
            for path, data in raw["parameters"].items()
        }
        return ParameterManifest(
            strategy_id=raw["strategy_id"],
            schema_version=str(raw["schema_version"]),
            max_adjustments_per_run=int(raw["max_adjustments_per_run"]),
            parameters=specs,
            invariants=raw.get("invariants", []),
        )
```

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_parameter_manifest.py -q`

Expected: `2 passed`.

```bash
git add ai_tuner/parameters tests/test_ai_tuner_mvp/test_parameter_manifest.py
git commit -m "feat(ai-tuner): add entry quality manifests"
```

### Task 3: Enforce current values, ranges, change caps, and invariants

**Files:**
- Modify: `ai_tuner/parameters/manifest.py`
- Modify: `ai_tuner/adapters/base_adapter.py:255-370`
- Test: `tests/test_ai_tuner_mvp/test_proposal_validation.py`

- [ ] **Step 1: Write failing validation tests**

```python
from ai_tuner.parameters.manifest import validate_proposal
from ai_tuner.parameters.models import ParameterManifest, ParameterSpec, ParameterStatus, ValueType


def manifest():
    return ParameterManifest("s", "1", 2, {
        "scoring.min": ParameterSpec("scoring.min", ParameterStatus.TUNABLE,
            ValueType.FLOAT, 50, 70, 0.10, "entry_quality", "probe"),
        "scoring.strong": ParameterSpec("scoring.strong", ParameterStatus.TUNABLE,
            ValueType.FLOAT, 20, 35, 0.10, "entry_quality", "probe"),
        "scoring.medium": ParameterSpec("scoring.medium", ParameterStatus.TUNABLE,
            ValueType.FLOAT, 15, 30, 0.10, "entry_quality", "probe"),
    }, [{"kind": "greater_than", "left": "scoring.strong", "right": "scoring.medium"}])


def test_rejects_fabricated_from_value():
    result = validate_proposal(manifest(), {"scoring": {"min": 60, "strong": 25, "medium": 20}},
        "PROPOSE", {"scoring.min": {"from": 55, "to": 61}})
    assert result.valid is False
    assert "does not match effective value" in result.errors[0]


def test_rejects_more_than_ten_percent_and_unknown_path():
    result = validate_proposal(manifest(), {"scoring": {"min": 60, "strong": 25, "medium": 20}},
        "PROPOSE", {"scoring.min": {"from": 60, "to": 67}, "risk.leverage": {"from": 3, "to": 4}})
    assert result.valid is False
    assert len(result.errors) == 2


def test_validated_values_are_the_only_values_returned():
    result = validate_proposal(manifest(), {"scoring": {"min": 60, "strong": 25, "medium": 20}},
        "PROPOSE", {"scoring.min": {"from": 60, "to": 63}})
    assert result.valid is True
    assert result.adjustments == {"scoring.min": {"from": 60.0, "to": 63.0}}
```

- [ ] **Step 2: Run the tests and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_proposal_validation.py -q`

Expected: FAIL because `validate_proposal` is missing.

- [ ] **Step 3: Implement deterministic validation**

```python
def validate_proposal(manifest, effective_config, decision, adjustments):
    from .models import ValidatedProposal
    errors, warnings, accepted = [], [], {}
    if decision in {"HOLD", "DATA_ERROR"}:
        if adjustments:
            errors.append(f"{decision} must have no adjustments")
        return ValidatedProposal(decision, {}, errors, warnings)
    if decision != "PROPOSE":
        return ValidatedProposal(decision, {}, [f"unsupported decision: {decision}"], warnings)
    if not adjustments:
        errors.append("PROPOSE requires at least one adjustment")
    if len(adjustments) > manifest.max_adjustments_per_run:
        errors.append(f"at most {manifest.max_adjustments_per_run} adjustments are allowed")
    for path, change in adjustments.items():
        spec = manifest.parameters.get(path)
        if spec is None or not spec.can_adjust:
            errors.append(f"{path}: not tunable")
            continue
        actual = get_path(effective_config, path)
        old = spec.coerce(change.get("from"))
        new = spec.coerce(change.get("to"))
        if abs(float(old) - float(actual)) > 1e-12:
            errors.append(f"{path}: from value does not match effective value")
            continue
        if not spec.min_value <= float(new) <= spec.max_value:
            errors.append(f"{path}: target is outside manifest range")
            continue
        denominator = abs(float(actual))
        change_pct = abs(float(new) - float(actual)) / denominator if denominator else float("inf")
        if change_pct > spec.max_change_pct + 1e-12:
            errors.append(f"{path}: change exceeds {spec.max_change_pct:.0%}")
            continue
        accepted[path] = {"from": old, "to": new}
    candidate = yaml.safe_load(yaml.safe_dump(dict(effective_config)))
    for path, change in accepted.items():
        set_path(candidate, path, change["to"])
    errors.extend(validate_invariants(manifest, candidate))
    return ValidatedProposal(decision, accepted if not errors else {}, errors, warnings)
```

Add `set_path()` and `validate_invariants()` beside `get_path()`. Support exactly `greater_than` and `greater_than_or_equal`; reject any unknown invariant kind when loading the manifest.

- [ ] **Step 4: Make adapters use the manifest**

Replace the legacy whitelist/range implementation in `BaseAdapter.validate_params()` with the manifest. Also make `get_param_whitelist()`, `get_param_ranges()`, and every adapter's `get_current_params()` derive their values from the same manifest, so prompt input and validation cannot disagree.

```python
def validate_params(self, adjustments, decision="PROPOSE"):
    project_root = Path(__file__).resolve().parents[2]
    registry = ManifestRegistry(project_root / "ai_tuner/parameters/manifests")
    manifest = registry.load(self.strategy_id)
    config = self._read_config()
    result = validate_proposal(manifest, config, decision, adjustments)
    return {
        "valid": result.valid,
        "errors": result.errors,
        "warnings": result.warnings,
        "validated": {path: item["to"] for path, item in result.adjustments.items()},
    }
```

Update `ResponseParser.validate_adjustments()` and its caller in `WeeklyTuningJob` to pass `parsed["decision"]`; do not infer `PROPOSE` merely because the adjustments field exists.

- [ ] **Step 5: Run focused and legacy validation tests**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_proposal_validation.py ai_tuner/tests/test_response_parser.py -q`

Expected: all tests pass; update legacy fixtures to use manifest paths rather than invented `param1` paths.

- [ ] **Step 6: Commit**

```bash
git add ai_tuner/parameters ai_tuner/adapters/base_adapter.py tests/test_ai_tuner_mvp ai_tuner/tests/test_response_parser.py
git commit -m "feat(ai-tuner): enforce manifest proposal limits"
```

### Task 4: Replace the ambiguous LLM response contract

**Files:**
- Modify: `ai_tuner/engine/response_parser.py:21-187`
- Modify: `ai_tuner/prompts/common_rules.txt`
- Modify: `ai_tuner/prompts/btc_eth_aggressive_system.txt`
- Modify: `ai_tuner/prompts/btc_eth_aggressive_user.txt`
- Modify: `ai_tuner/prompts/hrs_system.txt`
- Modify: `ai_tuner/prompts/hrs_user.txt`
- Test: `tests/test_ai_tuner_mvp/test_response_contract.py`

- [ ] **Step 1: Write failing response-contract tests**

```python
import json
from ai_tuner.engine.response_parser import ResponseParser


def envelope(decision="HOLD", adjustments=None):
    return json.dumps({
        "decision": decision,
        "objective": "maximize_risk_adjusted_net_return_under_drawdown_cap",
        "diagnosis": [],
        "target_metrics": {"risk_adjusted_return": "increase", "max_drawdown": "not_increase", "net_return": "increase"},
        "adjustments": adjustments or {},
        "applicable_market_regimes": ["range_normal_vol"],
        "expected_impact": {"trade_count": "unchanged", "expectancy": "unchanged", "max_drawdown": "not_increase"},
        "confidence": 0.4,
    })


def test_hold_is_a_valid_first_class_decision():
    parsed = ResponseParser().parse_response(envelope())
    assert parsed["decision"] == "HOLD"
    assert "error" not in parsed


def test_wrong_objective_is_rejected():
    raw = json.loads(envelope())
    raw["objective"] = "maximize_win_rate"
    parsed = ResponseParser().parse_response(json.dumps(raw))
    assert "error" in parsed


def test_hold_with_adjustments_is_rejected():
    parsed = ResponseParser().parse_response(envelope("HOLD", {"x": {"from": 1, "to": 2}}))
    assert "error" in parsed
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_response_contract.py -q`

Expected: FAIL because the parser still requires the legacy fields.

- [ ] **Step 3: Implement strict envelope checks**

Set `REQUIRED_FIELDS` to the eight envelope fields from the design. Validate the decision enum, exact objective string, confidence range, list/dict field types, `PROPOSE`/empty-adjustment consistency, and `HOLD|DATA_ERROR`/empty-adjustment consistency. Preserve reasoning extraction, but never repair a semantically invalid envelope.

```python
OBJECTIVE = "maximize_risk_adjusted_net_return_under_drawdown_cap"
DECISIONS = {"PROPOSE", "HOLD", "DATA_ERROR"}

def _validate_envelope(self, data):
    if data.get("decision") not in self.DECISIONS:
        return "decision must be PROPOSE, HOLD, or DATA_ERROR"
    if data.get("objective") != self.OBJECTIVE:
        return "objective does not match the configured optimization goal"
    adjustments = data.get("adjustments")
    if data["decision"] == "PROPOSE" and not adjustments:
        return "PROPOSE requires adjustments"
    if data["decision"] != "PROPOSE" and adjustments:
        return f"{data['decision']} must not contain adjustments"
    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        return "confidence must be between 0 and 1"
    return None
```

- [ ] **Step 4: Rewrite prompt rules around one objective**

The shared prompt must contain these exact operational rules:

```text
唯一优化目标：在最大回撤硬约束内，最大化扣除手续费、滑点和资金费率后的风险调整收益。
你只能从本轮 ParameterManifest 展示的 entry_quality 参数中选择 1–2 个参数。
你只提出候选；程序通过盲测、成本压力、稳定性和样本门槛决定是否生效。
诊断数据为截止调优时点的此前 8 周；最近 2 周候选成绩不会提供给你。
证据不足、数据缺失、当前参数没有显著问题时必须输出 HOLD。
连续多次 HOLD 不构成必须调参的理由。
```

The user prompts must render `objective`, `market_regime`, `diagnostic_window`, and `allowed_parameters`, and must use the JSON envelope in design section 15.

- [ ] **Step 5: Run parser tests**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_response_contract.py ai_tuner/tests/test_response_parser.py -q`

Expected: all tests pass after updating old fixtures to the new schema.

- [ ] **Step 6: Commit**

```bash
git add ai_tuner/engine/response_parser.py ai_tuner/prompts ai_tuner/tests/test_response_parser.py tests/test_ai_tuner_mvp/test_response_contract.py
git commit -m "feat(ai-tuner): make tuning objective explicit"
```

### Task 5: Remove forced tuning and isolate the legacy auto-apply route

**Files:**
- Modify: `ai_tuner/feedback/learning_signal.py:68-143,284-340`
- Modify: `ai_tuner/config.yaml`
- Modify: `ai_tuner/scheduler/weekly_job.py:99-180,230-520`
- Modify: `ai_tuner/tests/test_learning_signal.py`
- Modify: `ai_tuner/tests/test_auto_apply.py`
- Test: `tests/test_ai_tuner_mvp/test_mvp_routing.py`

- [ ] **Step 1: Write failing safety-routing tests**

```python
from unittest.mock import AsyncMock, MagicMock
import pytest

from ai_tuner.feedback.learning_signal import LearningSignalGenerator
from ai_tuner.scheduler.weekly_job import WeeklyTuningJob


@pytest.mark.asyncio
async def test_repeated_hold_never_creates_forced_adjustment_instruction():
    db = MagicMock()
    db.get_recent_applied_memories = AsyncMock(return_value=[
        {"ai_suggestions": {"adjustments": {}}},
        {"ai_suggestions": {"adjustments": {}}},
        {"ai_suggestions": {"adjustments": {}}},
    ])
    generator = LearningSignalGenerator({"feedback": {"rating": {"min_trades_for_valid": 3}}})
    text = await generator._apply_l4_stale_trigger(db, "btc_eth_aggressive")
    assert text is None


@pytest.mark.asyncio
async def test_legacy_job_skips_mvp_strategy_in_audit_mode(job_factory):
    job = job_factory({"purpose_driven_mvp": {"mode": "audit", "candidates": ["btc_eth_aggressive", "hrs"]}})
    result = await job._tune_single_strategy({"strategy_id": "btc_eth_aggressive", "name": "MTPCS激进策略"})
    assert result == "mvp_managed"
    job.config_operator.apply_overrides.assert_not_called()
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_mvp_routing.py ai_tuner/tests/test_learning_signal.py -q`

Expected: FAIL because L4 still forces a change and no MVP route exists.

- [ ] **Step 3: Make repeated HOLD informational only**

Keep `_apply_l4_stale_trigger()` for compatibility, but return `None` unconditionally and remove the L4 call and constraint paragraph from `build_learning_instructions()`. Delete `stale_unchanged_count` and `stale_adjustment_ratio` from `ai_tuner/config.yaml`.

```python
async def _apply_l4_stale_trigger(self, db_handler, strategy_id):
    logger.debug("连续 HOLD 不触发强制调参", strategy_id=strategy_id)
    return None
```

- [ ] **Step 4: Add explicit MVP modes**

> ⚠️ **实现偏差（见 D3）**：最终实现改为「完整跑流水线、仅拦截写入（步骤12 auto_apply）」，不返回 `mvp_managed`。

```yaml
purpose_driven_mvp:
  mode: audit              # audit | shadow | live
  candidates: [btc_eth_aggressive, hrs]
  selected_strategy: null
  parameter_group: entry_quality
  diagnostic_weeks: 8
  blind_weeks: 2
  max_historical_versions: 3
  min_score_advantage: 5.0
  max_switches_in_days: 7
```

At the top of `_tune_single_strategy()` add:

```python
mvp = self.config.get("purpose_driven_mvp", {})
if strategy_id in set(mvp.get("candidates", [])):
    selected = mvp.get("selected_strategy")
    if mvp.get("mode") != "live" or strategy_id != selected:
        logger.info("策略由目的驱动 MVP 管理，跳过旧调优路径", strategy_id=strategy_id, mode=mvp.get("mode"))
        return "mvp_managed"
```

Teach `run_weekly_tuning()` to count `mvp_managed` as a skip with an explicit detail, without writing an error status.

For both MVP candidates, replace `param_whitelist`, `param_ranges`, and `change_rate_threshold` in `ai_tuner/config.yaml` with `parameter_manifest: ai_tuner/parameters/manifests/<strategy_id>.yaml`. Leave legacy definitions intact for non-MVP strategies until their migration phase.

- [ ] **Step 5: Run focused and legacy tests**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_mvp_routing.py ai_tuner/tests/test_learning_signal.py ai_tuner/tests/test_auto_apply.py -q`

Expected: all tests pass and no MVP candidate calls `apply_overrides()` through the legacy path.

- [ ] **Step 6: Commit**

```bash
git add ai_tuner/feedback/learning_signal.py ai_tuner/config.yaml ai_tuner/scheduler/weekly_job.py ai_tuner/tests tests/test_ai_tuner_mvp/test_mvp_routing.py
git commit -m "fix(ai-tuner): prevent forced and unqualified tuning"
```

### Task 6: Build the reproducible 90-day readiness audit

**Files:**
- Create: `ai_tuner/readiness/__init__.py`
- Create: `ai_tuner/readiness/models.py`
- Create: `ai_tuner/readiness/auditor.py`
- Create: `scripts/ai_tuner_mvp.py`
- Test: `tests/test_ai_tuner_mvp/test_readiness_auditor.py`

- [ ] **Step 1: Write failing selection tests**

```python
from datetime import datetime, timezone
import pytest

from ai_tuner.readiness.auditor import ReadinessAuditor


@pytest.mark.asyncio
async def test_selects_aggressive_when_all_hard_gates_pass(fake_readiness_repo):
    fake_readiness_repo.seed("btc_eth_aggressive", trades=24, signals=180,
        complete_trade_ratio=0.98, config_identity_ratio=0.95,
        kline_coverage=0.99, external_snapshot_coverage=1.0, regime_count=4)
    fake_readiness_repo.seed("hrs", trades=31, signals=220,
        complete_trade_ratio=0.99, config_identity_ratio=0.30,
        kline_coverage=0.92, external_snapshot_coverage=0.20, regime_count=3)
    result = await ReadinessAuditor(fake_readiness_repo).run(
        as_of=datetime(2026, 10, 17, tzinfo=timezone.utc))
    assert result.selected_strategy == "btc_eth_aggressive"
    assert result.candidates["hrs"].eligible is False


@pytest.mark.asyncio
async def test_returns_stop_when_neither_strategy_is_replayable(fake_readiness_repo):
    fake_readiness_repo.seed_incomplete("btc_eth_aggressive")
    fake_readiness_repo.seed_incomplete("hrs")
    result = await ReadinessAuditor(fake_readiness_repo).run(
        as_of=datetime(2026, 10, 17, tzinfo=timezone.utc))
    assert result.selected_strategy is None
    assert result.decision == "STOP"
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_readiness_auditor.py -q`

Expected: FAIL because the readiness package is missing.

- [ ] **Step 3: Define explicit readiness gates**

> ⚠️ **实现偏差（见 D5/D6）**：`ReadinessAuditor(db_manager, config)` 两参数；`min_external_snapshot_coverage` 实际为 `0.0`（库内无外部快照表，降级为报告项）。

```python
# ai_tuner/readiness/models.py
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class CandidateReadiness:
    strategy_id: str
    closed_trades: int
    effective_signals: int
    complete_trade_ratio: float
    config_identity_ratio: float
    kline_coverage: float
    external_snapshot_coverage: float
    regime_count: int
    failures: List[str] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class ReadinessResult:
    as_of: str
    window_days: int
    decision: str
    selected_strategy: Optional[str]
    candidates: Dict[str, CandidateReadiness]

    def to_dict(self):
        return asdict(self)
```

The auditor applies these initial evidence gates, all configurable under `purpose_driven_mvp.readiness`:

```yaml
readiness:
  window_days: 90
  min_closed_trades: 20
  min_effective_signals: 100
  min_trade_field_completeness: 0.95
  min_config_identity_coverage: 0.90
  min_kline_coverage: 0.98
  min_external_snapshot_coverage: 0.95
  min_regime_count: 3
```

The selection order is deterministic: MTPCS aggressive wins when eligible; otherwise HRS wins when eligible; otherwise decision is `STOP`. The report includes actual values and failure text for every gate.

- [ ] **Step 4: Query only timestamp-valid evidence**

Implement a repository inside `auditor.py` using parameterized queries:

```sql
SELECT COUNT(*) AS closed_trades,
       AVG(CASE WHEN symbol IS NOT NULL AND side IS NOT NULL
                     AND quantity > 0 AND price > 0
                     AND realized_pnl IS NOT NULL AND executed_at IS NOT NULL
                THEN 1.0 ELSE 0.0 END) AS complete_trade_ratio
FROM trading.trade_records
WHERE strategy = $1 AND executed_at >= $2 AND executed_at < $3;
```

Use strategy signal tables for `effective_signals`; use registered historical K-line tables for expected/actual closed-bar coverage. Treat a missing historical candidate/OI/market-cap/funding snapshot as zero external coverage rather than filling it with a current value. Derive configuration identity coverage from trade/signal records only after Plan 3 adds identity columns; until then, use version-at-time mapping from immutable overlay file creation times and report the weaker evidence source as `inferred`.

- [ ] **Step 5: Add the audit CLI**

```python
# scripts/ai_tuner_mvp.py
def build_parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit")
    audit.add_argument("--as-of", required=True)
    audit.add_argument("--output", required=True)
    return parser
```

`audit` initializes the existing `DatabaseManager`, runs the auditor, writes sorted UTF-8 JSON atomically, prints the selected strategy and decision, and exits `0` for a selected strategy or `2` for `STOP`.

- [ ] **Step 6: Run the readiness tests**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_readiness_auditor.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add ai_tuner/readiness scripts/ai_tuner_mvp.py ai_tuner/config.yaml tests/test_ai_tuner_mvp/test_readiness_auditor.py
git commit -m "feat(ai-tuner): add mvp readiness audit"
```

### Task 7: Close Plan 1 with regression and an evidence artifact

**Files:**
- Create: `docs/reports/ai-tuner-mvp-readiness-report.example.json`
- Modify: `README.md`

- [ ] **Step 1: Run all Plan 1 tests**

Run:

```bash
python3 -m pytest \
  tests/test_ai_tuner_mvp/test_parameter_models.py \
  tests/test_ai_tuner_mvp/test_parameter_manifest.py \
  tests/test_ai_tuner_mvp/test_proposal_validation.py \
  tests/test_ai_tuner_mvp/test_response_contract.py \
  tests/test_ai_tuner_mvp/test_mvp_routing.py \
  tests/test_ai_tuner_mvp/test_readiness_auditor.py \
  ai_tuner/tests/test_response_parser.py \
  ai_tuner/tests/test_learning_signal.py \
  ai_tuner/tests/test_auto_apply.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run the existing adapter/config regression suite**

Run: `python3 -m pytest ai_tuner/tests/test_config_operator.py ai_tuner/tests/test_version_manager.py tests/test_tuning_overrides.py -q`

Expected: all tests pass.

- [ ] **Step 3: Document the operator command and report schema**

Add to `README.md`:

```markdown
### Purpose-driven AI Tuner MVP readiness

Run the 90-day audit before building or enabling replay:

`python3 scripts/ai_tuner_mvp.py audit --as-of 2026-10-17T00:00:00Z --output var/ai_tuner/readiness.json`

Exit code `0` means one strategy was selected. Exit code `2` means neither candidate is safe to replay; the JSON report lists every failed gate.
```

Create an example report containing both candidates, their raw gate values, explicit failures, selected strategy, window, and `as_of` timestamp.

- [ ] **Step 4: Commit Plan 1 closure**

```bash
git add README.md docs/reports/ai-tuner-mvp-readiness-report.example.json
git commit -m "docs(ai-tuner): document mvp readiness gate"
```
