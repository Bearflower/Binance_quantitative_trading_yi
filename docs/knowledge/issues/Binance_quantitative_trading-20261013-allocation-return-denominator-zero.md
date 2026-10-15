---
id: Binance_quantitative_trading-20261013-allocation-return-denominator-zero
类型: issue
项目: Binance_quantitative_trading
标题: 月度分配收益率分母取 0 被静默置 0
日期: 2026-10-13
标签: [bug, ai_tuner, 分母口径]
任务标识: allocation_base_capital_caliber_2026-10-13.md
状态: 已解决
审查: 已审查
---

# 月度分配收益率分母取 0 被静默置 0

## 现象
用户核对 2026-10 月度资金分配报告时发现：new_coin 实际亏损 -4.58、btc_eth 实际盈利 +11.88，但两个策略显示的月收益率**均为 +0.00%**——亏损与盈利都被抹平为 0。

## 根因
ai_tuner 月度分配计算月收益率时，分母取「月初实际占用保证金 `open_margin`」（`pnl_collector.py` `_query_month_start_margin`）。但占用快照表 `public.strategy_position_snapshot_history` 自 **2026-09-10 17:00** 才起积累数据，2026-09 及更早月份的「月初」没有快照，分母取到 0。`allocation_calculator.py` 的除零保护 `return_rate = pnl / capital if capital > 0 else 0.0` 随即把收益率**静默置 0**（无告警、无异常），既掩盖了真实盈亏，也与**当时**看板口径不一致（当时看板用 `_query_strategy_base_capital` 的 4 步回退链，能兜底取到资金基数）。

> **注（2026-10-15 补记）**：上述「当时看板口径」的看板方法 `dashboard/backend/services/data_service_docker.py::_query_strategy_base_capital` **已于 2026-10-15 删除**，看板侧升级为「资金段」分段口径（详见下方「后续口径修正」）。本条根因（分母取 0 被静默置 0）不变，仅修复时的口径参照对象已演进。

## 解决
把分母口径从「月初占用保证金」改为与**当时**看板同源的「策略资金基数」4 步回退链（该链为 **ai_tuner 月度分配侧自有口径**，月粒度；看板侧口径已于 2026-10-15 另行升级，见「后续口径修正」），`pnl_collector.py` `_query_month_start_margin` → `_query_base_capital`，SQL 常量由 2 个增至 4 个（`_BASE_CAPITAL_ALLOC_AT_QUERY` / `_BASE_CAPITAL_ALLOC_EARLIEST_QUERY` / `_BASE_CAPITAL_MARGIN_AT_QUERY` / `_BASE_CAPITAL_MARGIN_EARLIEST_QUERY`），docstring 同步更新；`allocation_calculator.py` 仅改注释（capital 语义 → 策略资金基数）。测试同步迁移：`test_allocation.py` 原 4 个 margin 测试改为 6 个 base_capital 测试（AC-9.1~9.5 + 时区），`_make_trade_record_db` 与 `test_monthly_job.py` `_FakeDb` 增加 ALLOCATED_AMOUNT 分支。

> 说明：本次仅调整 ai_tuner 侧分母口径；`pnl_collector._query_base_capital` 的 4 步回退链**至今（2026-10-15）逻辑未改**，其与看板的「同源/逐字一致」表述已因看板侧重写而失效（见「后续口径修正」）。

| 文件 | 改动 |
|---|---|
| `ai_tuner/allocation/pnl_collector.py` | `_query_month_start_margin` → `_query_base_capital`（4 步回退链）；SQL 常量 2 → 4 |
| `ai_tuner/allocation/allocation_calculator.py` | 注释更新（capital → 策略资金基数） |
| `ai_tuner/allocation/tests/test_allocation.py` | 4 个 margin 测试 → 6 个 base_capital 测试 |
| `ai_tuner/allocation/tests/test_monthly_job.py` | `_FakeDb` 增加 ALLOCATED_AMOUNT 分支 |

## 验证
- `pytest ai_tuner/allocation/tests/ -q` = **97 passed**（全量）
- `pnl_collector.py` **100% 行/分支覆盖**；`py_compile` OK
- 生产库只读验证新口径 9 月分母与**当时**看板取值一致：btc_eth=87.56、btc_eth_aggressive=107.69、hrs=58.37、new_coin=116.75
- 部署 commit `05888cc`，deploy_id=**00004301**；ai-tuner 容器 VERSION GIT_SHA=`05888cc` 与部署日志一致，其余容器增量未动
- 独立审查（code-specification-inspector-local）通过、无阻塞项

## 适用范围与修订
`ai_tuner/allocation/{pnl_collector.py, allocation_calculator.py, tests/test_allocation.py, tests/test_monthly_job.py}`；commit `05888cc` 已 push main 并自动部署成功。口径参照**当时**看板 `dashboard/backend/services/data_service_docker.py` `_query_strategy_base_capital`（该看板方法已于 2026-10-15 删除，ai_tuner 侧 `_query_base_capital` 保留自有 4 步链，见「后续口径修正」）。新口径 9 月收益率（生产数据反算）激进 +32.52% > btc_eth +13.57% > new_coin -3.92% > hrs -53.64%，**排名与现行 2026-10 分配比例（30/25/20/10%）不变**。既有条目 `-20261009-capital-allocation-month-offset` 已补「后续口径修正(2026-10-13)」段，其月份口径结论与状态不受影响。无替代条目。
**待验证**：2026-11 月初（10/31 23:05 定时）首次以新口径运行，届时 step1（月初前 allocated_amount）应能命中，分母才是真月初生效分配额。

## 后续口径修正（2026-10-15，看板侧重写）
本条修复时「与看板对齐」的看板方法 `_query_strategy_base_capital` 已在 2026-10-15「看板资金基数 / 最大回撤口径修复」中**删除**，看板侧升级为「资金段（capital segment）」分段口径，要点如下（权威见 [architecture v1.1](../../plans/fix-2026-10-15-capital-base-caliber-architecture.md) 与 [decision 20261015-segmented-drawdown-caliber](./Binance_quantitative_trading-20261015-segmented-drawdown-caliber.md)）：

- **段边界**：主锚 E1 month 切换 / E2 `allocated_ratio` 变更 + 辅锚 E3 静态跳变（跳变后恒定 ≥ K 天，K 默认 3 且配置化）；日内每日刷新漂移**不算**段边界。
- **段初口径**：段边界生效时刻**之后**、段内首条 `allocated_amount > 0` 快照（向后取，SQL `_SEGMENT_OPENING_CAPITAL_SQL`）；旧 4 步回退链中「全表最早一条 58.37」兜底**不再用于无数据期**。
- **零分母期整段跳过 + INFO 审计日志（本结论保留）**：段初分配额为 0 或不存在（含 `allocated_amount=0` 却有成交的零分母期）时，该段**整段跳过、不计入任何段回撤**，不静默用 0 或其它段值作分母；`_derive_segments_for_strategy` 对每个段打常开 INFO 审计日志（`strategy_id`/`segment_start`/`opening_capital`/`skip`）。btc_eth_aggressive 9-10~9-20 零分母期即整段跳过。
- **grid 不可评分不产新行**：grid 在 `strategy_position_snapshot_history` 无任何行，新算法对其**不产生任何新行、不覆盖旧值**（无回归），`performance_metric_snapshot` 残留行维持现状。
- **ai_tuner 侧不变**：本条修复的 `pnl_collector._query_base_capital` 4 步回退链**逻辑未改**，仍是 ai_tuner 月度分配的自有（月粒度）口径；与看板不再「逐字一致」，仅「同源」。

> 修订记录（2026-10-15）：上述为**补记/澄清**，本条根因（分母取 0 被静默置 0）、解决方案（`_query_month_start_margin` → `_query_base_capital`）与验证证据均不变。补注来源经 code-document-curator-local 核对（看板旧方法 `_query_strategy_base_capital` 与 `_build_strategy_daily_returns` 已从 `data_service_docker.py` 删除、新 `derive_segments` / `_SEGMENT_OPENING_CAPITAL_SQL` 存在、ai_tuner `_query_base_capital` 保留）。

## 教训
「除零保护写 0」看似稳妥，实则把「数据缺失」与「真实收益为 0」两种语义混为一谈——缺数据时应告警或回退到可用来源，而不是静默归 0。跨模块共用同一业务指标（收益率/资金基数）时，分母口径必须与展示端（看板）显式对齐，否则同一份盈亏在两处呈现不同结果。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/allocation_base_capital_caliber_2026-10-13.md)（不随 Git 同步）
- 持久证据: commit `05888cc`（`ai_tuner/allocation/pnl_collector.py` `_query_base_capital`）；需求文档 `docs/requirements/capital_allocation/月度资金分配需求文档.md` §5.2/§9.2（v1.5）
- 必要证据摘要: 快照表 2026-09-10 才起积累致 9 月分母取 0 → 除零保护静默置 0；97 passed、pnl_collector 100% 覆盖；生产库只读 9 月分母（btc_eth=87.56 等）与看板一致；deploy_id=00004301
- 相关: [Binance_quantitative_trading-20261009-capital-allocation-month-offset](./Binance_quantitative_trading-20261009-capital-allocation-month-offset.md)（同属 ai_tuner 月度分配口径缺陷；该条目记录月份错位，本条记录分母口径）
