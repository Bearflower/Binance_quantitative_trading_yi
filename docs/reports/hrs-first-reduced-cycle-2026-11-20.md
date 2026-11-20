# HRS 恢复缩量试跑后的首轮实盘核对

北京时间 2026-11-20。观察自 09:01 恢复权益门禁起。HRS 每小时第 21 分钟扫描，首轮于 09:21:00 开始，09:21:43.831 完成，决策轮次 `8c515a8e36804e0f8823143ba5e8b34a`，运行版本 `214835f2`。轮次有 7 个标准评分和 27 个 LV-RM 评分，最终生产执行批次为空。

## 信号、预留与币安回执

| 信号 | 实盘资格 | 到达环节 |
| --- | --- | --- |
| `5847` MMTUSDT 做空 6.55 | 标准实盘候选，评分 `should_entry=true` | 建预留前的组合级 12 小时熔断 |
| `5848` MMTUSDT 做空 7.23 | 半 EMM 影子评分 | 同轮组合预过滤 |
| `5849`、`5850` EVAAUSDT 做空 7.15 | EMM、半 EMM 影子评分 | 同轮组合预过滤 |

HRS 读取的 08:00 等权指数快照 `2881` 于 08:03:00 入库，覆盖 EVAA、LIT、MMT、UNI。1 小时涨幅 `+0.04178555%`，12 小时涨幅 `+2.65691692%`。轮次实际配置启用 12 小时规则，新增空单阈值为 `+2%`。使用该版本 `CircuitBreaker.combined_blocked` 独立复算，四条做空记录均在生产与影子分流前被过滤。该信号未进入权益门禁复核、资金预算、预留及提交授权。现行 HRS 规则明确使用上一整点指数。09:00 的指数已于 09:03 入库，12 小时涨幅为 `+1.65928753%`，按该口径未参与此轮决策。[现行 T-1 规则](https://github.com/Bearflower/Binance_quantitative_trading_yi/blob/214835f26d3a7e2d5fbaccbb2cd63d99ff455042/docs/requirements/market_circuit_breaker/%E5%8D%95%E8%BE%B9%E8%A1%8C%E6%83%85%E7%A9%BA%E5%A4%B4%E7%86%94%E6%96%AD%E9%9C%80%E6%B1%82%E6%96%87%E6%A1%A3.md#L281-L285)

09:27:50 双路签名账户读回均无这两币的实仓，币安 PM `allOrders`、`userTrades` 在 09:01 后对 MMTUSDT 和 EVAAUSDT 均返回空数组。数据库既无 HRS 新预留，也无不可变 `submitting` 记录或后续订单。该回合无成交及对应的新保护单。

## 需要修复的记录缺陷

原过滤先写入信号，再直接丢弃被熔断的方向，导致四个原始信号行停留在 `status=triggered` 且 `resolution` 为空。单靠该字段无法区分正常风控拒绝和提交异常。修复把这条路径按独立 `signal_id` 写为 `resolved / pool_circuit_breaker`，并保存方向、来源、影子标记、指数时点及两组指数。回归使用这一轮的真实指数，检验标准信号和影子评分均被准确结案，另一方向仍经过现有执行路径。

**09:29:32 发布完成。** 活动版本 `476da205`，仅替换 HRS 容器。143 项回归含 PostgreSQL，CI 在实际线上镜像用原 MMT 指数复放，确认记录拒绝并零执行调用。已有历史补录收据、生产配置、权限与政策快照核对通过。生产 HRS `ready=true`、`allowed=true`，签名持仓核验为空。[限定 CI](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44653736252)

## 后续验收

- **首笔实盘。** AI Tuner 每分钟继续采集真实订单，09:27 核查近 10 分钟有 10 个采集批次。下一笔获准信号须串起不可变预留、`submitting`、币安签名 `clientOrderId` 和订单编号，成交后再用逐笔数量、签名持仓、全仓止损及按入场计划的止盈核对。
- **完整滚动 30 天。** 最新投影仍为 `inception_segment`，`qualified_30_day=false`。权益检查点继续积累，最早候选验收为 **12 月 18 日 11:59:43**，须连续覆盖并产生 `window_label=rolling_30_day` 及 `qualified_30_day=true`。[权益历史续验](hrs-equity-history-recovery-2026-11-20.md#后续验收)
