---
id: Binance_quantitative_trading-20261016-fact-before-cursor-transaction-boundary
类型: decision
项目: Binance_quantitative_trading
标题: 幂等采集采用「先事实后游标」事务边界：事实落库确认成功才推进游标，失败上抛为不完整
日期: 2026-10-16
标签: [数据采集, execution_facts, 游标, 事务边界, 幂等, 重试]
任务标识: strategy_remediation_R1-R6_2026-10-16
状态: 已采纳
审查: 待审查
---

# 幂等采集采用「先事实后游标」事务边界

## 背景
只读采集脚本（`scripts/export_execution_facts.py`）原先「先 `_save_cursor`、再写事实」，且事实/sink 失败被静默吞掉，导致重试后永久漏采较早记录（见 [cursor-before-fact-silent-drop](../issues/Binance_quantitative_trading-20261016-execution-facts-cursor-before-fact-silent-drop.md)）。需确定游标推进与事实写入的顺序与失败反馈口径。

## 备选方案
- **A：事实与游标同事务提交**——最强一致，但需重构 sink 事务边界、改动面大，且跨「事实表 + 原始留痕表 + 游标表」的多表事务复杂。
- **B（采纳）：先可靠写事实并确认成功，再推进游标**——复用现有 `upsert_fills`/`upsert_income`（已同事务写 fact + raw）；事实幂等 upsert，重复写无害。
- **C：游标仅在内存推进、进程结束统一落盘**——进程崩溃丢失全部进度，不可取。

## 决策
采纳 B，并配套两条硬约束：①事实（含 `execution_fact_raw`）写入成功并确认后，才 `save_cursor`（游标覆盖位置不得领先于已持久化事实）；②sink 的 `upsert_fills`/`upsert_income`/`save_cursor` 失败**必须 `raise`**（不再吞异常），由导出循环捕获 → `page_complete=False` + `errors` 非空 + 游标不推进。`load_cursor` 读失败仍降级 `return None` 走窗口起点。

## 理由
- 改动面小、复用现有 sink 事务能力，满足「不引入新中间件、优先复用现有 sink 事务」约束（原需求 R2「不做什么」）。
- 幂等 upsert 使「游标写失败后重采该段」无副作用（重复写无害），重试可回补。
- 失败显式上抛把「伪成功（已完成 + 零错误）」转为可见失败，杜绝静默漏采。

## 后果
- 收益：任一写失败不推进游标、保持可重试范围；重试后持久化集合 == 完整采集集合。
- 技术债：游标写失败会触发该窗口段重采（幂等，但多一次请求）；「事实与游标严格同事务」未实现（换取低改动面）。
- 回退安全：顺序反转集中在 `_run_export_loop` 唯一出口，revert 即可。

## 适用范围与修订
- 适用：`scripts/export_execution_facts.py`（`_run_export_loop` / `_sink_upsert_checked` / `_save_cursor_checked`）、`shared/execution_facts_sink.py`（`upsert_fills`/`upsert_income`/`save_cursor`）。基线与收口：`strategy_remediation_R1-R6_2026-10-16`。
- 替代条件：若未来引入「原始响应持久队列 + 事实批提交」的更强一致性设计，本决策可迁移；届时新建 decision 并链接，无替代。

## 关联与证据
- 来源任务: `strategy_remediation_R1-R6_2026-10-16`
- 证据摘要: 回归 `tests/test_shared/test_execution_facts_regression.py::TestR2CursorBeforePersist` 三用例通过（`first_marked_complete=False`、`first_errors` 非空、`persisted_after_retry_ids == expected_ids`）；`cursor_before_persist` 场景反转；本轮新增失败 0
- 关联 issue: [cursor-before-fact-silent-drop](../issues/Binance_quantitative_trading-20261016-execution-facts-cursor-before-fact-silent-drop.md)
- 关联文档: 需求 `docs/research/strategy_remediation/requirements_R1-R6_2026-10-16.md` §2-R2；架构 `architecture_R1-R6_2026-10-16.md` §2.4
