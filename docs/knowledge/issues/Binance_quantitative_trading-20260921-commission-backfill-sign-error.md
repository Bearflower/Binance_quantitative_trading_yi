---
id: Binance_quantitative_trading-20260921-commission-backfill-sign-error
类型: issue
项目: Binance_quantitative_trading
标题: 佣金回填符号错误（无符号 commission 被写成正值，口径应为负值）
日期: 2026-09-21
标签: [bug, 佣金, 口径]
任务标识: commissions_backfill_2026-09-21.md
状态: 已解决
审查: 已审查
---

# 佣金回填符号错误（无符号 commission 被写成正值，口径应为负值）

## 现象
佣金回填功能上线后，回填入库的佣金显示为**正值**（如 +0.22），与看板总佣金应为支出的预期不符。

## 根因
币安 `userTrades.commission` 是**无符号正数金额**，首版回填直接把该值累加写库成**正值 +0.22**。但项目佣金口径统一为**负值支出**（看板 `net = realized_pnl + commission` 靠负值扣减），符号方向相反 → 回填值符号错误。

## 解决
- 修复代码：`shared/trade_logger.py` `reconcile_commissions` 写库处改为 `expense = -commission`（约 L816-L822），`total_commission` 同步取负。
- 修正存量数据：手动执行 `UPDATE trading.trade_records SET commission = -ABS(commission) WHERE commission > 0`，共修正 **8 条**。
- 重新部署 dashboard（仅重建 `dashboard-api` 容器，策略容器不重启、零交易风险）。

## 验证
- 重新部署后新调度回填即为负值：matched_orders=1, total_commission=**-0.0278**
- 落库核验：HRS 策略 **-0.0277** / MTPCS 激进策略 -0.1005 / 新币做空策略 -0.1196，均为负值 ✅
- 部署五层验证通过（容器 healthy / DEPLOY_ID 一致 / trade_logger MD5 一致 / 日志无 ERROR）
- 事后补跑单测：`tests/test_shared/test_commissions.py` L116 断言改为 `Decimal("-0.8")`，全量 `pytest tests/test_shared/` → **188 passed, 1 skipped**（唯一失败为既有 `test_production_url`，与本次无关）

## 适用范围与修订
`shared/trade_logger.py`；commit `371114b`（「fix: 佣金回填统一按负值落库，与看板 net = realized_pnl + commission 口径对齐」）已上线。无替代条目。同源的问题模式另见「数据来源缺失」条目（`-20260921-dashboard-commission-always-zero`），两条根因不同，保留为独立条目。

## 教训
外部接口的数值字段常**不带符号语义**——写入自有账本前必须按本系统口径（支出为负）显式定符号，不能假定「接口给正数就是正数」。修复已污染数据时，代码修复与存量回填必须同时做（本例 `UPDATE ... = -ABS(...)` 8 行），否则新数据负、旧数据正，报表仍失真。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/commissions_backfill_2026-09-21.md)（不随 Git 同步）
- 持久证据: commit `371114b`（`shared/trade_logger.py` `reconcile_commissions` 写库处 `expense = -commission`）；测试 `tests/test_shared/test_commissions.py`
- 必要证据摘要: 首版把无符号 commission 写成正值 +0.22；改 `expense=-commission`；手工修正 8 行；重新部署后落库 HRS -0.0277；188 passed, 1 skipped
- 相关: [Binance_quantitative_trading-20260921-dashboard-commission-always-zero](./Binance_quantitative_trading-20260921-dashboard-commission-always-zero.md)（同源 handoff，根因不同：本条为符号口径错误，另一条为数据来源缺失）
