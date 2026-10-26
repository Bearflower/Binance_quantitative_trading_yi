---
id: Binance_quantitative_trading-20261026-bnb-false-mutex-terminal-mark-missing-realized-pnl
类型: issue
项目: Binance_quantitative_trading
标题: 终态标记 SQL 缺 realized_pnl IS NULL 致平仓单被标 CLOSED、入场记录残留 NEW（BNBUSDT 互斥永久误拦 + 告警 owner 恒 null）
日期: 2026-10-26
标签: [bug, 持仓归属, 互斥, trade_records, 终态标记, 全策略]
任务标识: 2026-10-26-bnb-false-mutex-owner
状态: 待复查
审查: 待审查
---

# 终态标记 SQL 缺 realized_pnl IS NULL 致平仓单被标 CLOSED、入场记录残留 NEW

## 现象

- 生产环境（2026-10-26）：MTPCS 激进版（`btc_eth_aggressive`）对 `BNBUSDT` 发出开仓信号，被「占用互斥」永久拦截，日志报
  `⚠️ BNBUSDT 开仓被占用互斥拦截（已被 其他策略 持有），跳过`，但币安 APP 账户内**并无 BNBUSDT 持仓**。
- 容器日志（UTC，两个周期连续复现）：
  - `2026-10-26T00:10:02.267`：`{"owner": null, "competing": ["btc_eth"], "event": "BNBUSDT 已被其他策略持有（占用互斥），跳过开仓"}`
  - `2026-10-26T02:10:01.862`：同上
- `trading.trade_records` 记录形态实证（`executed_at` 为北京时间 naive，容器日志为 UTC）：
  - id=4292：`strategy=btc_eth` / `side=BUY` / `order_type=LIMIT` / `status=NEW` / `realized_pnl=NULL` / `executed_at=2026-10-23 06:05:03`（**真正入场单**，残留）
  - id=4315：`strategy=btc_eth` / `side=SELL` / `order_type=LIMIT` / `status=CLOSED` / `realized_pnl=0.4344` / `executed_at=2026-10-23 20:05:00.945`（**平仓单**，被误标 CLOSED）
- 影响：合法开仓信号被误判为「已被 `btc_eth` 持有」而跳过（策略漏单）；且告警 `owner` 恒为 `null`、「其他策略」，运维无法定位实际拦路策略，排查被误导。

## 根因

两条并列（主因 + 观测缺口）：

1. **终态标记 SQL 与互斥判定 SQL 口径漂移（主因）**：`shared/position_ownership.py::_mark_entry_terminal_sql`（L34-61）构建
   「把最新非终态入场记录置终态」的 `UPDATE ... WHERE id = (SELECT ... ORDER BY executed_at DESC, id DESC LIMIT 1)`，
   其内层 SELECT 条件为
   `symbol=$1 AND strategy=$2 AND order_type IN ('LIMIT','MARKET') AND status NOT IN ('CANCELED','EXPIRED','CLOSED')`
   —— **缺 `realized_pnl IS NULL`**。平仓单本身也是 `LIMIT` 类型、先落库且先被 `update_realized_pnl` 回写，
   于是 `release_claim(reason='position_closed')` 选「最新」记录时命中的是**平仓单**并把它标 `CLOSED`，
   真正入场单永久残留 `status=NEW / realized_pnl=NULL`；而互斥判定 `_open_entry_sql(False)`（L147-175）口径为
   `order_type IN ('LIMIT','MARKET') AND realized_pnl IS NULL AND status NOT IN (终态)` → 幽灵入场单**永远满足**，BNBUSDT 被永久误判持有。
2. **冲突告警固定传 `owner=None`**：`shared/protection_retry.py::_acquire_entry_slot`（L648-693）归属互斥命中分支固定调用
   `strategy._handle_claim_conflict(symbol, None)`，而 `handle_claim_conflict` 文案退化为 `owner or '其他策略'` → 日志 `owner=null`。

## 解决

最小修复（两处，均在 `shared/`，策略文件零改动）：

1. `shared/position_ownership.py::_mark_entry_terminal_sql` 内层 SELECT 增加 `AND realized_pnl IS NULL`，与 `_open_entry_sql` 判定口径逐条对齐；派生常量 `_MARK_ENTRY_CLOSED_SQL` / `_MARK_ENTRY_CANCELED_SQL` 与 `_ENTRY_TERMINAL_SQL_BY_REASON` 映射不变。
2. `shared/position_ownership.py` 新增 `resolve_conflict_owner`（L473-519）：为冲突告警解析真实拦路策略名，内部短路顺序与
   `is_symbol_owned_by_other` **完全同序**（① 权威归属 `trade_records` → ② 占用表 `position_claims`），保证「判定 True ⇔ 返回非 None」；
   决策仍由 `is_symbol_owned_by_other` / `try_claim_symbol` 负责，本函数只做告警定位，异常一律返回 `None` 不抛异常。
3. `shared/protection_retry.py::_acquire_entry_slot` 新增参数 `resolve_owner_fn=resolve_conflict_owner` 并透传；冲突分支解析 owner 后传给
   `_handle_claim_conflict`，解析异常降级为 `None`（文案回落「其他策略」）且不中断主循环。
4. **明确不改**：`strategies/btc_eth/strategy.py`（L2748-2750）、`strategies/btc_eth_aggressive/strategy.py`（L2946-2948）调用点只传
   `is_owned_fn` / `try_claim_fn`，默认参数生效；`new_coin` / `grid` / `hrs` 不涉及该路径。

## 验证

- 单元测试：`tests/test_shared/test_ghost_entry_cleanup.py` 新增 3 个测试类（SQL 文本断言 + 伪 DB 行为断言：入场单 `realized_pnl=NULL` + 平仓单 `realized_pnl` 非空时 CLOSED 落到入场单；owner 解析成功/失败两路径）。
- 回归：相关 5 个测试文件 **283 passed**；本次修改的 4 个函数行/分支覆盖 **100%**。
- **部署前状态（2026-10-26）**：修复代码已在工作区完成并通过测试，生产部署与新代码运行验证待做。
  部署后按 spec AC1–AC5 验收：`BNBUSDT` 的 `_open_entry_sql(False)` 命中 0 行、下轮 `:10` 周期不再误拦、日志 `owner` 为真实策略名（如 `btc_eth`）而非 `null`。
- **2026-10-26 后续只读复核**：相关五个测试文件本地重跑 `209 passed`。生产库中历史记录 id=4051、4292 均已为 `CLOSED`，说明此前存量清理已落库；当时互斥查询命中的是新记录 id=4381（`btc_eth_aggressive`、`NEW`、`realized_pnl=NULL`），币安 PM 账户同币种实时持仓 `positionAmt=0.03`。因此不能把当时「查询命中 0 行」作为验收条件，也不能在真实仓位未平时再做整币种空仓清理；须等待该真实仓位平仓后复查。

## 适用范围与修订

- 适用于全部策略的 `shared/` 单点修复；直接受影响 `btc_eth` / `btc_eth_aggressive`（共享账户归属互斥），`new_coin` / `grid` / `hrs` 不受影响。
- 与既有条目的关系：**不替代** [ghost-entry-record-mutex](Binance_quantitative_trading-20261015-ghost-entry-record-mutex.md)（其根因为「未成仓不落终态」+ 归属名不一致，已修复）；本条目是**同一症状（BNBUSDT 互斥误拦）的第三处独立根因**——由前者 D2 扩展引入的单点构建器 `_mark_entry_terminal_sql` 的匹配口径缺陷所致。
- 决策侧不变：[entry-terminal-state-hookpoint](../decisions/Binance_quantitative_trading-20261015-entry-terminal-state-hookpoint.md) 的挂钩点选型（`release_claim` 而非 `update_realized_pnl`）与按 reason 查表均维持，本次仅修 SQL 匹配条件。
- 遗留风险（本次不修）：
  - 入场记录 `status` **成交后永不更新为 `FILLED`**（结构性缺陷）——依赖清理脚本 + 新 SQL 口径共同收敛，而非终态 SQL 回补；
  - 历史幽灵记录需靠 `scripts/fix_ghost_entry_records.py` 逐单对账兜底（须借空仓窗口，默认 dry-run）；
  - 仍只标「最新一条」非终态记录，同 symbol 更早残留保持 `NEW`（保守超拦）。
- 无替代条目。

## 教训

（AI 归纳仅供参考）

- 「标最新一条为终态」这类 `UPDATE ... WHERE id = (SELECT ... ORDER BY ... LIMIT 1)` 必须让**内层 SELECT 与下游判定 SQL 的每一子句对齐**。两处口径漂移时，标记会落在错误行上，而症状表现为「标记成功」（不报错、影响行数=1）→ 生产上极难发现，只在另一策略被误拦时才暴露。
- 判定函数已能解析 owner 时，**告警侧必须复用同一解析路径**（同序短路），不可另写一套或固定传 `None`；否则「有冲突」与「谁冲突」脱钩，运维只能看到「其他策略」。
- **排查方法（自查幽灵入场记录）**：用与 `_open_entry_sql(False)` 完全同口径的 SQL 直查生产库，命中行的 `strategy` 即「互斥 owner」；再对 `positionRisk` 核对实际持仓，无持仓即为幽灵：

```sql
-- 与 shared/position_ownership.py::_open_entry_sql(False) 同口径（不带 status='FILLED' 过滤）
SELECT id, strategy, side, order_type, status, realized_pnl, executed_at
FROM trading.trade_records
WHERE symbol = 'BNBUSDT'
  AND order_type IN ('LIMIT','MARKET')
  AND realized_pnl IS NULL
  AND status NOT IN ('CANCELED','EXPIRED','CLOSED')
ORDER BY executed_at DESC, id DESC;
```

- **收敛方式**：`scripts/fix_ghost_entry_records.py --symbol BNBUSDT`（默认 dry-run，`--execute` 才写库；须在该币种空仓窗口执行，分类器依赖币种级净持仓）。
- 时间口径提醒：`trade_records.executed_at` 为北京时间 naive，容器日志为 UTC，对账时勿直接比较。

## 关联

- 本地补充来源: [spec](../../specs/2026-10-26-bnb-false-mutex-owner/spec.md)（工作区未跟踪，未提交）
- 持久证据: `shared/position_ownership.py` `_mark_entry_terminal_sql`（L34-61）/ `resolve_conflict_owner`（L473-519）/ `_open_entry_sql`（L147-175）；`shared/protection_retry.py` `_acquire_entry_slot`（L648-693）；`tests/test_shared/test_ghost_entry_cleanup.py`（新增 3 个测试类）
- 必要证据摘要: `trade_records` id=4292（NEW / `realized_pnl=NULL`，10-23 06:05:03）与 id=4315（CLOSED / `realized_pnl=0.4344`，10-23 20:05:00.945）；日志 UTC `2026-10-26T00:10:02.267` / `02:10:01.862` `owner=null` / `competing=["btc_eth"]`；283 passed + 变更函数 100% 覆盖（工程师复测）；**未提交 / 未部署**，生产验证与上线结论待补
- 相关: [Binance_quantitative_trading-20261015-ghost-entry-record-mutex](Binance_quantitative_trading-20261015-ghost-entry-record-mutex.md)（同一症状的前序根因）、[Binance_quantitative_trading-20261015-entry-terminal-state-hookpoint](../decisions/Binance_quantitative_trading-20261015-entry-terminal-state-hookpoint.md)（终态标记挂钩点决策）、[Binance_quantitative_trading-20260922-shared-account-position-ownership](Binance_quantitative_trading-20260922-shared-account-position-ownership.md)（持仓归属判定主题）
