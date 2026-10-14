---
id: Binance_quantitative_trading-20261014-hrs-third-close-path-missing-summary
类型: issue
项目: Binance_quantitative_trading
标题: HRS 第三平仓路径（时间止损/移动止盈）结构漏写 PNL_SUMMARY 汇总
日期: 2026-10-14
标签: [bug, hrs, 盈亏口径, 写入层]
任务标识: hrs_pnl_governance_2026-10-14.md
状态: 已解决（代码修复完成，待部署验证）
审查: 已审查
---

# HRS 第三平仓路径（时间止损/移动止盈）结构漏写 PNL_SUMMARY 汇总

## 现象
HRS 统计口径层防双计修复（`f940a61`/`91e99c2`）部署后，看板 HRS **日最大回撤仍为 100%**。生产库核对：2026-09 有 **114 行明细**属于"从未写过 `PNL_SUMMARY` 汇总"的 (symbol, side) 组，合计 **-33.71** USDT。铁证 BTRUSDT：三笔平仓真实成交（`-6.76575` / `-9.8343` / `-10.754`，合计 **`-27.35405`**）经币安 income 逐笔对账确认归属 HRS，但 trade_records 中**无任何对应汇总行**；净值因此被压至 0 并钳制，回撤必然显示 100%。

## 根因
HRS 存在**三条平仓路径**，汇总行只挂在其中一条上：

1. 部分平仓（TP1/TP2/止损）：`_writeback_pnl`（明细 UPDATE）；
2. TP 达标全平：`_writeback_pnl_for_full_close` → `insert_pnl_summary`（INSERT 汇总）；
3. **第三路径（H6 根因）**：时间止损 / 移动止盈 → `trading_executor.close_position(close_percent=1.0)` → [`_finalize_close_if_filled`](../../../strategies/hrs/strategy.py) → `_writeback_pnl`（明细 UPDATE）→ `remove_position`。该路径**结构性不含 `insert_pnl_summary` 调用**，且 `remove_position` 后主循环 `self.positions.get(symbol)` 已为 None，**永不触发全平回写**。

补充次生缺陷：`update_realized_pnl` 降级匹配会按 `executed_at DESC` 取最近一条 `order_id IS NULL` 行，可能**错回填到 LIMIT 开仓单 / CONDITIONAL_ORDER 条件单创建记录**（该路径下 56 行 CONDITIONAL_ORDER 带 `realized_pnl` 即错行污染）；`insert_pnl_summary` 的 `since` 去重以"金额相同"为键，同 symbol 同 side 跨持仓金额巧合相等时会被误挡。

## 解决
**写入层修复（代码完成，尚未部署）**：

| 文件 | 改动 |
|------|------|
| `strategies/hrs/strategy.py::_finalize_close_if_filled`（L3574-3670 附近） | 全平分支在 `remove_position` **前**补调 `_writeback_pnl_for_full_close`，复用"实际值优先 → 理论兜底 → since 幂等"取值链，补写 PNL_SUMMARY 汇总；`pos=None` 时跳过（单测兼容） |
| `strategies/hrs/strategy.py` 时间止损路径（L1997-2001 附近） | 移除冗余 `mark_stop_loss`（其 fallback 会再插一条金额相同的 `STOP_LOSS` 行造成双写）；改由 `_writeback_pnl_for_full_close` 在 pnl<0 时自动带 `close_reason='STOP_LOSS'`。**副作用（如实记录）：盈利的时间止损不再计入看板止损次数** |
| `shared/trade_logger.py::insert_pnl_summary`（L496-510 附近） | H1 去重收紧：移除 `AND realized_pnl = $4`（`since=entry_time` 已保证幂等，避免金额巧合误挡） |
| `shared/trade_logger.py::update_realized_pnl`（L349-416 附近） | L1 降级匹配守卫：时间窗上限常量 `_MAX_FALLBACK_WINDOW_SECONDS`（默认 1800s）+ SQL 增加 `order_type NOT IN ('LIMIT','CONDITIONAL_ORDER')` 排除错行 |

历史缺口由 `ai_tuner/reconciler/hrs_pnl_backfill.py` 兜底（锚 income 真值，dry-run 就绪）。

## 验证
- **代码修复**：178 项相关单测通过（含 `tests/test_strategies/test_hrs_ghost_position_guard.py` 断言全平后 `_writeback_pnl_for_full_close` 被调用一次、`tests/test_shared/test_trade_logger.py` 断言 `_MAX_FALLBACK_WINDOW_SECONDS` 硬守卫截断）。
- **GC 审查**：C 段 code-specification-inspector 复检通过。
- **根因实证**：BTRUSDT 三笔 income `tradeId=143943408 / 162269923 / 166830754` 在币安 PM API 真实存在且匹配归属 HRS（`docs/reports/hrs_income_reconcile_report.md` §一）。
- **待验证**：部署后核对 HRS 日最大回撤自 1.0 回落、且新写汇总行金额与 income 真值一致（本任务 D 段执行）。

## 适用范围与修订
适用于 `strategies/hrs/strategy.py` + `shared/trade_logger.py`（`shared/` 变更 → 全部策略容器重建）。当前**代码修复已完成、未部署**，状态为「已解决（代码修复完成，待部署验证）」；部署证据补齐前不得视为生产已根治。上游统计口径条目 [20261013-hrs-pnl-double-count](./Binance_quantitative_trading-20261013-hrs-pnl-double-count.md) 保留历史（统计层）结论，本条为其写入层延续。无替代条目。

## 教训
当平仓路径不止一条时，"写汇总"必须挂在**最终一致的结算点**上，而不是依赖某一条路径顺带触发；一旦某条路径以 `remove_position` 收尾，就切断了"全平回写"的最后触发时机，漏写因此成为**结构性**而非偶发。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/hrs_pnl_governance_2026-10-14.md)（不随 Git 同步）
- 持久证据: 对账报告 `docs/reports/hrs_income_reconcile_report.md`（§一 BTRUSDT 铁证、§二 各月真值表）；设计文档 `docs/plans/hrs_pnl_governance_design.md`（§2.1 三路径分解、§2.2 H6）；代码 `strategies/hrs/strategy.py::_finalize_close_if_filled`、`shared/trade_logger.py::insert_pnl_summary / update_realized_pnl`
- 必要证据摘要: 2026-09 无汇总组 114 行 = -33.71（其中 TAKE_PROFIT -30.9864、LIMIT -1.3402）；BTRUSDT 三笔 -27.35405；修复代码未部署
- 相关: [Binance_quantitative_trading-20261013-hrs-pnl-double-count](./Binance_quantitative_trading-20261013-hrs-pnl-double-count.md)（上游：统计层防双计，本条是其写入层延续）
