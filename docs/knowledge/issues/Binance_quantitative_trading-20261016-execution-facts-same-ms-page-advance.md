---
id: Binance_quantitative_trading-20261016-execution-facts-same-ms-page-advance
类型: issue
项目: Binance_quantitative_trading
标题: 同毫秒多笔成交/收入超过一页时无法续传，分页卡死在同一时间起点
日期: 2026-10-16
标签: [bug, 数据采集, execution_facts, 分页, 同毫秒, fromId, 高优先级]
任务标识: strategy_remediation_R1-R6_2026-10-16
状态: 已解决
审查: 待审查
---

# 同毫秒多笔成交/收入超过一页时无法续传，分页卡死在同一时间起点

## 现象
采集脚本翻页仅把 `start_time` 推进到已消费的最大时间，**不使用已保存的 `last_event_id` 续传**。当同一毫秒内记录数超过单页上限时，下一页仍以同一时间起点请求，只能反复取到同一批前 N 条，最终以 `max_pages` 退出、其余同毫秒记录无法越过（离线复现：同毫秒 4 笔、`page_limit=2`，请求起点 `[1,1000,1000]`，只取 2 笔）。该缺陷即独立复审 R6。

## 根因
- `_consume_page` 只求本页 `max_time`；`_one_page_iteration` 用「仅 max time」作下一请求 `start_time`，`last_event_id` 虽记录但**未用于续传**。
- 时间粒度（毫秒）不足以区分同毫秒多条记录，`start_time` 无法推进。
- 既有「同毫秒」测试仅 5 笔单页 500，未覆盖「同毫秒跨页」。

## 解决
- **fills（`userTrades`）**：用事件 ID 续传——`shared/binance_api.get_user_trades` 新增可选 `from_id`（映射币安 `fromId`，项目内旧调用点向后兼容）；满页推进时下一请求 `from_id = 本页最大 trade_id`，同毫秒剩余靠 `fromId` 越过，完整取回。
- **income（`um/income`）**：`get_income_history` **未暴露 `tranId` 续传能力**，故当「本页满 + 时间卡住 + ID 卡住」三连命中时**显式不完整**：`page_complete=False`、`request_status='incomplete'`、`error_detail="同毫秒记录超过单页上限且接口不支持事件ID续传，无法继续"`。
- **禁止 `+1ms`**（会直接跳过余下同毫秒记录），**禁止无限重取同页**。
- 边界覆盖：同毫秒 = limit、= limit+1、多页、最后时间 == 窗口右边界。

## 验证
- 回归用例 `tests/test_shared/test_execution_facts_regression.py::TestR6SameMsCrossPage`（先红后绿）：`test_same_ms_exactly_at_limit`、`test_same_ms_limit_plus_one`、`test_same_ms_four_trades_limit_two`（断言取回全部 4 笔、`page_complete=True`）、`test_last_time_equals_window_boundary`、`test_income_no_from_id_falls_back_to_incomplete`、`test_multi_page_with_different_times`。
- 复现脚本 `reproduce_findings.py` 的 `same_ms_cross_page` 场景已反转（`expected == 4`、`actual == 4`）。
- 结果：本轮新增失败 0。

## 教训
用单一时间戳做分页游标时，**时间粒度无法唯一排序**的场景（同毫秒多条）必须依赖接口自身的事件 ID/页码续传；接口不支持时，**宁显式不完整也不要 `+1ms` 近似推进**——`+1ms` 看似推进，实则静默跳过同毫秒余下记录，制造更隐蔽的漏采。（AI 归纳，仅供参考）

## 关联
- 来源任务: `strategy_remediation_R1-R6_2026-10-16`（R6 收口）
- 持久证据: 复审报告 `docs/reviews/strategy_reaudit_2026-10-16/README.md` §1-R6 与 §7.1；需求 `requirements_R1-R6_2026-10-16.md` §2-R6；架构 `architecture_R1-R6_2026-10-16.md` §2.5、§7.2
- 必要证据摘要: `get_user_trades` 增 `from_id`→`fromId`；income 无 `tranId` 续传走显式不完整；同毫秒 4 笔 limit=2 取回全部；`same_ms_cross_page` 反转
- 相关 issue: [cursor 先于事实落库](./Binance_quantitative_trading-20261016-execution-facts-cursor-before-fact-silent-drop.md)（R2，同采集脚本）
