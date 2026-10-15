---
id: Binance_quantitative_trading-20261015-ghost-entry-record-mutex
类型: issue
项目: Binance_quantitative_trading
标题: 幽灵 NEW 入场记录 + 归属名不一致致跨策略开仓误拦（SNPSUSDT 案例）
日期: 2026-10-15
标签: [bug, 持仓归属, 互斥, trade_records, 全策略]
任务标识: ghost_entry_mutex_fix_2026-10-14
状态: 已解决
审查: 待审查
---

# 幽灵 NEW 入场记录 + 归属名不一致致跨策略开仓误拦

## 现象
- 用户报告（2026-10-14）：`SNPSUSDT 总分7.9 [入场失败: 该币种已被其他策略持有，跳过开仓]`，但实际无任何真实持仓。
- 生产库实证（2026-10-14）：trade_records id=3923（SNPSUSDT/new_coin/status=NEW/qty=0/price=0，10-13 19:03）为下单自动记录、LIMIT 单 60 秒超时撤单后未清理的幽灵记录；position_claims 最近 4 次尝试全部 `authority_conflict` 秒释放。
- 存量规模：trade_records 入场类记录 864 条全部 status=NEW、0 条终态（new_coin 101 / hrs 147 / btc_eth 229 / aggr 103），绝大多数为同类幽灵；且开仓单**成交后也永不更新 FILLED**（结构性缺陷）。

## 根因
三因叠加：
1. `shared/binance_api.py` 下单成功即自动 `log_order` 写 trade_records（status=NEW, qty=0），撤单后无人清理 → 幽灵记录永久残留；
2. 互斥 B 路径 `_open_entry_sql` 以 `order_type IN ('LIMIT','MARKET') AND realized_pnl IS NULL` 判「未平开仓单」——但 `update_realized_pnl` 回写目标是**平仓单自身行**而非入场记录，入场记录 realized_pnl 结构性永不回写 → `IS NULL` 不能表达「未平」；
3. new_coin config `record_name: "新币做空策略"` vs TradeLogger 写库 `"new_coin"`（main.py:107 硬编码）→ 自家幽灵记录被判定为「其他策略」→ authority_conflict 拦截。

## 解决
提交 `fce42bf`（2026-10-14，Run 74 部署）：
- **D1** `strategies/new_coin/config.yaml:8` record_name → `"new_coin"`（消除归属名不一致）；
- **D2** `shared/position_ownership.py` `_open_entry_sql` 追加 `AND status NOT IN ('CANCELED','EXPIRED','CLOSED')`（互斥口径改为「最新非终态入场记录」）；
- **D3** 撤单/平仓两个单点写终态：`binance_api.cancel_order` 撤单成功且 `status=='CANCELED' AND executedQty==0`（缺 executedQty 键不标记，F5 加固；-2011 经 get_order 复查）时经 `TradeLogger.mark_entry_canceled` 标记（仅 NEW→CANCELED，命名避开 condition_orders.py:123 同名函数）；`release_claim(reason='position_closed')` 持仓归零确认时标最新非终态入场记录为 CLOSED（不在 update_realized_pnl 挂钩，避免 TP1/TP2 部分平仓误标）；
- **D4** `scripts/fix_ghost_entry_records.py` 历史清理（四分类：CANCELED/不存在(-2011,-2013 同类，F4 加固)→置 CANCELED；FILLED+持仓0→CLOSED；FILLED+持仓→保留；异常→SKIP，dry-run 默认）。

**本轮增量修补（2026-10-15，提交 `8071f06`，Run 76 已部署）**：用户续报 btc_eth 推送 BNBUSDT/SOLUSDT 开仓被互斥拦截但币安 APP 无其他策略持仓。逐单对账定性：BNBUSDT 为「真实成交、事后已了结」的幽灵记录（E1，id=3773 / strategy=btc_eth_aggressive / status=NEW，8 笔订单全 FILLED 而 `positionRisk` 净持仓 0）；SOLUSDT 经时点重建**亦为幽灵误拦**（E2 更正：11:05 告警时点 owner = id=3926（`2026-10-13 21:10:02` 幽灵）；真仓 id=4026 系 11:10 才开、**晚于告警**，故「SOL 拦截正确、非缺陷」结论**已废止**）；「终态标记失效」假设被 E3 证伪（旧 SOL id=4002 已正确 CLOSED）；真正根因（E4）是 `release_claim` 原先仅 `reason='position_closed'` 落终态，`open_failed`/`open_exception`/`micro_zeroed` 三条「未成仓」reason 完全不落终态 → 下单即写的 NEW 记录永久残留，且命中 0 行时静默无观测。

- **D2 扩展** `shared/position_ownership.py`：新增单点构建器 `_mark_entry_terminal_sql(status)`（复用 `_ENTRY_ORDER_TYPES`/`_TERMINAL_ENTRY_STATUSES`），派生 `_MARK_ENTRY_CLOSED_SQL`/`_MARK_ENTRY_CANCELED_SQL`；新增映射 `_ENTRY_TERMINAL_SQL_BY_REASON`（position_closed→CLOSED、open_failed/open_exception/micro_zeroed→CANCELED）；`release_claim` 改为按 reason 查表决定终态（未命中不标记），解析 `"UPDATE n"` 影响行数（`_parse_update_count`），命中 0 行时 `logger.warning("未匹配到可标记终态的入场记录")`；标记失败仅告警不影响占用释放。
- **D3 分类器修复（T2b）** `scripts/fix_ghost_entry_records.py`：`build_candidate_query` symbol/strategy 过滤一律 `= ANY($n)` 参数化 + 新增 CLI `--symbol`/`--strategy`（`action="append"`）；`audit_entry`「`get_position` 查询成功但无该币种行」由 `position_amt=None`（被判 SKIP）改为查询成功即初值 `0.0`（无该币种持仓行 = 持仓归零），仅查询异常才保留 None（保守 SKIP）。

## 验证
- 测试：124 passed（含 47 个清理脚本/终态标记用例），变更函数 100% 行/分支覆盖（GA/GB/GC 三关通过）。
- 部署五层验证：Run 38570925290 全绿；13 容器 healthy；11 容器 VERSION DEPLOY_ID=00004A01 一致；4 关键文件 MD5 与本地一致；deploy_logs `DEPLOY_SUCCESS deploy_id=00004A01`。
- 数据清理（2026-10-15，new_coin 容器内执行）：候选 582 → CANCELED 240 / CLOSED 0 / KEPT 49（真实持仓正确保留）/ SKIP 293；落库后 **SNPSUSDT 互斥命中数=0**（G1 达成）。

**本轮增量验证（2026-10-15，提交 `8071f06`，Run 76）**：

- 测试：`tests/test_shared/test_ghost_entry_cleanup.py` **70 passed**；互斥相关 4 文件（test_ghost_entry_cleanup / test_position_claims / test_newcoin_hrs_fixes/test_r03_r04 / test_mtpcs_fixes/test_r05_r06_mtpcs）隔离跑 **153 passed**；`tests/test_strategies/test_mtpcs_v628_add_position.py` 隔离跑 **98 passed**。全量套件出现的 **99 failed / 325 errors** 经隔离跑判定为「事件循环污染」的既有测试隔离问题，**非本次改动引入**。
- T3 dry-run（btc_eth 容器内，`--symbol BNBUSDT --symbol SOLUSDT`）：BNB 8 条 → CLOSED / SOL 47 条 → KEPT / SKIP=0。
- T4 写库（用户已批准 P1，`--execute --symbol BNBUSDT`）：`落库完成：CANCELED=0，CLOSED=8`；执行后 BNBUSDT 互斥 owner **变空**（`_open_entry_sql(False)` 命中 0）；SOL 记录分布逐项与执行前一致（4026 仍为该策略真仓记录）；仅 8 条目标行（id 1699/1795/2307/2354/2465/2778/2891/3773）→ CLOSED。
- 部署五层验证（Run 76，2026-10-15 15:08 北京时间）：11 容器 `ghcr.io/bearflower/trading-*:latest` healthy；11 容器 `/app/VERSION` 全为 `GIT_SHA=8071f0696f92d89c97b5f25fe00339f6259b795d` / `DEPLOY_ID=00004C01`；容器内 `shared/position_ownership.py` MD5 = `d842463fe38d792669820b56b1fa4ff4` = 本地 `md5 -q`；`DEPLOY_SUCCESS deploy_id=00004C01` 与容器 VERSION 一致。
- **AC1b 已观测通过（2026-10-15 北京时间 16:05 轮次）**：btc_eth 该轮对 5 个币种（BTC/ETH/BNB/SOL/XRP）完成分析，全量日志**互斥相关条数 = 0**；5 币均在信号阶段被拒（「量价确认拒绝：成交量未缩量」/ETH「趋势过滤」），**未走到互斥判定**（弱证据）。因果补强：SQL 直查互斥口径 `BNBUSDT` 命中数 = **0**（owner 为空）；`SOLUSDT` owner（**当前态**）= `btc_eth_aggressive` id=4037（16:10:05 新开）——此为**当前态**，**不可**回溯证明 11:05 告警正确：11:05 时点 owner 实为幽灵 id=3926（见方案 §10.9 更正）。

## 适用范围与修订
- 适用于全部策略（shared/ 单点修复）；基线 `fce42bf`（首轮）→ `8071f06`（本轮增量）。
- 修订（2026-10-15，`8071f06`）：`release_claim` 终态挂钩点 reason 范围由「仅 `position_closed`→CLOSED」扩展为「`position_closed`→CLOSED + `open_failed`/`open_exception`/`micro_zeroed`→CANCELED」（三条均为「未成仓」语义，见 [entry-terminal-state-hookpoint 决策](../decisions/Binance_quantitative_trading-20261015-entry-terminal-state-hookpoint.md)）；清理脚本新增 symbol/strategy 参数化过滤（`--symbol`/`--strategy`）与「`get_position` 查询成功即持仓归零（`0.0`）」分类口径，收敛「FILLED+已平仓」历史记录。
- 结论**已更正**（防止后续误保留错误结论）：**SOLUSDT 11:05 的互斥拦截同属幽灵记录误拦（与 BNB 同根因），是缺陷**。原「SOL 为真仓、拦截正确」结论系**用当前态回溯历史事件**所致，**已废止**；真仓 id=4026 系 11:10 开仓、**晚于 11:05 告警**，11:05 时点 owner = id=3926（`2026-10-13 21:10:02` 幽灵）。**存量清理已执行（2026-10-15 17:32 北京时间，用户手动平仓触发空仓窗口）**：SOL 当时由激进版真仓持有（`positionAmt=1.57`）→ dry-run 呈 no-op（49 条全 KEPT，安全闸），用户平仓后空仓窗口 dry-run `CLOSED 49` → `--execute --symbol SOLUSDT` `CLOSED=49`，复检互斥 SQL 命中数 = 0，49 条逐条 `CLOSED`（旧「44 条 aggr NEW 待清理」已清零）。**注**：清理须借空仓窗口（分类器依赖币种级净持仓），非空仓时无法收敛。
- 遗留：SKIP 293 条保守未处理（多为已下架币对 positionRisk 查不到 / REJECTED 状态），new_coin 记录因 D1 后自家放行、btc_eth 记录为大币无跨策略重叠，实际误拦风险极低；如需收敛另立任务。
- 已知保守缺口：new_coin/hrs 平仓路径无 `release_claim(position_closed)` 调用点（仅 btc_eth/aggr 有）→ 该两策略已平仓入场记录维持 NEW 保守超拦，无资金风险，由 D4 存量清理补偿。
- 无替代条目。

## 教训
（AI 归纳仅供参考）
- 「订单状态列」的默认值语义会被下游查询当业务不变量使用——下单自动写 NEW 却无终态回写路径时，任何以 `status='NEW'` 或 `realized_pnl IS NULL` 推断「未平仓」的 SQL 都会随时间积累误判；写库点与终态回写点必须成对设计。
- 归属/互斥类判定对「名字字符串」高度敏感：config 显示名与写库名必须同源（一处定义），新策略接入时按 `main.py` TradeLogger 实参核对。
- 数据清理脚本必须逐单与交易所对账且 dry-run 默认；错误码 -2011/-2013 同归「订单不存在」处理。

## 关联
- 本地补充来源: [handoff](../../handoffs/ghost_entry_mutex_fix_2026-10-14.md)（不随 Git 同步）
- 本轮增量来源: [handoff](../../handoffs/ghost_entry_bnb_sol_2026-10-15.md)（不随 Git 同步）
- 持久证据: 提交 `fce42bf`（shared/binance_api.py cancel_order 判据、shared/position_ownership.py `_open_entry_sql`/`release_claim`、shared/trade_logger.py `mark_entry_canceled`、scripts/fix_ghost_entry_records.py）；方案 `docs/plans/fix-2026-10-14-ghost-entry-record-mutual-exclusion.md`（§9 修订权威 + §9.8 GC 记录）
- 本轮增量证据: 提交 `8071f06`（shared/position_ownership.py `_mark_entry_terminal_sql`/`_ENTRY_TERMINAL_SQL_BY_REASON`/`release_claim` 按 reason 查表、scripts/fix_ghost_entry_records.py `build_candidate_query`/`audit_entry` 分类修复、tests/test_shared/test_ghost_entry_cleanup.py）；Run 76 `DEPLOY_ID=00004C01`；方案 §10
- 必要证据摘要: 124 passed（pytest，主会话+C 复审独立复跑）；Run 74 五层验证通过；容器内 execute 后 `SELECT count(*) ... status NOT IN (终态)` 对 SNPSUSDT = 0；本轮 70/153/98 passed（隔离跑）、BNB 8 条 CLOSED、SOL 47 条 KEPT、Run 76 五层验证（DEPLOY_ID=00004C01）
- 相关: [Binance_quantitative_trading-20260922-shared-account-position-ownership](Binance_quantitative_trading-20260922-shared-account-position-ownership.md)（持仓归属判定主题）、[Binance_quantitative_trading-20261009-hrs-cancel-not-persisted](Binance_quantitative_trading-20261009-hrs-cancel-not-persisted.md)（撤单未回写 DB 同类模式）
