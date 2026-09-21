# 佣金回填功能实现 + 部署（已完成）

- 完成时间: 2026-09-21 10:35
- 任务源: dashboard 看板总佣金恒为 0（trade_records.commission 恒 0）

## 问题根因
币安合约/PM 账户**下单返回不含 commission 字段**（佣金只在 userTrades 成交明细接口返回）。

## 拍板方案（用户多轮对齐）
**事后回填，不在下单时获取**。公共逻辑放 shared，调度宿主复用 dashboard 现有 APScheduler，不新建容器。

## 已实现
- `shared/trade_logger.py`：`TradeLogger.reconcile_commissions(binance_client, lookback_hours=24)` + 辅助方法 + SQL 常量
- `shared/binance_api.py`：新增只读 `get_user_trades(symbol, order_id=None, start_time=None)`（PM /papi/v1/um/userTrades，普通 /fapi/v1/userTrades）
- `dashboard/backend/services/commission_reconcile_job.py`：宿主 job `run_reconcile(data_service)`
- `dashboard/backend/main_docker.py`：`commission_reconcile` 调度器（IntervalTrigger，默认 3600s，窗口 24h，走 env COMMISSION_RECONCILE_INTERVAL_SECONDS / COMMISSION_RECONCILE_LOOKBACK_HOURS）
- `tests/test_shared/test_commissions.py`：11 个单测

## 佣金符号 bug 修复
首版累加 userTrades 无符号正数写库成正值，违反项目"佣金=负值支出"口径。修复：写库处 `expense = -commission`，并手动修正已落库 8 条为负值。

## 部署
- 仅重建 dashboard-api 容器（策略容器不重启）。
- 五层验证通过：容器 healthy / DEPLOY_ID 一致 / trade_logger MD5 一致 / 日志无 ERROR。
- 落库核验：HRS -0.0277 / MTPCS激进 -0.1005 / 新币做空 -0.1196，均为负值。

## 遗留（未完成）
- git 未提交（工作区大量历史改动，需只 add 佣金回填相关文件）
- 调度宿主耦合 dashboard 隐患（长期方向：独立数据后台宿主）
- 条件单平仓佣金仍无法按 order_id 归集
- 单测未针对符号取负重跑

## 详细快照
见 `docs/handoffs/commissions_backfill_2026-09-21.md`