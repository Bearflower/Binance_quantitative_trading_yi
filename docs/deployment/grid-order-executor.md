# ETHUSDT 订单网格执行器：验证与启用门禁

本执行器读取现有 V2.5.5 网格信号，使用独立子账号的普通 USDⓈ-M 合约订单执行 ETHUSDT 网格。它不调用币安内置网格模块，也不修改现有信号、区间、格数或建议保证金公式。`grid-executor` 属于 `grid-executor` Compose profile，且 `GRID_EXECUTION_MODE` 默认 `off`。

## 本地验证（2026-11-10）

- `python3 -m pytest -q tests/test_grid_execution tests/test_strategies/test_grid_kline.py tests/test_strategies/test_grid_capital_linkage.py tests/integration/test_grid_strategy.py`：205 passed。
- 模拟交易所完整周期覆盖建网格、BUY 成交、相邻价位 SELL、预警暂停、强趋势退出、清零及新信号自动重建。
- 故障用例覆盖丢失下单响应、平仓 ACK 后精确查询、重启交易补录、保护单失败、静默 WebSocket 对账、积压信号优先退出、接口环境路由、账户外订单及 REST 失败锁定。
- `git diff --check`：通过。Compose 与工作流 YAML 使用 PyYAML 成功解析。本机 Docker Compose 插件不可用，因此未能运行 `docker compose --profile grid-executor config`。

## 子账号准备

专用子账号只承载这一套 ETHUSDT USDⓈ-M 网格。启用前需确认子账号 API、IP 白名单、可交易权限、单向持仓、单资产保证金、ETHUSDT 逐仓及预期杠杆。账户有任何外部 ETHUSDT 订单、条件单或无法归属的仓位时，执行器锁定开仓。密钥仅通过服务器私有 `.env` 注入，不写入代码、日志或文档。

生产子账号路径设置 `GRID_EXECUTION_MODE=live`，并显式配置 `GRID_SUBACCOUNT_API_KEY`、`GRID_SUBACCOUNT_API_SECRET`、`GRID_MAX_MARGIN_USDT`、`GRID_MAX_NOTIONAL_USDT`、`GRID_MAX_DAILY_LOSS_USDT`、`GRID_MAX_DRAWDOWN_USDT`、`GRID_MAX_OPEN_ORDERS`、`GRID_EXPECTED_LEVERAGE`。所有资金与亏损上限必须为正值；不能从策略建议保证金推导。`GRID_EXECUTOR_DEPLOY_ENABLED` 保持 `false`，直到子账号只读核验完成，且准备开展有人值守的小额实盘验收。

## 生产子账号验收（待新的子账号 API）

先只读核验生产子账号：确认账号身份、API 权限和服务器 IP 限制；读取 `exchangeInfo`、单向/逐仓/杠杆、余额、ETHUSDT 仓位、普通挂单、条件单及近期成交。任何账户事实、权限或数据响应与预期不符时，不开启执行器。

2026-11-10 首次只读核验：本机及现有交易服务器访问普通 U 本位合约账户接口均返回 `-2015`。服务器访问 API 权限接口成功，显示 `enableReading=true`、`ipRestrict=true`、`enableFutures=false`、`enablePortfolioMarginTrading=true`、`enableWithdrawals=false`。因此当前密钥与执行器要求的普通 U 本位合约账户模式不符；未核实合约余额、仓位和订单，也未提交任何交易请求。后续需先确认使用普通合约子账号，还是重新设计组合保证金适配器。

只读核验通过后，再按单独确认的资金与损失硬限额，进行有人值守的小额实盘验收。逐项记录脱敏的请求时间、订单 ID、交易所回执和账本状态，验证 GTX 挂单、成交配对、保护单可见性、撤单竞争、`reduceOnly` 平仓、用户数据流与 REST 一致性，以及重连后的精确成交补录。任一环节结果不确定时停止新增敞口并按账本对账。

## 自动运行门禁（待子账号和限额）

记录实际子账号身份、资金规模和上述硬限额；确认目标镜像包含本提交和 SQLite schema 版本；完成只读核验及有人值守的小额实盘验收后，才启用自动运行。当前设计和本地测试完成，不代表生产子账号已通过验收。风险锁定后需人工核实订单、持仓、保护和权益事实，并记录解锁原因；不能由后续震荡信号自动解除。

执行日志中 `LOCKED`、`EXITING`、心跳过期或风险证据未知都表示不应继续开仓。不要通过直接修改 SQLite 游标、删除账本或重启进程来绕过锁定。
