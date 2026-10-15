---
id: spec-2026-10-15-ghost-entry-bnb-sol
类型: spec
项目: Binance_quantitative_trading
标题: 清除 BNBUSDT/SOLUSDT 幽灵入场记录互斥误拦 + 修补「未成交入场单不落终态」缺口
日期: 2026-10-15
风险: R3（生产交易系统，共享 PM 账户，涉及真实资金与跨策略互斥）
状态: 待批准
---

# 0. 复用与引用

本 spec **不重做**前一轮已完成的 D1–D4（提交 `fce42bf`，Run 74 已上线）。同一问题类的权威方案见：

- 方案（权威）：[fix-2026-10-14-ghost-entry-record-mutual-exclusion.md](file:///Users/yl/vscode/Binance_quantitative_trading/docs/plans/fix-2026-10-14-ghost-entry-record-mutual-exclusion.md) §1–§9
- 知识条目：[Binance_quantitative_trading-20261015-ghost-entry-record-mutex.md](file:///Users/yl/vscode/Binance_quantitative_trading/docs/knowledge/issues/Binance_quantitative_trading-20261015-ghost-entry-record-mutex.md)

本 spec 只覆盖**本轮新发现**：BNBUSDT/SOLUSDT 个案复核、「只标最新一条」结构性缺口、以及一处时区观察。

---

# 1. 目标 / 非目标

## 目标

- G1：解除 btc_eth 原版对 BNBUSDT 的**幽灵**互斥误拦（实证：无任何真实持仓）。
- G2：对 SOLUSDT 与交易所逐单对账，按事实处置其互斥来源记录（真仓→保留，幽灵→终态化）。
- G3：给出「平仓/未成交后入场记录仍为 NEW」**残留复发的根因**与修补方案。

## 非目标

- 不修改「同币互斥」策略本身（互斥语义正确，问题在数据）。
- 不调整 btc_eth `ownership.competing_record_names`（当前 `["btc_eth_aggressive"]` 正确）。
- 不改 `executed_at` 的北京时间口径（属设计决定，见 E5；本轮不修，避免历史数据 8h 错位）。
- 不在服务器上运行回测。
- 不重做 §0 引用的前一轮改动。

---

# 2. 实证发现（2026-10-15，生产库只读查询）

互斥判定链：btc_eth 开仓前 `is_symbol_owned_by_other` → `_resolve_owner_with_lock` → `_open_entry_sql(False)`：
`trading.trade_records` 中 `order_type IN ('LIMIT','MARKET') AND realized_pnl IS NULL AND status NOT IN ('CANCELED','EXPIRED','CLOSED')` 取 `executed_at DESC, id DESC` 最新一条的 `strategy` 作 owner；btc_eth 的 `competing_record_names=["btc_eth_aggressive"]`，命中即拦。

- **E1（BNB 确为误拦）**：BNBUSDT 命中 id=**3773**（`btc_eth_aggressive`, LIMIT, NEW, `realized_pnl` NULL, 2026-10-09 10:10）。`trading.strategy_open_positions` 无 BNBUSDT 行；用户币安 APP 亦无该持仓 → **幽灵记录**。
  注：BNB 较新的 3989/3986/3918 因 `realized_pnl` 非 NULL 被同一 SQL 排除；3773 是唯一命中行。
- **E2（SOL 需对账）**：SOLUSDT 命中 id=**4026**（`btc_eth_aggressive`, LIMIT, NEW, order_id=249454901842, 2026-10-15 03:10:03Z）。同轮还写入条件单 4027/4028/4029；`strategy_open_positions` 有 `btc_eth_aggressive→SOLUSDT 0.52`（updated 03:10:06Z）；aggressive 日志 03:10:01Z「震荡入场通过」→ 03:10:02Z 生成信号（score 78, grade A）→ 03:10:03Z 下限价单 0.52@102.74 → 03:10:05~06Z 下 STOP/TP1/TP2。
  **结论待定**：若该限价单已成交则 SOL 为**真仓**（互斥正确，不应清）；若未成交/已撤则与 BNB 同类。**必须与交易所逐单对账后决定**（不由本地推断）。
- **E3（CLOSED 标记功能正常，非「失效」）**：同一 03:10:00Z 轮次，SOL 旧仓（previous_quantity 0.41）经「交易所返回空持仓列表，视为已平仓」→ `release_claim(position_closed)` 已成功将 id=**4002** 标为 `CLOSED`。⇒ 前一轮 D3.2 的标记链路**有效**，「标记失效」假设**不成立**。
- **E4（结构性缺口，复发根因）**：`release_claim` 仅在 `reason=='position_closed'` 时标 CLOSED，且**只标最新一条**；而入场未成仓路径（`open_failed` / `open_exception` / `micro_zeroed`）**完全不落终态** → 已下单但未成交/被撤的入场记录永久 NEW（E1 的 3773 即此类）。另外「只标最新」使得同一 symbol 的**更早**残留永不回收。
- **E5（时区观察，非本轮修复项）**：`trade_records.executed_at` 按**北京时间（UTC+8，无时区）**写入（`shared/trade_logger.py` 第 240/274 行，`datetime.now(BEIJING_TZ)`），而 `position_claims` 用 DB `now()`（UTC）→ 跨表时间不可直接比较（实证：4026 事件发生于 03:10:03Z，落库为 11:10:03）。**同表内一致，排序不受影响**，故非 CLOSED 失效原因。

---

# 3. 根因

- **RC1（本轮直接根因）**：入场单「下单即写 NEW、未成仓也不落终态」。`protection_retry.py` 在 `open_failed`/`open_exception`/`micro_zeroed` 分支只释放占用（`_release_claim_if_no_position` → `release_claim(reason=...)`），**不把已下但未成交的入场记录置终态** → 每条此类尝试留一条永久 NEW 记录，随时间累积成幽灵，命中互斥 SQL。
- **RC2（放大因子）**：`release_claim(position_closed)` 的 CLOSED 标记只处理**最新一条**非终态记录，同 symbol 更早的残留永不回收（E1 的 3773 属于被遗忘的一批）。
- **RC3（观测缺口）**：CLOSED 标记失败/命中 0 行时**静默**（`release_claim` 未解析 UPDATE 影响行数）→ 无法从日志发现残留。

---

# 4. 改动设计

## D1 存量清理（运维动作，复用既有脚本 + 一处加固）

- 复用 [scripts/fix_ghost_entry_records.py](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/fix_ghost_entry_records.py)（四分类 + 逐单交易所对账 + dry-run 默认）。
- **加固（小改）**：`parse_args` 增加 `--symbol`（可重复）与 `--strategy` 过滤；`run_audit` 用**参数化** SQL 追加 `AND symbol = ANY($n)` / `AND strategy = ANY($n)`（禁止字符串拼接），使处置可**分币种受限**执行、可复跑幂等。
- 处置顺序：**先 dry-run 全量输出**（含 SOL 4026 的交易所状态判定）→ 人工核对分类 → 再 `--execute --symbol BNBUSDT`（必要时并 `SOLUSDT`）。
- **安全闸（硬约束）**：只对 `CANCELED` / `CLOSED` 两类落库；`KEPT`（交易所 FILLED 且持仓非 0）与 `LIVE`（交易所仍为活单）**一律不动**。即：SOL 若为真仓或活单 → 保留，互斥继续生效（属正确行为，非缺陷）。

## D2 「未成交入场单落终态」修补（共享模块，改前需单独批准）

- 文件：`shared/position_ownership.py` · 函数 `release_claim`（第 516–572 行）。
- 变更：把终态标记的触发 reason 从仅 `{'position_closed'}` 扩展为 `{'position_closed', 'open_failed', 'open_exception', 'micro_zeroed'}`；`position_closed` → 置 `CLOSED`，其余三者 → 置 `CANCELED`（入场单未成仓/被撤，语义正确）。两者均为终态，均被互斥 SQL 排除。
- 复用既有 `_MARK_ENTRY_CLOSED_SQL` 结构，新增 `_MARK_ENTRY_CANCELED_SQL`（同构：`SET status='CANCELED'` + 同一子查询），**不复制**子查询文本以外逻辑。
- 生效前提（安全）：`protection_retry.release_claim_if_no_position`（第 122–129 行）已有 `if symbol in positions: return`（未登记持仓才释放）→ 真仓不会被误标。
- ⚠️ 该函数为 4 个策略共享（btc_eth / btc_eth_aggressive / new_coin / hrs 均 import）→ **改动影响面广，列为需单独批准的批准项（§8 P2）**。若不批准，本轮仅执行 D1 + D3，复发风险以「定期跑 D1」兜底。

## D3 观测加固（低风险，随 D1/D2 一并）

- 文件：`shared/position_ownership.py` · `release_claim`：用既有 `_parse_update_count` 解析终态标记 UPDATE 的返回，命中 0 行时记 `logger.warning("持仓归零：未匹配到可标记入场记录", symbol=..., strategy=...)`（当前为静默）。
- 不改变行为，仅增加可观测性，便于运维发现残留。

---

# 5. 验收标准（AC）

- **AC1**：`_open_entry_sql(False)` 对 BNBUSDT 命中数 = 0；且 btc_eth 下一次 `:05` 轮次对 BNBUSDT 不再推送「开仓被占用互斥拦截」。
- **AC2**：SOLUSDT 命中行处置与交易所事实一致——`KEPT`/`LIVE` → **保留**（互斥继续，判定为正确行为）；`CANCELED`/`CLOSED` → 命中数 = 0。
- **AC3**：`--execute` 后，`KEPT`/`LIVE` 类记录的 `status` **逐条未变**（SQL 复检前后快照一致）。
- **AC4**（D2 若批准）：新增单测覆盖——`reason='open_failed'/'open_exception'/'micro_zeroed'` 触发 CANCELED 标记；`positions` 非空时不标记（守卫生效）；`position_closed` 行为不回归。
- **AC5**：`pytest` 相关用例全绿；改动函数行/分支覆盖 100%（沿用前一轮口径）。
- **AC6**：部署走 push main → Actions；五层验证口径（容器状态 → 镜像 ID → 本轮构建容器 VERSION DEPLOY_ID → 关键文件 MD5 → 部署日志无错）。

---

# 6. 测试计划

- 单测：`tests/test_shared/test_ghost_entry_cleanup.py` 增补 D2/D3 用例；`--symbol` 过滤参数化用例。
- 脚本级：dry-run 对 `--symbol BNBUSDT` 输出来自交易所的逐单判定（人工核对）。
- 数据级：AC1/AC2/AC3 的 SQL 前后对比（`SELECT id,status FROM trading.trade_records WHERE ... `）。
- 禁降级：AC1–AC6 不得以「改需求/放宽门槛」替代。

---

# 7. 发布与回退

- 发布：`D1` 为**服务器运维动作**（无需发版）；`D2/D3` 若批准 → push main → Actions 构建部署 → 五层验证。
- 回退：D1 幂等且只置终态（可人工按 id 还原为 NEW）；D2/D3 代码回退用 `git revert` + push。
- 顺序：**先 D1（立即解 G1/G2）→ 再评估 D2/D3**。

---

# 8. 风险与待批准项

- **P1（必须批准）**：对生产库 `trading.trade_records` 执行 `--execute` 写终态（仅 CANCELED/CLOSED 两类，KEPT/LIVE 不动）。
- **P2（单独批准）**：D2 修改共享 `release_claim` 语义（影响 4 策略）。不批准则退化为「D1 + 定期清理」。
- 风险：误标真仓 → 由「逐单对账 + KEPT/LIVE 硬闸 + AC3 快照校验」三重兜底。
- R3 任务：**GA 放行前须完成独立设计复核（A-review）**。

---

# 9. 关键文件 / 函数索引

- `shared/position_ownership.py`：`_open_entry_sql`(L144)、`_mark_entry_terminal_sql`(L35)、`_MARK_ENTRY_CLOSED_SQL`/`_MARK_ENTRY_CANCELED_SQL`(L65-66)、`_ENTRY_TERMINAL_SQL_BY_REASON`(L67)、`release_claim`(L545)、`is_symbol_owned_by_other`、`_parse_update_count`
- `shared/protection_retry.py`：`release_claim_if_no_position`(L122)、L595/617/646（`open_failed`/`open_exception`/`micro_zeroed` 调用点）
- `shared/trade_logger.py`：L240/L274（`executed_at` 北京时区写入）
- `scripts/fix_ghost_entry_records.py`：`_CANDIDATE_SQL`、`build_candidate_query`、`classify_entry`、`audit_entry`、`run_audit`、`parse_args`
- `strategies/btc_eth_aggressive/strategy.py`：L5154、L6073（`release_claim(position_closed)`）、L2694（`_release_claim_if_no_position`）
- `strategies/btc_eth/config.yaml` L722-727（ownership 段）
- 表：`trading.trade_records`、`trading.position_claims`、`trading.strategy_open_positions`

---

# 10. 执行记录与偏差（2026-10-15）

## 10.1 T3 dry-run 逐单对账（服务器 btc_eth 容器内，只读）

候选集 `--symbol BNBUSDT --symbol SOLUSDT` → 55 条 `status='NEW' AND realized_pnl IS NULL` 入场记录。

交易所实证（`get_order` 原始返回 + `get_position`）：

- **BNBUSDT 8 单**（94402177995 / 94689255547 / 95512572247 / 95557406062 / 95660060591 / 95830469329 / 95937247823 / 96733987860）全部 `status=FILLED`；BNBUSDT 当前**无持仓行**（净持仓 0）→ 分类 **CLOSED**。
- **SOLUSDT 249454901842**（记录 4026）`status=FILLED executedQty=0.52`；SOLUSDT 当前持仓 `positionAmt=0.52 / entryPrice=102.74` → 分类 **KEPT**。

结论：

- E1 复核成立：BNB 8 条是「真实成交、事后已了结」却被互斥口径当作未平仓的幽灵记录。
- E2 定性完成：SOL 4026 为**真仓**（激进版 03:10Z 开 0.52 多单 @102.74，仍持有）→ btc_eth 被互斥拦截属**正确行为**，非缺陷；AC2 走「保留」分支。
- 另有 47 条 SOL 记录（多为 btc_eth 历史记录）因 SOL 当前有持仓被判 KEPT → 一律保留不动（保守超拦，安全）。

## 10.2 偏差记录（新增任务 T2b）

T3 首轮 dry-run 暴露脚本分类器缺陷：`audit_entry` 在「持仓查询成功、但返回列表中无该币种行」时把 `position_amt` 留为 None → `classify_entry` 判 **SKIP**，导致 FILLED+已平仓的历史记录永不收敛（8 条 BNB 首轮全部 SKIP，`--execute` 将无任何落库）。

修复：持仓查询成功即把 `position_amt` 初值置 `0.0`（仅查询异常才 None→SKIP），并补 4 例单测 `TestT2bAuditEntryPositionResolution`。修复后同口径 dry-run：BNB 8 × CLOSED、SOL 47 × KEPT、SKIP = 0。

## 10.3 T4/T5 执行与复检（P1 已批准）

- 执行前快照：BNB `btc_eth NEW=26 / aggressive NEW=8`，mutex owner = `btc_eth_aggressive`；SOL `btc_eth CANCELED=28/NEW=37`、`aggressive CANCELED=2/CLOSED=1/NEW=43`，mutex owner = `btc_eth_aggressive`。
- `--execute --symbol BNBUSDT` → `落库完成：CANCELED=0，CLOSED=8`。
- 执行后复检：
  - **AC1**：BNB `_open_entry_sql(False)` 命中为空（无 owner）→ 下一 :05 轮次 btc_eth 对 BNBUSDT 不再互斥拦截。
  - **AC2**：SOL 分布与执行前逐项一致（28/37/2/1/43），SOL 4026 仍为 `NEW` → 真仓互斥继续生效（保留）。
  - **AC3**：仅 8 条 BNB 目标行 status 变为 `CLOSED`（id 1699/1795/2307/2354/2465/2778/2891/3773），其余 NEW 行逐条未变。

## 10.4 D2/D3 实现（P2 已批准）

`shared/position_ownership.py`：

- 新增 `_mark_entry_terminal_sql(status)` 单点构建器（终态列表 / 订单类型均引用既有常量，杜绝重复 SQL 文本）；
  `_MARK_ENTRY_CLOSED_SQL = _mark_entry_terminal_sql('CLOSED')`、`_MARK_ENTRY_CANCELED_SQL = _mark_entry_terminal_sql('CANCELED')`。
- 新增 `_ENTRY_TERMINAL_SQL_BY_REASON`：`position_closed → CLOSED`；`open_failed / open_exception / micro_zeroed → CANCELED`；未命中映射（如 `released` / `authority_conflict`）不标记。
- `release_claim` 改为查映射表执行终态标记，并用 `_parse_update_count` 解析影响行数，命中 0 行时 `logger.warning`（D3 观测）。

测试：`tests/test_shared/test_ghost_entry_cleanup.py` 70 passed（含新增 `TestT2BuildCandidateQuery` 4 例、`TestT2bAuditEntryPositionResolution` 4 例、扩 reason 3 例、D3 告警 1 例、`TestAC4ReleaseClaimGuard` 2 例；原 `test_open_failed_does_not_mark_entry_closed` 已按新语义改为 `test_open_failed_marks_entry_canceled`）。
