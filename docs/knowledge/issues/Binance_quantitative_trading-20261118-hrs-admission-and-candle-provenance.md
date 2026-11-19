---
id: Binance_quantitative_trading-20261118-hrs-admission-and-candle-provenance
类型: issue
项目: Binance_quantitative_trading
标题: HRS 策略槽位与全账户保证金分离、真实 K 线修复及回放来源绑定
日期: 2026-11-18
标签: [hrs, 风险预算, 仓位归属, K线, 因果回放, 部署]
任务标识: hrs-followup-admission-data_2026-11-18
状态: 工程修复与真实午夜已验证；市值可用性和绩效未全验收
审查: 已审查
---

# HRS 槽位与原始来源修复

旧入场预留将全账户 BNB/SOL/WLD 三个持仓与 HRS 未释放预留合并，与 HRS 三槽位上限比较；HRS 实际只持有 WLD。用户批准改为仅已证明归属 HRS 的持仓与其未释放预留占用 HRS 槽位，外仓继续计入全账户保证金约束，归属不明时拒绝。单笔 1%、组合 3%、禁止加仓及原 SL/TP 数值保持。这是明确的准入行为变更，不能声称经济等价。

`ScopedRiskSnapshot` 在政策锁内读取实际权益门禁原始 SHA、数据库时钟和完整有效 ownership claims，正向校验两套实际库存、完整成交归属路径及本地 HRS 状态。预留只在事务提交后暴露本次审计记录并立即捕获；正常风控拒绝记录 INFO 和 reason/context，未知异常保留。不能删除真实已成交但仍有持仓的预留来释放风险额度。

ARIA/BTW/PUMP/EVAA/MMT 的不连续小时数组改为有限 GET 补真实小时，先验证整个原始跨度，再选正常评分窗口。4h 必须由四根连续 UTC 小时线组成；成交量以明确 Decimal128 累计后一次转换，消除 Python 3.11/3.12 浮点 sum 的 1ulp 差异。原始响应的未来/过去越界在过滤前拒绝。成功和异常分析均捕获实际消费数组，避免并发缓存覆盖证据。

离线只使用实际门禁所指的父记录，不回退到更旧、碰巧能通过的记录；候选币种局部 claims 不能证明全账户归属。原始数组、GET 响应、实际消费数组及运行代码身份均重新闭包验证。缺真实身份、时钟、完整归属或非平仓窗口起点证据时明确 BLOCKED，不补造事实。

2026-11-18 21:38 CST 发布 `3aa32e9e137eb6ae22c6809e567282bdee1fb0dc`，[云端 CI](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44314458303) 1408 测试通过。39/39 运行文件、配置只读挂载、采集/写入器及其他容器 ID 验证通过。真实只读诊断：账户三仓、HRS 一仓，预留并集占 1/3 槽位；WLD 28 及原硬止损保持。首轮22:21自然扫描现已核实：29/29真实repair闭包通过，含五个旧缺口标的；后续00:21和07:22分别29/29、28/28通过。原始ticker分母814（518 crypto、243 TradFi、53元数据未核）保留。

2026-11-19 00:00 CST 原始检查点已真实重建，00:01:13创建：equity31.889305060、units32.043698480、NAV0.9951817852706246036303359948。随后实际门禁ready/allowed；这是初始段保护，不是完整30天验收。跨午夜的独立人工实时观察中断过，使用原始持久记录核验，不宣称连续人工监控。

最新发布 facde71034e9d8ce8803d98e34acfabb871f44eb 于2026-11-19 09:01:51 CST完成，[候选CI](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44432307179)1412测试通过。39源码、原只读配置、真实collector/writer与门禁初始化通过。后续6个缺失身份补齐后registry73；服务器CG仍429。独立CG/CMC原始配对时间相隔113秒，只有AKE/LIT/MMT/PIPPIN的市值和流通量差异都<=5%，已启用相应CMC备用；另8个数值口径不合格继续禁用。未注入CG DemoKey，不能把身份核验等同取数完全恢复，不能降低95%覆盖、延长过期数据或回写旧小时。

整体优化尚未完成：8–9月完整反事实缺历史决策和市场快照；v4的24个退出候选已事前登记但无赢出参数；连续24小时和五个合格自然闭合样本尚未达到。测试、组件诊断和成交对账不替代收益验收。

证据见 [部署状态](../../reports/hrs-production-optimization-status-2026-11-18.json)、[逐文件审查](../../reports/hrs-followup-review-2026-11-18.md)、[v4 登记](../../reports/hrs-exit-experiment-registration-2026-11-18-v4.json) 及 [离线与日初来源审计](../../reports/hrs-offline-slot-and-daily-origin-audit-2026-11-18.md)。

备用源原始配对、12项数值及逐文件复审见 [资格报告](../../reports/hrs-market-cap-secondary-qualification-2026-11-19.json)。
