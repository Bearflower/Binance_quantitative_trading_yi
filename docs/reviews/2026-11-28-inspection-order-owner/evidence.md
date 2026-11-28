# 条件单归属重复告警：模式边界核验

基线：b52f442a；独立工作目录 `server-inspection-fix-20261128`。生产仅只读查询，无交易接口调用、数据库写入、权限变更、发布或通知。

## 根因

生产 AI Tuner 的 `USE_UNIFIED_ACCOUNT=false`，切点 `BINANCE_CLASSIC_CUTOVER_MS=1795582464000`（北京时间 2026-11-25 12:54:24）。IncomeReconciler 已按模式区分事实时间、北京时间交易记录时间和进度标识，nightly_exit_repair 却仍扫描所有账户历史，且用账户 ID 而非模式 progress_id 读游标，然后通过当前经典客户端查历史 PM 条件单。

| 币种 | 订单 ID | 成交时间 UTC | 数量 | 已实现盈亏 |
|---|---|---|---:|---:|
| ETHUSDT | 8389766326413274532 | 2026-11-25 04:53:18.060 | 0.02 | 0.7622 |
| XRPUSDT | 169188040851 | 2026-11-25 04:53:18.072 | 15.6 | 0.03744 |

两单均在切点之前。真实成交事实存在，但 `trading.trade_records` 中无对应 order_id/algo_id 记录，`public.condition_orders` 中也无对应 order_id 记录。不能根据币种、时间相邻或其他条件单记录归属为某策略，也不能断言它们是手动订单。

生产存在 `trading.historical_risk_acceptance` revision 1（2026-11-27 09:44:04 UTC）；本次没有改动或重签批准。既有历史容缺批准不能转换成已证明的订单归属。

## 修复

- 复用收益校准器模式切点；客户端模式不一致、经典模式缺切点时，在数据库或 API 调用之前拒绝。
- GAPS 的 income、fill、trade_records 全部限定模式；使用该模式的 progress_id 查询进度和缺口。
- repair_order 调用交易所前先读取并校验当前模式成交；条件单父归属和事务内复核亦限定模式。
- 同订单号在不同模式的真实历史记录不会错误屏蔽当前缺口。时间未知的重复记录仍保守阻止新增，防止丢失原有重复保护。
- 不通过当前经典接口查询历史 PM 订单；历史缺口保留一条 `BLOCKED_DATA` 汇总和 `historical_pending` 计数。不会补写未知策略归属，不推进 PM 游标。

## 验证

首次新增六条回归在原实现全部失败，修复后通过。

最终命令：

```sh
/tmp/binance-candidate-venv/bin/python -m pytest ai_tuner/tests/test_nightly_exit.py ai_tuner/tests/test_nightly_exit_epoch.py ai_tuner/tests/test_nightly_exit_epoch_postgres.py ai_tuner/tests/test_pnl_repair_cadence.py -q
```

结果：18 passed。包括真实本地 PostgreSQL，隔离 Unix socket，无生产连接；验证切点所属经典模式、同号订单跨模式、旧父条件单不污染新归属、两个数据库连接并发仅插入一次、再次调用幂等、时间未知重复记录仍阻止补录。

用新 GAPS SQL 在生产 `trading_user` 的只读事务中复算，statement_timeout=10s：经典模式候选 0，PM 历史候选恰为上述 2 单。没有调用 repair_order 或执行任何 INSERT。

未声明历史归属已补齐；解决的是错误调用当前模式接口处理历史 PM 缺口的代码缺陷。最终部署由主代理执行并核验。
