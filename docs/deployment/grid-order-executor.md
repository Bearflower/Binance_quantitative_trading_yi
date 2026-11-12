# ETHUSDT PM 订单网格执行器：验证与启用门禁

本执行器读取 V2.5.5 网格信号，以独立统一账户 PM 子账号的 USDT 本位 ETHUSDT 永续合约普通订单执行**单向净仓中性网格**。它不调用币安内置网格模块，不改变市场状态、网格参数和重建规则。Compose `grid-executor` profile 默认不启动，`GRID_EXECUTION_MODE` 默认 `off`。PM 不提供本项目可用的测试网路径，本执行器只接受 `off` 或 `live`。

## 当前生产状态（2026-11-12）

- 网格信号代码已通过专用 `grid-pm-oneway` CI 定向发布；[运行记录](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/43000314975) 成功，生产 pin 为 `4faafd876832e6b87dd7265a37a6fe203eb3d019`。该次发布仅改变 `grid-strategy` 容器身份，执行账本迁移至 schema 5；执行器在后续独立激活中创建。
- 子账号 PM 已切至**单向持仓**，ETHUSDT 为 `CROSSED`、5 倍杠杆；仓位、普通挂单和条件单均为空。币安对 10 倍请求返回 `-4421`；普通用户新子账号有 5 倍上限，故经用户确认按 5 倍推进。
- 300 USDT 已从子账号现货钱包划入 PM；PM 原币钱包余额为 300 USDT。实盘委托、撤单、成交、保护单、reduceOnly 平仓、用户流与 REST 成交查询已经验收，最终为空仓、无挂单。
- 服务器私密风控配置：最大保证金 240 USDT、最大名义本金 2400 USDT、单日亏损 15 USDT、最大回撤 30 USDT、最多 7 笔挂单、账户杠杆 5 倍。在 5 倍和 240 USDT 保证金上限下，实际名义敞口还受约 1200 USDT 的更紧约束。
- 独立执行器已用修复镜像 `grid-executor:814af508` 启用，实际容器环境 `GRID_EXECUTION_MODE=live`。容器健康、用户流就绪、风险为 `ALLOW`、账本为 `FLAT`。服务器 `.secrets/grid-limits.env` 的默认模式仍为 `off`，以避免普通 Compose 操作误启用；本次受控激活覆盖文件和证据保存在 `.releases/grid-executor-activation-20261112/814af50855623dd93d8abdd29cef0f566d1011c8/`。
- 2026-11-12 发现执行器最初未注入 `FEISHU_WEBHOOK_GRID`，已有锁定告警因此不能投递。核对原网格信号和实时提醒容器使用同一地址后，将该地址写入服务器仅 root 可读的 `.secrets/grid-notifications.env`，按完成状态的当前生产 pin 重新创建执行器。配置和结果在 `.releases/grid-executor-webhook-20261112/814af50855623dd93d8abdd29cef0f566d1011c8/`；复核执行器容器健康、Webhook 与原网格相同、风险 `ALLOW`、账本 `FLAT`。成交与持仓通知功能尚未实现，此项只修复已有锁定告警的投递配置。
- 独立成交与持仓报告随后在同一飞书地址启用，见 [2026-11-12 执行报告发布记录](grid-execution-feishu-report-2026-11-12.md)。它不改变原 V2.5.5 信号文案或发送节奏；仅在本执行器实际成交经 PM 核实后另发一条。

执行器要求单向持仓 `dualSidePosition=false`，且 PM 有足额可用抵押权益；模式不符或资金不足时锁定新增敞口。执行器不会自动切换持仓模式、改变杠杆、划转现货资金或补足限额。V2.5.5 原计划超出硬限额时跳过该决策；已有网格先暂停新增委托。当前市场为强趋势退出信号，执行器保持空仓。

## 本地验证

- 合并最新主线后的网格、共享、定向发布、相关策略与归因回归：`1001 passed, 1 skipped`（2026-11-11）。
- 模拟交易所周期覆盖建网格、成交与相邻格配对、暂停、退出及自动重建。PM 契约测试覆盖 `/papi` 路由、单向模式门禁、空仓 `positionRisk=[]`、PM 权益字段、条件单取消回执及按时间窗口回补成交。
- 2026-11-12 的超限处理修复回归：`110 passed`；实盘写接口验收见下节。

## 实盘启用前的账号与参数

专用子账号仅运行这一套 ETHUSDT 网格；不得存在无法归属的 ETHUSDT 普通单、条件单或仓位。账号已切换为单向持仓，ETHUSDT 保持 `CROSSED` 且杠杆与 `GRID_EXPECTED_LEVERAGE=5` 一致。300 USDT 已转入同一子账号的 PM 钱包；这不是普通 U 本位合约钱包。切换模式和资金划转不由执行器代做。

生产配置需显式提供 `GRID_SUBACCOUNT_API_KEY`、`GRID_SUBACCOUNT_API_SECRET`、`GRID_MAX_MARGIN_USDT`、`GRID_MAX_NOTIONAL_USDT`、`GRID_MAX_DAILY_LOSS_USDT`、`GRID_MAX_DRAWDOWN_USDT`、`GRID_MAX_OPEN_ORDERS`、`GRID_EXPECTED_LEVERAGE`；所有资金与亏损限额必须是独立确定的正值，不能从策略建议保证金推导。密钥仅经服务器私有环境注入，不写入代码、日志或文档。

本次验收记录了 PM `GTX` 挂单与撤单；另一笔约 26.79 USDT 的市价买单 `FILLED`，随后 `STOP_MARKET closePosition` 条件单在 PM 挂单列表可见，`BOTH/reduceOnly` 平仓 `FILLED`。用户流收到了开仓和平仓 `TRADE` 事件，REST 查得入场成交。平仓后保护单不再开放，最终无仓位和挂单。真实网格相邻格配对、撤单竞争和断线后的成交补录仍依赖运行时对账及已有模拟测试；不能把这次小额验收表述成这些场景在生产全部发生过。任一写请求结果未知时，按客户端订单号查询，不能直接重发。

## 自动运行门禁

确认目标镜像与 SQLite schema 版本、子账号身份、账户模式、PM 可用权益、风险硬限额和完整小额验收结果后才可启用自动运行。风险锁定后需人工核实订单、仓位、保护及权益事实，并记录解锁原因；后续震荡信号不会自动解除 `LOCKED`。不得靠删账本、改游标或重启进程绕过锁定。

本项目知识库记录过两类直接相关事故：PM `positionRisk` 可能混入其他策略仓位，故账户数据不得直接等同于本策略所有权；V2.5.5 的“张”与 ETH 单位曾造成 100 倍可行性误判，交易数量必须统一按 ETH 和交易所实时过滤器校验。
