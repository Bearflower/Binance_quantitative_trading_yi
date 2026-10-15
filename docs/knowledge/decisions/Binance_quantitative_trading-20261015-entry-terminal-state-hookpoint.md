---
id: Binance_quantitative_trading-20261015-entry-terminal-state-hookpoint
类型: decision
项目: Binance_quantitative_trading
标题: 入场记录终态标记挂钩点选在 release_claim(position_closed) 而非 update_realized_pnl
日期: 2026-10-15
标签: [trade_records, 互斥, 状态机, 持仓归属]
任务标识: ghost_entry_mutex_fix_2026-10-14
状态: 已采纳
审查: 待审查
---

# 入场记录终态标记挂钩点选在 release_claim(position_closed)

## 背景
幽灵入场记录误拦互斥修复（见 [ghost-entry-record-mutex](../issues/Binance_quantitative_trading-20261015-ghost-entry-record-mutex.md)）需要为 trade_records 入场记录建立终态回写路径。平仓时机有一个天然的回写点 `update_realized_pnl`，但直接挂钩会产生严重副作用。

## 备选方案
- **A（初始设计）：挂钩 `update_realized_pnl`**——平仓回写盈亏时顺带标入场记录 CLOSED。实现最简单，单点覆盖。
- **B（采纳）：挂钩 `release_claim(reason='position_closed')`**——持仓归零确认（claim 释放的公共确认点）时标最新非终态入场记录 CLOSED。
- **C：成交时标 FILLED**——下单成交回调处标记。覆盖不了已平仓场景，且市价/LIMIT 成交路径分散。

## 决策
采纳 B：终态标记在 `position_ownership.release_claim` 内、仅当 `reason == 'position_closed'`（持仓归零确认）时执行模块内直接 SQL。

## 理由
- A-review 二轮证伪 A：分批止盈（TP1/TP2）**每次部分平仓都会调用 `update_realized_pnl`**——第一次 TP1 就会把尚有剩余持仓的入场记录过早标 CLOSED，使剩余持仓失去互斥权威记录，跨策略可对同一币再开仓（资金风险）。
- `reason='position_closed'` 的全部调用点均在 `del self.positions[symbol]` 归零/僵尸清理语境（A-review 三轮源码实证），语义上等价「该 symbol 该策略持仓已彻底了结」，此时标 CLOSED 无误标窗口。
- release_claim 是全部策略的公共路径，单点覆盖 btc_eth/aggr；new_coin/hrs 缺调用点仅导致保守超拦（无资金风险），风险方向安全。

## 后果
- 收益：部分平仓（TP1/TP2）不再破坏互斥；归零语境明确；失败仅告警不影响 claim 释放。
- 技术债：①new_coin/hrs 平仓路径未接入（保守缺口，存量由 D4 清理补偿）；②多笔加仓只标最新一条非终态记录，更早记录保持 NEW（保守超拦，靠 D4 周期性清理收敛）；③同 symbol 存在刚提交未成交 NEW 挂单时会被一并视为最新而标 CLOSED（互斥权威由策略层 positions 与交易所 positionRisk 兜底）。
- `update_realized_pnl` 接口零改动，回退安全（git revert 即可；CANCELED 数据对旧口径仅多保守拦截）。

## 适用范围与修订
- 适用：`shared/position_ownership.py` release_claim，基线 `fce42bf`（首轮）→ `8071f06`（本轮扩展，见下节）。
- 替代条件：若未来 trade_records 建立「成交状态回写 + 平仓事件」完整状态机，本决策的挂钩点可迁移至平仓终态事件；届时更新本条目并链接新决策。无替代。

## 本轮扩展（2026-10-15，提交 `8071f06`，Run 76 已部署）
挂钩点**选型不变**（仍选 `release_claim`，不选 `update_realized_pnl`），但 reason 覆盖范围扩展：

- 原先仅 `reason='position_closed'` 落 CLOSED；本轮扩为按 reason 查表：`position_closed`→CLOSED，`open_failed`/`open_exception`/`micro_zeroed`→CANCELED（未命中 reason 不标记）。终态 SQL 复用单点构建器 `_mark_entry_terminal_sql(status)`，映射常量 `_ENTRY_TERMINAL_SQL_BY_REASON`。
- 理由：`open_failed`/`open_exception`/`micro_zeroed` 三条 reason 均为「未成仓」语义（下单即写 NEW 但未成交 / 微仓归零），原先不落终态会使 NEW 记录永久残留并被互斥口径当「未平」→ 幽灵误拦（实证见 [ghost-entry-record-mutex](../issues/Binance_quantitative_trading-20261015-ghost-entry-record-mutex.md) §E4）。BNBUSDT id=3773 即此类幽灵。
- 可观测性：解析 `db_manager.execute` 的 `"UPDATE n"` 影响行数（`_parse_update_count`），命中 0 行时 `logger.warning("未匹配到可标记终态的入场记录")`，替代原静默。
- 遗留不变：仍只标「最新一条」非终态记录（更早记录保持 NEW 保守超拦，靠 D4 清理收敛）；标记失败仅告警，不影响占用释放主流程。

## 关联与证据
- 关联任务：docs/plans/fix-2026-10-14-ghost-entry-record-mutual-exclusion.md §9.3（修订权威）、§9.7（A-review 三轮确认）、§10（本轮增量）；handoff `docs/handoffs/ghost_entry_mutex_fix_2026-10-14.md`、`docs/handoffs/ghost_entry_bnb_sol_2026-10-15.md`
- 证据摘要：AC9 反例用例（部分平仓不变更入场记录状态）通过；124 passed； btc_eth strategy.py:5145/6064、aggr 5154/6073 调用点归零语境实证
- 本轮扩展证据（2026-10-15，`8071f06`）：提交 `8071f06`（`release_claim` 按 reason 查表 + `_mark_entry_terminal_sql`/`_ENTRY_TERMINAL_SQL_BY_REASON` + 0 行告警）；互斥相关测试隔离跑 153 passed；Run 76 `DEPLOY_ID=00004C01` 五层验证通过
- 相关 issue: [ghost-entry-record-mutex](../issues/Binance_quantitative_trading-20261015-ghost-entry-record-mutex.md)
