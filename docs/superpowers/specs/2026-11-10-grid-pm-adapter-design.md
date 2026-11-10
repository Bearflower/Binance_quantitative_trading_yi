# ETHUSDT 订单网格：PM 双向持仓修订设计

日期：2026-11-10。状态：待用户审阅。本文修订 [原设计](2026-11-09-grid-order-execution-design.md)的账户接口和订单语义；冲突处以本文为准。V2.5.5 的市场状态、区间、格数、每格数量、资本可行性、暂停和重建触发条件继续沿用。

## 核实结果与关键区别

用户指定统一账户 PM 接口，且要求多仓和空仓可同时存在。只读生产核验确认：该 API 有 PM 交易权限；普通合约私有 `/fapi` 返回 `-2015`，PM 私有 `/papi` 可用。ETHUSDT 为双向持仓、全仓、5 倍杠杆，目前无仓位、普通挂单及条件单。现货钱包有 300 USDT，PM `accountEquity` 和 `totalAvailableBalance` 均为 0。没有提交交易、撤单、划转或账户配置请求。

币安官方说明其内置“中性合约网格”初始空仓，上行首笔卖出形成空仓、下行首笔买入形成多仓；其机器人只支持单向持仓。因此中性网格不等于双向持仓。本文选择的是**独立订单执行器的双腿网格**，它允许两腿并存，成交与资金占用会不同于币安内置网格。原执行器的 205 项单向测试不能充当双腿验收。

## 可选方案与裁定

1. PM 单向净仓：最接近币安内置中性网格，改动小；需要切换当前账户持仓模式，不满足用户已选择的多空并存。
2. **PM 双向双腿（选定）**：沿用 V2.5.5 网格价位与信号；每腿独立开仓、配对、保护、退出；按总敞口设风险限额。需要重做订单账本和验证。
3. PM 双向但只交易单腿：不切模式，不能实现多空并存，也无法连续覆盖双向震荡。

## 订单及持仓账本

从现有 `DynamicGridParams` 和 `CapitalFeasibility` 生成格价及每格 ETH 数量，继续按实时 tick、step、最小数量和名义额校验。初始空仓时，现价下方挂 `BUY/LONG` 开多，现价上方挂 `SELL/SHORT` 开空。每笔 `BUY/LONG` 成交在上邻格产生 `SELL/LONG` 平多义务；每笔 `SELL/SHORT` 成交在下邻格产生 `BUY/SHORT` 平空义务。平仓成交释放本腿库存；下一轮开仓单只在格位可用且全局风险允许时恢复。不同腿可以占同一价位；同腿的重复义务不得产生重复订单。

订单意图、逐笔成交、配对及保护记录都持久保存 `position_side`、开/平角色及 epoch。按交易所成交 ID 去重，仅以已确认成交分配配对量。部分成交低于交易所最小量时留作残量，不能反向开仓补齐。平仓单总量不得超过该腿已确认、尚未由其它平仓单占用的库存。双向模式始终显式传 `LONG` 或 `SHORT`，不得传 `BOTH` 或 `reduceOnly`。每次重启或数据流缺口先用 REST 补齐成交，再允许补单。PM 空仓的 `positionRisk=[]` 只有在查询成功且两腿均无仓时才视为空仓；任何不能解释的仓位或外部 ETHUSDT 订单均锁定新增敞口。

## 保护、退出与重建

有 LONG 仓时需有交易所侧 `SELL/LONG STOP_MARKET closePosition=true`，触发价为 `stop_loss_low`；有 SHORT 仓时需有 `BUY/SHORT` 同类保护，触发价为 `stop_loss_high`。两腿保护分别创建、查询及核对；任一腿缺保护，禁止新增敞口并进入退出流程。项目既有 PM 排障记录指出，`STOP` 限价条件单配 `closePosition=true` 会报 `-4136`，因此这里明确使用 `STOP_MARKET`；只读预检仍不能证明写接口接受这些订单，必须在后续小额实盘门禁中确认。

退出时先置 `EXITING`，阻止新单，撤本执行器两腿普通挂单，补录撤单竞争期间的成交；再按各腿交易所确认的仓量分别提交 `SELL/LONG MARKET` 和 `BUY/SHORT MARKET`。双向模式不支持 `reduceOnly` 参数，故每次下单前后精确查询订单和该腿持仓；响应未知时不得盲目重发。两腿都已空仓，且旧普通单、条件单均清零后进入 `FLAT`。暂停时仍保持已有仓位的两腿保护。仅在旧 epoch 完全清零、有效可交易信号、资本可行及风险门禁通过后自动重建。

## PM 接口、风险与上线门禁

私有请求走 `papi.binance.com`：普通单 `/papi/v1/um/order`、挂单 `/papi/v1/um/openOrders`、双腿仓位 `/papi/v1/um/positionRisk`、持仓模式与杠杆 `/papi/v1/um/positionSide/dual` 和 `/papi/v1/um/symbolConfig`、账户 `/papi/v1/account`、成交 `/papi/v1/um/userTrades`、收入 `/papi/v1/um/income`、条件单 `/papi/v1/um/algo/*`、数据流 `/papi/v1/listenKey`。公开行情及交易规则可继续从公开合约接口读取。2026-04-28 后失效的旧 PM 条件单接口不得使用。各接口需请求/响应契约测试，特别覆盖双腿仓位、条件单撤销及数据流续期。

PM 为全仓：按 LONG 与 SHORT 的**总名义敞口**计风险，不以净仓抵消；把所有可成交开仓单纳入最坏情形，再与最大名义额、最大保证金、最大挂单数及每日亏损/回撤硬限额比较。权益使用 PM `accountEquity` 和 `totalAvailableBalance`，日内收入与划转必须形成完整基线；证据不足时锁定。子账号须专用。知识库记录的 PM `positionRisk` 混入其他策略持仓事故要求严格归属过滤；V2.5.5 “1 张 = 0.01 ETH”事故要求交易数量统一使用 ETH 单位。

执行器默认 `off`，不自行改持仓模式、杠杆、保证金类型或划转资金。当前 PM 可用权益为 0，启动必须拒绝开仓。现货 300 USDT 的抵押划转、资金与亏损硬限额、双腿写接口的小额验收及自动运行启用均为后续门禁。密钥只经服务器私有环境注入，不写入代码、日志或文档。

## 官方依据

- [币安：中性网格首次建仓机制](https://www.binance.com/en/support/faq/detail/904e47602a3941b99e960a31e152a986)
- [币安：内置合约网格只支持单向持仓](https://www.binance.com/en/support/faq/detail/408ab63b852e47748a9554501669bce5)
- [币安：PM UM 普通订单及条件单](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/trade)
- [币安：PM 账户接口](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/account)
