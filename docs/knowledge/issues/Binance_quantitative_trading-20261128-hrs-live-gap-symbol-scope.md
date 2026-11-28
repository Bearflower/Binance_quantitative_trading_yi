---
id: Binance_quantitative_trading-20261128-hrs-live-gap-symbol-scope
类型: issue
项目: Binance_quantitative_trading
标题: 其他策略成交币种误入 HRS 当前权益缺口补证范围
日期: 2026-11-28
标签: [bug, HRS, 归属, 权益补证]
任务标识: successor-integration-20261128
状态: 已解决（回归及生产准入验证；自然触发样本另留）
审查: 已审查
---

# HRS 缺口补证必须按已证明的策略归属确定币种

## 现象与根因

HRS 持有 BLESSUSDT 的缺采样窗口内，其他策略新增 BTCUSDT 成交，准备阶段收集两币种；最终恢复计划仅包含 HRS 币种，精确范围比较抛出 `accepted_live_gap_path_changed`。根因是准备 SQL 按账户读取全部成交币种，未按订单归属筛选。

## 解决

`46fa983c`，`strategies/hrs/equity_service.py::_prepare_accepted_live_gap` 使用本地交易记录及已认证订单凭据，仅将唯一归属 HRS 的成交币种补入端点持仓范围。最终事务仍读取全部账户成交，未知／混合归属以及同币种的其他策略成交继续拒绝；不能只看净仓归零就忽略中间经济路径。

## 验证

已在本机复现原失败，修复后真实 PostgreSQL 与缺口测试共 127 项通过。包含其他策略不同币种、HRS 瞬时往返仓、未知及混合归属往返仓拒绝；整合完整回归 9,696 项通过。已随 `46fa983c` 完成生产切换；07:42 整组验收和当前 HRS 准入通过。未人为制造生产缺口，自然命中该恢复路径的样本仍按报告独立保留。

## 适用范围与修订

适用于 `accepted_live_mark_gap_v1` 当前经典账户段；不补齐或重写已批准缺口的 PM 历史。无替代条目。

## 教训

**AI 归纳，仅供参考：** 准备阶段与提交阶段的范围必须来自相同归属契约；排除已知其他策略噪音时仍要保留未知归属的拒绝路径。

## 关联

- [整合验收报告](../../reports/successor-unified-production-acceptance-2026-11-28.md)
- [红绿与测试边界](../../reviews/2026-11-28-successor-integration/business-fix-evidence.json)
- [有限历史容缺](../decisions/Binance_quantitative_trading-20261127-bounded-history-acceptance.md)
