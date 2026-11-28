---
id: Binance_quantitative_trading-20261111-dashboard-closure-gaps
类型: issue
项目: Binance_quantitative_trading
标题: 交易所收益已对齐、自动归属已恢复，但看板绩效指标仍缺失
日期: 2026-11-11
标签: [bug, dashboard, 收益口径, 绩效指标, 自动归属, 证据冲突]
任务标识: dashboard_exchange_caliber_repair_2026-11
状态: 月度看板绩效已验收，历史证据缺口待补
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

生产只读影子对账检查 209 条收入，冲突 0、覆盖缺口 0，找到 2 条新候选；补丁挂载写入成功后，自动归属 53→55 条，重跑候选 0。16:49 手动与 17:15 计划周期均成功。现有定时器的 CI 更新路径已加入主干，提交 `ae9c7e5949586b54e96043eb18ebc1533e6b0203` 经 [CI/CD 运行](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/42999571345) 正式部署成功。18:00 新镜像运行成功，209 条收入、候选 0、冲突 0、覆盖缺口 0；定时器 active，临时 drop-in 已移除。具体证据见[修复报告](../../reports/income-attribution-proof-stability-hotfix-2026-11-11.md)。

## 待处理

1. 为绩效指标建立由交易所成交与收入事件推导的仓位生命周期数据，明确总胜率、夏普率、最大回撤的样本、时间窗和资金基数；样本不足时明确展示原因。
2. 继续观察正式镜像的后续计划周期，并对新增收入的自动归属做持续抽查。

## 归档验收

- 同一交易所水位下，账户 USDT 收入原始求和与看板总收益一致；策略合计与账户一致；未归属数及覆盖状态真实呈现。
- 账户及各策略概览的交易数、胜率、夏普率、最大回撤有可追溯计算值；无法计算时展示明确原因，并用独立样本复核公式与时间窗。
- 页面可打开；日、周、月、年视图无加载死循环或接口错误；刷新失败可见，不以旧发布快照冒充新数据。
- 自动归属定时任务连续至少两个计划周期成功，影子对账无覆盖缺口、无归属冲突，新增收入可自动入库；既有手工归属和证据保持可追溯。

## 2026-11-12 看板绩效发布与验收

交易所口径的已实现收益夏普、回撤和严格核实仓位样本胜率已由提交 `46f0c417` 通过 [CI/CD 运行 43136111534](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/43136111534) 发布。月度 11 个完整日账户夏普 −9.8249、已实现收益回撤 5.978%；两笔完整核实平仓样本胜率 0%，明确标记为非期间全量。月度收益 −31.68756469 USDT 与同水位交易所收入事实 212 条求和及策略合计一致，未归属 0。日、周、月、年 API 均正常返回；归属定时器仍 active，最近一次执行成功。详见[生产验收报告](../../reports/dashboard-performance-exchange-income-repair-2026-11-12.md)。

首屏默认“日”导致当日无完整样本的展示空值，已通过提交 `1d0a71a9` 改为默认“月”；随后提交 `7825607c` 更新脚本缓存版本号。两次后继 [CI/CD 运行](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/43137427675) 均成功。服务器返回的首页与脚本已验证为新版本，最终看板 active pin 指向 `7825607c`。

## 适用范围与修订

月度展示及自动归属故障已验收。年视图缺 2026 年 1–6 月交易所收入覆盖，网格缺资金快照，部分策略缺可验证平仓样本；这些指标按证据边界展示原因。历史全量胜率不能从两笔新样本推断，因此本 issue 保留以跟踪长期证据覆盖，不作为本次上线失败。

## 关联

[自动归属发布记录](https://github.com/Bearflower/Binance_quantitative_trading_yi/blob/main/docs/reports/exchange-auto-attribution-release-2026-11-10.md)、[收益看板适配器](https://github.com/Bearflower/Binance_quantitative_trading_yi/blob/main/dashboard/backend/services/exchange_ledger.py)、[自动归属对账器](https://github.com/Bearflower/Binance_quantitative_trading_yi/blob/main/scripts/reconcile_income_attribution.py)、[订单归属证据](https://github.com/Bearflower/Binance_quantitative_trading_yi/blob/main/shared/accounting/order_evidence.py)。

## 2026-11-18 生产复查

- 归属服务在 11 月 18 日仍显示每小时运行成功，但收入处理截止时间停在 11 月 13 日。历史补采为非活跃币种留下的游标被误纳入当月共同截止时间。修复用当月同账户的交易所收入和成交识别活跃币种，继续分别校验这些币种的完整成交覆盖。[发布记录](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44223485447)
- 修复后的两次生产执行补写 36 笔归属，无冲突。月度待归属清零。日、周、月净收益均和同一窗口的交易所收入事实、策略归属加未归属之和对平。归属任务及定时器已恢复到正式发布版。
- 网格子账户已通过真实成交和资金费对账。直接用子账户交易所接口核对 21 笔成交、1 笔资金费，已实现收益扣手续费为 +1.20760965 USDT，资金费 +0.00514133 USDT，空仓累计盈亏 +1.21275098 USDT。
- 巡检按 HTTP 状态识别 Uvicorn 访问日志。11 月 18 日定时巡检记录 dashboard-api 日志无错误。
- 11 月 17 日新增 152 笔历史资金费归属，净额 +1.25715011 USDT。剩余 1,155 笔净额 +13.91415692 USDT，绝对额合计 22.18487166 USDT。开仓订单缺归属 1,081 笔，平仓归属冲突 53 笔，多策略持仓 16 笔，缺更早空仓边界 5 笔。已导出按金额绝对值排序的人工复核清单。币安历史委托归档的委托号和客户端委托号比对未提供新的策略映射。
- 年度数据证据已补采至生产受限归档目录。上半年币安 PM 历史收入文件有 3,095 行，其中已实现盈亏、手续费、资金费合计 −18.18688019 USDT。上半年成交文件有 1,341 笔，1–10 月普通委托文件有 2,616 行。与 7 月 1 日以来已覆盖的同口径数据库收入合并，截至北京时间 11 月 18 日 09:35 的年度离线净额为 −69.12639226 USDT。
- 年视图仍待验收。归档时间精度为秒，收入文件缺成交 ID，须设计和验证归档收入与成交的匹配及发布口径。主账户最早权益快照为 9 月 1 日，全年夏普及最大回撤仍缺早期资金基数。

## 2026-11-28 跨切换看板与历史缺口接手

经典账户切换后，11 月 27 日的后继看板源码已接入周、月、年视图：已核的 Classic 收益单独展示，PM 尾段的待核区间仍明确可见。另一个对话记录了 40 条和 41 条 Classic 流水的两轮币安签名对账，三个视图均能显示对应分段；因此上文“Classic 尚未接入看板”的旧结论已由生产验收更新。主线现含 `verified_classic_income` 和 `cutover_income_gap` 后端字段及前端展示。此项是**分段展示验收**，不是跨切换全窗对账完成。

仍缺 11 月 25 日北京时间 12:35:01—12:54:24 的 PM 尾段完整覆盖；现有证据只核出 2 笔成交和 4 条收入，已核部分净额 +0.77592593 USDT，旧 PM 签名接口返回 `-2015`。最近一次年度核查有 1,959 条待归属，7 月含 42 个待认领订单和 16 条非 USDT 流水；全年夏普及最大回撤仍缺旧 PM 期初权益序列。8—10 月“3,298 条未归属”的旧导出漏读自动归属层，已被后续正式月报纠正，不得再次作为当前缺口引用。

这些历史数值均为各自核查时点，需先刷新当前正式派生报表，再逐条处理可证归属与估值。全年权益无法补出时，保持“数据不足”，阶段指标按窗口单列。当前执行顺序与关闭条件见[统一接手清单](../../reports/binance-open-items-handoff-2026-11-28.md)。

同日 09:08—09:10 北京时间只读复核：生产周、月、年接口均为 `published`，Classic 分段 47 条收入、未归属 0、净额 +0.46432316 USDT；年视图仍有 1,959 条待归属，全年夏普和最大回撤为 `null`，39.62% 胜率明确为部分样本。[脱敏现场回执](../../reviews/2026-11-28-strategy-handoff/live-readonly-summary.json)。这更新了分段金额与当时 40／41 条样本的时点，不关闭 PM 尾窗或历史绩效缺口。
