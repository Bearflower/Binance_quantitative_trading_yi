# ETHUSDT 订单网格执行器：验证与启用门禁

本执行器读取现有 V2.5.5 网格信号，使用独立子账号的普通 USDⓈ-M 合约订单执行 ETHUSDT 网格。它不调用币安内置网格模块，也不修改现有信号、区间、格数或建议保证金公式。`grid-executor` 属于 `grid-executor` Compose profile，且 `GRID_EXECUTION_MODE` 默认 `off`。

## 本地验证（2026-11-10）

- `python3 -m pytest -q tests/test_grid_execution tests/test_strategies/test_grid_kline.py tests/test_strategies/test_grid_capital_linkage.py tests/integration/test_grid_strategy.py`：204 passed。
- 模拟交易所完整周期覆盖建网格、BUY 成交、相邻价位 SELL、预警暂停、强趋势退出、清零及新信号自动重建。
- 故障用例覆盖丢失下单响应、平仓 ACK 后精确查询、重启交易补录、保护单失败、静默 WebSocket 对账、积压信号优先退出、测试网普通接口路由、账户外订单及 REST 失败锁定。
- `git diff --check`：通过。Compose 与工作流 YAML 使用 PyYAML 成功解析。本机 Docker Compose 插件不可用，因此未能运行 `docker compose --profile grid-executor config`。

## 子账号准备

专用子账号只承载这一套 ETHUSDT USDⓈ-M 网格。启用前需确认子账号 API、IP 白名单、可交易权限、单向持仓、单资产保证金、ETHUSDT 逐仓及预期杠杆。账户有任何外部 ETHUSDT 订单、条件单或无法归属的仓位时，执行器锁定开仓。密钥仅通过服务器私有 `.env` 注入，不写入代码、日志或文档。

显式配置 `GRID_EXECUTION_MODE`、`GRID_SUBACCOUNT_API_KEY`、`GRID_SUBACCOUNT_API_SECRET`、`GRID_MAX_MARGIN_USDT`、`GRID_MAX_NOTIONAL_USDT`、`GRID_MAX_DAILY_LOSS_USDT`、`GRID_MAX_DRAWDOWN_USDT`、`GRID_MAX_OPEN_ORDERS`、`GRID_EXPECTED_LEVERAGE`。所有资金与亏损上限必须为正值；不能从策略建议保证金推导。测试网额外配置已验证的 `GRID_TESTNET_WS_BASE`。`GRID_EXECUTOR_DEPLOY_ENABLED` 保持 `false`，直至完成测试网验收及另行授权实盘启用。

## 测试网验收（待子账号测试网密钥）

在独立测试网账户上记录脱敏的请求时间和订单 ID，并验证以下事实：`exchangeInfo` 过滤器、单向/逐仓/杠杆返回值、`/fapi/v1/order` 的 GTX 与 `reduceOnly` MARKET `RESULT` 状态、`/fapi/v1/algoOrder` 保护单可见性、listenKey 流与 REST 一致性、撤单和成交竞争、重连后精确交易补录。任何响应形态与适配器预期不符都停止开仓并修正实现，不用生产账号“试一下”。

## 实盘启用门禁（待单独授权）

记录实际子账号身份、资金规模和上述硬限额；完成测试网验收；确认目标镜像包含本提交和 SQLite schema 版本；在开启 profile 前做只读账户、ETHUSDT 普通订单、条件单及仓位对账。随后才可进行有人值守的小额验证。设计与实现通过不等于授权实盘下单。风险锁定后需人工核实订单、持仓、保护和权益事实，并记录解锁原因；不能由后续震荡信号自动解除。

执行日志中 `LOCKED`、`EXITING`、心跳过期或风险证据未知都表示不应继续开仓。不要通过直接修改 SQLite 游标、删除账本或重启进程来绕过锁定。
