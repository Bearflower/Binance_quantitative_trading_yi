---
id: Binance_quantitative_trading-20260921-allocation-first-month-fallback-caliber
类型: issue
项目: Binance_quantitative_trading
标题: 月度分配首月 fallback 误用 allocatable_amount 致口径错误
日期: 2026-09-21
标签: [bug, ai_tuner, 首月口径]
任务标识: monthly_allocation_4strategy_split_2026-09-21.md
状态: 已解决
审查: 已审查
---

# 月度分配首月 fallback 误用 allocatable_amount 致口径错误

## 现象
首月（无历史收益率数据）走 fallback 默认基准分配时，分配金额口径错误：fallback 中各策略比例未按「占总资金比例」计算。原始 handoff 未记录具体的金额偏差数值。

## 根因
首月分配 `_calculate_first_month` 以「可分配资金 `allocatable_amount`（= total_capital − reserve_amount）」作比例基数，而非「总资金 `total_capital`」，导致 fallback 分配金额口径偏小、与配置语义不符。需求文档 §4 原文：此前**误按「可分配资金 × ratio」计算**。

## 解决
`ai_tuner/allocation/allocation_calculator.py::_calculate_first_month` 改用 `total_capital × ratio`（不再用 `allocatable_amount`）；重写 `test_first_month_allocation`（4 策略、不传 `fallback_capitals`）。

## 验证
- ai_tuner allocation 测试全通过（`monthly_allocation_4strategy_split_2026-09-21.md` 记 **19 条** allocation 单测；姊妹存档 `ai_tuner_allocation_four_strategies_2026-09-21.md` 记 **14/14**——两处存档口径不一致，原样并列，见「必要证据摘要」）。
- 需求文档 §4 明确记录「**比例语义（2026-09-21 变更）**：分配金额 = `total_capital × ratio`，此前误按『可分配资金 × ratio』计算，已修复」。

## 适用范围与修订
- 模块：`ai_tuner/allocation/allocation_calculator.py`；本次改动未随该任务提交（原始 handoff 记载未提交）；据 git 复核，该改动于 commit `3e17962`（2026-09-24，随「月度资金分配支持每日按净资产刷新金额」等特性合并提交）首次入库。
- 语义确认：`fallback.ratios` 各比例总和 = 0.85 = 1 − 0.15（风险备用金）。
- 无替代条目。

## 教训
「比例基数」必须在命名与注释里写死对应：**占总资金比例** 与 **占可分配资金比例** 是两种口径，二者相差一个 reserve。凡涉及 ratio 的计算都要标注基数，且测试应针对基数断言（而非只断言分配总额），否则基数写错也可能在总额层面看起来「差不多」。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/monthly_allocation_4strategy_split_2026-09-21.md)、[handoff](../../handoffs/ai_tuner_allocation_four_strategies_2026-09-21.md)（均不随 Git 同步）
- 持久证据: 需求文档 `docs/requirements/capital_allocation/月度资金分配需求文档.md` §4（比例语义 2026-09-21 变更）；`ai_tuner/allocation/allocation_calculator.py::_calculate_first_month`
- 必要证据摘要: 修复为 `total_capital × ratio`；`fallback.ratios` 总和 0.85；单测数量两处存档记载不一致（`monthly_allocation_4strategy_split` 记 19 条 / 姊妹存档 `ai_tuner_allocation_four_strategies` 记 14/14），原始 handoff 未统一，沿用各自记载不作合并
- 相关: [Binance_quantitative_trading-20260921-allocation-pnl-collect-always-zero.md](./Binance_quantitative_trading-20260921-allocation-pnl-collect-always-zero.md)（同任务，PnL 采集恒 0 缺陷）；[Binance_quantitative_trading-20261009-capital-allocation-month-offset.md](./Binance_quantitative_trading-20261009-capital-allocation-month-offset.md)（同属月度资金分配模块）
