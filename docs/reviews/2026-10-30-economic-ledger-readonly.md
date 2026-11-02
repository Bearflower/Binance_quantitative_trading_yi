# 交易经济账只读核对（第一阶段）

入口：`python3 -m scripts.report_economic_ledger --month 2026-10 --account-id main`。仅当已部署逐事件复核表并有审核证据时，可加 `--use-reviewed-overrides`。
在部署 `pnl_source` 列后，可加 `--include-hrs-provenance`，附加同账户、同月份的 HRS `PNL_SUMMARY` 来源分布；历史来源空值列为 `legacy_unknown`，无账户标识的历史行不被猜测纳入。该分布是交易记录诊断，不参与收入守恒或月度分配。
月份使用北京时间自然月，数据库连接读取 `DATABASE_HOST`、`DATABASE_PORT`、`DATABASE_NAME`、`DATABASE_USER`、`DATABASE_PASSWORD`；程序只运行 `SELECT`，输出 JSON，不更新交易、分配或配置。退出码 0 表示所需事实窗口连续覆盖且本轮收入无未归属项；2 表示存在缺口或未归属项；1 表示参数或连接等错误。

本阶段以 `execution_fact_income` 的 USDT 收入事件为唯一金额来源。`REALIZED_PNL` 与 `COMMISSION` 仅在 `account_id + trade_id → execution_fact_fill → account_id + order_id → trade_records.strategy` 唯一、且收入金额与成交事实金额一致时归属。报告同时读取月内全部已采集成交；若成交显示非零已实现盈亏或 USDT 手续费，却没有相应收入事件，就列入 `missing_income_fills`，验收标记为假。`TradeLogger` 现在会把明确配置的 `TRADE_ACCOUNT_ID` 写入新记录；既有记录的账户列保持 `NULL`，不猜测回填。`FUNDING_FEE` 缺少逐成交归属，默认保留在未归属项。可选的 `trading.reviewed_income_attribution` 为逐事件人工复核提供归属：报告会逐项核对账户、流水 ID、币种、类型、金额、资产、时间和审核证据，未显式启用时完全不读取。非 USDT 资产单列，绝不直接换算为 USDT。相同 `tran_id` 的冲突事件会中止报告。三个 income 类型与相关币种的 fills 还需有连续、完成态的采集游标，否则 `id_match_and_coverage_complete=false`。

报告提供 `account_usdt_total = attributed_usdt_total + unattributed_usdt_total` 守恒检查。该总额仅涵盖已观察到的三类 USDT income；不含未实现盈亏、划转、权益期初期末，也不证明五策略的账户净收益。历史 `trade_records.account_id=NULL` 会阻止该订单归属；因此即使 `id_match_and_coverage_complete=true`，也只能称作**三类收入事件的 ID 匹配和采集覆盖完成**，不能称作账户权益守恒或可直接接入月度分配的净收益真值。接入分配前，需要补资金费率归属，并以实际月份数据复核。

本地未配置数据库连接环境变量；离线单元测试不证明生产事实已覆盖。

## 生产环境复核（2026-10-30 至 2026-10-31，北京时间）

用户确认 HRS、MTPCS 原版/激进版、新币做空和网格共用同一个币安 PM 账户。现有 HRS 收入归属审计表的 8 条记录使用 `account_id=default`，故后续新事实统一采用 `default` 作为该账户的内部标签；这不是交易所账户 ID，也不证明历史每条记录的身份。

生产库先备份 `trade_records` 到服务器上的 `/root/database/postgres/backups/trade_records_pre_account_migration_20261030.dump`，验证备份文件存在后，对迁移做事务内 `ROLLBACK` 演练，再以 5 秒锁等待和 30 秒语句超时执行并提交。迁移前后 `trade_records` 均为 4486 条；新增 `account_id`、`pnl_source` 和 `reviewed_income_attribution`，没有历史回填。迁移后 4486 条记录的 `account_id` 和 `pnl_source` 仍为空，执行事实的成交、收入、游标表仍均为 0 条。交易容器保持运行。

历史记录分布为 HRS 1525、MTPCS 原版 1090、激进版 983、新币做空 888；网格在此表中没有记录。全部 4486 条仍缺账户标签。用户对当前共用账户的确认足以配置新记录的统一标签，但不宜自动推断 2026 年 5 月以来每笔历史记录的账户归属；历史回填须结合订单与交易所事实逐笔复核。

在不写数据库的生产探针中，HRS 容器以统一账户客户端查询最近一天的 `REALIZED_PNL`，返回 13 条、分页完成且无采集错误。当前生产镜像的客户端初始化日志会输出部分遮盖的 API key，且采集 CLI 未关闭客户端；因此旧镜像不应继续用于批量探针或回填。上述两处已在独立发布代码中修复，待部署后再做受控采集。生产容器目前未设置 `TRADE_ACCOUNT_ID`，HRS 未开启 `EXECUTION_FACTS_DB_WRITE`；这些事实也说明此刻不能把账本报告当作真实净收益。

上线前先执行 `database/postgres/migrations/2026-10-30-account-and-income-attribution.sql`，并为实际运行的交易策略与采集任务设置同一个真实 `TRADE_ACCOUNT_ID`。HRS 执行端现在只有在逐笔 income、成交和同账户订单归属均可证明时，才接受交易所已实现盈亏；否则继续使用理论估算。新生成的 `PNL_SUMMARY.pnl_source` 分别标识 `exchange_order_matched`、`theoretical_estimate` 或 `unknown`；历史记录保持 `NULL`，不猜测来源。订单匹配也不等于唯一持仓生命周期核实。HRS 月分配仍只采用 `REALIZED_PNL`，未自动采用本报告净额。
