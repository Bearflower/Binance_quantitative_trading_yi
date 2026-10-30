# AI Tuner 前瞻资金费日程采集 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每五分钟为 MTPCS 激进版 BTC/ETH 与 HRS 历史候选池追加留存当时可见的逐币种资金费结算预告；采集缺口可审计，不触碰策略与覆盖层。

**Architecture:** 独立只读行情采集进程通过两个公开 GET 端点取得 `nextFundingTime` 与辅助间隔，按确定性 UTC 槽位写入追加式批次和逐币种观测。数据库角色只可读 HRS universe、只可向证据表追加；重试复用 `attempt_id` 并比较已写内容。此计划只交付前瞻采集，不把历史数据判为合格；离线验证及资格考试接线另列后续计划。

**Tech Stack:** Python 3.10、asyncio、asyncpg/`DatabaseManager`、PostgreSQL、pytest、Docker Compose。

**Design:** `docs/superpowers/specs/2026-10-30-ai-tuner-funding-schedule-evidence-design.md`

---

## 文件边界

- `database/postgres/init-scripts/14-ai-tuner-funding-schedule-evidence.sql`：两张仅追加证据表与最小权限角色。
- `ai_tuner/funding_schedule/models.py`：规范化 UTC 时间、哈希、字段校验。
- `ai_tuner/funding_schedule/collector.py`：只读接口采集与策略币种并集，不含资格考试逻辑。
- `ai_tuner/funding_schedule/repository.py`：事务性保存批次及观测、冲突拒绝。
- `ai_tuner/funding_schedule/cli.py`：`once`/五分钟 daemon 与失败告警。
- `docker-compose.yml`、`ai_tuner/Dockerfile`、`scripts/remote_deploy_runtime.sh`：独立容器和最小权限运行身份，不改交易容器命令。
- `tests/test_ai_tuner_mvp/test_funding_schedule_*.py`：合成测试；数据库测试需显式测试库，不能碰生产。

## Task 1：追加式表与权限

- [ ] **Step 1：写失败测试。** 在 `tests/test_ai_tuner_mvp/test_funding_schedule_schema.py` 检查新 SQL 包含 `ai_tuner_funding_schedule_runs`、`ai_tuner_funding_schedule_observations`、`ai_tuner_funding_writer`；表有 `attempt_id`/`symbol` 主键、`scheduled_at`/`received_at`/`source_time`/`next_funding_time`、SHA-256 字段和时间约束；角色只有证据表 SELECT/INSERT 与 HRS universe SELECT，明确无 UPDATE/DELETE/TRUNCATE。运行 `python3 -m pytest -q tests/test_ai_tuner_mvp/test_funding_schedule_schema.py`，预期文件不存在导致测试失败。
- [ ] **Step 2：实现最小 DDL。** 两表按 `attempt_id UUID` 关联；批次状态 `success|failed`，失败原因非空；观测唯一 `(attempt_id,symbol)`，`next_funding_time > source_time`，`source_time <= received_at`。只使用 `CREATE TABLE/INDEX IF NOT EXISTS`、`GRANT SELECT,INSERT` 与明确的 `REVOKE`，不修改已有交易表。
- [ ] **Step 3：复测并提交。** 运行新测试及 `git diff --check`；提交 `feat(ai-tuner): add append-only funding schedule evidence tables`。

## Task 2：只读响应规范化

- [ ] **Step 1：写失败测试。** 在 `tests/test_ai_tuner_mvp/test_funding_schedule_models.py` 用 BTC/ETH 与 HRS 候选并集、重复币种、缺少 `nextFundingTime`、来源时间滞后大于 120 秒、来源时间在接收时间之后、币种漏报、4/8 小时 `fundingIntervalHours`、无辅助间隔等夹具，要求返回结构化缺口或规范化行。运行目标文件，预期 import 失败。
- [ ] **Step 2：实现纯函数。** `normalize_observations(*, scheduled_at, received_at, required_symbols, premium_rows, funding_info_rows) -> (canonical_hash, observations)`；只接受 aware UTC 时间和正整数毫秒；按 symbol 排序并用 `json.dumps(..., sort_keys=True, separators=(",", ":"))` 算所选字段哈希；不得对 `fundingInfo` 缺项推断默认 8 小时。
- [ ] **Step 3：复测并提交。** 运行目标测试和全部 `tests/test_ai_tuner_mvp`，提交 `feat(ai-tuner): normalize prospective funding schedule observations`。

## Task 3：采集与事务落库

- [ ] **Step 1：写失败测试。** 在 `tests/test_ai_tuner_mvp/test_funding_schedule_collector.py` 与 `test_funding_schedule_repository.py` 用 FakeDB/FakeBinance 断言仅调用 `GET /fapi/v1/premiumIndex` 与 `GET /fapi/v1/fundingInfo`；required symbols = `BTCUSDT,ETHUSDT` ∪ `latest_universe('hrs', scheduled_at)`；一次成功批次与观测同事务；相同 `attempt_id`/相同哈希重试幂等，内容冲突拒绝；接口失败留下 failed 批次，不伪造成功观测。运行两文件，预期 import 失败。
- [ ] **Step 2：实现。** `FundingScheduleCollector.collect_once(scheduled_at, attempt_id)` 使用注入的只读 client/repository/clock；`FundingScheduleRepository.store_attempt(...)` 使用单个 `execute_transaction` 或显式事务。写入前完成规范化；对既有 attempt 比较状态、哈希与全部观测，任何不一致抛冲突错误。不得调用任何交易或参数写接口。
- [ ] **Step 3：复测并提交。** 运行目标测试、全套 AI Tuner MVP 测试与 `git diff --check`，提交 `feat(ai-tuner): collect immutable funding schedule attempts`。

## Task 4：独立运行入口与上线安全边界

- [ ] **Step 1：写失败测试。** 在 `tests/test_ai_tuner_mvp/test_funding_schedule_cli.py` 验证 UTC 五分钟对齐、漏一次计划槽位不追溯填值、连续失败告警、`once` 不循环；静态检查 Compose 新服务不共享交易 API 凭据、不调用回放命令，Dockerfile 能 import 新入口；`scripts/remote_deploy_runtime.sh` 为新角色生成服务器侧 0600 密码文件并验证权限。运行目标测试，预期失败。
- [ ] **Step 2：实现。** 新服务 `ai-tuner-funding-schedule` 复用 ai-tuner 镜像，命令 `python3 -m ai_tuner.funding_schedule.cli daemon`；仅加载独立 DB 凭据与公开行情读接口，容器重启策略 `unless-stopped`。daemon 每五分钟触发一次，时间槽与实际请求时间分开保存；失败记录/日志/飞书告警不干扰 `ai-tuner` 和 `ai-tuner-snapshot`。部署脚本创建新登录角色密码并进行只读权限断言。
- [ ] **Step 3：复测并提交。** 运行目标测试、全套 `tests/test_ai_tuner_mvp`、`docker compose config --quiet`（若本机缺生产 secret 则以静态 Compose 测试代替并报告），提交 `feat(ai-tuner): run prospective funding schedule collector separately`。

## Task 5：最终审查与交付边界

- [ ] 对 Task 1–4 做规格符合性复审，再做代码质量复审；修复所有重要问题。
- [ ] 运行 `python3 -m pytest -q -p no:cacheprovider tests/test_ai_tuner_mvp`、`git diff-tree --check HEAD^ HEAD`、`git status --short`，输出提交 SHA 与证据。
- [ ] 未经单独上线核对，不 push/deploy；即使采集器上线，90 天历史日程仍不完整，真实资格考试继续 `BLOCKED_DATA`。下一个独立计划才做离线验证与资格考试接线。
