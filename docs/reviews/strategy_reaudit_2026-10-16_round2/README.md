# 策略修复第二轮复审（2026-10-16）

审查对象：本地 HEAD `a17a1c296beb42f646059735765b8e084d6a529b` 加工作区未提交修改。文件哈希见 manifest.json。只新增本目录审查材料；未修改生产代码、连接交易所或数据库，也未部署。

结论：上一轮修复确实有进展，但尚不能接受“R1–R6 全部闭环”。本轮确认 3 个待修问题。155 项定向测试通过，说明已有测试覆盖的行为成立；独立组合案例发现其未覆盖的调用链问题。

## 1. [P1] 成交续页同时发送 fromId 与时间参数，违反真实接口约束

位置：[导出器](/Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py:439)、[API 参数构造](/Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py:1660)。

`export_fills()` 每次调用都传入 start_time、end_time。第一页填满后，下一页增加 from_id，API 包装层把三者原样发送。币安 PM UM 文档明确禁止 fromId 与 startTime/endTime 同时发送，因此本地 Fake API 的通过结果不能证明真实成交分页可用。

依据：[币安官方 Portfolio Margin Trade API，UM Account Trade List 查询参数](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/trade)。本轮查看的页面 UM 部分对应第 11899 行，明确列出互斥约束；不是套用 CM 接口说明。

独立复现：调用真实 exporter 和真实 BinanceClient.get_user_trades，仅替换底层 `_request`，按上述官方约束拒绝参数组合。合成同毫秒记录，limit=2，第二次请求为 startTime=1000、endTime=2000、fromId=2。导出在 2 条后中断，page_complete=False。没有声称已用真实凭据请求交易所。

影响：超过一页的成交窗口无法完成采集，恢复游标后仍可能遇到同一参数组合，阻塞完整成交和费用账本。

修复及验收：按接口约束分开时间定位与 ID 续传；ID 续传时在本地维护窗口边界，并验证 fromId 起点语义，避免重复页或越界。让 Fake API 对非法参数组合立即报错，再覆盖同毫秒跨页、窗口右边界及保存游标后的恢复路径。现有 Fake API 同时接受两种过滤条件，并使用 id > from_id，不能用它替代真实接口契约。

## 2. [P1] 新币执行前重取报价后，没有重新核算数量和保证金限额

位置：[额度和数量计算](/Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py:469)、[重取价后沿用数量](/Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py:514)。

执行顺序是：按旧 current_price 核算额度 → 格式化数量 → 设置杠杆 → 报价超龄时重取 → 用新价格和旧数量下单。entry_guard 只重新判断报价与反弹参照，没有重新计算仓位额度。

独立复现：真实 execute_short，模拟旧价100，预算函数返回数量1；执行准备超时后新价120，仍以数量1、价格120进入下单函数，预算函数仅调用一次。下单函数为 AsyncMock，未发真实订单。

影响：例如杠杆2、可用保证金50时，旧价对应保证金50，新价对应60，可能越过原已校验额度。价格从100涨至120仍可以低于固定反弹参照，因此反弹闸门不能替代额度复核。这里的50/60是合成价格下的算术结果，不是实盘成交或滑点估计。

修复及验收：最终有效报价改变后，重新核算可用额度、数量、精度和最小下单条件，并在下单前校验最终数量。增加“重取价上涨但未越过反弹参照、原保证金额度已用满”的组合测试，断言缩仓或拒开；下跌场景也检查最小名义金额等边界。

## 3. [P2] 同币同方向生产信号会把影子信号记录一起标为 executed

位置：[先落库再分桶](/Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py:1401)、[宽范围结果更新](/Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py:3770)。

本轮标准评分与 shadow 评分已分开，生产和影子执行也分桶了。但两者先一起写入 hrs.hrs_signals，更新结果仍只按 symbol + direction + status='triggered' 匹配。生产桶先执行，成功更新 executed 时会同时更新同币同方向的影子记录；后面的 shadow 更新已找不到 triggered 记录。

独立复现：调用真实 `_finalize_and_execute_signals`、冲突消解、影子分桶、落库和更新方法。模拟执行成功，并以内存 DB 模型按实际 SQL 的 INSERT/UPDATE 谓词更新记录。输入同币同向 standard + emm 两条信号，最终得到 executed、executed，而非 executed、shadow。没有实际 PostgreSQL 集成测试；复现的是当前 SQL 谓词及调用顺序。

影响：污染信号结果统计和影子评估证据。本发现不表示影子路径真实下单，也不等同于交易所成交事实表被写入伪成交。

修复及验收：插入后返回唯一 signal_id，并贯穿执行、冲突、超时和结果更新；按唯一 ID 更新。保存 entry_mode、signal_origin 或决策关联 ID 以便研究归属。增加同币同向生产+影子、同周期多条影子、历史未决信号共存的持久化验收；验证一条结果更新只改变对应行。

## 4. 上轮六项的复验状态

| 项目 | 本轮判断 |
|---|---|
| R1 标准信号被 shadow 替换、非空池 LV-RM 被影子化 | 主评分独立、LV-RM 来源分流已修复；结果归属仍有本轮问题3 |
| R2 事实写失败但游标已推进 | 在本地故障注入范围内通过：先写事实再推进游标，失败显式上报，重试可恢复 |
| R3 账户及收入类型共用游标 | 新键隔离在本地测试范围内通过；旧数据库迁移未部署，不能据此宣称线上已生效 |
| R4 缺真实市值仍可入场、样本门槛错误 | 新开仓 veto 和逐截面样本门槛已修复，退出侧不被代理市值阻塞 |
| R5 慢报价被当新报价、执行前未复查 | 请求耗时与下单前超龄重查已修复；重新报价与额度计算衔接存在问题2 |
| R6 同毫秒跨页 | fills 仍受问题1阻塞；income 显式不完整分支已生效，尚不是完整采集 |

income 的独立案例使用同毫秒3条、limit=2：返回2条、page_complete=False，并提示无法继续翻页。需求文档收口部分已登记这一边界，本轮将其列为已披露的能力限制，不另算新缺陷。不能把“不再静默丢数据”写成“收入流水已全部采齐”。

## 5. 验证记录与边界

定向测试：**155 passed**。命令：

```sh
python3 -m pytest strategies/hrs/tests/test_r1_r4_regressions.py strategies/new_coin/tests/test_price_freshness_r5.py strategies/new_coin/tests/test_rebound_entry_contract.py tests/test_shared/test_execution_facts_regression.py tests/test_accounting/ tests/test_shared/test_execution_facts_sink.py -q --disable-warnings --tb=short
```

扩大回归：**119 passed, 4 failed, 4 errors**，达到 maxfail=8 后停止，不能宣称全量通过。失败集中于 HRS 测试夹具缺失 circuit_breaker 以及 Python 3.9 下缺少当前事件循环；表现与首轮记录一致，本轮不将它们推断为这次修复新引入的问题。完整输出见 regression_tests.txt。

```sh
python3 -m pytest strategies/hrs/tests/ strategies/new_coin/tests/ tests/test_shared/test_order_fill_waiter.py tests/test_strategies/test_time_stop_review.py tests/test_strategies/test_capital_allocation_enforcement.py ai_tuner/allocation/tests/ -q --disable-warnings --tb=short --maxfail=8
python3 docs/reviews/strategy_reaudit_2026-10-16_round2/reproduce_findings.py
```

独立脚本4个案例的断言均通过：前3个断言确认上述缺陷仍存在，第4个确认 income 显式不完整的边界。见 reproduction_results.json、reproduction_log.txt。脚本通过不是生产功能通过。

本轮不重新证明策略经济定位、收益有效性、净收益归属、权益守恒、实际多腿执行与线上部署。这些也不能由155个单元测试推导出来。建议先处理本轮3项，再继续此前的数据证据和组合研究工作。
