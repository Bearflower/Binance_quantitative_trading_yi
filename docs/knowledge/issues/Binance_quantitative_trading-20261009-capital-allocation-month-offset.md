---
id: Binance_quantitative_trading-20261009-capital-allocation-month-offset
类型: issue
项目: Binance_quantitative_trading
标题: 月度资金分配「记录月份错位」导致 10 月限额落空
日期: 2026-10-09
标签: [bug, ai_tuner, 月份口径]
任务标识: capital_allocation_month_fix_zerotrade_2026-10-09.md
状态: 已解决
审查: 已审查
---

# 月度资金分配「记录月份错位」导致 10 月限额落空

## 现象
看板【月度资金分配】停留 2026-09；生产库 capital_allocation 无 2026-10 记录，10 月全月 4 策略 DB 限额落空、每日刷新跳过。

## 根因
monthly_job.py 的 run_monthly_allocation() 用「刚结束当月」now.strftime("%Y-%m") 同时作幂等键与写入 month；消费方按「当前月=生效月」查询 → 9/30 幂等键 2026-09 已存在被跳过，10 月永远查不到。

## 解决
新增 _next_month / _resolve_months，分离 pnl_month（数据窗口月）与 effective_month（幂等键+写入月）；run_monthly_allocation 增两可选参数；补生成 2026-10 记录。

## 验证
95 passed（test_allocation 45 + test_monthly_job 37 + test_daily_refresher 13），monthly_job/alloc_calculator/pnl_collector 三模块行/分支 100%；五层防幻觉验证全绿；三项只读上线验证通过（DB、消费方、看板）。

## 适用范围与修订
ai_tuner/allocation/{monthly_job.py, pnl_collector.py, allocation_calculator.py}、scripts/manual_allocation_trigger.py；commit 48b8bf2 已上线。零开仓垫底规则本轮一并落地（has_trades 排序键）。

## 教训
幂等键与业务月份是两种语义，不能复用同一个字段；「刚好结束的月」与「生效月」在月末边界必然错位。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/capital_allocation_month_fix_zerotrade_2026-10-09.md)（不随 Git 同步）
- 持久证据: 需求文档 docs/requirements/capital_allocation/月度资金分配需求文档.md §14；commit 48b8bf2
- 必要证据摘要: 95 passed；生产补生成 2026-10 后四策略限额恢复；10/31 自动生成 2026-11 为自然月末验证点