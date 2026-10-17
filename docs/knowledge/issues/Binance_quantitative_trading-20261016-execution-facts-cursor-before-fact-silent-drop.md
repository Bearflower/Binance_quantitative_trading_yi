---
id: Binance_quantitative_trading-20261016-execution-facts-cursor-before-fact-silent-drop
类型: issue
项目: Binance_quantitative_trading
标题: 采集游标先于事实落库且写失败被吞，重试后永久漏掉较早成交记录
日期: 2026-10-16
标签: [bug, 静默失效, 数据采集, execution_facts, 游标, 事务边界, 高优先级]
任务标识: strategy_remediation_R1-R6_2026-10-16
状态: 已解决
审查: 待审查
---

# 采集游标先于事实落库且写失败被吞，重试后永久漏掉较早成交记录

## 现象
只读采集脚本 `scripts/export_execution_facts.py` 在「先保存游标、再写事实」的顺序下，事实写入失败被静默吞掉，导出仍返回 `page_complete=true`、`errors=[]`。游标已推进到较大时间，恢复写入后重试只采到较晚的记录，**较早的一笔永久漏采**（离线复现：两笔成交 t=1000/1100，第一次写事实失败，游标已到 1100，重试只保存第二笔）。该缺陷即独立复审 R2。

## 根因
- 顺序错误：`_run_export_loop` 先 `_save_cursor(cursor)`、后 `_maybe_sink_upsert(...)`，游标覆盖位置**领先于**已持久化事实。
- 失败不可见：`_maybe_sink_upsert` 与 `shared/execution_facts_sink.py` 的 `_bulk_upsert_with_raw`、`save_cursor` 均「全吞异常 + warning」，失败不反馈为导出未完成。
- 结果：游标已推进但事实未落库，重试从推进后的起点开始，漏掉未落库的较早记录；且返回「已完成 + 零错误」的伪成功。

## 解决
- 事务边界反转为「**先可靠写事实（含 `execution_fact_raw`）并确认成功，再推进游标**」：`_run_export_loop` 收尾改为 `_sink_upsert_checked(...)` 成功后才 `_save_cursor_checked(cursor)`；事实为幂等 upsert，重复写无害。
- sink 的 `upsert_fills`/`upsert_income`/`save_cursor` 失败**改为 `raise`**（不再全吞），由导出循环捕获转换为 `errors`；任一失败 → `cursor.page_complete=False` 且 `errors` 非空、游标不推进。
- `load_cursor` 读失败仍降级 `return None` 走窗口起点（可接受）。
- 选型说明：采用「先事实后游标」而非「事实与游标同事务」——改动面小、复用现有 sink 事务能力、不引入新中间件（原约束）。

## 验证
- 回归用例 `tests/test_shared/test_execution_facts_regression.py::TestR2CursorBeforePersist`（先红后绿）：`test_upsert_fails_retry_collects_all`、`test_save_cursor_fails_retry_collects_all`、`test_mid_run_termination_retry_collects_all` —— 三种故障注入（事实写失败 / 游标写失败 / 中途终止）下，`first_marked_complete=False`、`first_errors` 非空、重试后持久化 trade_id 集合 == 全集。
- 复现脚本 `docs/reviews/strategy_reaudit_2026-10-16/reproduce_findings.py` 的 `cursor_before_persist` 场景已反转。
- 结果：本轮新增失败 0；既有失败（test_fix_verification 等）非本例引入，另见 triage。

## 教训
「先推进进度游标、再落库数据」+「失败吞掉只打日志」是**数据丢失组合拳**：游标是幂等采集的恢复锚点，一旦领先于已持久化事实，重试就会被锚点跳过丢失段。**先事实后游标**（或同事务）并让失败**显式上抛**为不完整，才可由重试回补。（AI 归纳，仅供参考）

## 关联
- 来源任务: `strategy_remediation_R1-R6_2026-10-16`（R2 收口）
- 持久证据: 复审报告 `docs/reviews/strategy_reaudit_2026-10-16/README.md` §1-R2 与 §7.1；需求 `docs/research/strategy_remediation/requirements_R1-R6_2026-10-16.md` §2-R2；架构 `architecture_R1-R6_2026-10-16.md` §2.4、§7
- 必要证据摘要: 顺序 `_save_cursor` 先于 `_maybe_sink_upsert`；sink 三处全吞异常；修复后 upsert/save_cursor raise + 先事实后游标；回归 3 用例通过；本轮新增失败 0
- 相关 issue: [income-history-silent-empty-non-unified](./Binance_quantitative_trading-20261015-income-history-silent-empty-non-unified.md)（同属采集完整性/静默失效族，R3 关联）；[same-ms page advance](./Binance_quantitative_trading-20261016-execution-facts-same-ms-page-advance.md)（R6，同采集脚本）
- 相关 decision: [先事实后游标事务边界](../decisions/Binance_quantitative_trading-20261016-fact-before-cursor-transaction-boundary.md)
