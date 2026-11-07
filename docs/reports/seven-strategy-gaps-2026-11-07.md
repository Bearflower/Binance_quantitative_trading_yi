# 七项策略调整整改与验收记录（2026-11-07）

范围：沿用交易所账户净额为最终口径；127 笔未归属收入事件由账户所有者另行处理。本次不赋予这些事件策略归属，不恢复自动分配，不增加风险额度。以下区分已经修复的代码缺陷、已经验证的生产事实和仍需要外部证据的结论。

## 生产只读基线

- 活动发布 pin 为 `8991d03d3c4625612b4d42d2014e829aa6750f4f`；本分支尚未上线。2026-11-07 早间，HRS、激进版、原版、新币、网格、看板 API、AI Tuner 容器健康。
- `strategy_gate` 只读通过；账户当时有 SOLUSDT LONG 0.27，门禁确认该仓有保护。不能沿用 11 月 6 日的空仓快照。
- 风险政策到期为 2026-11-07 14:11:40 UTC。到期后拒绝新风险；续期是新的风险决策，不能作为本次代码发布的隐含动作。
- 条件单位于 `btc_eth.condition_orders`，共 3915 行。激进版 STOP_LOSS 355 行、原版 STOP_LOSS 120 行，均有 algo_id 而无普通 order_id。`trading.execution_fact_fill` 有 2110 行，具有普通 order_id、trade_id 与 realized_pnl。当前缺少持久、逐笔核实的 algoId → actualOrderId → tradeId → lot 链。
- `trading.trade_records` 的 2026 年 8 月原版 101 行中，account_id 为空的为 101 行；订单 ID 非空 30 行。`trading.reviewed_income_attribution` 为 0 行。网格在该表无记录。没有可用的出入金事实表。

## 逐项处理与验收

| 项目 | 本次处理 | 剩余条件 | 验收标准 |
|---|---|---|---|
| 1. 激进版 STOP 后冷却与入场过滤 | 修复频控把带 PnL 的退出当成第二次开仓计数；增加 PM UM 按 algoId 只读查询和 STOP 普通订单、逐笔成交的严格校验，查询页满 1000 条时拒绝宣称完整。 | 尚无 lot 所有权、幂等事件和频控原子持久化，不能把校验器直接接到实盘冷却；入场过滤阈值也缺成本完整的对照样本。 | 在数据库中建立 account+strategy+lot+algoId+orderId+tradeId 的唯一事件链；重复、乱序、部分成交、重启、不同币种和跨策略用例均通过；真实止损后观察到冷却且无重复入场。阈值变化须单变量留出验证。 |
| 2. 新币恢复仓时间退出和 TP1 | 复查恢复逻辑：时间退出继续处于 unknown/review_required，数量减少不触发 TP1。现有重启、异常时钟和价格保护测试通过。 | 恢复仓开仓时间、TP1 真实成交与剩余仓位的统一身份链未获生产逐笔核验。 | 签名交易所订单与 userTrades 对齐 DB 仓位身份和数量，证明时间锚点、TP1 成交及剩余量；新旧仓区分测试通过后才考虑恢复自动时间退出。 |
| 3. 原版 8 月亏损归因 | 只读核对表结构和数据完整性，确认历史 account_id 全缺，未把币种净额猜作原版收益。 | 127 笔用户自处理事件及历史 lot/order 所有权。 | 每个收入事件有明确账户、订单、策略、费用与资金费归属；逐事件求和与交易所月度净额守恒，未归属仍显示 unknown。 |
| 4. 网格风险和收益 | 修复 `signal_bot.mode=semi` 但 bot disabled 时策略转入可下单状态、及直接调用 OrderManager 绕过 semi 的两处缺口。 | 无真实成交收益样本；无证据可承接其他策略释放资金。 | semi 模式的所有直接下单入口拒单；以后取得可归属订单、费用、资金费和持仓样本，再按预先固定的净收益与压力风险预算评估。 |
| 5. 自动分配 | 检查 AI Tuner 当前会因缺 capital/activity 事实而冻结，月度、日度和配置更新均有拒写门禁。保持冻结。 | 127 笔归属、资本基数、策略活动、完整月度报告与人工发布门禁均未满足。 | 完整输入通过守恒和覆盖率校验，未知值不按零处理；影子分配和现行额度并行对比，经独立审查后再单独决定是否启用。 |
| 6. 盈利能力验证 | 未凭历史小样本或代码修复声称策略变得盈利；固定未来验收方法。 | 至少两个不重叠的月度留出窗口与真实交易成本样本尚未发生。 | 各策略按成交归属计算已实现净额、资金费、最大回撤、压力损失和交易次数；同时报告未实现盈亏与缺失覆盖率；跨窗口复核后才讨论增配。 |
| 7. 现金流调整投资收益率 | 确认看板现有净值变化率已明确标注为未剔除现金流，未冒充投资收益率。 | 缺出入金、划转、估值时点与币种折算事实；不能安全回填真实 ROI。 | 按账户和币种收齐带唯一 ID 的外部现金流与期初/期末估值，先做现金流守恒，再计算约定的时间加权或资金加权收益；和交易所净值桥接后在看板单独展示，缺数据时显示未知。 |

## 本分支代码验收

测试 `tests/test_aggressive_frequency_readiness.py`、`tests/test_strategies/test_new_coin_recovery_evidence.py`、`tests/test_allocation_remediation`、`tests/test_accounting/test_dashboard_ledger.py`、`tests/test_accounting/test_stop_exit_evidence.py`、`tests/test_shared/test_open_algo_orders.py`、`tests/test_grid_semi_order_gate.py`、网格旧集成套件（排除既有 mock 价格类型错误的 `test_strategy_analyze`）：173 passed、2 skipped、1 deselected。`git diff --check` 通过。

## 发布边界

当前 CI 的 `auto` 路径会受活动 pin 阻断；本分支同时涉及 `shared/`，普通增量构建会扩散到全部服务。不得通过切换部署模式或忽略 pin 强推。需要为实际变更服务制作新的定向后继发布：固定前驱 pin 和镜像 digest，发布前后签名核验仓位、普通挂单、条件挂单与保护覆盖，禁止修改额度或延长政策，并核验无关容器身份不变。完成前，本报告的“代码验收”不等于生产上线。
