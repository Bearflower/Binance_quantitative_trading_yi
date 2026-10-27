# AI Tuner 阶段二 · S5 双策略适配器（等价提取收尾 + 适配器落地）Spec

- 日期：2026-10-27
- 分支/工作树：`codex/ai-tuner-stage2-readiness-plan` @ `/Users/yl/.codex/worktrees/ai-tuner-stage2-readiness-plan/Binance_quantitative_trading`
- 上游计划：`docs/superpowers/plans/2026-10-22-ai-tuner-stage2-replay-contracts.md`
- 状态：待用户批准后执行（本轮只做本地 commit，**不 push、不部署**）

## 1. 目标

1. 完成 MTPCS 激进版「等价提取」收尾：`strategies/btc_eth_aggressive/strategy.py` 的纯计算逻辑全部转发到 `strategies/btc_eth_aggressive/core/`，消除实盘与 core 的双份实现。
2. 修正回放适配器协议签名偏差：`protective_prices`。
3. 新增两个回放适配器：
   - `ai_tuner/replay/adapters/mtpcs_aggressive.py`：只依赖 `core/` 与回放底座，禁止 import `strategy.py` / `backtest/`。
   - `ai_tuner/replay/adapters/hrs.py`：按历史时点恢复三张表，缺数据抛 `DataBlocked`（映射为 `BLOCKED_DATA`），禁止实时补值。

## 2. 已核实事实（本轮读码结论）

| 事实 | 证据 |
|------|------|
| core 六个模块已存在且金样逐字段一致 | `tests/test_strategies/test_btc_eth_aggressive_core_equivalence.py` 3 条通过 |
| `strategy.py` 仅 `_safe_last` 转发 core | `strategy.py:75-77` 唯一一处 `_core_*` |
| 金样冻结件含 288 用例、5 个确定性场景 | `tests/test_strategies/fixtures/btc_eth_aggressive_golden{,_expected}.json`、`..._golden.py` |
| `_check_trend_filter` 的 `symbol` 参数在函数体内未被使用，全部调用点均为位置参数 | `strategy.py:900/1250/1738` |
| `base.py` 声明 `protective_prices(self, position)`，`engine.py` 传入 `symbol` | 签名不一致（summary 已记录） |
| `core/exits.trailing_stop_price` 自包含激活/档位推导，忽略传入 `retrace_ratio`，仅对 A 级 fallback 等价 | `core/exits.py:158-225`；`golden.py:753-774` |
| 实盘 `_calculate_dynamic_trailing_stop` 有「保本模式」分支（`pct < 首档 && !tp1_hit → stop=entry`），core 无此分支 | `strategy.py:4779-4783` |

## 3. 设计决策（执行时不得偏离）

### D1 · core.exits.trailing_stop_price 增加可选实参，保留旧路径不变
- 新签名（仅追加 keyword-only 参数）：
  `trailing_stop_price(..., *, activated: Optional[bool] = None, tiers: Optional[List[Dict]] = None, breakeven_floor: bool = False)`
- `activated is None`：保持现有自包含逻辑**逐行不动**（金样 `test_core_matches_frozen_golden` 必须仍通过）。
- `activated is False`：返回 `None`。
- `activated is True`：按实盘口径计算——`pct` 用方向符号（LONG: `(ref-entry)/entry`；SHORT: `(entry-ref)/entry`），负值归零；`breakeven_floor=True` 时 `stop_price = entry`；否则按 `tiers`（缺省用 A 级 fallback）选档位回撤比例，再套硬止损与单向移动保护。
- 实盘 wrapper 传 `activated=True, tiers=实盘配置档位, breakeven_floor=实盘分支判定`，保证逐值等价。

### D2 · core.filters.should_keep_position 增加可选波动检查回调
- 签名追加：`volatility_check: Optional[Callable[[Dict, Dict], Tuple[bool, str, Optional[float]]]] = None`
- 默认 `None` 时用纯函数 `evaluate_volatility_regime`（金样与适配器路径不变）。
- 实盘 wrapper 传入携带 `symbol`、会写 `symbol_extreme_pause_until` 的闭包，保留原有副作用与日志。

### D3 · 幽灵参数处理
- `_check_trend_filter` 的 `symbol` 重命名为 `_symbol`（调用点均为位置参数，已核实无 keyword 调用）。
- 其余 wrapper 签名一律保留，避免破坏测试/外部调用。

### D4 · 禁止行为
- 不修改生产参数、不写 `.active`、不调低门禁、不新增 LLM 改代码路径。
- 不在容器/服务器执行回放；本轮不 push、不部署。
- 适配器不得 import `strategies.btc_eth_aggressive.strategy` 或 `backtest.*`。

## 4. 变更清单（精确到函数）

### F1 `strategies/btc_eth_aggressive/core/exits.py`
- `trailing_stop_price`：按 D1 追加参数与显式激活路径；`from typing import ... List`。
- 不改变 `get_grade_risk` / `calculate_tp_price` / `calculate_stop_price` / `apply_limit_offset` / `time_stop_decision`。

### F2 `strategies/btc_eth_aggressive/core/filters.py`
- `should_keep_position`：按 D2 追加 `volatility_check` 形参并在步骤 4 使用；`from typing import ... Callable`。

### F3 `strategies/btc_eth_aggressive/strategy.py`（保持签名，函数体改为转发）
| 方法 | 转发目标 |
|------|---------|
| `_calculate_score` | `_core_scoring.calculate_score(indicators, klines, self.scoring_config, self.risk_config, market_state)` |
| `_calculate_trend_strength_score` | `_core_scoring.trend_strength_score(indicators, self.scoring_config)` |
| `_calculate_pattern_quality_score` | `_core_scoring.pattern_quality_score(indicators, klines, self.scoring_config)` |
| `_detect_bullish_engulfing` / `_detect_bearish_engulfing` | 同名 core 函数 |
| `_detect_breakout_pullback` | `_core_scoring.detect_breakout_pullback(df, indicators, self.scoring_config)` |
| `_detect_divergence` | `_core_scoring.detect_divergence(indicators, klines)` |
| `_calculate_momentum_divergence_score` | `_core_scoring.momentum_divergence_score(indicators, klines, self.scoring_config)` |
| `_check_trend_filter` | `_core_filters.check_trend_filter(indicators, klines, self.risk_config)`（`symbol`→`_symbol`） |
| `_bb_touch_votes` / `_rsi_extreme_votes` / `_reversal_pattern_votes` | 同名 `_core_direction` 函数 |
| `_vote_ranging_direction` | `_core_direction.vote_ranging_direction(indicators, klines, entry_conditions)` |
| `_check_direction_alignment` | `_core_direction.check_direction_alignment(direction, indicators, ranging_config)` |
| `_safe_last_klines_value` | `_core_common.safe_last_klines_value(...)` |
| `_evaluate_overheat` | `_core_filters.evaluate_overheat(...)` |
| `_check_overheat_ban` | `_core_filters.check_overheat_ban(...)` |
| `_apply_overheat_downgrade` | `_core_filters.apply_overheat_downgrade(...)` |
| `_check_volume_confirm` | `_core_filters.check_volume_confirm(...)` |
| `_determine_grade` | `_core_scoring.determine_grade(score, self.scoring_config, self.symbol_config, symbol)` |
| `_get_grade_risk` | `_core_exits.get_grade_risk(grade, self.risk_config)` |
| `_determine_direction` | `_core_direction.determine_direction(indicators, self.timeframes)` |
| `_check_volume_filter` | `_core_filters.check_volume_filter(symbol, grade, klines, self.risk_config, self.symbol_config, vol_boost)` |
| `_calculate_tp_price` | `_core_exits.calculate_tp_price(entry_price, atr, direction, tp_level, grade, self.risk_config)` |
| `_calculate_stop_price` | `_core_exits.calculate_stop_price(position.direction, position.entry_price, position.atr, position.grade, self.risk_config)` |
| `_apply_limit_offset` | `_core_exits.apply_limit_offset(...)` |
| `_build_indicators` | `_core_common.build_indicators(klines, self.risk_config)` |

保留状态/IO 的 wrapper 改造：
- `_check_volatility_regime`：保留 per-symbol 暂停读写与日志；纯比值判定改用 `_core_filters.evaluate_volatility_regime`。
- `_check_prohibited_conditions`：第 1 项改用 `_core_filters.check_kline_spike(klines, config)`；第 2–4 项网络逻辑原样保留。
- `_should_keep_position`：改为调用 `_core_filters.should_keep_position(..., volatility_check=self 的闭包)`，保留复核期暂停副作用；输出 `(keep, reason)` 与单条汇总日志。
- `_calculate_dynamic_trailing_stop`：激活判定、档位索引、`pending_profit_pct`、`trailing_activated`、日志、`_get_volatility_adjustment` 全部留在 wrapper；最终价格公式调用 `_core_exits.trailing_stop_price(..., activated=True, tiers=tiers, breakeven_floor=..., prior_trailing=prev)`。
- `_check_time_stop`：用 `_core_exits.time_stop_decision(holding_hours, grade_risk)` 决定「未到期 / 到点平仓 / 复核期」；`None` 且 `holding_hours >= max_holding_hours` 时进入原复核流程。

### F4 `ai_tuner/replay/adapters/base.py`
- 抽象方法 `protective_prices(self, position)` 改为 `protective_prices(self, symbol)`，与 `engine.py` 调用一致；同步更新文档字符串。

### F5 新增 `ai_tuner/replay/adapters/mtpcs_aggressive.py`
- 只 import：`strategies.btc_eth_aggressive.core.{common,scoring,direction,filters,exits}`、`ai_tuner.replay.*` 底座。
- 实现 `ReplayAdapter` 全部抽象方法（以 base.py 实际协议为准），决策链与实盘一致：
  指标由 `common.build_indicators`；趋势过滤 `filters.check_trend_filter`；评分/等级 `scoring.*`；方向 `direction.*`；过热/量价 `filters.*`；止盈止损与移动止损 `exits.*`。
- 禁止字符串级复制实盘逻辑；所有阈值来自传入的配置字典，禁止硬编码。

### F6 新增 `ai_tuner/replay/adapters/hrs.py`
- 按 `as_of` 从历史快照恢复 HRS 三张表所需数据；任一必需表在 `as_of` 时点无数据 → 抛 `DataBlocked`（由 runner 映射为 `BLOCKED_DATA`）。
- 禁止用实时数据补值、禁止 fallback 到当前快照。

### F7 测试
- `tests/test_strategies/test_btc_eth_aggressive_core_equivalence.py`：3 条必须全绿（不改断言）。
- 新增 `tests/test_strategies/test_btc_eth_aggressive_core_forwarding.py`：静态断言 `strategy.py` 中目标方法体均包含对应 `_core_*` 调用，且不再直接出现重复公式（至少覆盖 24 个方法白名单）。
- 新增 adapter 测试：`tests/test_ai_tuner/` 下按现有目录约定放置，覆盖
  - `protective_prices` 签名与 engine 一致；
  - MTPCS 适配器对金样 5 场景的决策输出与 core 直接调用一致；
  - HRS 缺任一表 → `BLOCKED_DATA`；
  - 适配器 import 白名单（静态扫描禁止 `strategy`/`backtest`）。

## 5. 验证步骤（全部在本地工作树执行）

```bash
# 1) 等价提取
python3 -m pytest tests/test_strategies/test_btc_eth_aggressive_core_equivalence.py -q
# 2) 实盘行为回归（止损/移动止损/时间平仓）
python3 -m pytest strategies/btc_eth_aggressive/tests \
  tests/test_strategies/test_dynamic_trailing_boundary.py \
  tests/test_strategies/test_btc_eth_trailing_boundary.py \
  tests/test_strategies/test_mtpcs_partial_tp_and_trailing.py \
  tests/integration/test_btc_eth_strategy.py -q
# 3) 新增测试
python3 -m pytest tests/test_strategies/test_btc_eth_aggressive_core_forwarding.py <adapter 测试路径> -q
# 4) 基线全量
python3 -m pytest tests/ -q
```

任一失败即视为 S5 未完成；不得放宽断言或删测试。

## 6. 明确不在本 Spec 范围

- S6：`regime` / `qualification` / `gates`（10 硬门禁先于评分）/ `runner` / `parity` / CLI `replay|parity|qualify` / `DataPortal.effective_config()` / `.gitignore var/replay/`。
- S7：代码检测 + 幻觉测试 10 项 + 覆盖率复核。
- S8：复审包与澄清项、push/部署（按用户指令保持暂停）。
- 两策略考试结果对比、生产参数生效、`.active` 写入。

## 7. 需用户确认的事项

1. 是否批准 D1/D2 的「core 追加可选参数」方案（保持旧路径不变，实盘 wrapper 传显式激活/档位）。
2. 是否同意 `_check_trend_filter` 的 `symbol` → `_symbol` 重命名。
3. 是否同意 S5 完成后仅本地 commit、不 push 不部署（沿用既有指令）。
