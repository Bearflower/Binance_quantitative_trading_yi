# 近期执行事实采集与 10 月回填

目标：只回填 PM 账户近期的 `REALIZED_PNL`、`COMMISSION`、`FUNDING_FEE` 和对应成交事实；不猜测或批量改写历史 `trade_records.account_id`，不改变月度资金分配。

## 上线前只读清点

- 生产事实表 `income/fill/cursor` 均为 0 行。10 月按北京时间自然月查询 PM 收入接口，三类分别返回 310、493、499 行，均为完成态，合计 1,302 行。
- 其中 303 个 `tranId` 在不同 `incomeType` 之间重复。旧 `(account_id, tran_id)` 主键会覆盖事件，不能直接回填。币安文档也仅保证 `tranId` 在同用户、同收入类型内唯一。
- 收入事件涉及 39 个币种；本地 `trade_records` 另有币种。成交采集需合并这两个来源的币种集合，按最多七天分窗。
- 当前运行中的 HRS 等部分策略仍被未入主线的 `allocation-fix` Compose override 固定在本地热修复镜像。GitHub CI/CD 的最新 `trading-hrs:latest` 镜像含此前账本发布代码，但不等于当前 HRS 运行镜像。本次推送用 `execution-facts-first-create` 目标化部署模式，仅构建 HRS 镜像并创建独立采集器；不执行全量 `compose up`。后续普通全量部署若未纳入该 override，仍可能重建策略容器，必须另行核对。

## 数据库变更

先备份 `execution_fact_income` 与 `reviewed_income_attribution` 到服务器 `/root/trading_system/database/backups/income_identity_pre_20261102.dump`。两表在变更前均为 0 行。[迁移](../../database/postgres/migrations/2026-11-02-income-event-type-identity.sql)的事务回滚演练通过，正式执行后两个主键均为 `(account_id, income_type, tran_id)`。

原始响应键同步采用 `account_id:income_type:tran_id`；月度报告及人工复核按同一三元组识别事件。采集入口显式开启写库后，连接失败或缺数据库口令会报错退出，不再降级为只输出摘要。

## 回填与验收边界

先部署包含新键的采集镜像，再从**新采集容器**执行 `docker exec trading_system-execution-facts python -m scripts.collect_execution_facts --start-ms 1790784000000 --end-ms 1793462400000` 回填 10 月；服务器宿主机没有同步这个 Python 模块。采集容器已配置 `EXECUTION_FACTS_DB_WRITE=true`、数据库连接和 PM API 环境变量。采集器按七天分窗、收入先行、成交随后；重试时还会从已持久化收入事实读取币种，避免收入游标完成后漏采成交。每个窗口的游标必须完成。之后独立采集容器每小时重查最近两天，以补迟到事件；超过两天的停机缺口需单独补采。采集失败会让容器退出并由 Compose 重启，便于暴露持续故障。

验收时在**新采集容器**运行 `docker exec trading_system-execution-facts python -m scripts.report_economic_ledger --month 2026-10 --account-id default`，核对三类收入行数及跨类型重复 ID 无损保存、成交和原始响应行数、各窗口游标连续性、月度报告未归属原因。本次目标化部署未更新正在运行的 AI Tuner 热修复镜像；其内旧月报代码不可用于本次验收。`trade_records` 的历史账户空值不会自动补齐，资金费默认未归属；即使采集覆盖完整，也不能直接把本报告称作五策略净收益。
