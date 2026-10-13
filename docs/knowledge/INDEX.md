# Binance_quantitative_trading 知识库索引

最后巡检: 2026-10-13（冷启动初始化；同日完成 5 条样例首次放下时复核；同日扩充第一批 11 条）
条目统计: 问题 12 / 决策 4 / 变更日志累计 1

## 巡检状态汇总
| 状态   | 数量 | 判定规则              |
|--------|------|-----------------------|
| 正常   | 12   | 本次检查未发现需复查项，不代表重新验证全部结论 |
| 待复查 | 4    | 存在证据缺口、相关代码变化或后续跟踪项等复查提示 |
| 孤立   | 8    | 无其他知识正文条目引用；索引入口不算入链，仅作提示 |

> 冷启动说明：5 条黄金样例的「审查」字段原均为「待审查」（仅表是否核对来源与表述，与状态互不混用）。2026-10-13 code-document-curator 完成首次放下时复核，5 条均已转「已审查」（其中 grid-min-quantity-unit 经「必要证据摘要」修正后转正）。
>
> 扩充说明（2026-10-13，同一批）：从近 30 天 handoff/review/commit 提炼第一批 11 条（9 issue + 2 decision），覆盖资金限额/共享账户归属、new_coin 状态恢复与对账、grid 实时行情与部署、hrs DB 一致性、grid 保证金引导与组合熔断等主题。同日 code-document-curator 完成首次放下时复核：11 条来源逐条核对通过，全部转「已审查」；其中 grid-margin-five-factor-guide 的 commit 引用经**更正**（部署确认报告误记 `8f882bf`，实为当时 HEAD 快照；该文件实际于 `75f1735` 首次入库）。
>
> 「审查」与「状态」是两个独立轴：审查=是否核对来源，状态=问题是否解决/决策是否生效。

## 问题模式库
| ID | 标题 | 标签 | 日期 | 状态 |
|----|------|------|------|------|
| Binance_quantitative_trading-20261013-grid-feed-silent-unhealthy | 币安 @aggTrade 停推叠加未捕获 TimeoutError 致 feed 静默假健康 | bug,实时行情,可观测性 | 2026-10-13 | 已解决 |
| Binance_quantitative_trading-20261013-grid-feed-queue-drop-latest | 队列溢出丢弃最新数据致采样取滞后价 | bug,实时行情,队列 | 2026-10-13 | 已解决 |
| Binance_quantitative_trading-20261010-grid-min-quantity-unit | 网格 min_quantity 单位错误导致资本可行性提醒全线误报 | bug,grid,单位换算 | 2026-10-10 | 已解决 |
| Binance_quantitative_trading-20261010-grid-profit-infeasible-copy | PROFIT 不可行分支把「利润率不达标」误述为「资金不足」 | bug,grid,文案 | 2026-10-10 | 已解决 |
| Binance_quantitative_trading-20261009-capital-allocation-month-offset | 月度资金分配「记录月份错位」导致 10 月限额落空 | bug,ai_tuner,月份口径 | 2026-10-09 | 已解决 |
| Binance_quantitative_trading-20261009-schema-migrate-before-up | DB schema 迁移晚于 docker up -d 致容器 crash-loop 且出口永久停用 | bug,部署,运维 | 2026-10-09 | 已解决 |
| Binance_quantitative_trading-20261009-hrs-cancel-not-persisted | 撤单只清内存未回写 DB 致 OPEN 残留被当假孤儿反复清理 | bug,hrs,DB一致性 | 2026-10-09 | 已解决 |
| Binance_quantitative_trading-20261001-newcoin-zombie-reconcile | 幽灵 open 对账须 fail-closed；kline 表存在性硬编码成死代码 | bug,new_coin,对账 | 2026-10-01 | 已解决 |
| Binance_quantitative_trading-20260927-newcoin-atr-loss-on-restart | 重启后 ATR 丢失致阈值退 0 立即误平 | bug,new_coin,状态恢复 | 2026-09-27 | 已解决 |
| Binance_quantitative_trading-20260922-shared-account-position-ownership | 共享 PM 账户持仓按币种而非归属过滤致互挂保护单/误平 | bug,mtpcs,持仓归属 | 2026-09-22 | 待复查 |
| Binance_quantitative_trading-20260921-pm-positionrisk-no-isolation | PM 账户 positionRisk 无策略隔离致占用虚增与越权管理他策略仓位 | bug,资金限额,PM账户 | 2026-09-21 | 已解决 |
| Binance_quantitative_trading-20260915-newcoin-target2-never-marked | target2_reached 从未被标记致移动止损整条链路永不生效 | bug,移动止损,止盈检测 | 2026-09-15 | 已解决 |

## 决策记录
| ID | 标题 | 日期 | 状态 | 替代入口 |
|----|------|------|------|----------|
| Binance_quantitative_trading-20261010-grid-capital-floor-reverted | 作废 be0a71a「资本下限抬升」机制，恢复诚实单位口径 | 2026-10-10 | 已采纳 | — |
| Binance_quantitative_trading-20261009-max-drawdown-caliber | 看板最大回撤统一为「资金基数 + 累计已实现盈亏」口径 | 2026-10-09 | 已采纳 | — |
| Binance_quantitative_trading-20260921-market-circuit-breaker | 组合级 1h 等权双向熔断（二期补 12h 累计维度） | 2026-09-21 | 已采纳 | — |
| Binance_quantitative_trading-20260917-grid-margin-five-factor-guide | 网格保证金由静态值改为五因子动态引导（BTC 独立检测器） | 2026-09-17 | 已采纳 | — |

## 每日变更（近 30 天）
| 日期 | 变更数 | 摘要 |
|------|--------|------|
| 2026-10-13 | 5 | [knowledge-base-build] 建立知识库样板（3 模板 + 5 样例 + INDEX）、knowledge 入 Git、修正 3 条样例证据/状态、补齐 skill 与 Claude 入口接入；[knowledge-base-expand] 扩充第一批 11 条（9 issue + 2 decision）并刷新 INDEX |

## 待复查清单
- [ ] Binance_quantitative_trading-20261010-grid-min-quantity-unit：§9.10 条件①②③（feasible=true 互算自洽）待补验
- [ ] Binance_quantitative_trading-20261010-grid-profit-infeasible-copy：§10.9 发布后真实推送观察尚无完成记录，待验证
- [ ] Binance_quantitative_trading-20260922-shared-account-position-ownership：根治方案（方案 C 持仓归属互斥）代码已落地但**尚未部署**，无 commit、无验证证据
- [ ] Binance_quantitative_trading-20260915-newcoin-target2-never-marked：移动止损已上线，但**尚无真实 TP2 成交数据**验证激活效果（部署时容器无持仓），待观察
- [x] 5 条黄金样例「审查」字段首次复核：2026-10-13 code-document-curator 逐条核对来源完成，5 条全部转「已审查」
- [x] 第一批新增 11 条的「审查」字段首次复核：2026-10-13 code-document-curator 逐条核对来源完成，11 条全部转「已审查」（其中 grid-margin-five-factor-guide 经 commit 引用更正后转正）（与状态维度相互独立）

## 孤立条目
- Binance_quantitative_trading-20260917-grid-margin-five-factor-guide（无其他知识正文条目引用）
- Binance_quantitative_trading-20260921-market-circuit-breaker
- Binance_quantitative_trading-20261001-newcoin-zombie-reconcile
- Binance_quantitative_trading-20261009-schema-migrate-before-up
- Binance_quantitative_trading-20261009-hrs-cancel-not-persisted
- Binance_quantitative_trading-20261010-grid-profit-infeasible-copy
- Binance_quantitative_trading-20261009-capital-allocation-month-offset
- Binance_quantitative_trading-20261009-max-drawdown-caliber

## 关联
- [项目文档入口](../README.md)
