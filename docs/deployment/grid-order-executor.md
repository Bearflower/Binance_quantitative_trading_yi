# ETHUSDT PM 订单网格执行器：验证与启用门禁

本执行器读取 V2.5.5 网格信号，以独立统一账户 PM 子账号的 USDT 本位 ETHUSDT 永续合约普通订单执行**单向净仓中性网格**。它不调用币安内置网格模块，不改变市场状态、网格参数和重建规则。Compose `grid-executor` profile 默认不启动，`GRID_EXECUTION_MODE` 默认 `off`。PM 不提供本项目可用的测试网路径，本执行器只接受 `off` 或 `live`。

## 当前生产子账号只读核验（2026-11-10）

- API 具有 PM 交易权限；普通合约私有 `/fapi` 返回 `-2015`，PM 私有 `/papi` 可用。
- ETHUSDT 当前为**双向持仓**、`CROSSED`、5 倍杠杆；PM 仓位、普通挂单和条件单均为空。
- 300 USDT 位于现货钱包；PM `accountEquity=0`、`totalAvailableBalance=0`。
- 未提交交易、撤单、划转或账户配置请求。

因此当前账户不能开网格。执行器要求单向持仓 `dualSidePosition=false`，且 PM 有足额可用抵押权益；模式不符或资金不足时锁定新增敞口。执行器不会自动切换持仓模式、改变杠杆、划转现货资金或补足限额。

## 本地验证

- `python3 -m pytest -q tests/test_grid_execution`：104 passed（2026-11-10 PM 适配阶段）。
- 模拟交易所周期覆盖建网格、成交与相邻格配对、暂停、退出及自动重建。PM 契约测试覆盖 `/papi` 路由、单向模式门禁、空仓 `positionRisk=[]`、PM 权益字段、条件单取消回执及按时间窗口回补成交。
- 本地模拟和只读 API 核验不能证明真实写接口已通过。

## 实盘启用前的账号与参数

专用子账号仅运行这一套 ETHUSDT 网格；不得存在无法归属的 ETHUSDT 普通单、条件单或仓位。账号需切换为单向持仓，ETHUSDT 保持 `CROSSED` 且杠杆与 `GRID_EXPECTED_LEVERAGE` 一致。300 USDT 当前仍在现货钱包，须在单独操作后确认 PM 的 `accountEquity` 与 `totalAvailableBalance` 非零。切换模式和资金划转不由执行器代做。

生产配置需显式提供 `GRID_SUBACCOUNT_API_KEY`、`GRID_SUBACCOUNT_API_SECRET`、`GRID_MAX_MARGIN_USDT`、`GRID_MAX_NOTIONAL_USDT`、`GRID_MAX_DAILY_LOSS_USDT`、`GRID_MAX_DRAWDOWN_USDT`、`GRID_MAX_OPEN_ORDERS`、`GRID_EXPECTED_LEVERAGE`；所有资金与亏损限额必须是独立确定的正值，不能从策略建议保证金推导。密钥仅经服务器私有环境注入，不写入代码、日志或文档。

`GRID_EXECUTOR_DEPLOY_ENABLED` 保持 `false`，直到账号模式、资金和限额均确认，且有人值守的小额实盘验收通过。验收应记录脱敏订单事实，验证 PM `GTX` 挂单、真实成交配对、PM `STOP_MARKET closePosition` 保护、撤单竞争、`BOTH/reduceOnly` 平仓、PM 用户流和 REST 对账、重连后的成交补录。任一写请求结果未知时，按客户端订单号查询，不能直接重发。

## 自动运行门禁

确认目标镜像与 SQLite schema 版本、子账号身份、账户模式、PM 可用权益、风险硬限额和完整小额验收结果后才可启用自动运行。风险锁定后需人工核实订单、仓位、保护及权益事实，并记录解锁原因；后续震荡信号不会自动解除 `LOCKED`。不得靠删账本、改游标或重启进程绕过锁定。

本项目知识库记录过两类直接相关事故：PM `positionRisk` 可能混入其他策略仓位，故账户数据不得直接等同于本策略所有权；V2.5.5 的“张”与 ETH 单位曾造成 100 倍可行性误判，交易数量必须统一按 ETH 和交易所实时过滤器校验。
