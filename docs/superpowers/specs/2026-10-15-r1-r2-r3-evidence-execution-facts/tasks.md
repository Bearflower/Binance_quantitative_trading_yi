# Tasks：R1/R2/R3 证据与 execution_facts 落地

> 状态图例：`[x]` 完成（已验证）｜`[~]` 进行中｜`[ ]` 待执行｜`[skip]` 本轮不做

## A. 实现（已完成）

- [x] R1 新增 [shared/execution_facts_sink.py](../../../shared/execution_facts_sink.py)：`PostgresExecutionFactsSink` + 4 表幂等 SQL + 鸭子类型 4 方法
- [x] R1 改 [scripts/export_execution_facts.py](../../../scripts/export_execution_facts.py)：`EXECUTION_FACTS_DB_WRITE` 开关（默认关闭）+ `_maybe_open_sink/_maybe_close_sink` + finally 释放
- [x] R2 新增 [shared/decision_evidence_sink.py](../../../shared/decision_evidence_sink.py)：`PostgresDecisionEvidenceSink.upsert_evidence`
- [x] R2 新增 [strategies/btc_eth/evidence_recorder.py](../../../strategies/btc_eth/evidence_recorder.py)：旁路 recorder（全吞异常）
- [x] R2 改 [strategies/btc_eth/main.py](../../../strategies/btc_eth/main.py)：initialize 接线 + run_strategy 单点 `await _rec.record(...)`
- [x] R2 改 [strategies/btc_eth/config.yaml](../../../strategies/btc_eth/config.yaml)：`decision_evidence.enabled: false`
- [x] R3 新增 [database/postgres/init-scripts/10-decision-evidence.sql](../../../database/postgres/init-scripts/10-decision-evidence.sql) / [11-execution-facts.sql](../../../database/postgres/init-scripts/11-execution-facts.sql)
- [x] R3 [migrations/2026-10-15-*.sql](../../../database/postgres/migrations/) 追加「init-scripts 为权威执行路径」头注
- [x] 新增 3 组离线测试（含 A-1/C-1 回归）

## B. 代码检测（已完成 + 返工闭环）

- [x] `code-specification-inspector-local` 出报告（4 项严重 + 若干疑似）
- [x] 返工：A-1（`_jsonable_feature_value`）、B-1（删空分支）、B-2（拆 `_do_record_async`）、B-3（sink 延迟 import `CursorRecord`）、B-4（`Optional[str]`）、C-1（`_normalize_decision_time`）、D-1..D-4
- [x] 返工后复检（调度者独立复跑，**121 passed**）

## B2. 测试期发现的缺陷修复（已完成）

- [x] **缺陷 D-tz**：`shared/decision_evidence_sink.py` 把 `serialize_decision` 产出的 **ISO 字符串** 直接绑到 4 个 `TIMESTAMPTZ` 列（`decision_time_utc` / `event_time` / `available_at` / `fetched_at`）。证据：asyncpg `pgproto/codecs/datetime.pyx` 的 `timestamptz_encode` 对非 datetime 实例抛 `TypeError` → 真实库上整条事务必然失败且被 `except` 静默吞掉（R2 实际不可用）
- [x] 修复：新增 `_iso_to_timestamptz(v)` 纯函数并应用于上述 4 处（`_build_decision_args` L135、`_build_feature_args` L159/160/162）；补 2 条回归用例
- [x] 复跑：**123 passed**（121 → 123）

## C. 待执行（本轮继续）

- [x] 幻觉测试 10 项（调度者逐项：import / API 方法名参数 / 配置 key / 文件路径 / 变量名 / 分支逻辑 / 异常路径 / 异步上下文 / 硬编码 / 文件完整性）
- [x] 核对 init-scripts 10/11 与 migrations 逐列一致（列类型 / CHECK / UNIQUE / 索引 / 授权），并核对 6 条 SQL 常量与 DDL 列名/列序/列类型无错位
- [x] 功能测试与覆盖率（`api-test-pro-local`）：分支 + 异常路径 100%、边界值、外部调用 mock（3 个被测模块覆盖率 100%；新增 `tests/test_shared/test_export_sink_toggle.py`、`strategies/btc_eth/tests/test_main_evidence_wiring.py`）
- [x] 代码审查与文档对照（`code-specification-inspector-local` 深审无阻断项；`code-document-curator-local` 完成对照与更新）
- [x] 文档更新（`code-document-curator-local`：数据库设计 v2.0 §3.14/§3.15、部署指南 v1.5、plan、spec §7、changelog/INDEX、README、baseline）
- [x] 审查期轻微项清理（幽灵导入 `Tuple`、魔法数 `[:200]`→已定义常量、`availability` 显式 None→NOT NULL 兜底）
- [x] 交接存档（`context-handoff`）：`docs/handoffs/strategy_evidence_execution_facts_2026-10-16.md`

## C2. 已识别的后续工单（本轮不改，不扩大范围）

- [ ] **设计待裁决**：`main.py` 的 `await _rec.record(...)` 是**有界阻塞等待**（`db_timeout=3.0s`），5 币种最坏 +15s；`enabled` 默认 false 故当前无影响。是否改后台任务（`asyncio.create_task`）由用户裁决
- [ ] `_normalize_decision_time` 的 `> 1e12` 阈值：恰等于 1e12 的时间戳按秒解释并抛异常（被外层吞掉；真实毫秒时间戳 1.7e12 不受影响）
- [ ] `_hourly_anchor_ms` / `_normalize_decision_time` 在 `result['timestamp']` 缺失时回落 `now`，与契约「cycle_anchor 非 now」存在口径张力
- [ ] `DatabaseManager` 构造块（env 名 + 默认密码）在 `scripts/export_execution_facts.py` 与 `strategies/btc_eth/main.py` 重复 → 建议抽共享工厂（涉及既有代码，本轮不动）
- [ ] `main.py` 的 `initialize()`（~103 行）与 `run_strategy()`（~221 行）超 50 行（既有问题，非本轮引入）

## D. 本轮不做（未授权）

- [skip] 上线部署（不 push / 不部署 / 不 apply 迁移）
- [ ] **用户统一审查**（所有 C 段完成后汇报，等待用户裁决）

## 验收证据

- 基线命令：`python3 -m pytest tests/test_shared/test_decision_evidence.py tests/test_accounting/ tests/test_shared/test_execution_facts_sink.py tests/test_shared/test_decision_evidence_sink.py strategies/btc_eth/tests/test_evidence_recorder.py -q` → **123 passed**
- 全量命令（含功能测试新增文件）：再加 `tests/test_shared/test_export_sink_toggle.py strategies/btc_eth/tests/test_main_evidence_wiring.py` → **188 passed**
- 覆盖率：`shared/execution_facts_sink.py` / `shared/decision_evidence_sink.py` / `strategies/btc_eth/evidence_recorder.py` 均 **100%**（244 stmts，0 miss）
- `scripts/export_execution_facts.py` 的 `_maybe_open_sink`/`_maybe_close_sink` 由 24 个用例覆盖；`main.py` 接线由 7 个用例覆盖
