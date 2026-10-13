---
id: Binance_quantitative_trading-20260921-allocation-pnl-collect-always-zero
类型: issue
项目: Binance_quantitative_trading
标题: 月度分配 PnL 采集恒 0 致排名失效（落库名映射缺失 + SQL 不支持数组）
日期: 2026-09-21
标签: [bug, ai_tuner, PnL采集]
任务标识: monthly_allocation_4strategy_split_2026-09-21.md
状态: 已解决
审查: 已审查
---

# 月度分配 PnL 采集恒 0 致排名失效（落库名映射缺失 + SQL 不支持数组）

## 现象
ai-tuner 月度资金分配中，各参与策略的当月已实现 PnL 采集恒为 0，收益率排名失去依据。该缺陷与本任务的「首月 fallback 口径错误」并列为待修两 bug（任务目标原文：「修复 PnL 采集恒为 0、首月 fallback 计算口径错误两个 bug」）。原始 handoff 未记录修复前的具体报错日志或错误 SQL 原文。

## 根因
采集侧在 `trading.trade_records` 中按策略名查询当月 `realized_pnl` 时存在两处缺陷（由修复动作反推）：① 缺少「策略标识 → 落库名」的映射，采集用的名字与 DB 实际落库值不一致；② 查询 SQL 只支持单值等值匹配、不支持多值数组。两者叠加导致查询命中不到任何行，`SUM(realized_pnl)` 恒为 0。

## 解决
| 文件 | 改动 |
|---|---|
| `ai_tuner/allocation/pnl_collector.py` | 新增 `_STRATEGY_DB_NAME_OVERRIDES`（`hrs → ["HRS策略"]`）落库名映射；新增 `_resolve_db_names` 解析落库名列表；PnL 查询 SQL 由单值改为 `strategy = ANY($1::text[])`，支持数组绑定 |

依赖：`asyncpg`（ANY 数组绑定）；`shared/database.py` 的 `_validate_sql` 不拦截 ANY/IN（已确认）。

## 验证
- 手动触发分配（`scripts/manual_allocation_trigger.py`）成功采集到真实 PnL：btc_eth **+11.47**、new_coin **+1.96**、激进版 **+24.00** USDT，驱动真实排名（btc_eth 收益率 13.1% 排第 1）。
- 姊妹存档 `ai_tuner_allocation_four_strategies_2026-09-21.md` 记 `pytest` **14/14 通过**（重写 `test_first_month_allocation`、新增 `test_normal_ranking_allocation_four_strategies`）；本条目任务标识所指 `monthly_allocation_4strategy_split_2026-09-21.md` 另记 **19 条** allocation 单测——两处存档口径不一致，原样并列（见「必要证据摘要」）。

## 适用范围与修订
- 模块：`ai_tuner/allocation/pnl_collector.py`；本次改动未随该任务提交（原始 handoff 记载未提交，部署直接生效）；据 git 复核，该改动于 commit `3e17962`（2026-09-24，随「月度资金分配支持每日按净资产刷新金额」等特性合并提交）首次入库。
- **后续修正（2026-10-09，F2）**：复核发现 `trade_records.strategy` 实际写入**英文短名**（`hrs` / `btc_eth` / `btc_eth_aggressive` / `new_coin`），`_STRATEGY_DB_NAME_OVERRIDES` 中文名映射属误判、已移除；`_resolve_db_names` 改为直接返回 `[strategy_id]`，`ANY($1::text[])` 数组 SQL 保留（见需求文档 §5.3）。
- 无替代条目。

## 教训
「同名不同源」陷阱：采集侧使用的策略标识必须与数据表落库值显式对齐，查询接口应按「多值集合」设计（`ANY($1::text[])`），避免单值等值匹配在改名 / 多别名场景静默查空。采集恒 0 时先核对「查询条件是否命中任何行」，而不是只看聚合结果以为公式错。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/monthly_allocation_4strategy_split_2026-09-21.md)、[handoff](../../handoffs/ai_tuner_allocation_four_strategies_2026-09-21.md)（均不随 Git 同步）
- 持久证据: 需求文档 `docs/requirements/capital_allocation/月度资金分配需求文档.md` §5.3（落库名口径，2026-10-09 修正 F2）；`ai_tuner/allocation/pnl_collector.py::_query_strategy_pnl`（`_PNL_QUERY_TEMPLATE` 的 `strategy = ANY($1::text[])`）
- 必要证据摘要: 修复后手动分配采集 btc_eth +11.47 / new_coin +1.96 / 激进版 +24.00（btc_eth 收益率 13.1% 排名第 1）；单测口径两处并存：姊妹存档 ai_tuner_allocation_four_strategies 记 14/14、本条目任务标识所指 monthly 存档记 19 条；落库名映射已于 2026-10-09 复核移除（F2）
- 相关: [Binance_quantitative_trading-20260921-allocation-first-month-fallback-caliber.md](./Binance_quantitative_trading-20260921-allocation-first-month-fallback-caliber.md)（同任务的首月口径缺陷）；[Binance_quantitative_trading-20260921-hrs-monthly-return-distortion.md](./Binance_quantitative_trading-20260921-hrs-monthly-return-distortion.md)（同 `pnl_collector.py`，收益率分母口径缺陷）；[Binance_quantitative_trading-20261009-capital-allocation-month-offset.md](./Binance_quantitative_trading-20261009-capital-allocation-month-offset.md)（同属月度资金分配模块）
