# S5 任务拆解与验收证据

对应 Spec：`./spec.md`。逐项完成后才能进入 S6。

| ID | 任务 | 关键文件 | 完成判据（证据） |
|----|------|---------|-----------------|
| [x] T1 | core.exits 追加显式激活路径 | `strategies/btc_eth_aggressive/core/exits.py` | 旧路径（`activated=None`）行为不变；`test_btc_eth_aggressive_core_equivalence.py` 3 条全绿 |
| [x] T2 | core.filters 追加 `volatility_check` 回调 | `strategies/btc_eth_aggressive/core/filters.py` | 默认路径与金样 `filters.should_keep_position` 输出一致；回调传入时步骤 4 使用回调 |
| [x] T3 | strategy.py 评分/形态方法转发 core | `strategy.py`（`_calculate_score`…`_calculate_momentum_divergence_score`） | 金样 `test_live_matches_frozen_golden` 通过 |
| [x] T4 | strategy.py 方向/护栏/过滤方法转发 core | `strategy.py`（`_check_trend_filter`、`_bb_touch_votes`…`_check_volume_filter`、`_determine_*`） | 金样通过；`tests/integration/test_btc_eth_strategy.py` 通过（该文件 2 条失败为 pre-existing，见 T15） |
| [x] T5 | strategy.py 禁止条件第 1 项转发 `check_kline_spike` | `strategy.py`（`_check_prohibited_conditions`） | 金样 `prohibition/*` 字段一致；网络三项逻辑未变 |
| [x] T6 | strategy.py 止盈/止损/限价/指标转发 core | `strategy.py`（`_calculate_tp_price`、`_calculate_stop_price`、`_apply_limit_offset`、`_build_indicators`） | 金样 `tp/*`、`stop/*`、`limit_offset/*`、`indicators/*` 一致 |
| [x] T7 | strategy.py 时间平仓决策转发 core | `strategy.py`（`_check_time_stop`） | 金样 `time_stop/*` 24 条一致；`strategies/btc_eth_aggressive/tests` 通过（3 条 pre-existing 失败，见 T15） |
| [x] T8 | strategy.py 移动止损价格公式转发 core | `strategy.py`（`_calculate_dynamic_trailing_stop`）+ `core/exits.py` | 金样 `trailing/0..3` 一致；`test_dynamic_trailing_boundary.py`、`test_btc_eth_trailing_boundary.py`、`test_btc_eth_kline.py` 通过 |
| [x] T9 | strategy.py 复核编排转发 core（保留暂停副作用） | `strategy.py`（`_should_keep_position`、`_check_volatility_regime`） | `strategies/btc_eth/tests/test_time_stop_review.py` 通过；复核期仍写 `symbol_extreme_pause_until`（激进版路径由 T14 适配器测试间接覆盖） |
| [x] T10 | 新增转发静态守护测试 | `tests/test_strategies/test_btc_eth_aggressive_core_forwarding.py` | 24 个方法白名单逐条断言 `_core_*` 调用存在；10 条测试通过 |
| [x] T11 | 修正 `protective_prices` 协议签名 | `ai_tuner/replay/adapters/base.py` | `tests/test_ai_tuner_mvp/replay` 71 条通过；签名守护见 `test_adapters_protocol.py` |
| [x] T12 | 新增 MTPCS 适配器 | `ai_tuner/replay/adapters/mtpcs_aggressive.py` | 适配器 `decide()` 在金样 5 场景与 core 直接调用逐字段一致（`test_adapters_mtpcs.py`）；无 `strategy`/`backtest` import；**复审修正**：`market_state` 无默认值，考试路径必须逐时点注入，缺失即 `QualificationBlocked` |
| [x] T13 | 新增 HRS 适配器（历史时点恢复） | `ai_tuner/replay/adapters/hrs.py` | 三表须为带 `as_of`/`complete`/`rows` 的历史快照记录，任一不合规 → `DataBlocked`；`rows=[]` 仅在 `complete=True` 且 `as_of` 一致时合法；**复审修正**：`intents()` 无条件抛 `QualificationBlocked(["hrs_decision_not_wired"])`，杜绝静默零交易回放 |
| [x] T14 | 新增适配器测试 | `tests/test_ai_tuner_mvp/replay/test_adapters_{mtpcs,hrs,protocol}.py` | 覆盖签名一致性、MTPCS 等价、HRS 缺数据阻断、import 白名单 |
| [x] T15 | 回归与基线 | 全仓库 | 本分支 `83 failed / 2549 passed / 248 errors`，基线 `d364537` 同命令 `83 failed / 2499 passed / 248 errors`，失败清单 `diff` 为空（无回归）；新增 50 条通过。Spec §5 各组命令结果见下方「基线对照」 |
| [x] T16 | 本地提交（不 push、不部署） | git | 提交 `48c2b43`（22 文件，+5508/-802），工作树 clean；未 push、未部署 |

## 执行顺序

T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10 → T11 → T12 → T13 → T14 → T15 → T16

## 回滚

- 单任务失败：仅回退该任务文件改动，不得通过放宽测试或删除断言绕过。
- 整体失败：`git restore <file>` 回到合并后基线（`d364537`），重走 Spec。

## 基线对照（T15 证据）

同一命令（`python3 -m pytest tests/ -q --continue-on-collection-errors --ignore=tests/integration/test_kline_integration.py`）：

| 版本 | failed | passed | errors |
|------|--------|--------|--------|
| 基线 `d364537`（独立 worktree 检出） | 83 | 2499 | 248 |
| 本分支（S5 改动） | 83 | 2549 | 248 |

- `FAILED` 清单 `diff` 为空 → 本分支未引入任何新失败；`+50 passed` 即 S5 新增测试。
- 248 errors 主因是环境级 `pytest-asyncio` 不兼容：`RuntimeError: There is no current event loop in thread 'MainThread'`，两版本一致。
- 83 failed 主体为 `MagicMock`/`float` 比较类断言（`tests/test_shared/*`、`tests/test_strategies/test_capital_allocation_enforcement.py` 等），两版本一致。
- `tests/integration/test_kline_integration.py` 必须排除：其中 `test_all_symbols_available[BTCUSDT]` 会连接生产 K 线服务并无限等待（本地无该服务），两版本行为相同。

## 已知 pre-existing 失败（Spec §5 第 2 组命令内的 5 条）

- `strategies/btc_eth_aggressive/tests`：3 条（`test_signal_persistence_wiring.py::test_run_strategy_persist_signal_failure_does_not_break_main_flow`、`test_stop_loss_mark.py::test_持仓同步_部分平仓个数同步`、`test_stop_loss_mark.py::test_持仓同步_持仓量正常`）——用基线 `d364537` 跑同样失败，与等价提取无关。
- `tests/integration/test_btc_eth_strategy.py`：2 条（`test_strategy_analyze`、`test_strategy_execute_signal`）——目标是 `strategies/btc_eth`（非激进版），本轮未改该文件。
- 同组 `tests/test_strategies/test_mtpcs_partial_tp_and_trailing.py` 的 34 errors 亦为上述 `pytest-asyncio` 环境问题，基线一致。

## 复审修正记录（2026-10-28，复审结论 R1–R3）

| ID | 复审意见 | 修正 |
|----|---------|------|
| R1（P1） | HRS 三表齐全时 `prepare` 通过，但 `intents()` 恒空、保护价恒 `None` → 可能产出「零交易但成功」回放 | `intents()` 改为**无条件** `raise QualificationBlocked(["hrs_decision_not_wired"])`，`run_replay` 在首根 K 线即失败；新增端到端测试断言抛 `QualificationBlocked` 而非返回 `ReplayResult`；删除只能切到 `NotImplementedError` 的 `decision_wired` 开关（避免误导性死路径） |
| R2（P1） | 实盘会删除并重写三表，当前表零行不能证明历史时点为空 | 契约收紧为「带时点与完整性证明的历史快照记录」`{"as_of", "complete", "rows"}`；gap 码 7 类（`missing_at_as_of` / `invalid_snapshot` / `incomplete_at_as_of` / `as_of_missing` / `as_of_invalid` / `as_of_mismatch` / `rows_missing`）；`rows=[]` 仅在 `complete=True` 且 `as_of` 一致时合法 |
| R3（P2） | MTPCS 默认 `TREND` 会使回放产出非实盘口径意图 | `market_state` 默认改 `None`；`intents()` 缺状态即 `QualificationBlocked(["market_state_unavailable"])`；`decide()` 收到非法状态抛 `ValueError` |
| R4 | 提交差异含 1 处尾随空格（`strategy.py` `_symbol: str,` 行） | 已删除，`git diff --check` 无输出 |

修正后回归：`tests/test_ai_tuner_mvp` 411 passed / 4 skipped；`tests/test_ai_tuner_mvp/replay` 85 passed；`strategies/btc_eth_aggressive/tests` 12 passed + 3 条 pre-existing；`tests/integration/test_btc_eth_strategy.py` 6 passed + 2 条 pre-existing；等价/转发守护 13 passed。

## 待 S6 承接的显式缺口（复审已确认，不得在 S6 前宣称「双策略资格考试可用」）

- HRS：决策编排（三轨信号 + 退出）整体替换 `intents()`；真实历史快照留存（带时点 + 完整性证明）落地后，`state_loader` 才有真实实现；保护价接入 HRS 目标价。
- MTPCS：逐时点市场状态识别（缺失阻断，不得用默认值）；`min_score` / `min_grade` / ATR 历史过滤 / 频率与生态门禁 / 仓位 sizing 接入后，才能表述为「完整交易决策与实盘等价」。
