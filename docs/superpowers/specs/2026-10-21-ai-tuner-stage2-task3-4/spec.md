# AI Tuner 阶段二 批次 A（Task 3–4）实施规格

- 制定时间：2026-10-21
- 上游计划：`docs/superpowers/plans/2026-10-21-ai-tuner-stage2-parallel-readiness.md`（Task 3 / Task 4）
- 工作区：`/Users/yl/.codex/worktrees/ai-tuner-stage2-readiness-plan/Binance_quantitative_trading`
- 分支/基线：`codex/ai-tuner-stage2-readiness-plan`，HEAD `10d396b`
- 状态：Task 1/2/3 代码已完成（**全部未提交**），Task 4 未开始

## 1. 目标

在不降低任何 readiness 门禁（`min_effective_signals=100`、`min_kline_coverage=0.98`、HRS `min_external_snapshot_coverage=0.95` 且 required）的前提下，完成批次 A：

1. K 线覆盖从「主力合约抽样」升级为「策略 universe 逐 symbol、按闭合桶加权」（Task 3）。
2. 输出逐 symbol/period 缺口清单，并提供**只读** CLI `kline-audit` 与 K 线服务精确区间补采能力（Task 4）。
3. 审计结果必须真实暴露缺口，**不允许**为凑门禁而调整阈值、伪造区间或放宽断言。

## 2. 非目标

- 不实现 HRS 外部快照（Task 5–8，批次 C）。
- 不实现确定性回放（Task 10，批次 D）。
- 不修改策略参数、覆盖层 `.active`、`selected_strategy`、`purpose_driven_mvp.mode`。
- 不自动补采数据；`kline-audit` 只生成建议命令。

## 3. 已完成部分的已核实事实（不再重复实现）

| 项 | 位置 | 结论 |
|---|---|---|
| 百分比精度 | `ai_tuner/readiness/auditor.py` `format_ratio` | 门禁/CLI 单一实现，`97.96% < 98.00%` |
| universe 契约 | `ai_tuner/readiness/universe.py` | 静态配置 + 历史快照，历史查询含 `observed_at <=`，无 `strategy_states` 回退 |
| 逐 symbol 覆盖 | `ai_tuner/readiness/kline_coverage.py` | `generate_series` 左连接 + `COUNT(DISTINCT open_time)`，闭合桶对齐，缺表按 0% |
| 探测去重 | `ai_tuner/readiness/db_probe.py` | `data_type` 探测 / naive-aware 绑定 / 表存在性唯一入口 |
| 报告字段 | `ai_tuner/readiness/models.py` `kline_symbols` | JSON 安全（datetime 已字符串化） |

**验证证据**：`python3 -m pytest tests/test_ai_tuner_mvp -q` → `260 passed`。

## 4. 决策记录（需在文档回标时写入）

**D1：ai_tuner 不 import `services.kline_service.core.table_name_guard`。**
事实：`ai_tuner/Dockerfile` 仅 COPY `ai_tuner/`、`shared/`、`scripts/ai_tuner_mvp.py`、5 个策略 config、`VERSION`，**不含 `services/`**；且 `table_name_guard` 依赖 `shared.utils.logger`，而仓库 `shared/utils.py` 是单文件模块而非包（kline-service 镜像用的是 `services/kline_service/shared/utils/` 自带包），运行时必然 `ImportError`。
替代：`kline_coverage._build_table_name()` 做三重白名单校验（interval 白名单 + symbol `[A-Z0-9]{2,20}` + 最终表名正则），fail-closed，禁止裸拼接。与计划 §3.3 的偏差已写入代码注释。

**D2：计数 SQL 异常必须产出 gap，不得静默记 0。**
`count_query_failed:<err>` / `count_query_row_none` 透传到 `SymbolCoverage.gap`，`expected` 用应有桶数兜底、`actual=0`。理由：审计必须能区分「确实没数据」与「查询炸了」。

**D3：`compute_kline_coverage` 固定 schema=`btc_eth`，删除 `schema` 形参；`_merge_missing_starts` 删除 `periods` 形参（改用模块常量）。** 消除可变攻击面与冗余形参。

**D4（本轮唯一未落实的正确性项）：`generate_series` 边界类型必须跟随列 `data_type`。**
现状 SQL 固定 `$1::timestamp`；若某张 K 线表 `open_time` 为 `timestamptz`，会话时区（容器 `TZ=Asia/Shanghai`）会把边界整体偏移 8 小时 → 覆盖率错算。
决策：`_detect_ts_column` 同时返回 `(col, data_type)`，`_COUNT_SQL`/`_MISSING_SQL` 的边界 cast 按 dtype 选择 `::timestamp`（naive）或 `::timestamptz`（aware），并新增断言该 cast 与 dtype 一致的测试。

**D5：`missing_ranges` 只按真实连续性合并。** 不因条数超过 200 而把离散缺口伪造成连续区间；补采建议必须保持精确。
这是「上界区间 + 不伪造细目」的折中；不截断成假数据。

**D6：门禁阈值与比较逻辑零改动。** 新口径只会让数值更严格（HRS 因 universe 历史缺失，K 线覆盖从 83.70% 变为 0% + `historical_universe_missing` gap），这是正确行为，**不得**为了让报告好看而回退。

## 5. 待执行变更（decision-complete）

### T3-FIX（D4）

- 文件：`ai_tuner/readiness/kline_coverage.py`
  - `_detect_ts_column(db, schema, table)` → `_detect_ts_column(db, schema, table) -> tuple[Optional[str], Optional[str]]`（列名, data_type）。
  - 新增 `_boundary_cast(dtype: Optional[str]) -> str`，返回 `"timestamptz"`（dtype == `db_probe.TS_AWARE`）否则 `"timestamp"`。
  - `_COUNT_SQL` / `_MISSING_SQL` 的 `$1::timestamp, $2::timestamp` 改为格式化占位 `$1::{cast}, $2::{cast}`（cast 只来自 `_boundary_cast` 常量输出，不接受外部输入）。
  - `_query_count` / `_query_missing_starts` 增加 `cast` 形参并透传。
- 测试：`tests/test_ai_tuner_mvp/test_kline_coverage.py` 新增 `test_generate_series_cast_follows_column_type`：假 DB 返回 `timestamp with time zone` → 断言 SQL 含 `$1::timestamptz`；返回 `timestamp without time zone` → 含 `$1::timestamp`。

### Task 4-1：K 线补采支持精确 UTC 区间与局部失败

- 文件：`services/kline_service/scripts/fetch_history_bulk.py`
  - 参数互斥：`--days N` **或** `--start 2026-07-23T00:00:00Z --end 2026-10-21T00:00:00Z`；两者同时给出 / 只给一个 → `parser.error`（exit 2）。
  - 时间解析：ISO8601，必须带 `Z` 或 `+00:00`；`end` 按 interval 向下对齐（只取闭合 K 线）。
  - 请求边界不变式：所有 `collect_klines` 调用的 `start_time >= int(start.timestamp()*1000)`、`end_time <= int(end.timestamp()*1000)`。
  - 任一 `(symbol, interval)` 失败 → 最终 exit code **1**，打印结构化失败列表（JSON 到 stdout 或 `logger.error` 结构化字段，避免"部分失败但整体成功"）。
- 测试：`tests/test_kline_service/test_fetch_history_bulk.py` 新增
  - `test_collect_range_never_requests_outside_utc_window`
  - `test_one_symbol_failure_is_reported_and_exit_is_nonzero`
  - `test_days_and_start_end_are_mutually_exclusive`
  - `test_end_is_aligned_down_to_interval_boundary`

### Task 4-2：只读 `kline-audit` CLI

- 文件：`scripts/ai_tuner_mvp.py`
  - 子命令 `kline-audit --strategy <id> --as-of <iso> [--output PATH]`
  - 输出固定字段：`strategy_id`、`window{start,end,days}`、`weighted_coverage`、`symbols[]`（复用 `symbol_row_to_dict`）、`missing_ranges[]`（跨 symbol 展平，含 symbol/interval）、`recommended_commands[]`（形如 `python3 services/kline_service/scripts/fetch_history_bulk.py --symbols BTCUSDT --intervals 1h --start ... --end ...`）。
  - **只读**：只允许 SELECT/DDL-free；不得写库、不得触发补采。
  - 无 `--output` → 打印到 stdout；JSON 必须 `json.dumps(..., ensure_ascii=False)` 可解析。
- 测试：`tests/test_ai_tuner_mvp/test_cli.py` 新增
  - `test_kline_audit_emits_required_fields`
  - `test_kline_audit_is_read_only`（断言 auditor/DB 只发生读查询）
  - `test_kline_audit_writes_output_file`

## 6. 验证步骤（必须逐条执行并贴真实输出）

1. `python3 -m pytest tests/test_ai_tuner_mvp -q` → 期望 ≥260 passed，0 failed
2. `python3 -m pytest tests/test_kline_service/test_fetch_history_bulk.py -q` → 新增用例全绿（该目录既有 3 个 cron 相关失败与本任务无关，需在报告中标注既有失败）
3. `python3 scripts/ai_tuner_mvp.py kline-audit --strategy btc_eth_aggressive --as-of 2026-10-21T00:00:00Z --output /tmp/kline-audit.json` → exit 0、JSON 可解析、无数据库写入
4. 幻觉测试 10 项（imports / API 方法 / 配置键 / 路径 / 变量 / 分支 / 异常路径 / 异步上下文 / 硬编码 / 文件完整性）
5. `python3 -m py_compile` 覆盖全部改动文件

## 7. 风险与回退

| 风险 | 影响 | 回退 |
|---|---|---|
| 新口径使 HRS K 线覆盖显示 0%（universe 历史缺失） | 报告数值变差但不影响决策（本就 STOP） | 无需回退；Task 5 建表后自动恢复 |
| `fetch_history_bulk` 参数改造影响既有周任务 | 补采脚本被其他流程调用 | 保留 `--days` 原路径，仅新增互斥分支 |
| 逐 symbol 查询放大 DB 负载（5 symbol × 3 interval = 15 对/策略） | 审计耗时增加 | 审计频率本身为低频（周/手动），可接受；必要时后续加缓存 |

## 8. 审批门（未获授权不得执行）

- 批次 A 代码检测（`code-specification-inspector-local`）
- 批次 A 幻觉测试 + 功能测试（`api-test-pro-local`）
- 独立审查 + 文档回标（plan / spec 口径）
- **分 Task 独立 commit + push main（需用户显式授权）**
