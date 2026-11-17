# 缩量试跑资本与风险证据实施计划

> 实施按独立组件交付，使用 planning-with-files-zh 跟踪，按 dispatching-parallel-agents 分配互不重叠的组件，集成后分别做规格与代码复审。

目标是让已核收益账本、追加资本历史和5%主账户回撤预算进入正常考核入口。

技术沿用 Python、asyncpg、PostgreSQL、现有只读 Binance 客户端和 CI/CLCI。

## 任务1 正式报告兼容

- [x] 用派生报告进入影子预览及 PnLCollector 的用例复现两项错误。
- [x] 在 published_report 声明基础/派生版本集合，消费者继续严格校验账户、时间窗和完整性。
- [x] `python3 -m pytest -q ai_tuner/allocation/tests/test_shadow_allocation.py tests/test_allocation_remediation/test_published_pnl.py tests/test_accounting/test_ledger_publication.py`，47 passed。

## 任务2 追加资本历史

文件包括 `database/postgres/migrations/2026-11-17-capital-history.sql`、`ai_tuner/allocation/capital_evidence.py` 和对应测试。实现安装时的原子 BASELINE、源表语句触发器、只追加保护、同账户月度分配资本加权投影。

接口为 `read_capital_evidence(db, account_id, start_ms, end_ms, strategy_ids)`，返回每策略 account_id/start_ms/end_ms/average_capital_usdt/source_id/coverage_complete。事实不足的策略返回 coverage_complete=false 与 reasons，不能给0作为未知值。

先用两个额度变更时段测试 `20 * 1/2 + 40 * 1/2 = 30`，并覆盖基线晚于窗口、缺策略、删除分配、未来月份、重复月份、时区月界和审计缺失。PG事务测试检查基线、UPDATE/DELETE后的快照及不可变保护。

## 任务3 账户风险观察

文件包括 `ai_tuner/monitor/account_risk.py`、`database/postgres/migrations/2026-11-17-account-risk-facts.sql` 及对应测试。接口为 `collect_account_risk(db, pm_client, wallet_client, budget)` 与 `assess_account_risk(evidence, budget)`。budget 包含 account_id=default、max_drawdown_ratio=0.05、独立版本和启用锚点。

收集签名权益、PM跨边界转账和分页水位，保留不可变事实。纯计算验证单位净值的账户、时序、现金流调整来源和采样范围，计算 `(peak-current)/peak` 及区间最大值。已核序列1、1.02、0.969的最大回撤达到5%，风险复核；现金流缺少时点估值、采样缺口等均返回未知。不能自动下单或变更政策。

## 任务4 入口集成

文件为 `ai_tuner/main.py`、`ai_tuner/monitor/reduced_trial.py`、`ai_tuner/config.yaml` 与 `ai_tuner/allocation/pnl_collector.py`。采集任务按已有调度注册，预算和缺口进入日周通知，资本读取接入 PnLCollector。真实入场活动按已核订单和风险预留身份收集；缺少身份则继续阻断该项资格。

## 任务5 发布与验收

- [x] 验证迁移、边界、重试、并发和正常入口，生产 Python 3.10 合跑305 passed，独立审查批准observer范围。
- [x] 固定生产实际AI Tuner镜像、源文件哈希和发布pin，专用CI/CLCI仅更新AI Tuner和3份迁移。
- [ ] 验证新增事实采集、资本基线、5%预算报告和现金流未知状态。
- [ ] 更新整改跟踪表，完整区分已发布代码、正在累计的未来数据和仍缺失的资格证据。
