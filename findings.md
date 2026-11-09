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
