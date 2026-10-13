---
id: Binance_quantitative_trading-20261010-grid-profit-infeasible-copy
类型: issue
项目: Binance_quantitative_trading
标题: grid PROFIT 不可行分支把「利润率不达标」误述为「资金不足」
日期: 2026-10-10
标签: [bug, grid, 文案]
任务标识: grid_profit_infeasible_copy_fix_2026-10-10.md
状态: 已解决
审查: 待审查
---

# grid PROFIT 不可行分支把「利润率不达标」误述为「资金不足」

## 现象
用户核对 18:05 网格信号灯真实推送，PROFIT 不可行分支首行「当前建议保证金 667 USDT 无法支撑最少 5 格（每格仅 0.535 ETH，需 ≥0.01 ETH）」——0.535 ≫ 0.01，资本明明够（可支撑 267 格），却被说成「无法支撑 5 格」，括号数字自相矛盾。

## 根因
不可行分支首行是 V2.5「资本口径」固定模板（signal_bot.py#L896-L908），无论 binding 是什么都先输出；上一任务仅抑制「可达上限」行与方案1/2，未改写首行。真实归因是利润率不达标（每格利润率 0.39% < min_profit_rate 1%）。

## 解决
PROFIT 首行改利润率口径（「区间过窄/波动率过低，无法创建满足利润率下限的网格」），CAPITAL 保留资本口径；推送改为四段序「行情→钱→格→校验」并砍掉行动板块；PROFIT 不可行退化为「行情→校验归因」两段。

## 验证
58/58 核心测试全绿（新增 TestGridPushMessageStructure 4 测例）+ 717/717 策略目录无回归。

## 适用范围与修订
strategies/grid/{signal_bot.py, margin_advisor.py}；设计 §10 取代 §9.3/§9.8 口径。margin_advisor else 分支删除为超设计范围的去重修正（Advisor 已接受）。

## 教训
归因文案必须与实际 binding 约束变量对齐，避免「计算正确但文案矛盾」——计算层正确不保证用户体验层归因正确。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/grid_profit_infeasible_copy_fix_2026-10-10.md)（不随 Git 同步）
- 持久证据: 设计文档 docs/requirements/grid/网格交易系统 V2.5.5 保证金-网格数联动（方案B）需求与设计.md §10；commit 663edea
- 必要证据摘要: 58/58 + 717/717 测试通过；四段序与 PROFIT 两段抑制落地；§10.9 发布后真实推送观察（feasible=true 四段序生产实证）尚无完成记录，待验证
- 相关: [网格 min_quantity 单位错误](./Binance_quantitative_trading-20261010-grid-min-quantity-unit.md)