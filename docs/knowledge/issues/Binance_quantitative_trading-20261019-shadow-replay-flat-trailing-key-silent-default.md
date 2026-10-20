---
id: Binance_quantitative_trading-20261019-shadow-replay-flat-trailing-key-silent-default
类型: issue
项目: Binance_quantitative_trading
标题: 回放引擎移动止损读扁平键（非生产嵌套路径）致退出规则静默取默认值
日期: 2026-10-19
标签: [bug, 回放, XAU影子, 配置路径, 静默取默认, 退出规则]
任务标识: xau-mtcps-shadow-pilot_2026-10-19
状态: 待复查
审查: 待审查
---

# 回放引擎移动止损读扁平键（非生产嵌套路径）致退出规则静默取默认值

## 现象

XAU 影子本地回放引擎在判定 `dynamic_trailing` 时，读到的是常量默认值（如 `retrace_ratio=0.5`、`activation_min_profit_pct=0`），而非对应 profile 冻结配置中的真实退出参数；trailing 相关用例因用假配置/扁平键构造而「全绿」，未暴露偏差。

## 根因

回放 `engine.py` 读扁平键 `trailing_cfg.get("activation_min_profit_pct")` / `get("retrace_ratio")`；而生产 `dynamic_trailing` 参数实际嵌在 `risk.signal_levels.{grade}.dynamic_trailing`（含 `activation` / `regression_tiers` / `volatility_adjustment`）。键路径不一致时 `dict.get` 静默回退默认值，不报错，属「配置路径幻觉 → 静默取默认」型缺陷。另 `_partial_close` 未置 `tp1_hit` 标记，`also_on_tp1` 语义失效。

## 解决

改从 `pos["trailing"]`（=`level_cfg.dynamic_trailing` 嵌套结构）读取 `regression_tiers` / `activation` / `volatility_adjustment`，不再读扁平键；`state.on_entry_approved` 注入 `highest_price/lowest_price/trailing_activated/trailing_stop_price/pending_profit_pct/tp1_hit`；按历史峰值浮盈分档 + 硬止损兜底 + 单向移动；`_partial_close` 后置 `pos["tp1_hit"]=True`。

## 验证

以真 profile 配置（`strategies/btc_eth/shadow/config/xau_original.yaml` / `xau_aggressive.yaml`）+ 真 1m K 线驱动新增回归用例，覆盖峰值浮盈分档 / 激活保持 / 保本模式 / 硬止损兜底分支。**本轮测试计数以工程师复测为准（文档侧未独立复核）**。

## 适用范围与修订

适用于 `backtest/btc_eth/replay/{engine,state}.py`；不改变生产 `strategies/`。当前适用性：本分支已修复、未部署。无替代条目。

## 教训

回放/影子等「复制生产语义」的组件，读取退出规则时必须**照抄生产的真实嵌套键路径**并加断言（配置缺键应显式失败而非静默取默认）；用扁平假键构造的测试会掩盖路径错位。AI 归纳，仅供参考。

## 关联

- 本地补充来源: [handoff](../../handoffs/xau-mtcps-shadow-pilot_2026-10-19.md)（不随 Git 同步）
- 持久证据: [2026-10-19-xau-replay-fix-design.md](../../superpowers/plans/2026-10-19-xau-replay-fix-design.md) §1.1、§1.5、§2.3–§2.6
- 必要证据摘要: 生产 `strategies/btc_eth/strategy.py` `_calculate_dynamic_trailing_stop` 从 `risk.signal_levels.{grade}.dynamic_trailing` 读参；回放 `engine.py` 原读扁平键（`get` 默认值静默回退）。
- 相关: [Binance_quantitative_trading-20261018-relative-path-wrong-root-silent-load-failure](./Binance_quantitative_trading-20261018-relative-path-wrong-root-silent-load-failure.md)（同为「配置/路径错位 + 静默回退」型）
