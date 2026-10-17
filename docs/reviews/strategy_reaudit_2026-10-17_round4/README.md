# 五项修复第四轮复核（2026-10-17）

范围：当前工作区（含未提交修改），HEAD与文件哈希见manifest.json。仅新增本目录审查材料；未修改生产代码，未连接生产数据库、交易所或通知服务。未部署。

结论：原来的结束毫秒跨页漏数已修复；仍有一个闭区间单毫秒查询的确定缺陷。其余项目必须分别表述为本地实现通过、上线待验、口径文档完成或采集能力未补齐，不能统称五项全闭环。

## 1. [P2] 起止时间相同的有效闭区间被直接跳过

位置：[export_execution_facts.py:652](/Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py:652)。

已将窗口契约统一为闭区间[start,end]，但前置守卫仍在 `cur_start >= window_end_ms` 且无已有游标时直接构造完整结果。首次查询[1000,1000]是有效的单毫秒窗口，不是已采集完成的窗口。

独立复现：构造time=1000的一笔成交，调用真实export_fills('TESTUSDT',1000,1000)。结果API调用数0、实际返回0条、page_complete=True，预期应采得1条。复现见check_remaining.py及remaining_results.json。fills和income共用该守卫。

影响限于这种单毫秒窗口/精确毫秒补采场景；不将其夸大为普通多毫秒窗口仍然漏数。修复应区分“已完成游标的幂等重跑”和“无游标首次查询”；相等窗口仍应查询，反向窗口应明确拒绝或单独定义。验收覆盖单毫秒零条、一条、超过一页，以及已完成/未完成游标重跑。

## 2. 五项逐项结论

| 项目 | 复核结果 | 尚需处理 |
|---|---|---|
| 原分页结束毫秒超过一页漏数 | **原案例通过**：5笔同毫秒、limit=2全部取回；原始满页判断、去掉时间墙已生效 | 修复上面的单毫秒守卫缺陷 |
| RK1 新增列与归属写入 | **本地实现通过，上线未验**：两列建表及幂等ALTER存在；写失败/缺ID/更新失败已接入告警，相关测试通过 | 实际库两列存在、真实写入及告警通道送达仍需上线验收；本轮没有这些证据 |
| 原评分行为断言失败 | **该行为断言已修复**：short门槛2.0与当前配置一致，补充低于门槛拒绝与主/影子独立测试；评分非性能测试41项通过 | 吞吐量测试另有失败，见下节，不能报告整个评分测试全绿 |
| RK2 超时信号统计口径 | **口径文档和固定样本验证完成**：在检查的仓库生产代码中未找到hrs_signals的统计读取方；新增测试是本地helper，不是生产统计实现 | 新增读取方或仓库外看板接入时按口径验收。当前执行率分母明确不按来源剔除，可能包含影子来源的conflict/timeout；若目标是生产执行率需另定义过滤，不能混称 |
| income同毫秒超过单页 | **显式缺口保护通过，完整采集未完成**：独立案例3笔、limit=2，两次运行均保持incomplete且有错误说明 | 完整补采仍缺；净收益正式接入时须实现并测试完整性门禁。规则披露不能代替消费端拦截验证 |

关于income：本轮源码搜索中，生产代码使用page_complete的模块为export_execution_facts.py与execution_facts_sink.py，未找到净收益消费端的完整性拒绝实现。文档列出的 `test_income_incomplete_never_enters_pnl_conclusion` 和 `test_income_no_plus_one_ms_workaround` 名称未找到实际定义；需求表将其列为规则+审查约束。应表述为“已规定禁止使用不完整数据”，不能表述为“正式净收益入口自动拒绝已验证”。这不是声称现有生产净收益已经误用数据，而是验收证据的边界。

## 3. 测试结果

定向组合：**170 passed, 1 failed**。唯一失败为TestPerformance.test_评分引擎吞吐量，约3453币种/秒，低于断言5000。

单独复跑同一性能测试仍失败，约3045币种/秒。评分非性能用例：**41 passed, 1 deselected**。性能失败可能受机器、日志及测试环境影响；未做同环境修复前性能基线，不能归因于本轮修改，也不能忽略后宣称全部通过。

```sh
python3 -m pytest tests/test_shared/test_execution_facts_regression.py tests/test_accounting/ strategies/hrs/tests/test_rk1_signal_db_alert.py strategies/hrs/tests/test_rk2_signal_stats.py strategies/hrs/tests/test_scoring_engine.py strategies/hrs/tests/test_r9_signal_attribution.py strategies/new_coin/tests/test_reprice_budget_r8.py -q --disable-warnings --tb=short
python3 -m pytest strategies/hrs/tests/test_scoring_engine.py::TestPerformance::test_评分引擎吞吐量 -q --disable-warnings --tb=short
python3 -m pytest strategies/hrs/tests/test_scoring_engine.py -k 'not Performance' -q --disable-warnings --tb=short
python3 docs/reviews/strategy_reaudit_2026-10-17_round4/verify_offline.py
python3 docs/reviews/strategy_reaudit_2026-10-17_round4/check_remaining.py
```

独立脚本确认：原5笔结束毫秒案例取全；新币100→120重新核算后数量0.833、模拟保证金49.98；HRS生产/影子分别标记executed/shadow；income重复运行保持incomplete；单毫秒窗口仍错误返回零条完整。模拟订单与DB仅验证调用链和SQL谓词，没有执行真实交易或写库。

完整输出见targeted_tests.txt、performance_retry.txt、scoring_functional.txt、probe_log.txt、remaining_log.txt。结果见results.json、remaining_results.json。

## 4. 建议收口方式

1. 修单毫秒守卫，增加首次查询与断点恢复的边界测试。
2. 性能项明确基准环境、日志开销与修复前基线，再决定代码优化或调整测试设计，不盲目降低阈值。
3. RK1保持“部署验收待办”，有实际数据库和写入证据后再关闭。
4. RK2标记为“当前仓库统计口径已定义，无生产读取方”；income标记为“缺口可见且不可用于正式收益，补采和消费端门禁待办”。
