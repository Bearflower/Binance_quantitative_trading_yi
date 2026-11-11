---
id: Binance_quantitative_trading-20261111-dashboard-closure-gaps
类型: issue
项目: Binance_quantitative_trading
标题: 交易所收益已对齐，但看板绩效指标缺失且自动归属任务因证据冲突失败
日期: 2026-11-11
标签: [bug, dashboard, 收益口径, 绩效指标, 自动归属, 证据冲突]
任务标识: dashboard_exchange_caliber_repair_2026-11
状态: 部分修复，待复查
审查: 已审查
---

# 看板修复任务未达到归档条件

## 现象

2026-11-11 北京时间生产复核：`dashboard-api` 健康，`GET /api/overview?type=monthly` 正常发布 `exchange-income-v1-derived-v1`，账户收益 −31.68142241 USDT、未归属收益 0，账户覆盖和策略归属标志均为完整。但账户总胜率、夏普率、最大回撤、绩效样本数为 `null`；各策略的订单数、成交数、平仓数、胜率、夏普率、最大回撤等也为 `null`。这与用户要求恢复的概览指标不符。

同日 `income-attribution.timer` 为 active，但最近一次 `income-attribution.service` 的 `Result=exit-code`、`ExecMainStatus=1`；日志报 `reconciliation failed: owner conflict blocks attribution writes`。只读影子对账检查 208 条收入、发现 1 条候选收入和 6 条 `derived_evidence_conflict`（3 个交易所 `tran_id` 各对应 REALIZED_PNL、COMMISSION）。因此自动归属写入链路当前未通过验收。已发布的收益数字不能代替持续运行的验收证据。

## 根因与证据边界

- `dashboard/backend/services/exchange_ledger.py` 当前有意将上述绩效字段置为 `null`，尚未以可信的仓位生命周期样本重新计算。不能把旧策略本地统计直接填回交易所口径看板。
- `scripts/reconcile_income_attribution.py` 对既有不可变归属记录使用完整证据 JSON 比较，任何变化均产生冲突并阻止整批写入。冲突记录的原归属均为 `btc_eth_aggressive`，`exact_order`；相关订单现在各有一条同策略 `trade_records`。新增成交记录使证明材料变丰富，是目前最可能的冲突触发原因；修复前须比较新旧证据逐字段确认，并检查没有真实归属变更。不得直接覆写原证据或跳过冲突。

## 2026-11-11 修复进展

已定位 6 条冲突来自两张 PM 条件单：同策略 `trade_records` 在初次自动归属之后才补入，原订单证明中的 PM 算法单 ID、关联成交单 ID 和载荷摘要均未变化。修复使 PM 算法单证明保持稳定，同时仅在兼容旧的补充 `trade_record_ids` 字段时忽略该字段；策略、订单、算法单和摘要变化仍会被拦截。代码与测试记录见[本次修复报告](../../reports/income-attribution-proof-stability-hotfix-2026-11-11.md)。

生产只读影子对账检查 209 条收入，冲突 0、覆盖缺口 0，找到 2 条新候选；补丁挂载写入成功后，自动归属 53→55 条，重跑候选 0。16:49 北京时间 `income-attribution.service` 手动运行返回 `Result=success`、`ExecMainStatus=0`，定时器仍 active。现行补丁为服务器 release 目录的只读源码挂载和 systemd drop-in；修复源码已推送至远端分支，下一次计划触发及正式镜像发布仍待验收。

## 待处理

1. 为绩效指标建立由交易所成交与收入事件推导的仓位生命周期数据，明确总胜率、夏普率、最大回撤的样本、时间窗和资金基数；样本不足时明确展示原因。
2. 将已推送的自动归属代码修复并入主干和标准部署镜像，撤销临时 drop-in；确认新镜像仍通过影子对账。
3. 验证归属定时任务连续至少两个计划周期成功，候选事件被幂等处理，历史不可变记录没有被改写。

## 归档验收

- 同一交易所水位下，账户 USDT 收入原始求和与看板总收益一致；策略合计与账户一致；未归属数及覆盖状态真实呈现。
- 账户及各策略概览的交易数、胜率、夏普率、最大回撤有可追溯计算值；无法计算时展示明确原因，并用独立样本复核公式与时间窗。
- 页面可打开；日、周、月、年视图无加载死循环或接口错误；刷新失败可见，不以旧发布快照冒充新数据。
- 自动归属定时任务连续至少两个计划周期成功，影子对账无覆盖缺口、无归属冲突，新增收入可自动入库；既有手工归属和证据保持可追溯。

## 适用范围与修订

本条是 2026-11-11 的生产状态快照，不宣称代码修复已完成。修复及生产验收完成后更新状态，再考虑归档看板修复任务。

## 关联

[自动归属发布记录](https://github.com/Bearflower/Binance_quantitative_trading_yi/blob/main/docs/reports/exchange-auto-attribution-release-2026-11-10.md)、[收益看板适配器](https://github.com/Bearflower/Binance_quantitative_trading_yi/blob/main/dashboard/backend/services/exchange_ledger.py)、[自动归属对账器](https://github.com/Bearflower/Binance_quantitative_trading_yi/blob/main/scripts/reconcile_income_attribution.py)、[订单归属证据](https://github.com/Bearflower/Binance_quantitative_trading_yi/blob/main/shared/accounting/order_evidence.py)。
