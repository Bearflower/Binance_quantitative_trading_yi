# HRS 资金费证据口径

已知结算时刻到达与收入接口分页成功，均不意味着该次资金费已公布。上线门禁须核对独立预测时表、实际持仓归属、实际已公布费率/标记价以及权威收入；缺口期间暂停新增风险，继续退出维护。

历史启用前的开账可用完整分页的已公布资金费历史和真实收入/成交回溯核对，但只在本次取得证据时确认，不声称当年的交易决策已经知道这些资料。启用后的连续性采用实际 nextFundingTime 观察，不能将收入行反推成预测时表。[币安资金费历史与信息接口](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)

有限的结算后轮询间隔只在完整市场接口证据和受支持的加密永续合约规则下衔接：最大 120 秒，小于当前整小时最小间隔；不据此推算后续每 8 小时结算。官方接口将 fundingIntervalHours 定义为整数，官方说明存在向每小时结算的调整，需持续使用真实下一结算时刻。[接口类型](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)、[结算调整说明](https://www.binance.com/en/support/faq/detail/360033525031)

实际资金费交易可能与标称时刻相差 15 秒。这个范围仅用于结合真实已签名成交、稳定归属和实际收入核对，不用于把未知持仓或未知费用填成零；窗口内变仓且不能唯一确定时继续阻断。[币安资金费说明](https://www.binance.com/en/support/faq/detail/360033525031)

来源查询亦有成本：PM income 权重 30、userTrades 权重 5，PM IP 限额按官方当前文档为每分钟 6,000。运行后应核对响应权重与实际取数耗时，不能用延长旧净值有效期掩盖取数延迟。[收入接口](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/account)、[成交接口](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/trade)、[限额](https://developers.binance.com/en/docs/products/derivatives-trading-portfolio-margin/general-info)
