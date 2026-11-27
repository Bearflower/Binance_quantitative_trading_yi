---
id: Binance_quantitative_trading-20261128-hrs-midnight-parent-version
类型: issue
项目: Binance_quantitative_trading
标题: 跨午夜中间提交使 HRS 缺口恢复的原父版本失效
日期: 2026-11-28
标签: [bug, HRS, 午夜, 事务, 权益补证]
任务标识: successor-integration-20261128
状态: 已解决（回归及生产准入验证；自然触发样本另留）
审查: 已审查
---

# 跨午夜缺口恢复保留原父版本并原子提交

## 现象与根因

本机真实 PostgreSQL 复现 23:59 至 00:02:15 的安全持仓采样缺口：准备凭据绑定账本版本 5，午夜路径先提交版本 6，恢复阶段再用版本 5 凭据校验而拒绝。事务回滚后下一轮重复相同过程，持续 `current_nav_history_incomplete`。

## 解决

`46fa983c`，`strategies/hrs/equity_service.py::_commit_accepted_history` 在任何午夜中间提交之前构造完整候选并判断当前路径是否缺失。确需补证时保持原父版本，以已收盘分钟序列恢复精确午夜检查点及保守价格边界，原子激活；无需补证的正常午夜流程保持不变。没有放宽版本、证据摘要或亏损校验。

## 验证

跨午夜正常与不利价格均加入真实 PostgreSQL 回归；验证午夜 NAV、次日风险拒绝、下一次刷新以及篡改父版本零提交。业务相关 127 项、整合完整回归 9,696 项通过。已随 `46fa983c` 完成生产切换；07:42 整组验收和当前 HRS 准入通过。未人为制造生产缺口，自然命中该恢复路径的样本仍按报告独立保留。

## 适用范围与修订

适用于已有有限历史批准、当前经典段采样不完整的恢复。原 PM 缺口和历史亏损事实保留；无替代条目。

## 教训

**AI 归纳，仅供参考：** 乐观版本凭据应绑定整个原子恢复动作；为了补日界先提交一次，会让后续正确的版本校验必然拒绝。

## 关联

- [整合验收报告](../../reports/successor-unified-production-acceptance-2026-11-28.md)
- [红绿与测试边界](../../reviews/2026-11-28-successor-integration/business-fix-evidence.json)
- [同批范围问题](Binance_quantitative_trading-20261128-hrs-live-gap-symbol-scope.md)
