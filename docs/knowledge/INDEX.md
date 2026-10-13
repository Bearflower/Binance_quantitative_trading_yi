# Binance_quantitative_trading 知识库索引

最后巡检: 2026-10-13（冷启动初始化；同日完成 5 条样例首次放下时复核）
条目统计: 问题 3 / 决策 2 / 变更日志累计 1

## 巡检状态汇总
| 状态   | 数量 | 判定规则              |
|--------|------|-----------------------|
| 正常   | 3    | 本次检查未发现需复查项，不代表重新验证全部结论 |
| 待复查 | 2    | 存在证据缺口、相关代码变化或超 90 天未核对等复查提示 |
| 孤立   | 3    | 无其他知识正文条目引用；索引入口不算入链，仅作提示 |

> 冷启动说明：5 条黄金样例的「审查」字段原均为「待审查」（仅表是否核对来源与表述，与状态互不混用）。2026-10-13 code-document-curator 完成首次放下时复核：4 条与来源一致已转「已审查」；1 条（grid-min-quantity-unit）先因「必要证据摘要」混入后续任务（commit 663edea）的 58/58 + 717/717 测试证据、与其「验证」节自注矛盾而保留「待审查」，其「必要证据摘要」修正回本任务真实证据（命令1 54 passed；命令2 101 passed, 1 failed=基线既有）后转「已审查」。至此 5 条样例均已「已审查」。另按「状态」维度，2 条因证据缺口列为「待复查」（见下方清单），与「审查」是两个独立轴。

## 问题模式库
| ID | 标题 | 标签 | 日期 | 状态 |
|----|------|------|------|------|
| Binance_quantitative_trading-20261010-grid-min-quantity-unit | 网格 min_quantity 单位错误导致资本可行性提醒全线误报 | bug,grid,单位换算 | 2026-10-10 | 已解决 |
| Binance_quantitative_trading-20261010-grid-profit-infeasible-copy | PROFIT 不可行分支把「利润率不达标」误述为「资金不足」 | bug,grid,文案 | 2026-10-10 | 已解决 |
| Binance_quantitative_trading-20261009-capital-allocation-month-offset | 月度资金分配「记录月份错位」导致 10 月限额落空 | bug,ai_tuner,月份口径 | 2026-10-09 | 已解决 |

## 决策记录
| ID | 标题 | 日期 | 状态 | 替代入口 |
|----|------|------|------|----------|
| Binance_quantitative_trading-20261009-max-drawdown-caliber | 看板最大回撤统一为「资金基数 + 累计已实现盈亏」口径 | 2026-10-09 | 已采纳 | — |
| Binance_quantitative_trading-20261010-grid-capital-floor-reverted | 作废 be0a71a「资本下限抬升」机制，恢复诚实单位口径 | 2026-10-10 | 已采纳 | — |

## 每日变更（近 30 天）
| 日期 | 变更数 | 摘要 |
|------|--------|------|
| 2026-10-13 | 4 | [knowledge-base-build] 建立知识库样板（3 模板 + 5 样例 + INDEX）、knowledge 入 Git、修正 3 条样例证据/状态、补齐 skill 与 Claude 入口接入 |

## 待复查清单
- [ ] Binance_quantitative_trading-20261010-grid-min-quantity-unit：§9.10 条件①②③（feasible=true 互算自洽）待补验
- [ ] Binance_quantitative_trading-20261010-grid-profit-infeasible-copy：§10.9 发布后真实推送观察尚无完成记录，待验证
- [x] 5 条黄金样例「审查」字段首次复核：2026-10-13 code-document-curator 逐条核对来源完成，5 条全部转「已审查」（grid-min-quantity-unit 经「必要证据摘要」修正后转正；审查与状态维度相互独立）

## 孤立条目
- Binance_quantitative_trading-20261010-grid-profit-infeasible-copy（无其他知识正文条目引用）
- Binance_quantitative_trading-20261009-capital-allocation-month-offset
- Binance_quantitative_trading-20261009-max-drawdown-caliber

## 关联
- [项目文档入口](../README.md)