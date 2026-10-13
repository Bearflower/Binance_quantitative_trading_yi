---
id: Binance_quantitative_trading-20260921-dashboard-commission-always-zero
类型: issue
项目: Binance_quantitative_trading
标题: dashboard 总佣金恒 0（commission 仅存在于 userTrades）
日期: 2026-09-21
标签: [bug, dashboard, 佣金]
任务标识: commissions_backfill_2026-09-21.md
状态: 已解决
审查: 已审查
---

# dashboard 总佣金恒 0（commission 仅存在于 userTrades）

## 现象
dashboard 看板【总佣金】始终显示为 0，与实际成交扣费不符。

## 根因
币安合约 / PM 账户**下单接口返回不含 `commission` 字段**，佣金只在 `userTrades` 成交明细接口返回。系统在下单路径落库 `trading.trade_records.commission` 时取的是下单返回，字段恒缺失 → 佣金**落库恒 0**。

## 解决
拍板方案为**事后回填**，不在下单时获取（避免下单延迟、避免每次条件单查 userTrades 的负担）：
- 公共逻辑放进 `shared/trade_logger.py`：`TradeLogger.reconcile_commissions(binance_client, lookback_hours=24)` + 辅助 `_fetch_pending_trade_keys` / `_query_order_commission` + SQL 常量 `_PENDING_RECORDS_SQL` / `_UPDATE_COMMISSION_SQL`
- `shared/binance_api.py` 新增只读公共方法 `get_user_trades(symbol, order_id=None, start_time=None)`（PM `/papi/v1/um/userTrades`、普通 `/fapi/v1/userTrades`）
- `dashboard/backend/services/commission_reconcile_job.py`：宿主 job `run_reconcile(data_service)`
- `dashboard/backend/main_docker.py`：新增 `commission_reconcile` 调度器（复用既有 APScheduler 设施，不新建容器；后随数据后台解耦迁至 `services/data_backend`）
- 明确**不改** `place_order` / `place_conditional_order` 下单路径与钩子

幂等：`UPDATE ... SET commission=$1 WHERE symbol=$2 AND order_id=$3 AND commission=0`，已回填过的行不再覆盖；同 order_id 多笔分笔成交佣金累加写回。配置走环境变量：`COMMISSION_RECONCILE_INTERVAL_SECONDS`（默认 3600s）、`COMMISSION_RECONCILE_LOOKBACK_HOURS`（默认 24h）。

## 验证
- `python3 -m py_compile` 5 文件语法 OK
- `pytest tests/test_shared/` → **179 passed, 1 skipped**（唯一失败为既有 `test_binance_api.py::test_production_url`，预存失败、与本次无关）
- 独立审查（code-specification-inspector）通过
- 上线部署五层验证通过（该次部署同时完成数据后台解耦）：data-backend DEPLOY_ID=**6081C8BC**、dashboard-api DEPLOY_ID=**59C00584**，均 Up (healthy)、L4 MD5 对齐、日志错误 0
- 落库核验：回填值为**负值**——HRS 策略 -0.0277 / MTPCS 激进策略 -0.1005 / 新币做空策略 -0.1196

## 适用范围与修订
`shared/{trade_logger.py, binance_api.py}`、`dashboard/backend/services/commission_reconcile_job.py`、`dashboard/backend/main_docker.py`（调度宿主后续迁至 `services/data_backend`）；commit `75f1735` 首次入库（与 new_coin 基线归属过滤等合并提交）。无替代条目。
**已知覆盖局限（用户裁决记录）**：仅能回填 `order_id` 非空的行（开仓单为主）；交易所触发的**条件单平仓**（成交用新的 orderId，库中无对应行）佣金仍无法按 order_id 归集。深化方向（未实施）：symbol+时间窗口+side+qty 近端匹配，或建 `trading.condition_order_map` 映射表。

## 教训
「下单成功」不等于「费用已知」——费用字段常只存在于事发后的成交/流水明细接口，落库路径若依赖下单响应就会永久丢数。这类缺口应把它当**异步补全**处理（幂等回填 + 明确覆盖边界），而不是硬塞进同步下单链路。回填务必明示覆盖范围（本例明确排除条件单平仓），避免看板数值看似完整却系统性漏计。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/commissions_backfill_2026-09-21.md)（不随 Git 同步）
- 持久证据: commit `75f1735`（`shared/trade_logger.py` `reconcile_commissions`）；评审记录 `.trae/memories/2026-09/21/1035-commission-backfill.md`
- 必要证据摘要: 下单返回无 commission → 落库恒 0；179 passed, 1 skipped；五层验证通过（DEPLOY_ID 6081C8BC / 59C00584）；落库负值 HRS -0.0277；条件单平仓佣金归集为已知局限
- 相关: [Binance_quantitative_trading-20260921-commission-backfill-sign-error](./Binance_quantitative_trading-20260921-commission-backfill-sign-error.md)（同源 handoff，根因不同：本条为数据来源缺失，另一条为符号口径错误）
