# 缺陷修复 Spec：BNBUSDT 幽灵入场记录误拦开仓 + 冲突告警 owner 恒为 null

- 日期：2026-10-26
- 类型：生产缺陷修复（最小修复）
- 影响范围：MTPCS btc_eth / btc_eth_aggressive 共享账户归属互斥

## 1. 缺陷描述与影响

2026-10-26 生产环境，MTPCS 激进版（btc_eth_aggressive）对 BNBUSDT 发出开仓信号，日志报
`⚠️ BNBUSDT 开仓被占用互斥拦截（已被 其他策略 持有），跳过`，但币安 APP 账户内并无 BNBUSDT 持仓。

**生产影响**：合法的开仓信号被「占用互斥」永久误判为「已被 btc_eth 持有」而跳过，导致策略漏单；
且告警 owner 恒为 `null`/「其他策略」，运维无法定位实际拦路策略，排查被误导。

## 2. 根因分析

### 根因 1：终态标记 SQL 缺 `realized_pnl IS NULL`，误把平仓单标记为 CLOSED（主因）

`shared/position_ownership.py:35-59` 的 `_mark_entry_terminal_sql(status)` 构建「把最新非终态入场记录置终态」的 UPDATE SQL，
其内层 SELECT 条件为（`shared/position_ownership.py:54-57`）：

`symbol=$1 AND strategy=$2 AND order_type IN ('LIMIT','MARKET') AND status NOT IN ('CANCELED','EXPIRED','CLOSED')`

**缺少 `realized_pnl IS NULL` 条件**。平仓单本身也是 `LIMIT` 类型、先落库，且先被回写 `realized_pnl`。
于是 `release_claim(reason='position_closed')`（`shared/position_ownership.py:541-542`）选中「最新」记录时，
命中的是平仓单并把它标成 `CLOSED`，真正的入场记录永久残留为 `status=NEW / realized_pnl=NULL`。

而互斥判定 `_open_entry_sql(False)`（`shared/position_ownership.py:144-172`）口径为：
`order_type IN ('LIMIT','MARKET') AND realized_pnl IS NULL AND status NOT IN (终态)`。
幽灵入场记录永远满足该条件 → BNBUSDT 被永久误判为「仍被 btc_eth 持有」。

**实测证据**（trade_records.executed_at 为北京时间 naive，容器日志为 UTC）：
- `trading.trade_records` id=4292：strategy=btc_eth，side=BUY，order_type=LIMIT，status=NEW，realized_pnl=NULL，executed_at=2026-10-23 06:05:03
- `trading.trade_records` id=4315：strategy=btc_eth，side=SELL，order_type=LIMIT，status=CLOSED，realized_pnl=0.4344，executed_at=2026-10-23 20:05:00.945
- 容器日志 UTC `2026-10-26T00:10:02.267` / `02:10:01.862`：`{"owner": null, "competing": ["btc_eth"], "event": "BNBUSDT 已被其他策略持有（占用互斥），跳过开仓"}`

### 根因 2：归属互斥命中的告警固定传 `owner=None`

`shared/protection_retry.py:638-656` 的 `_acquire_entry_slot` 中，归属互斥命中分支固定调用
`strategy._handle_claim_conflict(symbol, None)`（`shared/protection_retry.py:644`），
而 `handle_claim_conflict`（`shared/protection_retry.py:110-119`）告警文案退化为 `owner or '其他策略'`，
日志 `owner=null`，运维无法得知具体是哪个策略占着。

## 3. 修复范围（最小修复，不扩大）

| 文件 | 改动 |
|------|------|
| `shared/position_ownership.py` | `_mark_entry_terminal_sql` 内层 SELECT 增加 `realized_pnl IS NULL`，与 `_open_entry_sql` 判定口径对齐 |
| `shared/protection_retry.py` | `_acquire_entry_slot` 归属互斥命中时，把拦路策略的真实 owner 传入 `_handle_claim_conflict`，替换固定 `None` |

**明确不改**：`strategies/btc_eth/strategy.py`、`strategies/btc_eth_aggressive/strategy.py` **无需改动**。
两策略的 `_handle_claim_conflict(self, symbol, owner)` 已是纯委托（
`strategies/btc_eth/strategy.py:2777-2779`、`strategies/btc_eth_aggressive/strategy.py:2975-2977`），
owner 参数已透传至 `protection_retry.handle_claim_conflict`，且后者已有 `owner or '其他策略'` 兜底。

**存量清理**：对 BNBUSDT（当前空仓）执行一次写库清理；其余共享币种只做 dry-run 出报告。

## 4. 验收标准

- **AC1**：`_mark_entry_terminal_sql` 生成的 SQL 文本包含 `realized_pnl IS NULL`。
- **AC2**：构造「入场单 `realized_pnl=NULL` + 平仓单 `realized_pnl` 非空」场景，
  `release_claim(reason='position_closed')` 的 CLOSED 标记落到**入场记录**而非平仓单（SQL 文本断言 + 伪 DB 行为断言）。
- **AC3**：归属互斥命中时，告警与日志带**真实 owner**（如 `btc_eth`），不再是 `null`/「其他策略」；
  且 owner 解析失败时仍能降级为 `其他策略` 且不抛异常。
- **AC4**：历史幽灵入场记录清理后不再命中 `_open_entry_sql(False)`；若 BNBUSDT 当前有真实持仓，查询可命中该持仓的入场记录，须待其平仓归零后再验证命中 0 行。后续 :10 周期不再出现因历史幽灵记录导致的误拦。
- **AC5**：既有测试全绿，重点：
  `tests/test_shared/test_ghost_entry_cleanup.py`、
  `tests/test_position_ownership.py`、
  `tests/test_shared/test_position_ownership_regression.py`、
  `tests/test_mtpcs_fixes/test_r05_r06_mtpcs.py`（T16）。

## 5. 遗留风险

- 入场记录 `status` 在成交后永不更新为 FILLED（结构性缺陷），本次不修。
- 幽灵记录依赖清理脚本 + 新 SQL 口径收敛；已存在的历史幽灵记录需靠清理脚本兜底，而非终态 SQL 回补。

## 6. 非目标（非本次范围）

- 共享账户双策略并持。
- claim TTL 策略调整。
- 看板归属展示改造。
