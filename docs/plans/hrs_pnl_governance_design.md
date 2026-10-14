# HRS 盈亏数据治理 + income 对账（设计文档）

- 任务标识：hrs_pnl_governance
- 阶段：A 段（需求与设计）
- 日期：2026-10-14
- 版本：v2（修订后，待 A-review 复检）
- 上游 handoff：docs/handoffs/hrs_pnl_governance_2026-10-14.md
- 前置 issue：docs/knowledge/issues/Binance_quantitative_trading-20261013-hrs-pnl-double-count.md
- 风险定级：R3（资金记账写入层 + 月度分配口径）

## 修订记录（v2）

| 编号 | 阻塞项 | 修订内容 | 落点 |
|------|--------|----------|------|
| B1 | 根因清单遗漏第三条平仓路径 | 新增 H6（时间止损/移动止盈只写明细不写汇总）；9 月 114 行按平仓路径分解后定修复范围 | §0.1、§2.2、§2.3 |
| B2 | income 分页截断风险 | 对账脚本强制分片分页 + 完整性断言；补入风险表 | §1.2、§1.5 |
| B3 | 回填行字段语义与回滚定位 | 明确回填行 `executed_at` 规则与批次标记约定（`order_id` 存批次号）+ 审计回记主键 | §2.4、§5 |
| B4 | income symbol 归属纯净性 | 对账第一步加 symbol 交集只读断言；逐笔策略归属规则 | §0.1、§1.3、§1.4 |
| S1 | income 粒度验证 | 补「已知持仓 income 条数 vs 明细行条数」抽样核对 | §4.2 |
| S2 | 量化容差断言 | 回填后持仓级口径月度总和 vs income 聚合差异 ≤ 配置容差 | §4.2 |
| S3 | H3 重启注入测试 | H3 假设补重启注入测试步骤 | §2.2 |

---

## 0. 背景与范围

**已核实的证据链（本设计的事实基础）**：

1. `shared/trade_logger.py` 双路径记账并存（全库仅 HRS 有 `PNL_SUMMARY` 汇总行 75 条）：
   - `update_realized_pnl`：每笔部分平仓（TP1/TP2/止损）的盈亏 **UPDATE 回填**到已有明细行；
   - `insert_pnl_summary`：整笔持仓全部平仓后 **INSERT** 一条 `PNL_SUMMARY` 汇总行，金额 = 各部分之和。
2. **汇总行漏写（本任务核心缺陷）**：2026-09 有 114 行明细属于"从未写过汇总"的 (symbol, side) 组，合计 `-33.71`。铁证 BTRUSDT：9/1 两组 LIMIT→TP 结构，2 笔 TP 真实成交（-9.8343 / -10.7540），**无任何 PNL_SUMMARY 行**。
3. 按天 `BOOL_OR` 防双计对 HRS 失效：`dashboard/backend/services/data_service_docker.py` `_DAILY_PNL_QUERY`（L104-114）按 `DATE(executed_at)` 分组，跨天平仓持仓仍被双计。
4. `trade_records` 无持仓 ID 字段，任何纯 SQL 去重都有边界错误。
5. 净值触 0 → 回撤 100%：`_build_strategy_daily_returns`（L2597-2680）中 `equity=base_capital+cum_pnl` 多日 <0 被钳 0（L2669-2672）。
6. 全部 symbol 已由用户确认为真实币安交易对。

### 0.1 v2 数据任务证据（只读连生产库，2026-10-14）

**① trade_records 列清单（14 列）**：`id, strategy, symbol, order_id(VARCHAR,可空), side, order_type, quantity, price, commission, status, executed_at(timestamp 无时区), realized_pnl, close_reason(VARCHAR,可空), algo_id(VARCHAR,可空)`。**无 `client_order_id`、无批次字段、无持仓 ID**（可分解平仓路径的字段仅 `order_type` + `close_reason` + `order_id` + `algo_id` + `side`）。

**② 9 月 114 行无汇总明细分解**（口径：9 月窗口内 `(symbol,side)` 分组 `bool_or(PNL_SUMMARY)` 为假组，明细行；精确复现 handoff 的 114 行 / -33.7145）：

| order_type | close_reason | 行数 | 金额 |
|------------|--------------|------|------|
| LIMIT | 空 | 63 | -1.3402 |
| CONDITIONAL_ORDER | 空 | 20 | +0.9789 |
| TAKE_PROFIT | 空 | 15 | -30.9864 |
| STOP_MARKET | 空 | 14 | +2.6610 |
| CONDITIONAL_ORDER | STOP_LOSS | 1 | -2.6450 |
| TAKE_PROFIT | STOP_LOSS | 1 | -2.3828 |
| **合计** | | **114** | **-33.71** |

> 分解含义：无汇总组的 114 行里，**99.9% 的亏损来自无 `close_reason` 的 TAKE_PROFIT(-30.99) + LIMIT(-1.34)**。这些正是 `_finalize_close_if_filled → _writeback_pnl` 路径（时间止损/移动止盈/TP 达标减仓）回填的明细行——**只 UPDATE 不 INSERT 汇总**（H6）。`LIMIT`（开仓单）与 `CONDITIONAL_ORDER`（条件单创建记录）都带 `realized_pnl`，说明 `update_realized_pnl` 的降级匹配（fallback 模式匹配 `order_id IS NULL` 行）存在**错行回填**，但这属"明细金额准确性"次生问题，回填汇总后口径以汇总优先、不受其影响。两条 `close_reason=STOP_LOSS` 行是止损打标路径写入。

**③ symbol 交集断言（B4 铁证）**：`hrs` 与 `new_coin`（新币做空策略）**共享 25 个 symbol**（AAVEUSDT / ACEUSDT / ADAUSDT / BNCUSDT / DATAIPUSDT / DRAMUSDT / ETHFIUSDT / FORMUSDT / KORUUSDT / KSTRUSDT / LITUSDT / MUUSDT / PUMPUSDT / SAMSUNGUSDT / SKHYUSDT / SNXXUSDT / SPCXUSDT / STRCUSDT / TQQQUSDT / TTWOUSDT / TXNUSDT / WLDUSDT / 牛来USDT 等）。**关键：铁证中的"真实交易对"（牛来USDT / SAMSUNGUSDT / MUUSDT / DRAMUSDT 等）恰在这 25 个重叠集里**。→ income 是全账户流水且 API 无 symbol/strategy 参数，**"按 symbol 聚合即 HRS 真值"不成立**，会混入 new_coin 的盈亏。

**范围**：对账脚本（本地只读）+ 写入层修复 + 历史缺口回填 + 口径统一。本阶段**只做设计，不写实现代码**。

**非范围（§4）**：不修改 10 月已落地分配、不删改历史行、不在服务器跑回测、不在本任务内新增持仓 ID 字段。

---

## 1. income 对账方案

### 1.1 数据来源与运行位置

| 数据 | 来源 | 位置 | 说明 |
|------|------|------|------|
| trade_records | 生产库只读导出 | 本地 | `docker exec trading_system-postgres psql ... \copy` 或 `pg_dump`，限定 `strategy='hrs'` + 相关时间窗 |
| income 流水 | 币安 PM 账户 API | 本地运行 | `GET /papi/v1/um/income?incomeType=REALIZED_PNL`（`shared/binance_api.py::get_income_history` L1427-1468） |
| 只读 key | ai-tuner 容器 env | 本地 | `BINANCE_API_KEY` / `BINANCE_API_SECRET`，仅查询 |

约束确认：对账脚本本地运行，生产库数据下载后只读分析（符合"禁止服务器回测"）；income 走 `use_unified_account=True`，脚本启动时显式断言非空。

### 1.2 income 数据关键约束（对账前必须核实）

1. **income 无持仓 ID、无 side**：只有 `symbol / incomeType / income / asset / info / time`（可能含 `tranId`/`tradeId`）。
2. **income 粒度 = 每笔平仓成交**：`REALIZED_PNL` 每条对应一次 reduce-only 平仓成交，**与 trade_records 明细行粒度对齐，而非与 PNL_SUMMARY 汇总行对齐**。
3. **【v2 · B2】分页截断风险**：`get_income_history` 单次上限 `limit=1000`（币安上限）、**无翻页游标、无 symbol 参数**，超 1000 条时静默丢最旧记录——而 BTRUSDT 恰在 9/1 窗口最旧端，历史最可能被截断。现有 `ai_tuner/reconciler/income_reconciler.py` L163-197 已有"分片 + 游标分页"先例。
   - **对账脚本强制要求**：按天/周分片拉取 + 分页游标推进（复用 `_fetch_income_chunk` 思路）；**完整性断言**：任一分页返回条数 == limit 即续拉下一页，无法续拉则告警并标记该分片"可能截断"、不得静默计入真值。
4. **【v2 · B4】income 跨策略 symbol 污染**：见 §0.1③，income 无法直接区分策略。

> 设计前置动作（B 段实现第一步）：用只读 key 实拉一条 2026-09 的 income 样例，确认返回字段是否含 `tranId`/`tradeId`，据此决定 side 精确裁决路径。样例未确认前，§1.4 side 恢复按"可降级"设计。

### 1.3 三个口径对账矩阵

对 2026 年各月（重点 9 月）分别产出真值并交叉核验：

| 口径 | 定义 | 预期暴露问题 |
|------|------|-------------|
| A 仅汇总 | `SUM(realized_pnl) WHERE order_type='PNL_SUMMARY'` | 漏写 → 大幅低估亏损 |
| B 仅明细 | `SUM(realized_pnl) WHERE order_type<>'PNL_SUMMARY' AND realized_pnl IS NOT NULL` | 无汇总持仓累加 |
| C 混合 SUM | `SUM(realized_pnl)` 全部行 | 双计 → 高估 |
| D 持仓级去重（无 ID 近似） | 按 (symbol, side) 分组 `BOOL_OR` | 边界错误见 §3 |
| **E income 真值** | 币安 income **逐笔匹配后**按持仓聚合 | **权威真值** |

**【v2 · B4】E 口径关键修正**：income 真值**不得**简单"按 symbol 聚合"——因 hrs 与 new_coin 共享 25 个 symbol。必须**逐笔匹配到 trade_records 明细行**后，只聚合 HRS 侧的匹配结果；无法归属到具体策略的 income 进入"策略归属裁决清单"。

**输出物**：各月真值表（E 为基准，A/B/C/D 对照）；无汇总持仓完整清单（标注可回填金额）；逐笔匹配率、持仓级聚合匹配率、未匹配 income / 未匹配 trade_records 清单。

### 1.4 匹配规则设计

**主键**：`symbol` + `side` + 时间窗 + 金额容差（side 需恢复）。

**side / 策略归属恢复（四选一，按优先级降级）**：
1. `tradeId → /papi/v1/um/userTrades` 反查平仓方向与成交明细（若样例含 `tradeId`）——最精确，也是**唯一能同时区分 hrs/new_coin 同 symbol 成交**的手段。
2. trade_records 反推：取该 symbol 时间窗内最近一条非 `PNL_SUMMARY` 明细行的 side 作候选，金额符号做弱校验。
3. 时间窗内方向/策略不可判定 → 进"未决清单"人工复核，不强行匹配。

**时间窗匹配**：income.time（UTC ms）与 `executed_at`（北京 naive）统一换算后比较，容差窗口从配置读取（默认 ±30 分钟；跨日 TP 由"持仓级聚合"覆盖）。

**单边成交匹配容差**（全部从配置读取，禁止硬编码）：金额容差 `abs(income - realized_pnl) <= max(0.01, |income|*1%)`；时间容差 `<= match_window`。

**冲突裁决**：以 income 为主循环、同 symbol 未匹配候选集中选时间差最小（复用 `income_reconciler._find_best_match` 思路但不复用其副作用）；一对多/多对一无法唯一确定 → 金额最近优先，仍歧义 → 未决清单。

**噪声清洗**：income 按 `(symbol,time,income,incomeType)` 去重；剔除 `incomeType != 'REALIZED_PNL'` 与 `income=0`。

### 1.5 风险与回滚

| 风险 | 影响 | 缓解/回滚 |
|------|------|-----------|
| income 样例无 tradeId/tranId | side/策略归属只能靠 trade_records 反推，同 symbol 双向/双策略歧义升高 | 未决清单覆盖歧义组，人工复核；月度聚合可先按"逐笔已匹配子集"出保守真值 |
| 只读 key 权限不足/签名失败 | 拉取失败 | 脚本幂等无副作用；改读 ai-tuner 容器内已跑通 income_reconciler 的同款 key |
| **【v2 · B2】income 分页截断** | 最旧段漏录，BTRUSDT 等 9/1 平仓被丢 → 真值偏低 | 强制分片分页 + 完整性断言；截断分片不入真值并告警，人工补拉 |
| **【v2 · B4】hrs 与 new_coin 同 symbol 污染** | 按 symbol 聚合把 new_coin 盈亏算进 HRS 真值 | 逐笔 tradeId/userTrades 归属；无法归属 → 裁决清单；不产出"污染真值" |

---

## 2. 写入层修复设计

### 2.1 写入链路现状（已定位）

- 部分平仓：`_writeback_pnl`（strategy.py L1433-1504）→ `update_realized_pnl`（UPDATE 明细行）。
- 全平仓结算（仅 TP 达标路径）：`_writeback_pnl_for_full_close`（L1671-1775）→ `insert_pnl_summary`（INSERT 汇总行）。
- **第三条路径（B1 核心）**：时间止损（L1951-1978）/ 移动止盈（L2006-2026）→ `trading_executor.close_position(close_percent=1.0)` → `_finalize_close_if_filled`（L3602-3624）→ **只调 `_writeback_pnl`（明细 UPDATE）+ `remove_position`，从不调 `insert_pnl_summary`**；`close_position`（executor.py L869-938）本身无任何 trade_logger 调用。

### 2.2 漏写根因假设清单（H1-H6）与验证步骤

| # | 根因假设 | 机理 | 验证步骤（只读） |
|---|----------|------|------------------|
| H1 | `since` 幂等去重误挡 | 同 (symbol,side) 自 entry_time 起已有**金额相同**的 PNL_SUMMARY（跨月同侧金额巧合相等）→ 误判重复跳过 | 查 BTRUSDT 及 114 行对应 (symbol,side) 自 entry_time 后是否已存在任意 PNL_SUMMARY；若无 → H1 排除 |
| H2 | TP 分批后"最终平仓判定"未触发 | TP1/TP2 分批后剩余归零，`detect_take_profit_fills` 仍走部分平仓分支或持仓已移除，`_writeback_pnl_for_full_close` 未调用 | 读 HRS 日志查 BTRUSDT 9/1 是否有"全部平仓PnL回写"记录；读 `detect_take_profit_fills` tp_result 边界 |
| H3 | 策略重启丢状态 | 重启后 position_manager 内存持仓丢失，重启前已 TP 部分平仓的持仓无法识别"已全平" | 交叉比对 HRS 容器 RestartCount/部署时间与 9 月漏写持仓时间分布；**【v2 · S3】补重启注入测试**：模拟部分平仓后重启，断言全平回写仍触发 |
| H4 | 异常吞错无重试 | `_writeback_pnl_for_full_close` / `insert_pnl_summary` try/except 吞错，DB/API 异常静默丢失 | 搜 HRS 日志"全部平仓PnL回写失败"/"PnL汇总记录插入失败"/"从币安获取实际PnL失败" |
| H5 | PnL 计算源头返回 None | `_get_actual_pnl_from_binance` 或理论计算返回 None → return 不写 | 搜"PnL计算失败，无法回写"；查 `_get_actual_pnl_from_binance` 按 entry_time..now 拉全量 income 是否吞入前序持仓 |
| **H6（v2·B1）** | **时间止损/移动止盈路径从不写汇总** | 该路径 `_finalize_close_if_filled` 仅 `_writeback_pnl` + `remove_position`，代码中无任何 `insert_pnl_summary` 调用；`remove_position` 后主循环 `_positions.get(symbol)=None` 永不再触发全平回写 | **证据已铁定**：§0.1② 114 行里 TAKE_PROFIT/LIMIT/STOP_MARKET 均为无 close_reason 明细（= 该路径回填），无对应汇总；代码链路 L1961-1978 / L2010-2026 / L3602-3624 / executor L869-938 无 trade_logger 汇总调用 |

> 达成"不漏写"需在所有根因定位后统一修复，验证先行、结论落档案。**H6 已由只读数据 + 代码链路证实为独立确凿根因**，与 H1-H5 非互斥。

### 2.3 修复方案设计（待 B 段，本阶段只定原则）

1. **H6 主修复**：`_finalize_close_if_filled` 全平分支补 `insert_pnl_summary`（复用 `_writeback_pnl_for_full_close` 的"实际值优先→理论兜底→income 兜底"取值链，带 since 幂等）。
2. **H2/H3 判定补全**：全平判定不依赖内存持仓存活，改为"检测到 reduce-only 成交后向币安询 symbol 持仓是否为 0 或 income 自 entry_time 覆盖全部开仓量"的收敛校验，归零即触发汇总写入（幂等）。
3. **H4 失败升级**：`insert_pnl_summary` 失败记 ERROR + 落"待补写"状态表，供 income_reconciler/回填兜底。
4. **H5 兜底**：理论计算 None 时以 income 真值兜底，而非 return 不写。
5. **H1 去重收紧**：since 去重由"金额相同"改为"金额相同 + 时间窗 + source 标识"。

### 2.4 历史缺口回填（必须，且必须以 income 真值为准）

- **原则**：不得凭 trade_records 自身推算，必须以 §1 income 逐笔对账真值（无汇总持仓清单）为准。
- **【v2 · B3】回填行字段语义**：
  - `executed_at` = **该组最后一笔平仓 income 的成交时间**（换算为北京 naive，落回 9 月/对应历史月），**禁止取回填执行时刻**（否则 9 月缺口被记进 10 月）。
  - `order_id` = 回填**批次号**（如 `backfill-20261014-01`；该列 VARCHAR 可存放，历史真实成交行 order_id 为交易所整数 ID，二者可区分）——作为回滚定位标记。
  - 审计：回填执行后把「插入行主键 id」回记到审计日志/回填清单，形成 `订单号→批次号→主键` 双向可追溯。
  - 其余字段：`realized_pnl`=该持仓组 income 聚合真值；`side`=削减方向；`close_reason` 保持 NULL（若非止损）；`status='FILLED'`。
- **回填与部署时序**：① 对账定案 → ② dry-run 输出待回填清单人工复核 → ③ 执行回填（幂等、可定位）→ ④ 验证回撤回落 → ⑤ 再部署写入层修复 + 口径统一（避免"先改口径后回填"期间口径仍双计）。

---

## 3. 口径统一设计

### 3.1 统一口径：汇总优先 + 无汇总回退明细（持仓级）

对账定案后，ai_tuner 与 dashboard 均改用"持仓级去重"：按 `(strategy, symbol, side)` 分组 `BOOL_OR`，有 `PNL_SUMMARY` 只累计汇总行，否则回退明细行。

### 3.2 无持仓 ID 约束下的可行性与边界

- **可行性**：回填后 PNL_SUMMARY 完整覆盖每个已平仓持仓时，"汇总优先"收敛到 income 真值（每持仓一条汇总压住明细，不再双计）。
- **边界错误（文档化，不宣称零缺陷）**：同 symbol 同 side 跨月多次开平仓，历史月份有汇总 → 本月孤立明细被误丢；纯 SQL 无法区分两个不同持仓，只能"近似持仓级"。精确持仓级需写入层新增 `position_id`/`pnl_batch_id`（长线治理，不在本任务）。

### 3.3 两处 SQL 统一落地范围

| 服务 | 文件 | 改动点 |
|------|------|--------|
| ai_tuner | `ai_tuner/allocation/pnl_collector.py::_PNL_QUERY_TEMPLATE` | 分组键升为持仓级 (symbol, side) |
| dashboard | `dashboard/backend/services/data_service_docker.py::_DAILY_PNL_QUERY`（L104-114）与 `_TRADE_RECORD_AGG_COLUMNS` | 分组键同上升为持仓级 |

### 3.4 HRS 回撤恢复预期与验证方法

- 回填 + 口径统一后重跑 dashboard 预计算（ON CONFLICT 覆盖写 + 120 天回溯，历史自动重写）。
- 预期：`cum_pnl` 不再双计/漏计，`equity` 不再多日 <0，日最大回撤从 1.0 回落至真实水平。
- **验证**：本地用回填后数据重算 `_build_strategy_daily_returns`，断言 `max_drawdown_ratio < 1.0` 且与 income 真值一致；生产重跑预计算后核对 dashboard 回撤曲线。

---

## 4. 验收标准与非目标

### 4.1 非目标（明确不做）

1. 不修改 10 月已落地月度分配数据（新口径 11 月生效）。
2. 不删除、不篡改任何历史 `trade_records` 行（只允许 INSERT 缺失汇总行，金额以 income 真值为准）。
3. 不在服务器执行回测。
4. 不在本任务新增持仓 ID 字段。
5. 不改已完成防双计 SQL 行为（除非口径定案后统一替换）。

### 4.2 验收标准（可验证）

1. **对账**：income 逐笔匹配率 ≥ 阈值（默认 95%，配置读），持仓级聚合匹配率 100% 可解释（未匹配全部入人工复核清单且有结论）。
2. **真值定案**：9 月 HRS 真实盈亏定案（有 income 逐笔背书的"不污染"真值），落档案并修正知识库旧结论。
3. **回填**：回填后 HRS 日最大回撤回落（`< 1.0`）且与 income 真值一致；无汇总持仓清单清零或每剩余项有书面豁免。
4. **写入层修复**：H1-H6 每条假设有验证结论记录 + 修复分支单元测试覆盖；H3 含重启注入测试。
5. **口径统一**：ai_tuner 与 dashboard 双侧 SQL 有守卫测试互相锚定，非 HRS 策略口径回归全绿。
6. **部署**：shared/ 改动后五层验证通过，非 HRS 策略容器无新告警。
7. **【v2 · S1】income 粒度抽样核对**：选 3-5 个已知持仓，(income 条数) vs (明细行条数) 抽样比对，确认 income 粒度假设成立后再定逐笔匹配细节。
8. **【v2 · S2】量化容差断言**：回填后「持仓级口径月度总和」vs「income 逐笔聚合」差异 ≤ 配置容差，超出即复审回填清单。

---

## 5. 全局风险与回滚

| 风险 | 影响 | 缓解/回滚 |
|------|------|-----------|
| 回填金额错（income 聚合吞入同 symbol 前序持仓或 new_coin 成交） | 写错汇总行，污染口径 | 逐笔 tradeId/userTrades 归属（B4）；回填前 dry-run 人工复核；回填行带批次号（B3），错则 `DELETE WHERE order_id='backfill-<批次>'` 回滚 |
| shared 改动破坏非 HRS 策略 | 全策略盈亏异常 | 向后兼容 + 全量 pytest 守卫 + 五层验证；异常则 git revert && push 回滚 |
| 口径统一后预计算重写覆盖历史绩效 | 历史曲线突变 | 预计算表可重建；切换前保留旧口径快照对比回退 |
| income 字段/粒度与设计不符 | 匹配失效 | §1.2 样例确认先行；不符则调匹配层，未决清单兜底 |

---

## 6. 部署顺序建议

1. 本地对账脚本先行（不部署）→ 产出真值表 + 无汇总持仓清单，定案 9 月真实值。
2. 历史回填（dry-run 复核 → 执行回填 → 验证回撤，见 §2.4 时序）。
3. 写入层修复（含 H6）+ 口径统一 SQL 同一次推送落地（shared/ 变更 → 全量重建，接受约 10 min）。
4. 五层验证（容器状态 → 镜像 ID → VERSION → 关键文件 MD5 → 日志无错误），核对 HRS 回撤回落与非 HRS 口径不变。

---

## 7. 关键设计决策（记录）

- **D1 以 income 为唯一真值**：双路径/漏写使 trade_records 内部推算无法自证，income REALIZED_PNL 是唯一无偏权威。
- **D2 income 粒度对齐明细而非汇总**：income 每笔对应一次平仓成交，主匹配在明细层逐笔对齐，再按持仓聚合与 PNL_SUMMARY 比对。
- **D3 纯 SQL 只做"近似持仓级"**：本任务用回填补齐汇总覆盖兜口径，边界错误显式文档化，不宣称零缺陷。
- **D4（v2）逐笔匹配解决 symbol 污染**：hrs 与 new_coin 共享 25 symbol，income 真值必须经逐笔 tradeId/userTrades 归属 HRS，杜绝按 symbol 聚合污染。

---

## 8. 待确认项（交 A-review / 调度者转用户）

1. income 样例是否含 `tranId`/`tradeId`（决定 side + 策略归属精确裁决路径）。
2. 回填范围是否含 8 月 / 10 月（本设计默认只对 9 月及受影响月出真值表）。
3. 回填执行权限（本地生成 → 服务器执行需人工审批）与批次号命名规范确认。