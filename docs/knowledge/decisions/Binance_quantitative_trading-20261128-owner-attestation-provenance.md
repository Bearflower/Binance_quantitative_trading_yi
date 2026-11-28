---
id: Binance_quantitative_trading-20261128-owner-attestation-provenance
类型: decision
项目: Binance_quantitative_trading
标题: 人工策略归属按账户所有者声明留痕并区分独立证明
日期: 2026-11-28
标签: [账本, 人工归属, 证据, PM]
任务标识: historical-income-owner-confirmation-2026-11-20
状态: 已采纳
审查: 已审查
---

# 人工策略归属按账户所有者声明留痕并区分独立证明

## 背景

同一 PM 账户的多策略交易曾有收入无法凭交易所记录自动判定策略属主。账户所有者在历史清单中指定了策略，称主要按飞书策略推送和时间匹配；最后 3 笔 ETHUSDT 资金费又明确指定给 `btc_eth_aggressive`。这些指定可以补齐策略账本，但其证据强度不同于可独立复核的交易所订单或持仓属主。

## 备选方案

- 始终保留未归属：避免误认领，但已由账户所有者确认的历史收入仍不能用于策略账本归集。
- 直接视为交易所证明：月报表面完整，但混淆了声明与可独立核验的事实。
- 记录账户所有者声明及证据来源：在身份和冲突检查后用于归集，同时保留证据等级。本次采用此方案。

## 决策

人工指定写入 `trading.reviewed_income_attribution`，保留 `account_owner_attestation` 来源与判断依据。写入前核对账户、收入类型、流水号、币种、金额、时间及既有归属冲突；订单相关收入还核对成交和订单身份。不得覆盖原始交易所收入、成交事实和此前复核记录。缺少独立持仓属主证明时显式保留该状态；本案最后 3 笔记为 `exchange_position_owner_proven=false`，原自动重放理由是 `multiple_position_owners`。

## 理由

既尊重账户所有者对历史策略的明确指定，又使后续审计能分辨「人工声明」和「交易所独立证明」。自动归属在多属主情形下继续保持保守边界。

## 后果

2026-11-20 的 764 笔清单中，89 笔已有一致复核记录，675 笔新增人工复核归属（672 笔按表格标签、3 笔按追加指定）。当时对应封窗月的策略归属报告可显示完整，但 `strategy_attribution_complete` 只表示所选报告口径下无未归属和冲突，不表示全部人工指定获得独立证明，也不等于盈利、ROI 或自动分配已获准。

## 适用范围与修订

这是 2026-11-20 PM 历史收入事件的证据分层规则。2026-11-27 起 Classic 账户的有限历史容缺是另一项独立批准，见相关决策；本条不代表当前 PM 定时任务仍在运行。以后若找到更强原始证据，应通过可审计修订接续，而非抹除原声明依据。

## 关联与证据

- [历史收入人工归属与正式口径复核](../../reports/historical-income-owner-confirmation-2026-11-20.md)「逐事件处理」「正式月报与运行状态」：记载用户依据、675 笔写入、3 笔资金费及三个封窗月结果。
- [派生归属导出遗漏问题](../issues/Binance_quantitative_trading-20261128-derived-attribution-export-omission.md)；[Classic 有限历史容缺](./Binance_quantitative_trading-20261127-bounded-history-acceptance.md)。
