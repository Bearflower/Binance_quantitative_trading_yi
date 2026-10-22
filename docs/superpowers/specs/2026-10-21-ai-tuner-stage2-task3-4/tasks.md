# 批次 A（Task 3–4）任务清单

> 规格：`docs/superpowers/specs/2026-10-21-ai-tuner-stage2-task3-4/spec.md`
> 规则：所有改动保持**未提交**；commit/push 需用户显式授权。

## 已完成（验证证据：260 passed）

- [x] Task 1 百分比精度：`format_ratio` 单点实现，门禁 + CLI 共用
- [x] Task 2 universe 契约：`ai_tuner/readiness/universe.py` + `ai_tuner/config.yaml`
- [x] Task 3 逐 symbol 覆盖：`kline_coverage.py`（generate_series + COUNT DISTINCT + 闭合桶 + 缺表 0%）
- [x] 探测去重：`ai_tuner/readiness/db_probe.py`（auditor / kline_coverage 共用）
- [x] 报告字段：`models.py` 新增 `kline_symbols`（JSON 安全）
- [x] 阻断项修复：删除 ImportError fallback（改本地白名单校验）、计数异常产出 gap、测试绝对路径改为相对仓库根

## 待执行

### T3-FIX：generate_series 边界 cast 跟随列类型（D4）

- [x] 文件：`ai_tuner/readiness/kline_coverage.py`
- 改动：`_detect_ts_column` 返回 `(col, data_type)`；新增 `_boundary_cast(dtype)`；SQL 边界 cast 由 `$1::timestamp` 改为按 dtype 选择 `::timestamp` / `::timestamptz`
- 测试：`tests/test_ai_tuner_mvp/test_kline_coverage.py::test_generate_series_cast_follows_column_type`
- 验证：`python3 -m pytest tests/test_ai_tuner_mvp -q` → 261 passed

### Task 4-1：K 线精确区间补采

- [x] 文件：`services/kline_service/scripts/fetch_history_bulk.py`
- 改动：`--days` 与 `--start/--end` 互斥；`end` 按 interval 向下对齐；局部失败 exit 1 + 结构化失败列表
- 测试：`tests/test_kline_service/test_fetch_history_bulk.py`（4 个新用例）
- 验证：`python3 -m pytest tests/test_kline_service/test_fetch_history_bulk.py -q` → 18 passed；`py_compile` OK

### Task 4-2：只读 `kline-audit` CLI

- [x] 文件：`scripts/ai_tuner_mvp.py`
- 改动：新增子命令，固定字段 `strategy_id / window / weighted_coverage / symbols[] / missing_ranges[] / recommended_commands[]`；只读，不自动补采
- 测试：`tests/test_ai_tuner_mvp/test_cli.py`（3 个新用例）
- 验证：`python3 -m pytest tests/test_ai_tuner_mvp -q` → 264 passed；`kline-audit --help` 正常

### A5 代码检测

- 执行：`code-specification-inspector-local`
- 检查：硬编码 / 重复代码 / 幽灵参数 / 中文注释 / 函数 ≤50 行 / 行宽 ≤120
- [x] 结论：通过（两轮修复后）。修复项：`db_probe.bind_bounds` / `_prepare_series` / `_fetch_trading_window` 消除窗口绑定样板；删除 `_merge_missing_starts.max_ranges` 幽灵参数与 `_MAX_MISSING_RANGES`；`compute_kline_coverage` 拆为 `_has_historical_gap` + `_collect_pair_rows`；`_MISSING_SQL` 单行 123 字符换行
- 证据：AST 自查 0 违规（行宽/函数长度），`294 passed`；本地 commit `4827a44`
- 说明：合并 main 后（`4f4e6eb`）冲突解为「保留 `_prepare_series` + 采用 main 的 `timedelta` interval 绑定」；`tests/test_kline_service/test_scheduler.py` 9 个失败为合并前既有基线（main 同口径同样 9 failed / 418 passed），与本次改动无关

### A6 强制测试

- 幻觉测试 10 项（调度者逐项执行，对照真实文件/配置/SQL）
- 功能测试：`api-test-pro-local`
- 覆盖率：新增函数 100% 分支覆盖（含异常路径、空值、缺表、历史缺失）
- [x] 幻觉测试：import 目标（`db_probe`/`CandidateReadiness`/`UniverseResolver`/`SnapshotRepository`）均存在；配置键 `purpose_driven_mvp.readiness.universe.{btc_eth_aggressive,hrs}` 与 `per_strategy` 强制键齐备；`strategies/btc_eth_aggressive/config.yaml` 的 `strategy.symbols` 实读为 5 个 symbol；三张 HRS 快照表在生产库存在（见下）
- [x] 功能测试：322 passed → 补测试后 **343 passed**；`test_kline_service/test_scheduler.py` 9 个失败为既有基线（main 同口径同样失败）
- [x] 覆盖率缺口补齐：新增 `tests/test_ai_tuner_mvp/test_db_probe.py`（13 例，aware/naive/探测失败/三态）与 `test_kline_coverage.py` 8 例（`count_query_row_none`、`unknown_interval`、`table_name_format_invalid`、`timestamp_column_missing`、`universe_resolver_failed`、`universe_not_configured`、未知 interval 合并、切片不相交跳过）
- [x] 审计可解释性最小改动：`StaticUniverseResolver` 在 symbols/intervals 为空时追加 `static_universe_empty:<sid>` / `intervals_not_configured:<sid>` gap（不改变 slice 结构、config_hash 与门禁判定）

### 生产库现状核查（只读，2026-10-22）

- 三表存在：`ai_tuner_universe_scan_runs` / `ai_tuner_universe_snapshots` / `ai_tuner_market_snapshots`（DDL：`database/postgres/init-scripts/13-ai-tuner-market-snapshots.sql`）
- 关键列：`observed_at`(timestamptz)、`source_observed_at`(timestamptz)、`collected_at`(timestamptz)；`universe_snapshots` 含 `membership`；`market_snapshots` 含 OI/资金费率/市值/情绪/`payload_hash`/`raw_payload`
- **当前行数均为 0**（三表空）→ HRS 任何真实资格考试必须返回 `BLOCKED_DATA`；HRS universe resolver 会返回 `historical_universe_missing_before=...`
- 主键均为 `(strategy_id, observed_at, ...)`，与 resolver 的 `observed_at <= as_of` 查询口径一致

### A7 审查 + 文档回标

- 独立审查 + 计划文档回标（Task 3/4 进度、D1/D4 偏差）
- `docs/superpowers/plans/2026-10-17-ai-tuner-mvp-safety-readiness.md` 阶段二前置项进度

### A8 提交与部署（**需用户授权**）

- 分 Task 独立 commit：
  1. `fix(ai-tuner): 精确显示 readiness 覆盖率`
  2. `feat(ai-tuner): 定义策略 universe 历史口径`
  3. `feat(ai-tuner): 按策略 universe 审计 K 线覆盖`
  4. `feat(kline): 按审计缺口精确补采历史数据`
- push main → 观察 Actions → 五层验证
