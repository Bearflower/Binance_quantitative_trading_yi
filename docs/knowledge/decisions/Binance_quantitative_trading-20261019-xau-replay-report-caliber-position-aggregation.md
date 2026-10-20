---
id: Binance_quantitative_trading-20261019-xau-replay-report-caliber-position-aggregation
类型: decision
项目: Binance_quantitative_trading
标题: XAU 影子回放报告评估口径——仓位生命周期聚合净 pnl + 盯市回撤 + trailing 历史峰值浮盈
日期: 2026-10-19
标签: [回放, 报告口径, XAU影子, 盈亏口径, 最大回撤]
任务标识: xau-mtcps-shadow-pilot_2026-10-19
状态: 已采纳
审查: 待审查
---

# XAU 影子回放报告评估口径——仓位生命周期聚合净 pnl + 盯市回撤 + trailing 历史峰值浮盈

## 背景

本地确定性回放器 `backtest/btc_eth/replay/` 需要产出可比、可审计、可复现的准入报告。原实现存在三处口径偏差：`report.py` 逐条退出明细各算一笔交易（与生产 DB `trade_records` 的「平仓动作级流水」形态一致，但错位用于评估）；`equity_history` 只在平仓/事件点更新（未盯市），最大回撤漏掉「持仓期一度深亏后回本」；回放 `dynamic_trailing` 读扁平键默认值，未复刻生产「历史最高浮盈分档」语义。

## 备选方案

- A（动作级流水口径）：沿用生产 DB 记账形态逐条评估。缺点：把一笔仓位的分批止盈拆成多笔，污染胜率 / PF / 扣除最佳单笔。
- B（仓位生命周期聚合 + 净 pnl）：引擎保留逐条退出明细，报告层按 `position_id` 聚合为仓位级，指标一律用净 pnl（含手续费/资金费/滑点）。
- 回撤：C1 用已实现曲线；C2 用逐分钟盯市曲线（含未实现）。
- trailing 分档：D1 对当前浮盈分档；D2 对历史最高浮盈峰值分档。

## 决策

采纳 B + C2 + D2：报告层 `_aggregate_positions` 合成仓位级 `closed_trades`，PF / 胜率 / 连续亏损 / 扣除最佳单笔全部改**净 pnl** 口径；`equity_history` 三元组化 `(datetime, marked_equity, has_open_position)`，最大回撤与反事实 15/25/30% 触发均基于盯市曲线；trailing 维护 `highest_price/lowest_price` 峰值并按峰值浮盈分档。

## 理由

- 评估口径是「仓位生命周期」维度，与生产 DB 的「动作级流水」属两层不同语义，不必也不应强行统一；底层明细保留（对 DB 形态），报告层聚合（对评估口径）。
- 盯市曲线是唯一能记录「持仓期深亏后回本」的载体，且与 15% 回撤门槛、反事实触发时点口径一致。
- 生产 `dynamic_trailing` 的 `profit_ceiling` 对**历史最高浮盈**分档（证据：`strategies/btc_eth/strategy.py` `_calculate_dynamic_trailing_stop`），对当前浮盈分档是幻觉。

## 后果

- 收益：报告指标不再被分批止盈拆笔、不再漏记持仓期回撤、trailing 语义与生产对齐；准入判定更保守可靠。
- 技术债：回放 trailing 触发本期采用「全平（`close_ratio=1.0`）+ 简化模型」口径，与生产「平尾仓」不逐笔一致（报告元数据标注 `trailing_flat_close: true`）；`vol_adj` 因快照仅 30 天 1m 无法重算，降级取 1.0 并标注 `vol_adj_fallback_used`。

## 补充修订（2026-10-20 修复轮，P1#1）

第三轮复审打回后，「连续亏损判定」在**状态机层**细化落地（并入本 decision，同属「仓位生命周期聚合净 pnl」口径，**不另立新条目**）：

- `state.on_entry_approved` 建仓写 `pos["position_net_pnl"] = Decimal("0")`（整笔仓位级累计净盈亏，含全部腿的 fee/funding/slippage）。
- `engine._execute_exit` **每腿累加** `pos["position_net_pnl"] += net_pnl`；全平分支调用 `on_position_closed(..., position_net_pnl=pos["position_net_pnl"])`。
- `state.on_position_closed` 新增**必填关键字参数** `position_net_pnl`，连亏判定由 `realized_pnl < 0` 改为 `position_net_pnl < 0`（权益/费/资费/滑点累计口径不变）。

要点：分批平仓下一笔仓位产生多条退出明细，连续亏损**必须按整笔 `position_net_pnl`（= `Σ net_pnl`）判定**，不能按最后一批 `realized_pnl`——否则只看最后一腿、漏计前序批次已实现的盈亏。报告层 `report._aggregate_positions` 的仓位级 `net_pnl` 应与状态机层 `pos["position_net_pnl"]` 相等（回归测试断言 `Σ net_pnl == pos.position_net_pnl`）。

## 适用范围与修订

适用于 `backtest/btc_eth/replay/{report,state,counterfactual,engine}.py` 与 XAU 影子回放报告；不改变生产 `strategies/`。当前适用性：实施阶段裁定，本分支未部署。无替代条目。

## 关联与证据

- 持久证据: [2026-10-19-xau-replay-fix-design.md](../../superpowers/plans/2026-10-19-xau-replay-fix-design.md) §1.1–§1.2、§2–§4、§11、§13.1；[设计 spec §8.5](../../superpowers/specs/2026-10-18-xau-mtcps-shadow-pilot-design.md)
- 必要证据摘要: `report._aggregate_positions` 按 `position_id` 聚合；`net_pnl(pos)=Σ_i(realized_i+fee_i+funding_i+slippage_i)`；`equity_history` 三元组化；生产 `strategy.py` `_calculate_dynamic_trailing_stop` 的峰值浮盈分档。**（2026-10-20 补记）**状态机层连亏判定改按整笔 `pos["position_net_pnl"]`：`_execute_exit` 每腿累加、`on_position_closed` 新增必填 `position_net_pnl`，与报告层聚合值同口径（断言 `Σ net_pnl == pos.position_net_pnl`）。本轮测试计数以工程师复测为准（文档侧未独立复核）。
- 本地补充来源: [handoff](../../handoffs/xau-mtcps-shadow-pilot_2026-10-19.md)（不随 Git 同步）
