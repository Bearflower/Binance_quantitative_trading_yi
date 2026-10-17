# Spec：R1/R2/R3 证据与 execution_facts 落地（补齐 D1 消费方 + C1 生产者 + DDL 迁入）

- 日期: 2026-10-15
- 状态: **实现已完成并通过离线测试，待「强制测试 → 审查与文档对照 → 文档更新 → 交接存档」+ 用户统一审查**
- 任务源（用户原话）:「D1 未接消费方（CLI 不带真实 sink）、C1 未接策略生产者；下一步是消费方注入 PG sink + 把 SQL 迁入 `init-scripts/` 这个也修了吧。然后也不 push，不部署，到时候我统一审查。」
- 权威方案: [2026-10-15-strategy-evidence-and-relative-return-plan.md](../../plans/2026-10-15-strategy-evidence-and-relative-return-plan.md)「十一、架构设计：R1/R2/R3 落地细节」（L493-L823）

## 1. 范围（已拍板，不扩）

| 编号 | 目标 | 边界 |
|------|------|------|
| R1 | D1 消费方注入真实 PG sink | 新建 asyncpg sink，幂等写 4 张 `execution_fact_*` 表；CLI 由**开关控制、默认关闭**（默认保持 JSONL 旁路） |
| R2 | C1 接策略生产者（**只接 btc_eth**） | 决策点产出 DecisionEvidence + FeatureEvidence 写 2 张 evidence 表；**配置开关默认关闭**；**旁路化**：写失败不得影响交易决策、不得抛断主循环 |
| R3 | 两份 DDL 迁入 `database/postgres/init-scripts/` | 续号 10、11，幂等，随部署生效（`migrations/` 不自动执行） |

**非目标**：new_coin / grid / hrs / btc_eth_aggressive 本轮不接生产者；不改下单/平仓/资金计算；不 apply 迁移。

## 2. 硬约束（全程有效）

- **不 push main、不部署、不 apply 迁移、不连生产库、不调真实交易所**；完成后待用户统一审查。
- 中文注释与日志；禁硬编码业务参数；禁重复代码（≥5 行同构须抽函数）；禁幽灵参数/死字段；单函数 ≤50 行；单行 ≤120 字符。
- 工作区存在大量其他任务未提交改动，**只碰本轮相关文件**。

## 3. 交付物（已落盘）

| 文件 | 关键内容 |
|------|----------|
| [shared/execution_facts_sink.py](../../../shared/execution_facts_sink.py)（新增） | `PostgresExecutionFactsSink`；`_SQL_FILL_UPSERT/_SQL_INCOME_UPSERT/_SQL_RAW_UPSERT/_SQL_CURSOR_UPSERT/_SQL_CURSOR_LOAD`；`_bulk_upsert_with_raw`（消除 fills/income 同构）；~~`upsert_fills/upsert_income/load_cursor/save_cursor` 全吞异常~~ **（2026-10-16 R2/R3 收口修正：`upsert_fills`/`upsert_income`/`save_cursor` 失败改为 `raise`，由导出循环捕获转为 `errors` + `page_complete=False`；`load_cursor` 读失败仍降级 `return None` 走窗口起点，可接受；游标键扩 `account_id`/`income_type`）**；`_cursor_from_row` 延迟 import `scripts.export_execution_facts.CursorRecord`（不重复定义 dataclass） |
| [shared/decision_evidence_sink.py](../../../shared/decision_evidence_sink.py)（新增） | `PostgresDecisionEvidenceSink.upsert_evidence`；`_SQL_DECISION_UPSERT`（`ON CONFLICT (decision_id) DO UPDATE`，`updated_at=now()`）；`_SQL_FEATURE_UPSERT`（`ON CONFLICT (decision_id,name,source)`）；`_to_text` / `_jsonb_or_none(Optional[str])` |
| [strategies/btc_eth/evidence_recorder.py](../../../strategies/btc_eth/evidence_recorder.py)（新增） | `DecisionEvidenceRecorder.record` → `_do_record_async` → `_build_decision`/`_build_features`/`_write_or_drop`；`_hourly_anchor_ms` 周期锚点；`_jsonable_feature_value`（A-1 修复：Decimal→str、datetime→isoformat）；`_normalize_decision_time`（C-1 修复：naive 按系统本地时区经 `.timestamp()` 转 UTC，杜绝 8h 偏移） |
| [scripts/export_execution_facts.py](../../../scripts/export_execution_facts.py)（改） | `_ENV_DB_WRITE="EXECUTION_FACTS_DB_WRITE"`；`_maybe_open_sink`/`_maybe_close_sink`；`_run_cli`/`_run_cli_fills`/`_run_cli_income`；调用点用 `getattr(self.sink, ...)` 鸭子类型守卫（L408/446/706/726） |
| [strategies/btc_eth/main.py](../../../strategies/btc_eth/main.py)（改） | `initialize()` 内 `bool(decision_evidence.enabled)` 时构造 sink+recorder 挂 `strategy._evidence_recorder`（异常降级关闭）；`run_strategy()` 在 `analyze()` 之后、`if 'direction' in result` 之前单点调用 `await _rec.record(result, symbol)` |
| [strategies/btc_eth/config.yaml](../../../strategies/btc_eth/config.yaml)（改） | 末尾 `decision_evidence:` 段（`enabled: false` / `account_id: ""` / `db_timeout_seconds: 3.0`） |
| [database/postgres/init-scripts/10-decision-evidence.sql](../../../database/postgres/init-scripts/10-decision-evidence.sql)（新增） | 2 表 + 索引 `CREATE ... IF NOT EXISTS`、`CREATE SCHEMA IF NOT EXISTS trading`、表级 + 序列授权、`DO $$ RAISE NOTICE $$`；去 BEGIN/COMMIT |
| [database/postgres/init-scripts/11-execution-facts.sql](../../../database/postgres/init-scripts/11-execution-facts.sql)（新增） | 4 表 + 索引，口径同 10；序列 `trading.execution_fact_cursor_id_seq` 授权 |
| `database/postgres/migrations/2026-10-15-*.sql`（改） | 仅追加文件头「已迁入 init-scripts/10-*/11-*，init-scripts 为权威执行路径」，保留不删不 apply |
| 新增测试 | `tests/test_shared/test_execution_facts_sink.py`、`tests/test_shared/test_decision_evidence_sink.py`、`strategies/btc_eth/tests/test_evidence_recorder.py`（含 A-1/C-1 回归） |

## 4. 接口契约

- **sink 协议（鸭子类型）**：`upsert_fills(facts)` / `upsert_income(facts)` / ~~`load_cursor(source, symbol, window_start_ms)`~~ **`load_cursor(source, account_id, income_type, symbol, window_start_ms)`（2026-10-16 R3 收口：5 参，纳入 `account_id` + `income_type`）** / `save_cursor(cursor)`；脚本侧一律 `getattr(self.sink, "xxx", None)` + `try/except`。
- **SQL 安全**：所有 DML 串禁止 `--`、`/*`、`*/`、`;` 及 `CREATE/GRANT/ALTER/DROP/TRUNCATE`（对齐 `shared/database.py::_validate_sql`）。
- **稳定标识纯函数**：`generate_cycle_id(...)[:16]`、`generate_decision_id(...)[:32]`，不含 `now`；`serialize_decision()` 永不抛异常，用 `ok/failed_records/errors` 传递失败。
- **时间口径**：`decision_time_utc` 为 aware UTC；naive 输入按系统本地时区（容器 `TZ=Asia/Shanghai`）解释后转 UTC。

## 5. 验证要求（待执行）

1. **幻觉测试 10 项**（调度者逐项）：import 存在性 / API 方法名与参数 / 配置 key / 文件路径 / 变量名 / 分支逻辑 / 异常路径 / 异步上下文 / 硬编码 / 文件完整性。
2. **功能测试与覆盖率**（`api-test-pro-local`）：新增/修改函数用例覆盖、分支与异常路径 100%、边界值、外部调用 mock。
3. **代码审查与文档对照**（`code-specification-inspector-local` + `code-document-curator-local`）。
4. **文档更新**（`code-document-curator-local`）：计划 / baseline / changelog / INDEX / README。
5. **交接存档**（`context-handoff`）增量更新 handoff。
6. **第 8 步上线部署：跳过**（未授权）。

## 6. 已完成的独立核实（调度者）

- `python3 -m pytest tests/test_shared/test_decision_evidence.py tests/test_accounting/ tests/test_shared/test_execution_facts_sink.py tests/test_shared/test_decision_evidence_sink.py strategies/btc_eth/tests/test_evidence_recorder.py -q` → **123 passed**（121 → 123，新增 2 条 timestamptz 回归）。
- `git status --short` 确认仅本轮相关文件（+ 其他任务既有未提交改动）被触及。
- A-1 / C-1 / B-3 三处修复已逐行确认落地。
- **幻觉测试新增缺陷 D-tz（已修复）**：`shared/decision_evidence_sink.py` 曾把 ISO 字符串直绑 `TIMESTAMPTZ` 列；asyncpg `timestamptz_encode` 只接受 datetime 实例（`asyncpg/pgproto/codecs/datetime.pyx`），故真实库上整条事务必然失败且被 `except` 静默吞掉。修复：新增 `_iso_to_timestamptz` 并应用于 `decision_time_utc` / `event_time` / `available_at` / `fetched_at` 4 处。
- **SQL ↔ DDL 参数对齐**：6 条 SQL 常量（decision 19 + now()、feature 12、fill 12、income 9、raw 5、cursor 11 + now()、cursor load 3）的列名/列序/列类型与 init-scripts 10/11 逐项一致，无错位；`direction` 取值域 ⊆ {LONG, SHORT}，满足 CHECK。**（2026-10-16 R3 收口后：cursor upsert 与 cursor load 各新增 `account_id`/`income_type` 两列，列数相应 +2 与 +2，与 `11-execution-facts.sql` 新唯一键逐项对齐。）**
- **init-scripts ↔ migrations**：10/11 与对应 migrations 结构逐列一致，差异仅为预期项（init-scripts 的 `IF NOT EXISTS` / schema / GRANT / 序列授权 / `RAISE NOTICE`；migrations 的 `BEGIN/COMMIT`）。
- **游标唯一键修订（2026-10-16 R3 收口）**：`execution_fact_cursor` 唯一键由旧口径 `UNIQUE NULLS NOT DISTINCT (source, symbol, window_start_ms)` **改为** `UNIQUE NULLS NOT DISTINCT (source, account_id, income_type, symbol, window_start_ms)`（`account_id` NOT NULL，`income_type`/`symbol` 可空）。该新键**仅对全新安装生效**，**本轮未对既有库 apply 迁移**；旧游标行因键不匹配不被新代码命中，从窗口起点重采 + 幂等 upsert 回补（冷隔离迁移）。`migrations/2026-10-15-*.sql` 保留不删不 apply。

## 7. 风险与回滚

- 默认关闭（`decision_evidence.enabled: false`、`EXECUTION_FACTS_DB_WRITE` 缺省关闭）→ 生产行为零变化。
- 迁移文件保留在 `migrations/`，未被任何自动路径执行 → 无 apply 风险。
- 回滚：本轮均为新增文件 + 三处增量接线；如需回退，直接 revert 对应接线与新增文件即可，无历史数据改动。
- **已知低风险 ×1：`_iso_to_timestamptz` 对 naive datetime 补 UTC 的口径，与项目「naive=北京时间」约定不一致。** 该函数（[shared/decision_evidence_sink.py](../../../shared/decision_evidence_sink.py#L79-L94)）对 naive `datetime` 直接 `replace(tzinfo=timezone.utc)`，而项目内 naive `TIMESTAMP` 列的既有约定是「naive=北京时间」（如 new_coin 的 `_entry_time_to_naive_beijing`）。此处不返工：本列名以 `*_utc` 结尾（`decision_time_utc` / `event_time` / `available_at` / `fetched_at`），生产者侧 `serialize_decision` / `evidence_recorder` 已保证产出 aware UTC（naive 输入在 recorder 内按容器本地时区 `.timestamp()` 归一），naive 分支仅在异常输入下兜底触发。**仅留痕，不改代码**。
- **已知低风险 ×2（测试已固化现状）**：① `_normalize_decision_time`（[strategies/btc_eth/evidence_recorder.py](../../../strategies/btc_eth/evidence_recorder.py#L136-L157)）以 `> 1e12` 判定毫秒：**恰等于 1e12 的时间戳**会落入「秒」分支，被 `datetime.fromtimestamp(1e12, tz=utc)` 抛 `ValueError/OverflowError`（实际时间戳（毫秒级约 1.7e12、秒级约 1.7e9）不会落到该边界，且异常被 `record()` 外层吞掉，不影响主循环）；② `_cursor_to_args`（[shared/execution_facts_sink.py](../../../shared/execution_facts_sink.py#L125-L140)）对 `request_status` 用 `getattr(..., "ok")` 显式默认，而 `_cursor_from_row`（[同文件](../../../shared/execution_facts_sink.py#L143-L162)）用 `row.get("request_status") or "ok"` 兜底，两处不对称（差异仅作用于 `None`/空串边界，属低风险，已由测试固化当前行为）。**仅留痕，不改代码**。
