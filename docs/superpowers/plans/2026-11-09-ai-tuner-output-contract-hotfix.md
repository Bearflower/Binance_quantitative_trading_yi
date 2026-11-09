# AI Tuner MVP 输出契约修复实施计划

> 本计划记录本次独立热修的实现与验证证据。

**Goal:** 让两个 MVP 候选的提示词与已批准的八字段信封解析器一致，同时保持 `audit` 模式不写覆盖层。

**Architecture:** 以 `docs/superpowers/specs/2026-10-16-purpose-driven-ai-tuner-design.md` §15 为权威契约。只修正共享新契约提示词示例和候选失败后的重试说明，不放宽解析器、不更改旧契约或策略参数。测试直接渲染两策略的实际系统提示词，并用生产解析器解析其中的 JSON 示例。

**Tech Stack:** Python、pytest、Jinja 模板、现有 `ResponseParser`。

---

### Task 1: 用失败测试固定共享提示词与解析器的一致性

**Files:** `ai_tuner/tests/test_prompt_contract_routing.py`

- [x] 在现有测试中对 `btc_eth_aggressive` 和 `hrs` 分别调用 `WeeklyTuningJob._build_prompts()`，提取系统提示词中第一个 `json` 代码块。
- [x] 对示例断言 `diagnosis` 为列表、`target_metrics` 和 `expected_impact` 为字典；用 `ResponseParser.parse_response(..., contract_mode="new")` 验证示例为合法 `HOLD`，且 `adjustments == {}`。
- [x] 运行 `python3 -m pytest -q ai_tuner/tests/test_prompt_contract_routing.py`，确认当前示例因字段类型和决策值矛盾而失败。

### Task 2: 修复最小提示词与重试说明

**Files:** `ai_tuner/prompts/common_rules.txt`、`ai_tuner/scheduler/weekly_job.py`、`tests/test_ai_tuner_mvp/test_audit_mode.py`

- [x] 把 `common_rules.txt` 示例改为可被解析器接受的完整 `HOLD` 报文：`diagnosis` 为列表，`target_metrics`/`expected_impact` 为字典，`adjustments` 为空；保留 `PROPOSE` 的 `from/to` 字段规则。
- [x] 在现有失败重试测试中断言第二次请求明确写出三项类型和 `HOLD/DATA_ERROR` 的空调整规则；先运行该测试并观察预期失败。
- [x] 只修改 `weekly_job.py` 的新契约重试文字，使其与共享示例一致，旧契约分支保持原样。
- [x] 分别运行 `ai_tuner/tests` 与 `tests/test_ai_tuner_mvp`（仓库当前不能把这两个 pytest 包放在同一命令收集），确认候选不写覆盖层、旧策略原有行为不变：237 passed；753 passed、5 skipped。

### Checkpoint: 提交前验证与复审

- [x] 分别运行相关契约套件：解析器与提示词 48 passed；审计模式与双契约 60 passed。
- [x] 运行 `git diff --check`，核对 diff 仅限契约提示词、重试文案、测试和本计划。
- [x] 独立审查：发现并修复非空结构化 `diagnosis` 的下游拼接异常；未放宽解析器、未改 `auto_apply` 或 `purpose_driven_mvp.mode`。

**审查补项：** 独立审查发现真实 §15 `diagnosis` 对象列表在周度流程中被当作字符串拼接。已先添加双候选、`HOLD`/`PROPOSE` 四组失败测试，再把对象无损序列化为下游通知与记忆可用的文字；重新验证 `ai_tuner/tests` 237 passed、`tests/test_ai_tuner_mvp` 757 passed / 5 skipped。

**Production guard:** 本次改动即使部署，也仍维持 `audit + selected_strategy=null`。部署成功不能等同于回放、金样对账或资格考试通过，真实数据结论继续 `BLOCKED_DATA`。
