# ETHUSDT 独立子账户订单网格执行器

本执行器读取 V2.5.5 网格信号，使用独立 PM 或经典 USDⓈ-M 子账户的 ETHUSDT 永续合约普通订单执行**单向净仓中性网格**。它不调用币安内置网格模块，不改变市场状态、网格参数和重建规则。Compose `grid-executor` profile 默认不启动，`GRID_EXECUTION_MODE` 默认 `off`。PM 不提供本项目可用的测试网路径，本执行器只接受 `off` 或 `live`。

## 2026-11-11 的 PM 发布记录

- 网格信号代码已通过专用 `grid-pm-oneway` CI 定向发布；[运行记录](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/43000314975) 成功，生产 pin 为 `4faafd876832e6b87dd7265a37a6fe203eb3d019`。只有 `grid-strategy` 容器身份改变，执行账本已迁移至 schema 5；执行器容器不存在。
- 子账号 PM 已切至**单向持仓**，ETHUSDT 为 `CROSSED`、5 倍杠杆；仓位、普通挂单和条件单均为空。币安对 10 倍请求返回 `-4421`；普通用户新子账号有 5 倍上限，故经用户确认按 5 倍推进。
- 300 USDT 仍位于该子账号现货钱包；PM `accountEquity=0`、`totalAvailableBalance=0`。现有 API 的 `permitsUniversalTransfer=false`，尚未提交划转或订单。
- 服务器私密风控配置：最大保证金 240 USDT、最大名义本金 2400 USDT、单日亏损 15 USDT、最大回撤 30 USDT、最多 7 笔挂单、账户杠杆 5 倍；`GRID_EXECUTION_MODE=off`。在 5 倍和 240 USDT 保证金上限下，实际名义敞口还受约 1200 USDT 的更紧约束。

当时的账户资金不足以开网格。执行器要求单向持仓 `dualSidePosition=false`，且 PM 有足额可用抵押权益；模式不符或资金不足时锁定新增敞口。执行器不会自动切换持仓模式、改变杠杆、划转现货资金或补足限额。

## 本地验证

- 合并最新主线后的网格、共享、定向发布、相关策略与归因回归：`1001 passed, 1 skipped`（2026-11-11）。
- 模拟交易所周期覆盖建网格、成交与相邻格配对、暂停、退出及自动重建。PM 契约测试覆盖 `/papi` 路由、单向模式门禁、空仓 `positionRisk=[]`、PM 权益字段、条件单取消回执及按时间窗口回补成交。
- 本地模拟和只读 API 核验不能证明真实写接口已通过。

## 实盘启用前的账号与参数

专用子账号仅运行这一套 ETHUSDT 网格；不得存在无法归属的 ETHUSDT 普通单、条件单或仓位。账号已切换为单向持仓，ETHUSDT 保持 `CROSSED` 且杠杆与 `GRID_EXPECTED_LEVERAGE=5` 一致。300 USDT 当前仍在现货钱包，须先转入同一子账号的 PM 钱包并只读确认 `accountEquity` 与 `totalAvailableBalance` 非零；这不是转入普通 U 本位合约钱包。切换模式和资金划转不由执行器代做。

生产配置需显式提供 `GRID_SUBACCOUNT_API_KEY`、`GRID_SUBACCOUNT_API_SECRET`、`GRID_MAX_MARGIN_USDT`、`GRID_MAX_NOTIONAL_USDT`、`GRID_MAX_DAILY_LOSS_USDT`、`GRID_MAX_DRAWDOWN_USDT`、`GRID_MAX_OPEN_ORDERS`、`GRID_EXPECTED_LEVERAGE`；所有资金与亏损限额必须是独立确定的正值，不能从策略建议保证金推导。密钥仅经服务器私有环境注入，不写入代码、日志或文档。

`GRID_EXECUTOR_DEPLOY_ENABLED` 保持 `false`，直到资金和限额均确认，且有人值守的小额实盘验收通过。已发布的 `executor.json` 只保存默认关闭的 Compose profile，不创建执行器容器；后续启用须另行受控发布。验收应记录脱敏订单事实，验证 PM `GTX` 挂单、真实成交配对、PM `STOP_MARKET closePosition` 保护、撤单竞争、`BOTH/reduceOnly` 平仓、PM 用户流和 REST 对账、重连后的成交补录。任一写请求结果未知时，按客户端订单号查询，不能直接重发。

## 自动运行门禁

确认目标镜像与 SQLite schema 版本、子账号身份、账户模式、PM 可用权益、风险硬限额和完整小额验收结果后才可启用自动运行。风险锁定后需人工核实订单、仓位、保护及权益事实，并记录解锁原因；后续震荡信号不会自动解除 `LOCKED`。不得靠删账本、改游标或重启进程绕过锁定。

本项目知识库记录过两类直接相关事故：PM `positionRisk` 可能混入其他策略仓位，故账户数据不得直接等同于本策略所有权；V2.5.5 的“张”与 ETH 单位曾造成 100 倍可行性误判，交易数量必须统一按 ETH 和交易所实时过滤器校验。

## 子账户从 PM 切到经典合约

主账户模式与网格子账户独立配置。保留逻辑子账户名 `GRID_ACCOUNT_ID=grid_pm_ethusdt_01`，设置 `GRID_SUBACCOUNT_MODE=classic`。先保留 PM 执行、通知和资金费账本的备份，由账户持有人手动清仓并核对普通单、Algo 单与仓位，再用新的 `GRID_EXECUTION_DB` 运行现有 SQLite 迁移。信号生产者、执行器、通知器、投影器及看板采集器必须采用同一执行账本路径。经典通知库默认随新执行账本命名，也可通过 `GRID_NOTIFICATION_DB` 指定新路径。经典执行器初次绑定 USDT 钱包的 `accountAlias` 后，只消费新决策。

| 账本分段 | PostgreSQL account_id | 已成交订单的 pnl_source | 看板 account_scope |
|---|---|---|---|
| PM 历史 | grid_pm_ethusdt_01 | PM_USER_TRADES | grid_subaccount_pm |
| 经典起点及以后 | grid_pm_ethusdt_01:classic | FAPI_USER_TRADES | grid_subaccount_classic |

订单和成交关联均须保留上述 account_id，两个分段中的轮次号或客户端单号可能重复。跨切换报表先分别按各段的订单与逐笔成交、手续费和资金费核对，再汇总盈亏。看板的阶段和累计值从当前账本第一轮计算，划转、期初资金均不计入网格收益。

生产版本基线为执行器 `fc8fd997`、实时监听 `c6315a04`、信号与通知 `ac33f8d6`、看板采集 `af8ee812` 和 PostgreSQL 投影 `ab38fbc7`。复制运行实例实际的 `GRID_TRAILING_ENABLED` 和限额，新建实例默认关闭追踪。完整报告链使用 `grid-observers`（通知与投影）和 `grid-dashboard`（只读快照）两个可选 Compose profile。旧独立容器与新服务同名，交接须按新审查的发布方案完成。

投影启动前核查现有 `trading.trade_records.algo_id` 以及 `2026-11-12-grid-pm-order-projection.sql` 中的两列及唯一索引，执行数据库权限审计。迁移由迁移角色操作，投影运行角色为 `trading_user`。既有 PM 限定发布脚本固定旧账本、镜像或环境，不能用于经典切换。


### 经典网格的保证金与回撤结转

保留冻结期间的独立 PM 执行库备份、签名 PM 空仓钱包、签名经典空仓入金快照、主账户对该子账户的双向划转查询和子账户保证金转现货回执。核对同一 UID、划转编号、币种、金额、方向、日期和切点。旧账本须为 FLAT 且无未结轮次、订单、保护或风险锁。将材料打包为只读的摘要文件集，单独核准 manifest 的 SHA256。

在切换的北京时间当天，为新的经典执行库和通知库绑定真实钱包身份，保持执行器冻结。以候选网格镜像及同一子账户的经典只读客户端运行下列命令。提供该子账户的 GRID_ACCOUNT_ID、新 GRID_EXECUTION_DB、与实盘相同的 GRID_EXECUTION_LOCK，以及经核对的证据目录。

```sh
python -m strategies.grid.execution.classic_continuity --manifest /evidence/manifest.json --sha256 "$GRID_CLASSIC_RISK_CONTINUITY_SHA256"
```

导入程序查询真实权益、资金流水、挂单、仓位和切点后的 ETHUSDT 委托与成交。首次执行要求经典账本没有任何执行或权益历史。通过后仅在该账本登记结转依据。日内基线与历史峰值均按旧绝对损失和旧损失比例中较严格的一项换算，日内风险按原北京时间规则换日，历史峰值跨日保留。新权益检查点由执行器在实际取样时生成。将同一摘要固定在执行器的 GRID_CLASSIC_RISK_CONTINUITY_SHA256 环境变量中。切换当日的两笔指定划转必须与全部实际划转逐条相符，后续新增、缺失或被修改的划转仍会锁定新增敞口。
