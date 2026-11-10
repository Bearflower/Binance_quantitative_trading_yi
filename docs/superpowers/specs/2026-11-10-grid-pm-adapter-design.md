# ETHUSDT 订单网格：PM 单向净仓修订设计

日期：2026-11-10。状态：用户已选择 PM 单向净仓。本文修订 [原设计](2026-11-09-grid-order-execution-design.md)的账户及接口假设，冲突处以本文为准；V2.5.5 的信号、区间、格数、每格数量、暂停与自动重建规则不变。

## 已核实事实

该子账号 API 具有统一账户 PM 交易权限；普通合约私有 `/fapi` 返回 `-2015`，PM 私有 `/papi` 可用。ETHUSDT 当前双向持仓、全仓、5 倍杠杆；无仓位、普通挂单和条件单。现货钱包有 300 USDT，PM `accountEquity` 与 `totalAvailableBalance` 为 0。只读核验没有发送交易、撤单、划转或账户配置请求。

币安官方说明，内置中性合约网格初始空仓，上行首笔卖出形成空仓、下行首笔买入形成多仓；内置机器人只支持单向持仓。用户选择 **PM 单向净仓**，保留原执行器的 `BOTH` 净仓配对模式。这要求上线前账户确实已切换为单向；执行器只验证模式，绝不自行切换。

## 实现选择

1. **PM 单向净仓（选定）**：现价下方买、上方卖；成交后按相邻格价补反向单；净仓一份账本。与币安内置中性网格最接近，保留现有配对逻辑。
2. PM 双向双腿：两腿独立开平与风险占用，会改变交易行为，用户不选择。

专用 `GridExchange` 使用 PM 客户端和显式私有 `/papi` 路径：UM 订单、精确查单/撤单、UM algo 保护单、PM 账户权益、UM 净仓、成交、收入与 PM listenKey；公开 ETHUSDT 行情和 `exchangeInfo` 继续走公开合约接口。2026-04-28 后失效的旧 PM 条件单路径不使用。PM `positionRisk` 空仓可返回 `[]`：仅在成功响应、且确认账户单向模式时转换为本执行器内部 `BOTH/0` 快照；API 异常不能视为空仓。若返回其它 ETHUSDT 仓位形式、未知订单或不匹配净仓，锁定。

PM 全仓替代原设计的逐仓。启动核验账户正常、单向持仓、ETHUSDT 全仓与预期杠杆；其它交易对有未平仓仓位时拒绝运行。专用子账号内不得有其它策略或人工 ETHUSDT 仓位/订单。引擎不自动修改杠杆、保证金类型、账户模式或划转资金。普通平仓仍使用 `BOTH` + `reduceOnly`；保护仍用 `STOP_MARKET closePosition=true`，并按 PM algo 接口的回执查询确认。项目既有排障说明 PM `STOP` 限价条件单配 `closePosition` 可能报 `-4136`，因此不要把它替代 `STOP_MARKET`。

权益口径从普通合约的 `totalMarginBalance`/`availableBalance` 改成 PM 的 `accountEquity`/`totalAvailableBalance`，结合 PM 收入和划转事实验证日内基线。收入分页无法确认完整、权益缺失或资金变化无法解释时锁定。知识库曾记录 PM `positionRisk` 包含其他策略仓位，故不能把账户全量仓位默认为本策略所有；另有 V2.5.5 “1 张 = 0.01 ETH”教训，订单数量统一使用 ETH 单位。

继续沿用默认 `off`、独立密钥、持久账本、风险硬限额、精确成交补录、保护失败退出和自动重建。需新增 PM 请求/响应契约测试、空仓与账户模式门禁测试，以及完整模拟交易周期测试。旧的 205 项本地测试只证明普通合约路径，不能当作 PM 验收。只读生产核验无法证明写接口可用；小额实盘验收需单独确认。

当前账号仍为双向持仓且 PM 可用权益为 0，必须拒绝开仓。300 USDT 在现货钱包；账户模式切换、抵押资金划转、资金与亏损硬限额、实盘验收和自动运行启用均属后续门禁。密钥仅通过服务器私有环境注入，不进入代码、日志或文档。

## 官方依据

- [币安：中性网格首次建仓机制](https://www.binance.com/en/support/faq/detail/904e47602a3941b99e960a31e152a986)
- [币安：内置合约网格只支持单向持仓](https://www.binance.com/en/support/faq/detail/408ab63b852e47748a9554501669bce5)
- [币安：PM UM 普通订单及条件单](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/trade)
- [币安：PM 账户接口](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/account)
