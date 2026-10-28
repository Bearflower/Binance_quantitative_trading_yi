# AI Tuner 阶段二：风险调整指标与 1.5× 成本红线口径

> 日期：2026-10-28
> 状态：待用户审阅；用户已确认本文所述设计方向，尚未授权据此认定任何策略通过资格考试。
> 范围：仅定义 `btc_eth_aggressive` 与 `hrs` 的离线回放成绩卡和第 ⑧ 项硬门禁；不改生产参数、覆盖层、readiness 阈值或自动生效流程。

## 1. 决策摘要

1. 风险调整收益主指标采用 **Sortino**，基于按 UTC 日界线冻结的盯市权益计算。目标日收益率为 `0`，年度换算因子为 `365`（加密市场每日交易）。
2. **Calmar 仅作诊断**，不得参与首期候选排名或硬门禁。当前约 8 周诊断窗不足以稳健推断一年收益；报告仍可计算年化估计，但须标记 `short_window=true`。
3. 1.5× 手续费与滑点独立重放后，**任一时点**盯市权益若触及或低于初始权益的 `70%`，即触发 **30% 初始资金亏损红线**。这不是“至少保留 30% 资金”。
4. 基准成本档的 `max_drawdown_pct=15` 硬约束保持原样；`2.0x` 成本档只做诊断。三个成本档的成本单调性不变量仍须成立。
5. 指标无法可信计算时使用 `null` 加原因码，绝不填 `0`、极大值或无穷大；必需排序指标缺失时为 `BLOCKED_DATA`，不能自动放行。

## 2. 输入与时间口径

### 2.1 统一权益序列

- 使用 `ReplayResult.equity_curve.marked_equity`（含未实现盈亏、费用与资金费），不用已实现权益或只在平仓时采样。
- 回放起点显式加入 `initial_equity_usdt`。除非以后另有经审查的资金流契约，本期禁止入金/出金；若快照含外部资金流，整场考试 `BLOCKED_DATA`，不能把入金当收益。
- 每个 UTC `00:00:00` 生成一条日权益点：先处理该时点已到期的资金费和已收盘 K 线，再按最新**已收盘且未过期**价格盯市。不能使用未来 K 线，也不能将缺失日的上日权益机械复制成零收益。
- 对持仓交易对，若日界线缺少满足该策略 K 线周期的新鲜收盘价格，或资金费区间有缺口，标记 `daily_equity_gap` 并 `BLOCKED_DATA`。若引擎尚未产生日界线盯市点，不得从稀疏 `equity_curve` 直接假造日收益。
- 日收益为 `r_d = E_d / E_(d-1) - 1`；全部使用 `Decimal`。同一策略的当前层、候选层、历史层使用相同日界线、窗口、初始资金与快照。

### 2.2 窗口隔离

- **前 8 周诊断区**计算用于候选排序的 Sortino。至少 30 个连续、完整的日收益，且覆盖配置声明的整个诊断窗；数据不足为 `BLOCKED_DATA`。
- **最近 2 周盲测区**只提供资格门禁所需的独立交易数、净收益与回撤等证据；其结果不得回传 LLM，也不得并入前 8 周 Sortino。盲测区不足以单独做稳定的年化风险比率，不据此计算排序用 Sortino/Calmar。
- 如果窗口边界不是 UTC 日界线，使用边界后的首个完整 UTC 日到边界前最后一个完整 UTC 日；报告明确列出实际使用的起止日与日收益条数，不悄悄改变 8 周/2 周切分。

## 3. Sortino：主指标

设诊断区共有 `n` 个日收益 `r_d`，最低可接受日收益 `MAR=0`：

```text
mean_excess = Σ(r_d - MAR) / n
downside_deviation = sqrt[ Σ(min(r_d - MAR, 0)^2) / n ]
sortino_annualized = mean_excess / downside_deviation × sqrt(365)
```

- 分母按**所有有效日**的 `n` 计算，不只按亏损天数计算；日收益已包含交易费用和资金费。
- 先用 `Decimal` 完成计算，固定计算精度；报告序列化为十进制字符串，比较时不先四舍五入。`sqrt(365)` 只改变同窗候选的尺度，不改变其排序。
- `downside_deviation=0` 时，`sortino=null`、`sortino_unavailable_reason=no_downside_observation`；不得写为正无穷。该候选无法参加 Sortino 主排序，资格结果为 `BLOCKED_DATA`，即便净收益为正也不能自动放行。
- 日权益为零或负、日点缺失、收益非有限数时，`sortino=null` 并写明原因，资格结果为 `BLOCKED_DATA`；真实交易亏损或回撤已达到硬红线时，仍按既有硬门禁报告淘汰原因。
- Sortino 只在其他硬门禁通过后用于**同策略、同快照、同诊断窗**的候选比较；不能跨策略比较，更不能代替最大回撤硬约束。相同 Sortino 时先比诊断区扣费净收益，再比更低最大回撤，最后以稳定的候选引用排序，保证结果确定性。

## 4. Calmar：诊断指标

```text
window_return = E_end / E_start - 1
annualized_return = (E_end / E_start)^(365 / elapsed_days) - 1
calmar = annualized_return / (max_drawdown_pct / 100)
```

- `max_drawdown_pct` 取诊断区**所有**盯市权益点（含起始权益）的峰谷最大跌幅，不仅取日终点。`elapsed_days` 取实际诊断区首末权益时刻的 UTC 时间差，必须大于零。
- 诊断窗不足 365 天时，仍可给出上述估计，但 `short_window=true`、`calmar_use=diagnostic_only`。报告须同时显示未年化 `window_return` 和 `elapsed_days`，以免把短期年化数当成预期年收益。
- 最大回撤为零、权益非正或时间跨度无效时，`calmar=null` 并写原因；不得写无穷大，也不得把 `null` 当成最优值。由于 Calmar 不参与排名，此项单独不可计算不构成通过或淘汰依据。
- 成绩卡 schema 中 `sortino`、`calmar` 必须允许 `null` 并配原因字段；这修正既有契约 §5.4 将两者固定写成 `str` 的过窄类型。

## 5. 1.5× 成本压力硬门禁

### 5.1 独立重放与指标

- `1.0x`、`1.5x`、`2.0x` 使用相同快照、参数、起始资金和事件顺序独立重放；仅手续费与滑点按倍率改变，资金费率不乘倍率。
- 设 `E0 > 0` 为初始权益，`E_min_1_5x` 为 1.5× 档**全程盯市权益最低值**（包含起点、持仓期间、资金费结算点、结束点）：

```text
max_capital_loss_pct_1_5x = 100 × max(0, (E0 - E_min_1_5x) / E0)
preserved_capital_pct_1_5x = 100 - max_capital_loss_pct_1_5x
```

- 当 `max_capital_loss_pct_1_5x >= 30`，即触及红线，硬门禁 `FAIL`；只有原始 `Decimal` 值 `< 30` 才通过。报告展示值可舍入，但比较不得使用舍入值。
- 任一成本档缺少必要行情/资金费/权益点、初始权益非正或成本不变量被破坏时，不得 PASS；缺数据为 `BLOCKED_DATA`，已证实的不变量违规为 `FAIL`。
- `2.0x` 档记录最低权益、最大初始资金亏损和终值，只作诊断，不另设 30% 硬门禁。

### 5.2 配置键迁移

- 废弃含混的 `cost_1_5x_capital_preservation_pct`。新键名为 `max_cost_1_5x_capital_loss_pct`，建议值 `30`；值须显式写入 `purpose_driven_mvp.qualification.gates`，代码不能静默采用默认 30。
- 门禁实际值命名为 `max_capital_loss_pct_1_5x`，比较操作为 `<`（触线即失败）。报告另外展示 `preserved_capital_pct_1_5x` 便于人读。
- 旧键若仍存在，不得把 30 解释成“至少保留 30%”；迁移前该门禁为 `BLOCKED_DATA`。现有本地门禁骨架采用旧键和反向比较，必须在考试 runner 接入前修正并加回归测试。
- 这条 30% **压力档**红线不放宽基准档 15% 最大回撤约束，也不更改 readiness 的任何阈值。

## 6. 异常、报告与验收

- 资格总状态沿用现有传播规则：缺数据/未校准优先 `BLOCKED_DATA`；已证实的未来数据访问为框架级 `FAIL`；各项失败与阻断原因全部列出，不能只留第一个。
- 成绩卡必须包含：日收益数量/实际起止日、Sortino 与不可计算原因、Calmar 与短窗标记、基准档最大回撤、1.5× 最低盯市权益/最大初始资金亏损/保全比例、2× 诊断、成本单调性结果和每项门禁原始比较值。
- 测试至少覆盖：同输入逐字节一致；UTC 日界线与资金费顺序；缺日点不补零；零下行波动/零回撤不产生无穷大；起始权益非正；29.999% 通过、30.000% 触线失败；仅期末恢复但中途触线仍失败；1.5× 成本档缺数据阻断；盲测数据不进入 LLM 或诊断区 Sortino。
- 真实金样、HRS 时点历史、门禁其他待校准项不齐时，只能交付框架自测或 `BLOCKED_DATA` 报告，不得宣称正式资格考试通过或触发生产自动生效。

## 7. 备选方案及弃用原因

- **两个年化比率同时参与自动排名**：约 8 周数据会使 Calmar 的年化分子对偶然收益过于敏感，故本期不选。
- **1.5× 仅看期末余额**：中途可能触及严重亏损后恢复，漏掉资本红线，故不选。
- **将 30 当作最低保全资金比例**：允许最多亏 70%，与“30% 亏损红线”的项目既有用语冲突，故不选。

## 8. 参考与关系

- [Morningstar：Asset Allocation Optimization Methodology](https://morningstardirect.morningstar.com/clientcomm/Morningstar-Asset-Allocation-Optimization-Methodology.pdf)：目标收益以下的下行偏差定义。
- [Mutual Fund Directors Forum：Measuring the Investment Risk of Equity Funds](https://www.mfdf.org/docs/default-source/default-document-library/publications/white-papers/measuringrisk.pdf?sfvrsn=d00b8398_2)：Sortino 与 Calmar 的风险/收益定义；本文的短窗限制和自动化门禁是本项目的保守设计选择。
- 上游：`docs/superpowers/plans/2026-10-17-ai-tuner-mvp-replay-qualification.md`、`docs/superpowers/plans/2026-10-22-ai-tuner-stage2-replay-contracts.md`。
- 本文获审阅后，先修订上游契约 §5.4/§8.4 与门禁骨架，再实施 `qualification/metrics.py`、runner 和 CLI；不直接进入 push/部署。
