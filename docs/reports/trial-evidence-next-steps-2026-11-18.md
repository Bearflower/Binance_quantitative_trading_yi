# 缩量试跑证据接线与剩余数据工程

日期：2026-11-18，北京时间。范围：HRS、MTPCS 激进版的 AI Tuner 只读日周评估。

## 本次接线

日报和周报读取现有 `read_activity_evidence`，将已核入场逐笔成交数、窗口覆盖结论、来源摘要和缺口保存到报告，并在通知中单列。读取要求账户、窗口和策略身份一致；异常与不完整证据保留未知。该结果只证明入场事实的一个阶段，不代替信号→订单→成交→保护→退出的完整漏斗；`execution_funnel_unverified` 继续保留。定向测试 75 passed、3 skipped。

限定 [CI 44227121382](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44227121382) 成功；生产 pin `7d9fe3ab25ffc18d3f84012f0adec012f53addb2` active、发布状态 complete，只更新 AI Tuner。容器 healthy，`reduced_trial.py` 运行哈希与本地 `600d6d0709654aeb15595d13ac520ee697449145d8dfc67c4f1b048129ff1dd3` 一致。首个新日窗的实际报告与通知仍需下一次定时任务验收。

生产只读盘点：`entry_activity_source_fact` 中有 1 条 capture_boundary、22 条 legacy_intent、3 条 intent、6 条 intent_state、699 条 collection_boundary 和 699 条 collection_batch；`entry_order_observation` 有 26 条 ok。没有 protocol_window，因此现有采集不能证明所有入场路径均受覆盖，完整覆盖率仍不可认定。

## 继续实施的证据顺序

1. **成交漏斗**：先核 `protocol_window` 所需的全入口接线、完整 symbol 域、签名订单结果与 userTrades 游标；以稳定 signal ID 绑定 reservation、普通订单、逐笔成交与保护单。分别计数取消、拒单、部分成交、未归属和真实保护。未完成窗口不得返回完整覆盖。
2. **保证金**：PM `positionRisk` 不提供可直接审计的逐策略保证金。需要固定每个策略已归属持仓的 symbol、方向、数量与签名时点，再从可重放的合约规则和政策版本计算风险预算占用；当前 `markPrice × qty / leverage` 只是估算，不能替代 PM 交易所保证金或用于解除 `holdings_margin_unverified`。活跃预留和既有持仓须按同一批次去重，避免重复计入。
3. **策略权益与压力损失**：先建立每策略的起始资本与后续划转归属、费用和资金费的不可变事件及时点估值。完成现金流桥接后才形成策略单位净值及回撤；压力损失以冻结的情景、持仓和保护单执行假设重放。账户级 5% 回撤预算独立生效，不能冒充策略级业绩。
4. **放宽资格**：在首个正式判定前冻结数值阈值，积累两个互不重叠的成本完整留出月；AI Tuner 仅出具候选审查，不自动提额、续期或下单。

用户保留的风险收缩和资金分配冻结继续有效。真实 STOP、恢复仓和留出月依赖未来样本，数据契约与核验代码可以先行推进。
