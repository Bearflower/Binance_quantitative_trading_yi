# 七项整改调查发现

- 隔离基线：`origin/main` 的 `8991d03d3c4625612b4d42d2014e829aa6750f4f`；生产活动 pin 同 SHA，状态 active。
- 2026-11-07 早间，HRS、激进版、原版、新币、网格、dashboard-api、ai-tuner 容器均为 running/healthy。
- 现有代码对未完成策略归属保持显式 unknown，分配仍冻结；`pnl_collector.py` 明确因 `missing_capital_evidence`、`missing_activity_evidence` 拒绝排名。
- 新币恢复仓的 `time_exit_status` 为 review_required/unknown，代码有禁自动时间退出保护。
- 激进版频控恢复、时区和故障拒新单代码在当前基线中，但 STOP 专属退出冷却仍缺归属到 lot/order/trade 的已证成交事件。
- 原版历史亏损、网格收益样本和现金流调整收益率的缺口，不能用账户币种净额或净值变动替代。
- 生产 `strategy_gate` 于 2026-11-07 00:03 UTC 通过；账户有 SOLUSDT 多仓 0.27，门禁确认 1 个受保护持仓，非空仓。部署前的“无持仓”快照已过时。
- 网格 `signal_bot.mode=semi` 时，`OrderManager.place_grid_order` 仍能直接下单；若 `signal_bot.enabled=false`，`GridStrategy.is_signal_mode` 会变成 false。两处均有可复现失败测试。
- 旧网格集成测试 `test_strategy_analyze` 在未改动的分析函数中把 dict 价格送到 `GridCalculator`，报类型错误；不能把全套旧集成测试称为通过。
- 看板 `get_account_returns` 当前计算未剔除现金流的净值变动率；API 与页面已明确文字说明，完整投资收益率缺现金流/估值桥接证据。
- 生产实际条件单表是 `btc_eth.condition_orders`（3915 行）；`public.condition_orders` 为空。激进版 STOP_LOSS 355 行全部有 algo_id、全部缺普通 order_id；原版 120 行也同样缺 order_id。成交事实 `trading.execution_fact_fill` 有 2110 行且 order_id/trade_id/PnL 均齐全，但缺 algo→actualOrderId 的持久映射。
- 币安官方 PM UM `GET /papi/v1/um/algo/algoOrder` 可按 algoId 查询，响应包含 actualOrderId；取消/过期无成交的订单有查询时限。查询结果仍需普通订单 FILLED 和 userTrades 数量逐笔核对，不能单看 FINISHED/actualOrderId。
- 激进版 `FrequencyController.record_trade(..., pnl)` 原本会把平仓再算作一次开仓次数；只有开仓路径实际调用，但这是接入已核退出事件前的错误语义。
- 2026-11-09：`PositionState.entry_order_id` 在 `merge_position` 加仓时被覆盖，不能用作持仓批次键；增加不随加仓变化的首笔入场单身份。历史快照缺该字段时必须保持未知。
- 新挂 STOP 有通用 `_place_conditional_order_and_record` 路径，极端行情 `_tighten_extreme_stop` 直接调用交易所；两条路径均需记录候选绑定。仅有候选绑定不足以认定 STOP 成交或启动冷却。
- `fetch_lot_fills` 以六天窗口分页。旧恢复测试假设 2026-10-31 至当前日期始终只需一次查询，11 月 9 日开始失效；改成首窗返回成交、次窗为空，并检查首次调用参数。
- STOP 订单本身 FILLED 且逐笔数量相等，仍不足以证明它平的是同一批次；从首入场成交时间到 STOP 最后一笔成交的完整 userTrades 历史必须显示同向持仓持续不归零、STOP 后归零。闭合批次证明已作为只读函数实现，未核证据不入退出事件表。
- 币安当前 PM UM algo 下单支持 `clientAlgoId`，查询接口也可按该 ID 查订单；无成交撤销/过期单 3 天、所有单 90 天后可能不可查。先在策略持久卷 fsync 该 ID 与批次，再 POST，可覆盖响应未知和数据库中断重启后的恢复。查询不到不能当作未成交，意图保持待核并阻断新入场。
- 激进版 Docker Compose 将 `./data/btc_eth_aggressive` 挂到 `/app/data`；持久意图日志使用此卷，入场门禁在新开仓前执行写入与读取探针。
- 生产 2026-11-09 午后只读 `strategy_gate` 版本 2 通过，账户空仓、无普通/条件单。激进版 `public.strategy_states` 四条历史 `position:*` 快照都缺 `episode_entry_order_id`；不能凭快照的最后入场单或旧 algoId 补认闭合批次，需单独核交易所成交链。

## 2026-11-11 本轮新增发现

- 生产新币 14 条历史短仓全部 closed；PFE/MELI 无 TP1 成交，MSTU/RDW 有正样本。历史 metadata 无首单/TP1 身份，现有安全门禁不允许自动时间退出。新代码只为未来新仓写身份，旧仓不补猜。
- 新币时间退出可安全解锁的狭窄子集是首单完全成交、此后无任何同币种成交、交易所剩余仓位仍等于原量、TP1 未触发且全量止损仍在挂的近六天仓位。混批、减仓、漏页或保护缺失继续未知。
- 自动分配资本和自有成交活动历史来源缺失；本轮只提供影子预览的严格输入门禁，不启用实盘写入。
- 现金流 ROI 缺原始流水和事件时点权益；新增纯计算与看板显式未知态，不发布伪造收益率。
- 旧 `strategies/new_coin/tests/test_baseline_backfill_methods.py` 的 16 例引用主线早已不存在的 `_backfill_anchor_quantity`，属既有失效测试，不能作为本轮回归通过证明。
- 激进版 `persist_signal` 会原样保存 signal 的扩展 metadata，但此前成功新仓未回填 `entry_order_id`；HRS 的风险预留 `clientOrderId` 由 `signal_id` 确定性计算，但 geometry 未直接保留该 ID。两处已补稳定关联，供后续成交漏斗只读核验。
