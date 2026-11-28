---
id: Binance_quantitative_trading-20261128-hrs-shadow-overwrote-standard-signal
类型: issue
项目: Binance_quantitative_trading
标题: HRS 同币同向半 EMM 在旧执行队列覆盖正式信号
日期: 2026-11-28
标签: [bug, HRS, 半 EMM, 影子执行, 信号终态]
任务标识: reduced-trial-review-v1
状态: 已修复并发布
审查: 已审查
---

# HRS 同币同向半 EMM 在旧执行队列覆盖正式信号

## 症状与根因

11 月 19 日的 20 条 HRS 标准信号显示未解析。逐条比对原信号 ID、不可变执行批次和交易所签名历史，发现 7 条标准信号在旧队列按币种归并时被同币同向的半 EMM 影子信号替代。日志里的后续预算判定属于被选中的影子信号。按币种和时间匹配容易把它误记为标准信号的拒绝原因。其余 13 条另有组合熔断、新增风险门禁、费率检查或最小委托量的独立证据。[逐条复核](../../reports/trial-signal-execution-and-equity-gaps-2026-11-20.md)

同一天 EVAA、PIPPIN 的两条旧影子信号另有提交前预留。旧挂载缺少执行模式，路由误放行至预留，后续证据序列化和读回校验拦在提交前。原状态为 `reserved → released`，签名查单及逐笔均无对应订单或成交。这个现象与 7 条标准信号被覆盖应按信号 ID 分开核对。[两笔预留复盘](../../reports/hrs-pre-submit-repair-2026-11-19.md)

## 修复与验收

先拆分正式和影子信号，再归并和解决正式信号的方向冲突。影子模式在入口处短路，缺执行模式按既有默认观察处理。状态按原信号 ID 落证。旧未解析行通过按账户、时窗和原执行批次绑定的回证记录审计，保留原始信号状态。AI Tuner 在生产回放中确认 20 条均在提交前结束，逐信号验收通过。完整 HRS 日窗的开仓协议和主账户签名权益仍各自判定。[上线后的日报回放](../../reports/trial-signal-execution-and-equity-gaps-2026-11-20.md)

## 关联

- [HRS 影子来源与正式信号隔离](../decisions/Binance_quantitative_trading-20261016-hrs-shadow-signal-origin-not-substitute.md)
- [缩量试跑的交易证据与权益来源](../decisions/Binance_quantitative_trading-20261128-trial-evidence-provenance.md)
