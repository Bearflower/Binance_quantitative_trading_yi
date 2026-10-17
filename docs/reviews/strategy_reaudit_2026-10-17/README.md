# 第三轮修复复审（2026-10-17）

范围：本地 HEAD a17a1c296beb42f646059735765b8e084d6a529b 加工作区修改，复验上轮3项（本轮实现编号R7/R8/R9）。仅新增本目录审查材料；未改生产代码、连接真实交易所或数据库、部署或提交订单。文件哈希见 manifest.json。

结论：上轮3个直接根因均已处理；成交分页仍存在一个会静默漏数的右边界缺陷，不能接受采集完整性验收。新币重算和HRS归属在本地复验范围内通过。

## 待修问题：[P1] 满页到达窗口结束毫秒时，不能直接宣布采集完整

位置：[export_execution_facts.py:908](/Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py:908)。

`_resolve_page_outcome()` 将 `new_advance.time_ms >= window_end_ms` 作为独立完成条件，即使本页已经填满。若窗口最后一毫秒还有多笔记录，最大时间已经到边界并不能证明该毫秒已全部取完。

独立复现：窗口 [1,1000]，5笔成交全部 time=1000，page_limit=2。使用真实 exporter 和 BinanceClient.get_user_trades，仅替换底层请求，不发网络请求。模拟时间查询包含结束时间，第一页返回 id=1、2。结果只请求一次、采得2笔，page_complete=True、errors=[]，其余3笔未取回。这个缺陷发生在第一页，与 fromId 是否包含起点的语义无关。

依据：[币安官方 PM Trade API 的 UM Account Trade List 查询参数](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/trade) 将 endTime 定义为包含边界。当前客户端过滤也保留 `time <= window_end_ms`，所以这5笔均应属于当前采集窗口。

影响：缺失成交、手续费及已实现盈亏，且下游无法从完整性标记识别缺口。不是单纯的“无法采全但明确告警”。

现有测试遗漏原因：`test_last_time_equals_window_boundary` 的数据实际上是500、800、999，结束时间1000，没有任何记录恰落右边界。另一项客户端边界测试仅有2笔落在结束毫秒，刚好一页，没有覆盖结束毫秒超过一页。Fake 时间查询使用 `< end_time`，与客户端 `<= end_time` 口径也不一致。

修复建议：明确整个导出器的闭区间或半开区间契约。若沿用当前闭区间，满页到达结束毫秒仍需续传，直到确认结束毫秒已排空；不能只凭最大时间等于结束时间结束。保留原始页是否满页、是否实际遇到越界记录的信息。若改半开区间，则请求、过滤、游标和测试必须一起调整，确保相邻窗口无缺口。

验收至少覆盖：结束毫秒数量为 limit、limit+1、多页；结束毫秒后还有越界记录；采到边界中途失败后恢复；真实接口时间包含边界且参数互斥。income 共用本收尾方法，也应覆盖不能按ID续传时的显式不完整分支。

## 已确认的修复

| 上轮问题 | 本轮证据 |
|---|---|
| R7 fromId 与时间参数混传 | ID续页已去除时间参数；独立请求拦截断言互斥，窗口内部5笔同毫秒成交全部取回。完整性仍受上述右边界缺陷阻塞 |
| R8 新价配旧数量 | 最终价变化后再次调用统一额度/数量计算。独立案例100→120，预算核算2次，数量1→0.833，2倍杠杆的模拟保证金49.98，不超50预算 |
| R9 生产更新污染影子 | INSERT RETURNING id 回填，结果按唯一id更新，超时处理前先落库。独立真实收尾链得到 standard=executed、emm=shadow，各自ID不同 |

R8案例模拟预算函数及订单I/O，真实执行 execute_short 和数量重算路径；R9案例用内存DB按实际SQL的唯一ID谓词更新，真实执行落库、分桶和结果更新方法。这些是离线调用链验证，不是实际PostgreSQL或交易所集成测试。启动建表代码已包含R9新列声明和幂等补齐；未核实线上迁移。

## 测试与材料

定向验证：**90 passed in 0.15s**，退出码0。

```sh
python3 -m pytest strategies/hrs/tests/test_r9_signal_attribution.py strategies/new_coin/tests/test_reprice_budget_r8.py tests/test_shared/test_execution_facts_regression.py strategies/hrs/tests/test_r1_r4_regressions.py strategies/new_coin/tests/test_price_freshness_r5.py tests/test_shared/test_execution_facts_sink.py -q --disable-warnings --tb=short
```

扩大回归：**119 passed, 4 failed, 4 errors in 9.61s**，退出码1，maxfail=8停止。仍是此前HRS夹具缺失circuit_breaker与当前事件循环问题，未据此认定为本轮新增回归，也不能宣称全量通过。

```sh
python3 -m pytest strategies/hrs/tests/ strategies/new_coin/tests/ tests/test_shared/test_order_fill_waiter.py tests/test_strategies/test_time_stop_review.py tests/test_strategies/test_capital_allocation_enforcement.py ai_tuner/allocation/tests/ -q --disable-warnings --tb=short --maxfail=8
python3 docs/reviews/strategy_reaudit_2026-10-17/verify_offline.py
```

独立脚本退出码0，确认R8/R9原症状消失、窗口内部成交续页成立，并断言右边界缺陷仍可复现。见 results.json 和 probe_log.txt；测试完整输出见 targeted_tests.txt、regression_tests.txt。脚本的全部断言通过不等于所有生产功能正确。

本轮不重新验收策略收益有效性、完整净收益账本、权益守恒、多腿执行或部署。income无法续传时的已知不完整边界，也不能等同于收入采集能力完成。
