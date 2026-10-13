---
id: Binance_quantitative_trading-20261009-max-drawdown-caliber
类型: decision
项目: Binance_quantitative_trading
标题: 看板最大回撤统一为「资金基数 + 累计已实现盈亏」口径
日期: 2026-10-09
标签: [dashboard, 指标口径]
任务标识: max_drawdown_caliber_and_hrs_dup_2026-10-09.md
状态: 已采纳
审查: 待审查
---

# 看板最大回撤统一为「资金基数 + 累计已实现盈亏」口径

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

## 关联与证据
- 本地补充来源: [handoff](../../handoffs/max_drawdown_caliber_and_hrs_dup_2026-10-09.md)（不随 Git 同步）
- 持久证据: 审查报告 docs/reviews/2026-10-09_C段实现审查报告_看板口径与HRS修复.md；commit b43a8d9；迁移 database/postgres/migrations/2026-10-09-hrs-pnl-dedup.sql
- 必要证据摘要: 迁移删 48 行/误删真实成交 0/SL 19→19/总盈亏 -33.4482；五层验证全绿