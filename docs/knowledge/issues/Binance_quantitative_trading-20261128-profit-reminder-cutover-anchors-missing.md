---
id: Binance_quantitative_trading-20261128-profit-reminder-cutover-anchors-missing
类型: issue
项目: Binance_quantitative_trading
标题: 利润提取提醒仍缺少 PM 期末与 Classic 期初原生权益凭据
日期: 2026-11-28
标签: [历史证据, AI, 利润提醒, 账户切换]
任务标识: successor-integration-20261128
状态: 报错已修复并发布；提取建议待原始双锚点
审查: 已审查
---

# 利润提取提醒的权益桥接缺口不能用交易准入批准代替

## 现象与根因

统一版本 `46fa983c` 上线后，07:34 自然调度执行利润提取提醒。`ProfitExtractionJob.run_daily_check` 调用 `read_anchors` 抛出“缺少有效的 PM 期末或经典期初权益”，返回 `action=error`。一个事件对应堆栈头和结果中的两条扫描命中，不是两次独立故障。

受限运行角色、只读事务核对 `trading.account_mode_equity`：当前账户与切点、`snapshot_date IS NULL` 的锚点集合为空。有限历史审批保存的经典期初和当前 HRS／AI 准入，不等于该表所要求的 PM 期末／经典期初原生双锚点。此类历史缺口在 11 月 25 日日志审计中已出现，不是本轮回归后的新代码故障。

## 影响与处理边界

本次在建议金额计算和 ATH 更新前退出，仅走异常 outbox 记录分支；没有生成有效利润提取建议。该任务是提醒任务，不是资金划转执行器。本次未手动重跑或发送通知，没有补造切点权益、重置 ATH 或放宽验证。

当前 HRS ready/allowed、AI risk_ready、attribution_ready 和当前覆盖均为 true；不能据此关闭利润提醒的数据阻塞。工程发布可以按已有有限历史批准验收，不能写成全部业务恢复。

## 关闭条件

取得绑定同一账户身份与真实切点的可核验 PM 期末、Classic 期初凭据，按既有不可变证据流程恢复后，再验证利润提醒。自然等到次日不会自动补出过去的原生权益。若凭据无法恢复，继续明确保留该功能限制；本轮不提出替代本金或改写历史的方案。

## 证据

- [本轮生产验收](../../reports/successor-unified-production-acceptance-2026-11-28.md)
- [只读锚点查询](../../reviews/2026-11-28-successor-integration/anchor-read-summary.json)
- [历史日志审计](../../reports/classic-post-release-log-audit-2026-11-25.md)
- [有限历史批准范围](../decisions/Binance_quantitative_trading-20261127-bounded-history-acceptance.md)
- 源码：`ai_tuner/allocation/profit_extraction_job.py::run_daily_check`、`shared/accounting/equity_bridge.py::read_anchors`。

复核：日志堆栈、实际运行源码、只读库查询与历史批准范围交叉核验。**AI 归纳，仅供参考：** 放行交易的有限历史批准不能自动变成其他功能所需的完整财务凭据。

## 生产后续修复与接手（2026-11-28）

用户先确认本问题及其他剩余事项将在别的项目继续执行，并要求归档原对话；随后在“梳理币安经典账户切换改动”对话中要求读取该审查并接续处理。原对话的工程结论和本对话的后续修复按各自时点保留。

后续生产补丁 `5b57d130` 已将这类**预期且可识别的双锚点缺口**改为明确的 `skip` 回执：建议金额计算和 ATH 更新均不执行，UID 不符及其他异常仍保持错误通道。定向 88 项、AI 完整 2,032 项回归通过；三项 AI 服务已按该版本定向发布，健康、零重启，激活后没有新 traceback。[收口复查](../../reports/classic-cutover-closure-audit-2026-11-28.md)记录了测试、镜像和生产核验。此处只关闭“缺口导致每日 traceback”这一工程故障；真实利润提取建议仍因原始双锚点缺失而暂停。下一次 07:34（北京时间）自然调度需核对 `skip`、无金额建议、无 ATH 写入和无新 traceback。

原生 PM 期末与 Classic 期初凭据目前仍未取得。若确实无法恢复，持续保留该功能的明确限制，并按[统一接手清单](../../reports/binance-open-items-handoff-2026-11-28.md)跟踪；不能把 `skip` 记成利润建议恢复。
