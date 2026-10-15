---
id: Binance_quantitative_trading-20261009-max-drawdown-caliber
类型: decision
项目: Binance_quantitative_trading
标题: 看板最大回撤统一为「资金基数 + 累计已实现盈亏」口径
日期: 2026-10-09
标签: [dashboard, 指标口径]
任务标识: max_drawdown_caliber_and_hrs_dup_2026-10-09.md
状态: 已替代
审查: 已审查
---

# 看板最大回撤统一为「资金基数 + 累计已实现盈亏」口径

> **⚠️ 已被 2026-10-15 segmented 口径取代（2026-10-15 标注）**：本决策的「资金基数 + 累计已实现盈亏」口径（单一全窗口基数、不回退未来快照）已被新决策 [Binance_quantitative_trading-20261015-segmented-drawdown-caliber](./Binance_quantitative_trading-20261015-segmented-drawdown-caliber.md)（**资金段分段口径 `segmented-v1`**）取代。原文保留以追溯历史（尤其「技术债」一节的 58.37 分母问题已在新口径下根治）。取代依据：连续两次立项（[需求文档](../../plans/fix-2026-10-15-capital-base-caliber-requirements.md) §1 触发事件）——本决策文末「仅当未来用户再次要求对齐分母时才重新立项」的触发条件已成立。

## 背景
生产看板最大回撤异常：HRS 月度达 97.8%；月/周/日 tab 下 MTCPS 激进版与新币做空最大回撤完全不变，用户质疑口径。

## 备选方案
- A. 复利口径（净值连续相乘）。
- B. 简单口径「资金基数 allocated_amount + 累计已实现盈亏」（不复利、回撤上限 100%、历史月份不取未来快照）。

## 决策
采用 B 简单口径；HRS 历史重复记账按 (strategy, symbol, side, realized_pnl) 分组去重保留最早一条；HRS 双写代码改为单条写入 + since 幂等。

## 理由
用户拍板；简单口径符合「累计已实现盈亏」直观语义，避免复利放大的复杂性与历史快照错位。

## 后果
- 眼下收益：看板回撤修正（btc_eth 月度 100.93%→70.00%、账户级 10.72%→5.69% 等）。
- 技术债：HRS 分母按「最早一条」快照（58.37），周/月/年仍 100%（本金亏光语义），需等待快照窗口前滑自愈（日≈2026-10-10、周≈2026-12-09、月≈2027-03-09）。

## 适用范围与修订
dashboard/backend、shared/trade_logger.py、strategies/hrs；commit b43a8d9 已上线 + HRS 去重迁移已执行。分母口径用户已接受维持现状，非发布阻塞；仅当未来用户再次要求对齐分母时才重新立项。

**修订（2026-10-15）：已被替代。** 本决策状态由「已采纳」改为「**已替代**」；替代条目 = [Binance_quantitative_trading-20261015-segmented-drawdown-caliber](./Binance_quantitative_trading-20261015-segmented-drawdown-caliber.md)（`segmented-v1` 资金段分段口径）。替代原因：本决策的「单一全窗口基数」在本决策 §后果「技术债」中已自认——HRS 分母按「最早一条」快照 58.37，周/月/年仍 100%，需等快照前滑自愈；2026-10-15 的生产核实进一步证明该口径与实际「分配额随净资产每日漂移」不兼容（伪击穿），故另立 segmented 口径根治。本决策的**「不复利 / 回撤上限 100% / 历史月份不取未来快照」三项原则被新口径继承**（新口径：段内不复利、负值钳 0、段初向后取）；被取代的是「**全窗口单一基数**」这一具体取值方式。原文上述各节保留不改以便追溯。下述「关联与证据」中的分母问题解决方案（58.37 兜底）在新口径下已**不再适用**。

## 关联与证据
- 本地补充来源: [handoff](../../handoffs/max_drawdown_caliber_and_hrs_dup_2026-10-09.md)（不随 Git 同步）
- 持久证据: 审查报告 docs/reviews/2026-10-09_C段实现审查报告_看板口径与HRS修复.md；commit b43a8d9；迁移 database/postgres/migrations/2026-10-09-hrs-pnl-dedup.sql
- 必要证据摘要: 迁移删 48 行/误删真实成交 0/SL 19→19/总盈亏 -33.4482；五层验证全绿
- 替代: [Binance_quantitative_trading-20261015-segmented-drawdown-caliber](./Binance_quantitative_trading-20261015-segmented-drawdown-caliber.md)（2026-10-15 起，`segmented-v1` 分段口径取代本决策的全窗口单一基数；本决策保留以供追溯）