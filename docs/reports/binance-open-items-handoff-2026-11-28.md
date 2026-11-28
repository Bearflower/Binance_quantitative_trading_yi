# Binance 经典账户切换与后继审查统一接手清单

日期：2026-11-28（北京时间）。接手对话：“梳理币安经典账户切换改动”；来源对话：“审查 Binance 量化交易项目”。本清单合并两个对话的**未完成事项和后续验收条件**。原报告的结论均保留其当时的时间口径；此处按已取得的后续证据更新状态，不把未来自然周期写成已通过。

## 已关闭的工程事项

| 事项 | 已核验证据 |
|---|---|
| 七服务后继组合的完整回归及统一发布 | 源码 `46fa983c`，557 个文件、9,696 项测试通过，0 失败／错误／跳过；23/23 目标容器激活并通过真实联调；数据库权限 audit 1,355 对象、0 issue。[后继整合验收](successor-unified-production-acceptance-2026-11-28.md) |
| HRS 两项缺口恢复代码问题 | 补证币种按 HRS 归属筛选；跨午夜以原父版本为准、账本原子提交。回归与发布均已完成。自然命中验收单列在下表。[后继整合验收](successor-unified-production-acceptance-2026-11-28.md) |
| XAU 新版本首个自然事件 | 三个 profile 已产生 `46fa983c` 的自然事件，版本、producer 与配置指纹符合预期，质量标记 0；八周观察仍独立进行。[自然事件证据](../reviews/2026-11-28-successor-integration/xau-after.json) |
| 经典切换交付与生产盘 | `c50b6c92` 已纳入主线合并 `c6f19932`；统一发布后根盘 60G 中约 11G 可用、使用率 83%。网格账本已有 7 个 CLOSED 周期、12 条成交，当前 FLAT、无锁。两者不再按早先“分支未合并、96% 磁盘、尚无完整网格成交”的旧结论跟踪。[收口复查](classic-cutover-closure-audit-2026-11-28.md) |
| AI 利润提醒的重复异常日志 | `5b57d130` 已把已知双锚点缺口改为安全 `skip`，不计算建议或写 ATH；定向 88 项及 AI 全套 2,032 项通过，三项 AI 服务定向上线且无新 traceback。**仅关闭异常日志问题，利润提取建议未恢复。**[收口复查](classic-cutover-closure-audit-2026-11-28.md) |

## 仍需接续验收或凭据

| 优先级 | 事项 | 当前状态和下一步关闭依据 |
|---|---|
| 到期验收 | AI 月度自动分配 | 2026-12-01 09:30 北京时间首个自然运行尚未发生。到时按真实调度、allocation 落库、预算上限、预留、风险和审批以及下游配置逐项核对；不能提前生成生产结果。[有限历史审批](../knowledge/decisions/Binance_quantitative_trading-20261127-bounded-history-acceptance.md) |
| 到期验收 | 利润提醒安全跳过 | 下一个 07:34 北京时间自然周期核对 `skip` 回执、无建议金额、无 ATH 写入、无新 traceback；这是补丁上线后的自然验收，**不代表利润建议恢复**。[利润提醒问题](../knowledge/issues/Binance_quantitative_trading-20261128-profit-reminder-cutover-anchors-missing.md) |
| 到期验收 | 容器日志连续 14 天留存 | 最近实测最早日志为 2026-11-19 17:49 北京时间；2026-12-03 17:49 以后核对连续覆盖、当前最早时间、磁盘和 journald 使用量。此前不得标记满 14 天。[收口复查](classic-cutover-closure-audit-2026-11-28.md) |
| 自然样本 | HRS 跨午夜缺口恢复 | 正常、不利价格、篡改父凭据等真实 PG 回归已过；自然缺口发生时只读核对原父版本、午夜 NAV、原子账本提交及次日风控结果。不得为了验收制造生产缺口。[后继整合验收](successor-unified-production-acceptance-2026-11-28.md) |
| 自然样本 | 网格新镜像入场、保护单与退出 | 旧周期已有真实建单、入场和退出；新镜像当前 FLAT、无挂单。下次真实信号出现后核对订单、保护单、成交与账本，无信号时不主动下单。[收口复查](classic-cutover-closure-audit-2026-11-28.md) |
| 长期观察 | HRS 完整 30 日证明与研究晋级、XAU 八周观察 | 按原研究和风控标准逐期积累，不把当前 HRS `ready/allowed` 或 XAU 单个自然事件当作长期证明。[后继整合验收](successor-unified-production-acceptance-2026-11-28.md) |
| 原始凭据 | PM 历史、切换期权益、历史 NAV 与利润提醒双锚点 | 当前 `ACCEPTED_GAPS`、`original_history_complete=false`；主账户 PM 期末／Classic 期初原生双锚点仍缺。用户已说明没有留存当时截图，有限历史容缺已批准当前 HRS/AI 按原风控运行。若日后取得真实账单，按来源和时间逐条接续；若无法取得，持续保留历史完整性与利润建议的明确限制，不补造原始证据。[历史恢复验收](historical-evidence-recovery-2026-11-27.md)、[利润提醒问题](../knowledge/issues/Binance_quantitative_trading-20261128-profit-reminder-cutover-anchors-missing.md) |

## 执行边界

当前 HRS／AI 的有限历史批准、资金上限和实时风险闸门继续沿用；历史缺口不自动撤销当前已批准的准入，也不自动证明完整研究权益。上表中各项按自身关闭依据验收。利润建议的双锚点约束独立于交易准入；即使下一周期安全跳过成功，也不能计算或发布未经证明的提取金额。

本清单承接[后继整合验收](successor-unified-production-acceptance-2026-11-28.md)与[经典切换收口复查](classic-cutover-closure-audit-2026-11-28.md)。这里的“已关闭”以对应报告和版本为准；未来自然周期需在实际发生后另记结果。
