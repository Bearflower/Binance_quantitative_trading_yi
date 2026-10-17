---
id: Binance_quantitative_trading-20261016-hrs-shadow-signal-origin-not-substitute
类型: decision
项目: Binance_quantitative_trading
标题: HRS 影子信号不得替代生产信号：主结果恒 standard，EMM/半EMM 与空池 LV-RM 仅作 shadow（signal_origin 区分）
日期: 2026-10-16
标签: [hrs, 影子模式, signal_origin, 信号可达性, 先影子, 依赖 D2, D8]
任务标识: strategy_remediation_R1-R6_2026-10-16
状态: 已采纳
审查: 待审查
---

# HRS 影子信号不得替代生产信号

## 背景
HRS `ScoringEngine.score()` 原先在 EMM 触发时**只返回 EMM 结果**、standard 分支不执行；`_is_shadow_entry_mode` 只看 `entry_mode`，把「非空候选池原本可交易的 LV-RM」也一并影子化。后果：影子功能**减少了原策略原本可执行的交易**，且无法对「旧策略实盘」与「新增影子信号」做公平差异比较（即独立复审 R1）。承接业务决策 D2（EMM/半EMM 先影子）与 D8（空池 LV-RM 先影子），需固定「生产路径与 shadow 路径彻底分离」的口径。

## 备选方案
- **A：影子链继续抢占唯一信号**（原状）——会改变旧版真实订单意图，违反 D2「影子阶段不改变真实下单」。
- **B（采纳）：主结果恒为标准链 + shadow 独立产出**——`score()` 主结果等价 `price_change_24h=None` 的旧版 standard（不接收 `price_change_24h` 影响、无 `extreme_bonus`、不触发半EMM）；EMM/半EMM 作为独立 shadow 结果挂在 `ScoringResult.shadows`。
- **C：完全移除 EMM/半EMM/LV-RM**——属显式策略变更，超出本轮范围。

## 决策
采纳 B，并明确：
1. **主结果恒 standard**：`score()` 编排先出 `_score_standard` 主结果；`_score_emm`/`_score_semi_emm` 命中的结果 `append` 到 `primary.shadows`（叶子，`entry_mode="emm"/"semi_emm"`）。
2. **来源标识**：EMM/半EMM 的 shadow 结果经 `signal_origin="shadow_scoring"` 标识；`lv_rm` 信号用 `signal_origin ∈ {empty_pool, nonempty_pool}`——`empty_pool`（空池新增路径，属 D8）可命中影子，`nonempty_pool`（非空池原 LV-RM）**保持原政策、不影子化**；`signal_origin` 缺失 fail-closed 判影子并告警。
3. **shadow 不抢占生产**：shadow 信号不参与冲突消解、不真实下单、绝不落 `resolution='executed'`（落 `shadow`）；`_is_shadow_entry_mode(entry_mode, signal_origin)` 是否影子化仍由 config `{mode}.execution_mode=="shadow"` 决定——若改 `execute` 即转正式下单，属**显式放行，需用户单独拍板**。

## 理由
- 满足 D2/D8「先影子、不改变真实下单」；用「同一输入启用 shadow 前后旧版订单意图完全一致」作可测断言。
- 保留原生产决策分支可达性，才能把「旧策略实盘」与「新增逻辑影子」作公平差异比较。
- 非空池 LV-RM 不影子化，避免把「停止全部 LV-RM」混入 R1 修复（如需停止属显式策略调整，另立记录）。

## 后果
- 收益：影子不减少原可执行交易；shadow 与 standard 可同源对照、独立读取。
- 技术债：EMM/半EMM 的 `extreme_bonus` 整体从 standard 主链移入 shadow 链（属新增输入，不污染旧版公式）；`signal_origin` 仅对 `lv_rm` 有意义，EMM/半EMM 不看该字段。

## 适用范围与修订
- 适用：`strategies/hrs/scoring_engine.py`（`score()`/`ScoringResult.shadows`/`_score_standard`/`_score_emm`/`_score_semi_emm`）、`strategies/hrs/strategy.py`（`_is_shadow_entry_mode`、`_check_lv_rm_entries`、`analyze`、`execute_signal`、`_execute_shadow_signals`）。
- 替代条件：若用户显式放行 EMM/半EMM 或空池 LV-RM 转正式下单（config 改 `execute`），本决策的「影子」语义随之变更，届时应新建 decision 记录放行依据并双向链接。本决策与 D2/D8 同级、互不替代。

## 关联与证据
- 来源任务: `strategy_remediation_R1-R6_2026-10-16`；依赖业务决策 D2/D8（`docs/research/strategy_remediation/decisions.md`）
- 证据摘要: 回归 `strategies/hrs/tests/test_r1_r4_regressions.py` —— `test_r1_main_result_entry_mode_always_standard`、`test_r1_main_result_intent_unchanged_by_price_change`、`test_r1_main_result_extreme_bonus_always_zero`、`test_lv_rm_nonempty_pool_not_shadow`、`test_lv_rm_empty_pool_is_shadow`、`test_lv_rm_missing_origin_fail_closed` 均通过；`shadow_replaces_standard` / `lv_rm_existing_also_shadow` 场景反转；本轮新增失败 0
- 关联文档: 需求 `requirements_R1-R6_2026-10-16.md` §2-R1；架构 `architecture_R1-R6_2026-10-16.md` §2.1、§7.2
