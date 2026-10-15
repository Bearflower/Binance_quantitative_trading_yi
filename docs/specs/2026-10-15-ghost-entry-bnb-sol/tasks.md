---
id: tasks-2026-10-15-ghost-entry-bnb-sol
类型: tasks
项目: Binance_quantitative_trading
对应 spec: spec-2026-10-15-ghost-entry-bnb-sol
日期: 2026-10-15
状态: 待批准
---

# 任务清单

> 每个任务标注：产出物 / 依赖 / 验收证据。批准前不执行 T4（写库）与 T7（发布）。

| # | 任务 | 产出/动作 | 依赖 | 验收证据 | 状态 |
|---|------|-----------|------|----------|------|
| T1 | 固化本轮证据 | 记录 E1–E5 查询原文（BNB 3773 / SOL 4026 / 4002 CLOSED / 时区对账） | — | 已在本 spec §2 | ✅ |
| T2 | 脚本加固：`--symbol` / `--strategy` 过滤 | 改 `scripts/fix_ghost_entry_records.py`（`parse_args` + `run_audit` 参数化 SQL） | T1 | 单测 4 例（`TestT2BuildCandidateQuery`）：过滤生效且 SQL 参数化 | ✅ |
| T2b | 脚本分类器修复：持仓查询成功但无该币种行 → 视为归零 | 改 `audit_entry`（原误判 SKIP，使 FILLED+已平仓记录永不收敛） | T3 | 单测 4 例（`TestT2bAuditEntryPositionResolution`） | ✅ |
| T3 | dry-run 对账 | 服务器（btc_eth 容器内）执行 `--symbol BNBUSDT --symbol SOLUSDT` dry-run | T2,T2b | BNB 8 条 CLOSED / SOL 47 条 KEPT，无 LIVE/SKIP；SOL 4026 为真仓 | ✅ |
| T4 | **D1 写库（P1 已批准）** | `--execute --symbol BNBUSDT` | T3 | 落库 CLOSED=8；BNB mutex owner 变空；SOL 分布逐行未变（AC1/AC3） | ✅ |
| T5 | 互斥复检 | 对 BNBUSDT/SOLUSDT 重跑 `_open_entry_sql(False)` | T4 | BNB 无命中；SOL 命中行（**当前态**）为 btc_eth_aggressive（真仓保留；**【更正】**该次 11:05 拦截非由真仓触发，属幽灵误拦，见 spec §10.1 E2） | ✅ |
| T6 | D3 观测加固 | `release_claim` 命中 0 行时告警（`_parse_update_count`） | — | 单测 `test_terminal_mark_zero_rows_warns` + 日志可观测 | ✅ |
| T7 | **D2 修补（P2 已批准）** | `shared/position_ownership.py` `release_claim` 扩 reason → CANCELED/CLOSED | P2 | `TestAC4ReleaseClaimGuard` + 扩 reason 单测全绿 | ✅ |
| T8 | 发布与五层验证 | push main → Actions → 服务器核对 | T6,T7 | AC6 | ✅ |
| T9 | 文档对照与归档 | 更新知识条目 / 前轮方案 §10 / handoff | T5,T8 | 文档与代码一致 | ✅ |

## 待用户决策

- P1（已批准）：T4 对生产库写终态（仅 CANCELED/CLOSED）。
- P2（已批准）：T7 修改共享 `release_claim`（影响 btc_eth / aggressive / new_coin / hrs 四策略）。

## 建议路径

1. 先 T2 → T3（只读，零风险）→ 拿 dry-run 事实。
2. 由 dry-run 结果决定 SOL 处置（真仓即保留，避免误标；**【更正】**11:05 拦截属幽灵误拦，非真仓所致，见 spec §10.1 E2 更正）。
3. P1 批准后 T4/T5 立即解除 BNB（必然）与 SOL（按事实）误拦。
4. P2 若通过 → T6/T7/T8 走完整发版；否则 T9 收尾并登记「定期清理」运维项。
5. **后续（SOL）**：SOL 现存 44 条 aggr NEW（多为幽灵）需逐单对账清理——**dry-run 先行**，只清交易所确认「非真仓 / 非活单」的记录，**保留 id=4026 等交易所 FILLED 且持仓非 0 的真仓**（不可照搬 BNB 一刀切置 CLOSED）。
