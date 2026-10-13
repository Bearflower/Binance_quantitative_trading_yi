---
id: Binance_quantitative_trading-20261010-grid-capital-floor-reverted
类型: decision
项目: Binance_quantitative_trading
标题: 作废 be0a71a「资本下限抬升」机制，恢复诚实单位口径
日期: 2026-10-10
标签: [grid, 设计裁定]
任务标识: grid_min_quantity_unit_fix_2026-10-10.md
状态: 已采纳
审查: 待审查
---

# 作废 be0a71a「资本下限抬升」机制，恢复诚实单位口径

## 背景
be0a71a 提交新增 min_required_margin / apply_capital_floor 机制，把建议保证金抬升到 max(五因子值, ceil(N_min×P×q_min/L)) 以消除网格数不一致；与设计 §9「保持诚实单位口径、如实报告不可行」是互斥方案。

## 备选方案
- A. 采纳 be0a71a 资本下限抬升机制。
- B. 作废 be0a71a，恢复 §9 诚实单位口径（q_min=0.01 ETH，如实报告不可行）。

## 决策
作废 be0a71a 机制（代码+设计+测试全部 revert），§9 为唯一最终权威口径。

## 理由
四依据：①同根误判（be0a71a 与原始 bug 是同一 q_min=1 单位错误的两种表现，用「抬升到 1280」凑平而非修单位）；②生产现实反证（681 USDT 已成功建 12 格，be0a71a「需 1280 建 5 格」差约 100 倍）；③正确单位下 be0a71a 是 no-op 死代码（q_min=0.01 后 min_required_margin≈13 << 936，永不抬升）；④产品哲学冲突（主动抬升 vs 如实报告让用户决策，且抬升值错误会严重超额入金）。

## 后果
- 眼下收益：消除死代码与误导性抬升。
- 技术债：be0a71a 已上线生产，需纠正性 revert 部署（已在 cb4551e 落地，制品层确认 apply_capital_floor 计数=0）。

## 适用范围与修订
strategies/grid/{grid_calculator.py, margin_advisor.py, signal_bot.py}；作废见设计 §9.12 裁定，A-review 三轮确认（§9.13）。记录对象 = 「作废 be0a71a 资本下限抬升机制」这一决策本身，该决策至今有效、无替代条目；被本决策作废的是 be0a71a 提交新增的 min_required_margin / apply_capital_floor 机制。

## 关联与证据
- 本地补充来源: [handoff](../../handoffs/grid_min_quantity_unit_fix_2026-10-10.md)（不随 Git 同步）
- 持久证据: 设计文档 §9.12/§9.13；commit cb4551e
- 相关: [网格 min_quantity 单位错误](../issues/Binance_quantitative_trading-20261010-grid-min-quantity-unit.md)