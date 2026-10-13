---
id: Binance_quantitative_trading-20261009-hrs-cancel-not-persisted
类型: issue
项目: Binance_quantitative_trading
标题: HRS 撤单只清内存未回写 DB 致 OPEN 记录残留被对账当「假孤儿」反复清理
日期: 2026-10-09
标签: [bug, hrs, 条件单, DB一致性]
任务标识: hrs_condition_order_db_sync_2026-10-09.md
状态: 已解决
审查: 已审查
---

# HRS 撤单只清内存未回写 DB 致 OPEN 记录残留被对账当「假孤儿」反复清理

## 现象
飞书「孤儿条件单自动清理完成」数量异常：LITEUSDT 5 条 / HBARUSDT 49 条，「跳过（正常持仓）」达 376~420。交易所已无对应条件单，DB 却长期残留 OPEN 行，被孤儿清理任务反复「取消」（Binance `-2011` 被当作成功）并产生误导性告警。

## 根因
双写路径不完整：条件单只在**下单成功**拿到 `algoId` 时写 `condition_orders(OPEN)`；而**撤单路径此前只清本地内存 `algo_ids`、不回写 DB**。TP2 达成后监控循环每轮（1h）重建保护单并清场，旧 algoId 在交易所被撤、DB 永久 OPEN，线性沉积（`420 ≈ 140h × 3`）。这些单当时确实在交易所创建成功，**不是下单失败**。

## 解决
commit `e9466537`（3 代码 + plan 文档）：
| 编号 | 改动 |
|---|---|
| D1 | 新增 `shared/condition_orders.py::mark_open_orders_canceled(db, strategy_name, symbol)`（整币种置 CANCELED，异常吞掉只告警） |
| D2 | `cancel_all_orders()` 批量成功分支在 `clear_algo_ids` 后调用 `_sync_db_canceled(symbol)` |
| D3 | `_cancel_individual_orders()` 在 `failed == 0` 时调用 `_sync_db_canceled(symbol)`；`failed > 0` 保守保留 OPEN 不误标 |
| D4 | Step 2 单条落库改用 `mark_order_canceled(db, algo_id=...)`，消除重复 SQL |
| D5 | 新增模块常量 `_STRATEGY_NAME = "hrs"` |
平仓路径 `strategy.py:1889`、`strategy.py:3523` 均调用 `cancel_all_orders()`，被 D2 覆盖，无额外改动。

## 验证
- **测试**：`tests/test_position_manager_cancel.py`（新增 5 条 AC 用例）+ `tests/test_r01_r08_fixes/test_ai_tuner_orphan_cleanup_r08.py` → **16 passed**；`strategies/hrs/tests/test_fix_verification.py` + `tests/test_newcoin_hrs_fixes` → **119 passed, 3 failed**（`git stash` 比对确认为**基线既有失败**）；`tests/test_strategies/test_new_coin_tracking_entry.py` + `test_mtpcs_v628_add_position.py` → **116 passed**；`py_compile` 通过、`flake8 --max-line-length=120` 对新增代码无告警；
- **部署**：Run #54 全绿，`DEPLOY_ID=00003601`，五层防幻觉验证通过；
- **上线观察**：部署时 hrs 存量 OPEN 基线 **274 条**；21:21 HRS 补单周期（AEROUSDT `UPDATE 3`、COINUSDT 撤 5 建 3、DRAMUSDT 撤 4 建 2）**修复生产生效**；21:56 孤儿清理 `canceled=64 failed=0 skipped=255`，总量 **274 → 210**；22:26 第 2 轮无取消无失败，**210 → 209**，本轮未发飞书告警。两轮合计 **274 → 210 → 209（只降不增）**，告警已停止。

## 适用范围与修订
`shared/condition_orders.py`、`strategies/hrs/position_manager.py`、`strategies/hrs/strategy.py`；commit `e9466537`（Run #54, DEPLOY_ID=00003601）已上线。表实际落在 `btc_eth.condition_orders`（`search_path` 首位为 `btc_eth`，见 `database/postgres/init-scripts/01-create-schema.sql:29`）。

残余风险（handoff 记录，未纳入本次范围）：① `shared/reduce_only_close.py:275` 的 `-2022` 路径未同步 DB（无 DB 句柄、属共享模块）；② `ai_tuner` 对 `-2011` 口径仍记为「成功取消」，是否扩大范围待用户决定。

## 教训
「交易所状态与本地 DB 的双写路径必须全量覆盖」——只清内存不回写 DB，会让 DB 变成「幽灵账本」，被对账/清理任务误判为孤儿并反复处理；**无 DB 句柄的共享模块路径**是双写覆盖的常见盲区。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/hrs_condition_order_db_sync_2026-10-09.md)（不随 Git 同步）
- 持久证据: 方案/AC 文档 `docs/plans/fix-2026-10-09-hrs-condition-order-db-sync.md`；commit e9466537
- 必要证据摘要: hrs 存量 OPEN 274 → 210 → 209（只降不增）、告警停止；16 passed（新增 AC）、119 passed/3 failed（基线既有）、116 passed；DEPLOY_ID=00003601 五层验证通过
- 相关: 无
