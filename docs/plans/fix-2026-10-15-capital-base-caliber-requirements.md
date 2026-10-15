# 修复需求文档：看板资金基数 / 最大回撤口径修复

## 1. 文档信息

| 项 | 内容 |
|----|------|
| 文档名称 | 修复需求文档：看板资金基数 / 最大回撤口径修复 |
| 版本 | v1.1（对齐架构 v1.1，已按 A-review 复核结论闭环 B1–B5） |
| 作者 | requirements-document-expert |
| 创建日期 | 2026-10-15 |
| 触发事件 | 用户核对生产看板 http://43.156.242.184/ 发现 HRS 等策略「最大回撤」长期显示 100%；经上一轮代码精读 + 生产数据双向验证，确认非统计 bug，而是「固定单一资金基数」旧设计与 ai_tuner 月度资金分配机制（分配金额每日随账户净资产浮动）不适配导致的伪击穿 |
| 需求来源 | 生产事件；先置决策 [max-drawdown-caliber](../../knowledge/decisions/Binance_quantitative_trading-20261009-max-drawdown-caliber.md) 文末明写「仅当未来用户再次要求对齐分母时才重新立项」——现为该触发条件成立，用户已同意立项 |
| 风险级别 | R3（判定依据见 §6） |
| 适用范围 | 本文档 §4 的 AC-1 ~ AC-4；AC-4 已定稿「自然重算、不主动回填」（选项 A，见 §4.6） |
| 下游环节 | 架构设计（R3 已过 A-review）→ 编码实现 |
| 验收口径 | 每项以 §4「验收标准」为准，编号 `AC-1x` ~ `AC-4x` |

> 本 v1.1 为「A 段需求文档 ↔ 架构文档 v1.1」对齐版：架构 `docs/plans/fix-2026-10-15-capital-base-caliber-architecture.md`（v1.1）已闭环 R3 设计复核 5 项阻塞（B1–B5），本文档按其 §5.1 / §7.1 / §8.1 / §10.1 / §12.1 / §14 的最终口径回写，消除两文档冲突。逐条「原表述 → 复核发现 → 修订后表述」见 §8「A-review 复核回应记录」。

> 本文档只描述「应该是什么口径」与「如何验收」，不写实现方案。本任务处于 A 段（需求分析），**本轮不写实现代码、不写迁移脚本、不部署、不 push、不改任何容器**。

---

## 2. 背景与现状口径问题清单

### 2.1 问题总览（双根因，已定论）

上一任务（[hrs_pnl_governance_design](../../plans/hrs_pnl_governance_design.md)，D 段已上线）已确认的结论：

| 编号 | 根因 | 一句话描述 |
|------|------|-----------|
| R1 | base_capital 取值时点错 + 全窗口单值 | 看板回撤分母「资金基数」取的是 **365 天前** 时点、且**整个窗口复用同一个值**，对 HRS 命中兜底「全表最早一条 allocated_amount = 58.37」 |
| R2 | 分配额每日随净资产漂移 | 2026-09-21 起 ai_tuner 每日 00:15 用「当日净资产 × allocated_ratio」覆写 `allocated_amount`，额度逐日漂移，单一分母无法代表整个窗口 |

两个根因叠加的后果：HRS 净值曲线 `(base_capital + cum_pnl) / base_capital` 用错误的 58.37 作分母，而实际分配额自 9/21 起已漂离 58.37（9/11~9/21 恒 104.22、9/22 起逐日 52~58），导致「段内累计盈亏 ÷ 错误段初分配额」使净值多日 < 0 被钳 0，最大回撤顶格 100%（伪击穿）。

### 2.2 问题一：看板回撤分母取值时点错、全窗口单值

**代码事实（已复核）**：

- 回撤等绩效指标由 `compute_and_store_performance_metrics` 落在 `public.performance_metric_snapshot`，看板总览通过 `_attach_performance_metrics` 读取：见 [data_service_docker.py L2421-2516](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2421-L2516) 与 [data_service_docker.py L2859-2916](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2859-L2916)。
- 窗口起点 = `today - _PERF_RETROSPECT_DAYS`（默认 365 天）：`L2438`，[data_service_docker.py L2438](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2438)。
- 每策略只构建**一次完整 365 天序列**，四个粒度共用：`L2467-2470`，[data_service_docker.py L2467-L2470](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2467-L2470)。
- 各粒度只按 `_PERF_WINDOW_DAYS = {day:30, week:90, month:180, year:365}` 从**尾部截取**：`L2475`、`L2541-2545`，[data_service_docker.py L2541-L2545](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2541-L2545)。
- 净值序列构建 `_build_strategy_daily_returns` 在**完整 365 天序列**上累积 `cum_pnl`：`L2617-2700`，[data_service_docker.py L2617-L2700](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2617-L2700)；`net_value = (base_capital + cum_pnl) / base_capital`、负值钳 0：`L2687-2692`，[data_service_docker.py L2687-L2692](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2687-L2692)。
- **关键缺陷点**：`base_capital` 的入参 `at_time` 传的是 `start_date`（= `strategy_window_start = today - 365d`，见 `L2440`、`L2659`），**不是各粒度窗口起点**：`L2659`，[data_service_docker.py L2659](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2659)。

**数据事实（已双向验证，可直接引用）**：

- HRS 首条快照为 2026-08，365 天前（约 2025-10）**无任何快照** → `_query_strategy_base_capital` 命中断言步骤 #2「全表最早一条 allocated_amount > 0」= **58.37**（`L2745-2756`，[data_service_docker.py L2745-L2756](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2745-L2756)）。
- 四个粒度（day/week/month/year）共用这同一个 58.37 分母，且净值序列尾部切片**不按窗口重基准**——窗口内峰值继承全 365 天序列的峰值，回撤因此失真。

**代码位置汇总（回退链）**：`_query_strategy_base_capital` 4 步回退链见 `L2702-2787`，[data_service_docker.py L2702-L2787](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2702-L2787)。

### 2.3 问题二：AI 月度分配的 allocated_amount 自 9/21 起逐日漂移

**代码事实（已复核）**：

- 面板「AI监控 · 月度资金分配」的分配金额 = 当日账户净资产 × 该策略 `allocated_ratio`，每日北京时间 00:15 重算并**幂等覆写** `public.capital_allocation.entries`：`daily_refresher.py` 主流程 `L97-199`，[daily_refresher.py L97-L199](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/daily_refresher.py#L97-L199)。
- `_recalculate_entries` **只改 `allocated_amount`**：`L167-199`，[daily_refresher.py L167-L199](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/daily_refresher.py#L167-L199)。
- 调度注册于 `ai_tuner/main.py` `L331-409`，cron 键 `capital_allocation.daily_refresh_cron`，默认 `"15 0 * * *"`（见 [daily_refresher.py L32](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/daily_refresher.py#L32)）。机制 2026-09-21 上线。
- entries 由月度 job 写入：`config_updater.py::_save_to_db` `L95-157`，字段含 `strategy_id / strategy_name / realized_pnl / initial_capital / return_rate / rank / allocated_ratio / allocated_amount`，[config_updater.py L95-L157](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/config_updater.py#L95-L157)。
- **每日刷新不动 `initial_capital`**（`_recalculate_entries` 仅碰 `allocated_amount`）→ `initial_capital` 是月度 job 时点值、**整月不变**。
- `initial_capital` 来源 = `pnl_collector.py::_query_base_capital`（`L324-379`，4 步回退链 SQL `L77-112`），[pnl_collector.py L324-L379](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/pnl_collector.py#L324-L379)；`allocation_calculator.py` `L214-267` 中 `initial_capital = s["capital"]`、`return_rate = pnl / capital`，[allocation_calculator.py L214-L267](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/allocation_calculator.py#L214-L267)。
- 策略运行时消费：`shared/capital_manager.py` `L121-173`、`L318-363`，[capital_manager.py L121-L173](file:///Users/yl/vscode/Binance_quantitative_trading/shared/capital_manager.py#L121-L173)。

**数据事实（已双向验证）**：

- 快照表 HRS 自 9/22 起逐日小幅漂移（52.67 / 53.17 / 53.39 / … / 56.92 / 57.92），不再整月恒定；`0.1 × 537.70 = 53.77`（9 月 total_capital）、`0.1 × 579.17 = 57.92`（10 月）逐一对得上。
- **9/21 之前无每日刷新**：9/10–9/11 为 58.37，9/11 14:00–9/21 12:00 **恒为 104.22**（10 天不变，非每日漂移）→ 该档系某次分配事件写入的静态值，确切来源（月度 job / 手工触发）**尚未定论，不得臆断**。
- **资金从未发生外部划转**（用户已确认）；额度跳变纯属 ai_tuner 分配计划（ratio）变更的记账结果。
- HRS 当前 equity ≈ 3.74 USDT（净值 0.064）。

### 2.4 问题三：AI 月度分配盈亏口径存在「无汇总组明细漏计」（三机制归因）

> **回写说明（对应 B3）**：
> - **原表述**：§2.4 标题为「跨月持仓双计」，并以「判定粒度不一致 → 跨月持仓在上月/本月各计一次」，锚定「10 月 +10.29 vs income 真值 +3.43（跨月双计所致）」。
> - **复核发现（生产实测，2026-10-15）**：该因果链被逐笔对账**证伪**。10 月 HRS 四口径 = 整表 BOOL_OR **+17.90** / 持仓级 CTE **+12.62** / income 真值 **+3.4292**；差异 **−5.2765 全部来自「无汇总组明细」，不是跨月双计**。原 +10.29/+3.43 为历史时点绝对值，已随 10-14/10-15 新增成交漂移，**不可复现**。
> - **修订后表述**：改为架构 §7.1 的「三机制归因」。

**三机制量化归因（生产只读，截至 2026-10-15，架构 §7.1 已逐笔对账定论）**：

| 机制 | 量(USDT) | 性质 | 是否本任务可修 |
|------|---------|------|--------------|
| ① 无汇总组明细漏计 | **−5.2765** | **统计层**：`_PNL_QUERY_TEMPLATE` 整表 `BOOL_OR(order_type='PNL_SUMMARY')` 的缺陷并非「跨月双计」，而是「整表任一持仓有 summary 即全表只看 summary」，使「该月真实发生、但未写 `PNL_SUMMARY`」的持仓明细被**漏计**（导致旧口径高估） | **是（AC-2 要修）** |
| ② `CONDITIONAL_ORDER` 与 `PNL_SUMMARY` 同值双写 | **0** | 有汇总组里明细与 summary 同值（如 ARBUSDT 10-12 `CONDITIONAL_ORDER` −2.874080 == `PNL_SUMMARY` −2.874080）；持仓级 CTE 对有汇总组只取 summary、天然不双计 | **不是差异来源** |
| ③ `PNL_SUMMARY` 累计语义跨月 carry-in | **+3.03454** | **写入层**：`insert_pnl_summary` 写「整笔平仓累计盈亏」、自开仓起累计、可跨月（2 组跨月持仓：SEIUSDT +0.86764、牛来USDT +2.1669）；持仓级 CTE 无法消除，且「重做 HRS 写入层」已被非目标 4 排外 | **否（归上一任务 hrs_pnl_governance）** |

**代码事实（已复核）**：

- ai_tuner 月度 PnL 采集用 `_PNL_QUERY_TEMPLATE`（`L50-59`），窗口内整表 `BOOL_OR(order_type='PNL_SUMMARY')` 判定，**未按 (strategy, symbol, side) 持仓级分组**，[pnl_collector.py L50-L59](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/pnl_collector.py#L50-L59)。
- 对照：dashboard 日盈亏查询 `_DAILY_PNL_QUERY` 已升级为**持仓级 CTE**（`grp` 按 `(strategy, symbol, side)` 分组 `BOOL_OR`，`IS NOT DISTINCT FROM` 防 NULL 丢行），[data_service_docker.py L112-L134](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L112-L134)。
- 该缺陷**直接作为 11 月月度分配排名的输入**（`allocation_calculator._calculate_by_ranking` 按 `return_rate = pnl / capital` 降序排位）；旧口径漏计机制①会**高估**当月 PnL，扭曲 11 月各策略分配比例 → 真实资金。

> **边界（重要）**：本任务 AC-2 只修机制①（统计层，把整表 BOOL_OR 升级为持仓级 BOOL_OR，与 dashboard `_DAILY_PNL_QUERY` 对齐）；**不负责**修机制③跨月 carry-in（写入层，归上一任务 `hrs_pnl_governance`，非目标 4 已排外）。income 真值 +3.4292 为 10-13 对账值、随新增成交漂移，**不作为本任务锚点**。

### 2.5 问题四：分配记录 `initial_capital` 与看板 `_query_strategy_base_capital` 分母语义不一致

**代码事实（已复核）**：

- 看板分母用 `public.strategy_position_snapshot_history` 的 4 步回退链（详见 2.2）。
- 分配记录 `public.capital_allocation.entries` 里另有语义明确的 `initial_capital` 字段（9 月 = 58.37、10 月 = 76.82，作 `return_rate` 分母），由月度 job 写入、整月不变。
- 二者口径不一致：看板按 365 天前时点回退链取「最早一条 58.37」，分配记录按月度 job 时点取 `initial_capital`；同一份盈亏在两处呈现不同分母。

---

## 3. 目标与非目标

### 3.1 目标

1. 修复看板「绩效 · 回撤」口径：以「资金段」为单位、段内用「段初分配额」作分母，杜绝固定单值 + 取值时点错导致的伪击穿（对应 AC-1；**全策略显著变化，非仅 HRS**，见 §6.2/§10.1 预演表）。
2. 对齐 AI 月度分配盈亏口径：修复「无汇总组明细漏计」（统计层机制①），使 11 月分配排名输入更接近 income 真值（对应 AC-2；机制③跨月 carry-in 不属本任务，见 §2.4）。
3. 统一分配记录与看板分母语义：看板回撤分母 = 段事件生效时刻后段内首条正快照 `allocated_amount`，与 `capital_allocation` 在「记录创建时刻初值、月粒度」可对账同源（对应 AC-3）。
4. 处理历史绩效快照：**已定稿「自然重算、不主动回填」（选项 A）**，写清可见后果并保留改选回填路径（AC-4，见 §4.6）。

### 3.2 非目标（本轮明确不做）

1. **不改策略下单 / 风控 / 限额执行逻辑**：`capital_manager` 的限额优先级链、各策略开仓前额度检查均不动。
2. **不改 10 月已落地分配数据**：`public.capital_allocation` 的 2026-10 及更早记录、各策略 `config.yaml` 的 `capital_limits` 维持现状（新口径自 11 月分配起生效）。
3. **不引入外部资金流字段**：本任务**不新增** `external_cashflow` / 划转流水等字段；「资金从未外部划转」是既定事实，仅按分配计划（ratio）变更切段。
4. **不重做 HRS 盈亏写入层 / 去重**：上一任务（hrs_pnl_governance）已修复的写入层 H6、HRS 汇总行回填、持仓级防双计 SQL **不重做、不推翻**；本任务 AC-2 亦**不修机制③跨月 carry-in**（`PNL_SUMMARY` 累计语义，写入层，见 §2.4 三机制）。
5. **不修改 ai_tuner 分配比例规则**：`rank_ratios`、零开仓垫底、reserve 比例等规则不变。
6. **不写实现代码、不写迁移脚本、不部署、不 push**：本轮只产出本需求文档。

---

## 4. 期望口径定义与验收标准

### 4.1 术语定义（口径词汇表）

| 术语 | 定义 |
|------|------|
| **资金段（capital segment）** | 一个「分配额生效区间」，起点 = 某次 `allocated_ratio` / `capital_allocation` 记录变更事件（月度 job 写入、或历史静态值变更）发生的时点，终点 = 下一次变更事件之前。段内 `allocated_amount` 虽有每日漂移，但**分配的「名义基数」以段初值为准** |
| **段初分配额（segment opening capital）** | 某资金段「段边界事件生效时刻（E1/E2 取 `capital_allocation` 记录 `created_at`、E3 取跳变 `snapshot_hour`）**之后**」该段内**首条正快照 `allocated_amount`**（来自 `public.strategy_position_snapshot_history.allocated_amount`），作段内净值分母。**不用 `entries.initial_capital`、不回退「全表最早一条」**（架构 §4.1 落地规则 / §5.1 契约） |
| **段内净值（segment net value）** | `segment_net_value = 1 + 段内累计已实现盈亏 / 段初分配额`（不复利；段内为负时不做跨段借位，触 0 后钳 0） |
| **权益（equity）** | 账户级：`account_equity`；策略级：段内 `equity_t = 段初分配额 + 段内累计已实现盈亏` |
| **账户级 vs 策略级** | 账户级回撤以 `equity_snapshot` 净值曲线为准（现状不受本次影响）；策略级回撤以「资金段 + 段初分配额 + 段内累计盈亏」为准（本次修复对象） |

### 4.2 期望口径公式（策略级）

```
对每个策略、按「资金段」切段后：
  段内净值曲线  nv_t = max(0, 1 + cum_pnl_in_segment_t / segment_opening_capital)
  段内日收益    r_t  = day_pnl_t / segment_opening_capital
  最大回撤       max_dd_overall = max(各段的段内最大回撤)
                —— 分段各算取最大，跨段不比较、不把上一段净值下跌带入下一段峰值
```

要点：

- **段边界由「分配计划变更事件」决定**（`allocated_ratio` / `capital_allocation` 记录变更），**不用金额阈值切段**——因 9/22 起 `allocated_amount` 本身逐日随净资产漂移，阈值法不可靠。
- **段内分母 = 段初分配额**，不再沿用「全窗口单一 58.37」。
- **跨段不比较**：一次分配计划变更 = 一次「资金重置」，上一段权益不得作为下一段的峰值参照（否则 9/17 触 0 仍会污染新段的回撤）。

### 4.3 AC-1：看板绩效 · 回撤口径（核心）

**目标**：策略级回撤改用「资金段 + 段初分配额」口径，各粒度（day/week/month/year）的回撤不再由「固定单一 58.37 分母 + 全窗口峰值继承」计算。

**边界覆盖**：

| 边界 | 说明 |
|------|------|
| 段内多粒度切片 | 各粒度仍截取「近 30/90/180/365 天」，但净值序列按其**所属资金段**重基准，不使用跨段峰值 |
| 段初点与 365 天窗口的关系 | 窗口起点早于首段起点时，首段之前（无数据期）不得以「未来最早一条」兜底取值 |
| 9/21 前静态值 104.22 | 该档来源未定，**不臆断来源**；架构已定稿：按「变更事件切段」如实识别为辅锚 E3 段边界（9-11~9-20 恒 104.22，段初=104.22），来源究责不阻塞本修复（架构 §4.1/§12.2） |
| 分母为 0 / 无快照 | 某段无任何可用正快照 `allocated_amount` 时，该段跳过或标记「不可评分」，不得静默用 0 或其它段的值 |

**验收标准**：

- **AC-1.1（HRS 各粒度回撤回落至期望值）**：修复后重算，HRS 四个粒度最大回撤**不再恒等于 1.0**，预期 ≈ **18.82%**（允许 ±0.02，源于段 D 段初取值的微小浮动）；与「段内累计盈亏 ÷ 段初分配额」逐段重算一致（架构 §10.1「HRS 细分」：段 D 段初 56.81、段内 max_dd=0.1882 决定整体值）。
- **AC-1.2（分母来自段初正快照）**：对任一 HRS 资金段，其回撤分母 = 段事件生效时刻之后段内首条正快照 `allocated_amount`（段 B=104.22、段 C=53.84、段 D=56.81），**不取自 365 天前兜底「最早一条 58.37」，也不用 `entries.initial_capital`**。
- **AC-1.3（四个粒度不再共用单一 58.37）**：day/week/month/year 四个粒度的分母随各自窗口落入的**资金段**取段初值，不再四者恒等于 58.37。
- **AC-1.4（跨段不比较）**：构造「段 A 净值跌到 0、段 B 重新开始」的序列，断言段 B 内回撤**不受段 A 峰值影响**（段 B 峰值从 1.0 起算）。
- **AC-1.5（净值不重基准导致峰值继承的回归修复）**：尾部切片 `daily_returns[-window_days:]` 后，其 `net_values` 不继承全 365 天序列的峰值（切片序列在段内重新归一）。
- **AC-1.6（5 策略期望值，生产验收对照）**：修复后 5 策略各粒度回撤逐一对齐下述预演期望值（架构 §10.1，段初取值 ±2% 内浮动）：

  | 策略 | day(30d) | week(90d) | month(180d) | year(365d) |
  |------|---------|---------|---------|---------|
  | btc_eth | 1.40% | 3.22% | 3.22% | 3.22% |
  | btc_eth_aggressive | 1.65% | 1.65% | 1.65% | 1.65% |
  | hrs | 18.82% | 18.82% | 18.82% | 18.82% |
  | new_coin | 4.77% | 4.98% | 4.98% | 4.98% |
  | grid | 不可评分 | 不可评分 | 不可评分 | 不可评分 |

- **AC-1.7（grid 不可评分，无回归）**：grid 在 `strategy_position_snapshot_history` **无任何行**、`trade_records` 零成交、`capital_allocation` 无记录 → 新口径下标记「不可评分」，其 `performance_metric_snapshot` 残留行（day/week 至 10-09、month/year 残留 0.0）**维持现状不重写、不覆盖**，新算法对 grid 不产生新行（架构 §4.4）。
- **AC-1.8（零分母期整段跳过，无静默回退）**：某资金段段初分配额为 0 或不存在、却仍有成交（如 btc_eth_aggressive 9-10~9-20 `allocated_amount` 恒 0），该段**整段跳过、不计入任何段回撤**，且有日志；不得静默回退旧兜底分母或其它段值（架构 §4.5）。

> **段链事实澄清（对应 B1）**：复核证实**全策略都有显著变化，不是只有 HRS**。生产只读段清单（架构 §10.1）：btc_eth 87.56→208.44（9-11 静态跳变）→161.53→142.03；new_coin 116.75→156.33→134.61→113.63；hrs 58.37→104.22→53.84→56.81；btc_eth_aggressive 9-10~9-20 全 0（零分母期）→9-21 107.69→10-09 170.44。四个策略均有「9-11 静态跳变 + 9-21 起每日漂移 + 10-09 月切换」，段结构同构。

**可观察验收方法**：

- 本地单测：构造多段资金序列（含「跨段净值触 0 后新段」），断言 `max_drawdown_ratio < 1.0` 且与逐段手算一致。
- 生产只读对账：重跑看板预计算后，用下述只读 SQL 核对 HRS 各粒度 `max_drawdown` 不再为 1.0，且 `window_start/window_end` 落在正确资金段内。
- 对照口径一致性：本地用同一份生产日盈亏序列按新口径重算，与 production `performance_metric_snapshot` 的 `max_drawdown` 逐点吻合。

### 4.4 AC-2：AI 月度分配盈亏口径对齐（无汇总组明细漏计，三机制归因）

> **回写说明（对应 B3）**：原 AC-2 标题 / 目标 / AC-2.1 / AC-2.2 / AC-2.4 均表述为「跨月持仓双计」并锚定「+10.29 → +3.43」绝对数。复核以逐笔对账**证伪**该因果链，改按架构 §7.1 三机制归因 + 相对断言。

**目标**：`ai_tuner/allocation/pnl_collector.py::_PNL_QUERY_TEMPLATE` 的聚合口径从「整月窗口整表 BOOL_OR」升级为与 dashboard `_DAILY_PNL_QUERY` 一致的**持仓级 (strategy, symbol, side) BOOL_OR**，修复**机制①「无汇总组明细漏计」**（整表任一持仓有 summary 即全表只看 summary，导致「该月真实发生、却未写 `PNL_SUMMARY`」的持仓明细被漏计），并统一 ai_tuner 与 dashboard 两侧月度口径。

**输入影响与修复范围（正式结论）**：

- 旧口径漏计机制①使 10 月 HRS 月度 PnL **高估 5.28**（整表 BOOL_OR +17.90 vs 持仓级 CTE +12.62），直接污染 11 月分配排名 `return_rate` 输入。
- **本任务修机制①（统计层）；不修机制③跨月 carry-in +3.03454（写入层，归 hrs_pnl_governance，非目标 4）**；income 真值 +3.4292 为 10-13 对账值、随成交漂移，**不作为本任务验收锚点**。

**验收标准**：

- **AC-2.1（判定粒度对齐）**：ai_tuner 月度 PnL 分组键 = `(strategy, symbol, side)`，判定「该持仓是否有 `PNL_SUMMARY`」的窗口 = 整月（`[month_start, month_end)`，与现状一致、不做前后延展），与 dashboard `_DAILY_PNL_QUERY` 语义一致（架构 §7.1）。
- **AC-2.2a（精确复算，锚 DB，不依赖 income API）**：持仓级口径 − 整表 BOOL_OR 口径 == 无汇总组明细 `SUM(realized_pnl)`（10 月 == **−5.2765**），可通过对账 SQL 逐分组精确复算，两者必须一致（架构 §10.3）：
  - 预期（2026-10-15 快照）：`whole_table`=+17.90、`position_level`=+12.62、`position_level - whole_table` == 无汇总组明细合计 == −5.2765（LITEUSDT −2.6035 / ONDOUSDT −3.2190 / NBISUSDT +0.3861 / QQQUSDT +0.1599）。
- **AC-2.2b（方向性收敛）**：持仓级口径比整表口径更接近 income 真值（补回旧口径漏计、income 确已发生的无汇总组真实盈亏）；残余差异 = 跨月 carry-in（+3.03454）+ income 对账已有系统性差异，**不以绝对值锚定验收、不由 AC-2 消除**。
- **AC-2.3（非 HRS 策略不回归）**：btc_eth / btc_eth_aggressive / new_coin 无 `PNL_SUMMARY` 行 → `has_summary=false` → 回退「明细行 SUM」，与旧口径一致（SQL 文本守卫断言 `NOT g.has_summary` 回退分支）。
- **AC-2.4（11 月排名输入可追溯）**：11 月月度分配计算所读取的 HRS `pnl` 数值，与「持仓级口径」结果一致，且可在日志/落库中发现其来源为持仓级口径。

**可观察验收方法**：

- 本地单测：mock「整表任一持仓有 summary、但该月另有无汇总持仓明细」场景，断言无汇总组明细被纳入（不再漏计）；SQL 文本断言含 `GROUP BY strategy, symbol, side` + `IS NOT DISTINCT FROM` + `NOT g.has_summary` 回退分支。
- 生产只读对账（10 月样本，不依赖 income API）：执行对账 SQL，断言恒等式「持仓级 − 整表 == 无汇总组明细合计」成立（三机制对账，架构 §10.3）。

### 4.5 AC-3：分配记录与看板分母语义统一

> **回写说明（对应 B4）**：原 AC-3.1「看板回撤分母与分配记录 `initial_capital` 取值一致（`initial_capital` 优先、段起点快照回退）」的**回退方向与架构决策相反**。复核已证实弃用 `initial_capital` 的理由成立：entries 里 10 月 `initial_capital` = 76.82024229 与快照表最早一条 `open_margin` 精确相等，是旧口径残值。

**目标**：看板回撤分母统一为「段事件生效时刻之后、段内首条正快照 `allocated_amount`」，与 `capital_allocation` 在「记录创建时刻初值、月粒度、容差内」可对账同源；消除「看板用回退链 58.37、分配记录用 58.37/76.82」的两套口径。

**验收标准**：

- **AC-3.1（分母契约，架构 §5.1 唯一口径）**：对同一策略同一资金段，看板回撤分母 = **该段边界事件生效时刻（E1/E2 取 `created_at`、E3 取 `snapshot_hour`）之后、段内首条正快照 `allocated_amount`**；**不用 `entries.initial_capital`、不回退「全表最早一条」**。段初分配额为 0 或不存在时标记「不可评分」。`entries.initial_capital` 降级为**历史审计字段**、不再作看板分母；其字段语义注释/docstring 需在 B 段同步（见 §8 B4）。
- **AC-3.2（同源对账，月粒度 + 容差）**：看板该月第一个资金段的段初 `allocated_amount`，应与当月 `capital_allocation.entries[x].allocated_amount` 在「记录创建时刻」的初值于月粒度、容差内对账一致（如 HRS 10 月：段 D 段初快照 56.81 vs 记录初始 57.92，差 1.9%，同源可对账），**而非等于被每日刷新漂移后的当前值，更非等于 `initial_capital`**（架构 §5.3）。
- **AC-3.3（不再跨表口径漂移 + 10 月记录不变）**：`_query_strategy_base_capital` 的取值语义与「段初正快照」对齐，不再出现「365 天前兜底最早一条」新取法；10 月已落地 `capital_allocation` 记录与 `capital_limits` 不改（append-only + 只读核对）。10 月 `initial_capital=76.82` 与段初快照 56.81 的差异为已文档化既成残值，**不作为本条验收对象**。

**可观察验收方法**：

- 生产只读：对同一策略同一月，核对看板首段段初快照 `allocated_amount` 与当月 `capital_allocation` 记录创建时刻初值在容差内一致；`initial_capital` 不再参与看板分母取数（只读核对已文档化的 76.82 vs 56.81 残值差异）。

### 4.6 AC-4：历史绩效快照的处置（已定稿：自然重算，不主动回填）

> **回写说明（对应 B5）**：原 AC-4 自标「待用户拍板」。用户在两问给出不同选择（第 4 项勾「历史快照重算并回填」、专门一问选「只改算法，快照随定时任务自然重算，不主动回填」）。架构 §8.1 已**以专门一问的选择为准定稿选项 A**，本节据此把「待拍板矛盾」改写为「已定稿 A + 保留 B 可选路径」。

**目标**：明确 `public.performance_metric_snapshot` 历史桶（非当前桶）的处置策略，并锁定「自然重算、不主动回填」（选项 A）为最终定稿。

**已核实事实（判断依据）**：

1. **前端总览只读「当前桶」**：`_attach_performance_metrics` 查询 `WHERE granularity=$1 AND bucket_key=$2`，`bucket_key = 今日桶`（`L2875-2885`，[data_service_docker.py L2875-L2885](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2875-L2885)）。
2. ** `get_performance_metrics` 也只读当前桶**：`L2807-2816`，[data_service_docker.py L2807-L2816](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2807-L2816)。
3. **定时任务只 UPSERT 当前桶**：`compute_and_store_performance_metrics` 的 `bucket_key = self._bucket_info(today)[...]["key"]`，[data_service_docker.py L2478-L2484](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2478-L2484)；桶语义 `_bucket_info`（day=今天 / week=周一 / month=月1 / year=1月1）见 `L943-952`，[data_service_docker.py L943-L952](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L943-L952)。
4. **唯一约束**：`UNIQUE(granularity, scope, strategy_id, bucket_key)`，UPSERT 只覆盖同一 `bucket_key`，[09-performance-metric-snapshot.sql L20-L35](file:///Users/yl/vscode/Binance_quantitative_trading/database/postgres/init-scripts/09-performance-metric-snapshot.sql#L20-L35)。
5. **ai_tuner 读取器可能兜底读历史桶**：`ai_tuner/allocation/performance_reader.py::get_latest` 用 `bucket_key <= target ORDER BY bucket_key DESC LIMIT 1`，[performance_reader.py L87-L148](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/performance_reader.py#L87-L148)。

**已定稿结论（矛盾已闭环，B5）**：

- 用户两问矛盾，**以专门一问的选择为准** → **定稿选项 A（自然重算，不主动回填历史桶）**；第 4 项「重算并回填」作为矛盾对栏、不采纳（架构 §8.1）。
- **选项 A（定稿）**：改算法后，下一次 600s 定时任务（`PERFORMANCE_METRIC_INTERVAL_SECONDS=600`，[main.py L117](file:///Users/yl/vscode/Binance_quantitative_trading/services/data_backend/src/main.py#L117)）用新口径覆盖**当前 4 个桶**；历史桶保留旧值但当前无展示入口。
- **选项 B（保留可选路径，需用户另行批准）**：若未来要主动回填历史桶，需额外产生一次生产库写操作——迁移脚本（逐桶重算 + UPSERT）+ 幂等 + 生产写库审批，按架构 §8.3 的安全方案执行。**本轮不执行、不因跨过选项 A 而自动获得写库授权**。

**不主动回填的可见后果（本方案明确接受 = AC-4.2「接受声明」）**：

1. 历史桶（非当前桶）残留旧口径 `max_drawdown`（如 HRS 历史 = 1.0），在**当前无历史回看界面**的前提下永不可见。
2. 前端总览 + dashboard-api 读的都是当前桶；定时任务 600s 内用新口径 UPSERT 覆盖当前 4 个桶 → 用户页面看到的即修复后的值，无感知差异。
3. 若未来新增历史回看能力，历史桶旧值会被展示，届时需另立任务按选项 B 回填（路径已保留于此，无需重做架构）。

**验收标准**：

- **AC-4.1**：文档已把「历史桶是否被前端展示」「定时任务是否只写今日桶」「不回填的可见后果」三点核实结论写明（即本小节 1~5 条 + 上述三条接受声明）。
- **AC-4.2（选项 A 接受声明）**：明示「历史桶旧值残留、且当前无历史回看界面」已由本方案接受；「主动回填」作为需用户另行批准的可选路径保留（迁移脚本 + 幂等 + 生产写库审批）。

---

## 5. 未确认假设与待用户拍板项

本节（v1.1）已按架构 v1.1 收敛：原 6 项中 4 项已由架构定稿、从「待拍板」移出（下表）；仅剩 2 项仍需用户/后续确认。

**仍待用户拍板项（2 项）**：

1. **辅锚 E3「恒定 ≥ K 天」的去噪阈值 K 默认值与配置落点**：架构 §14 #3 建议 K=3 天，配置落点（`dashboard` 配置 or 环境变量）留 B 段定案；**必须配置化、禁硬编码**（架构 §12.1 风险 1 缓解）。
2. **是否采纳可选 `caliber_version` 审计列**：快照表追加可空 `caliber_version VARCHAR(16)`（架构 §6.1，非必需），用于 AC-4 回滚批次标记；不加也能正确跑。待用户确认。

**已由架构定稿、不再待拍板（逐条标注定稿处）**：

| 原待拍板项 | 定稿结论 | 架构出处 |
|-----------|---------|---------|
| 历史快照回填（A vs B） | **定稿选项 A（自然重算，不主动回填）**，B 保留可选路径（需另行批准） | §8.1 / §11 #1 / §14 #1 |
| 104.22 静态值来源 | **不臆断来源**；如实按辅锚 E3 识别为段边界（段初 104.22），来源究责不阻塞本修复 | §4.1 / §12.2 #1 |
| 「段」的持久化粒度 | **不落地新表/新字段，实时按 snapshot_history + capital_allocation 推导** | §6.1 / §11 #3 |
| 首段之前（无数据期）处理 | **跳过（不可评分）**，不退化为「未来最早一条」 | §4.5 / §11 #4 |
| AC-2 持仓级判定窗口边界 | **整月窗口内持仓级 GROUP BY，不做前后延展（与 dashboard 一致）** | §7.1 / §11 #5 |
| 账户级回撤是否调整 | **不改账户级**（以 equity_snapshot 为准，本次仅策略级） | §11 #6 |

---

## 6. 影响面与风险

### 6.1 风险级别判定

**结论：R3（最高风险）。**

**依据**（对照 model-development-workflow 方案 R3 明示类别）：

| R3 明示类别 | 本任务是否命中 | 说明 |
|-------------|--------------|------|
| 资金与精度 | **是** | 看板回撤分母 / AI 月度分配排名输入，直接决定「资金使用效率」度量与「11 月分配额度」→ 真实资金 |
| 数据迁移 | **否（AC-4 定稿 A）** | 历史快照已定稿「自然重算、不主动回填」，**无历史桶写操作**；仅「未来改选 B 回填」才触及生产写库（需人工授权 + 可回滚，路径保留于 §4.6/架构 §8.3） |
| 发布时间 | **是** | 改 `dashboard/backend/` 触发 data-backend + dashboard-api 重建；改 `ai_tuner/allocation/` 触发 ai-tuner 重建 |
| 下单/撤单/持仓归属/风控门控 | **否** | 本任务明确不动策略下单、风控、限额执行逻辑 |

**附加因素**：本任务不触及下单/撤单/持仓归属，风险点集中于「口径差错会误导资金分配决策」与「修复后 5 策略（除 grid）回撤全策略显著变化、需保留旧口径基线对比」。综合定 R3（资金与精度 + 发布时间为核心，数据迁移因定稿 A 退出为可选），编码前需 A-review，编码后独立审查不省略。

### 6.2 代码影响面

| 模块 | 文件 | 影响点 |
|------|------|--------|
| 看板绩效 | `dashboard/backend/services/data_service_docker.py` | `_build_strategy_daily_returns`（分段净值）、`_query_strategy_base_capital`（段初分配额取法）、`compute_and_store_performance_metrics`（无数据期处理） |
| 看板读取 | `dashboard/backend/api/routes_docker.py`、`data_service_docker.py::_attach_performance_metrics` | 读逻辑不变（仍读当前桶），但口径随上游表值变化 |
| AI 月度分配 | `ai_tuner/allocation/pnl_collector.py` | `_PNL_QUERY_TEMPLATE` 升级为持仓级判定 |
| 数据后台 | `services/data_backend/src/main.py`、`performance_metric_job.py` | 调度本身不变（仍 UPSERT 当前桶） |
| 快照 DDL | `database/postgres/init-scripts/09-performance-metric-snapshot.sql` | 不变（架构定稿不落地段表/segment_key）；可选追加可空 `caliber_version` 审计列（见 §5 待拍板项 2） |

> **B 段实现命名（实际，2026-10-15）**：上表「看板绩效」行中的 `_build_strategy_daily_returns` / `_query_strategy_base_capital` 为 A 段草案命名，B 段实际实现已**改名/删除**——`_build_strategy_segmented_series`（分段净值，旧 `_build_strategy_daily_returns` 已删）、`derive_segments` / `_derive_segments_for_strategy` + `_SEGMENT_OPENING_CAPITAL_SQL`（段切分与段初「向后取」，旧 `_query_strategy_base_capital` 已删）、`_compute_strategy_perf_by_segments` / `_segment_window_overlap_idx`（策略级多粒度，账户级仍 `_compute_one_perf_entry`）、`_PERF_SEGMENT_STATIC_DAYS`（K 配置化）、`_CALIBER_VERSION="segmented-v1"`（CS4 由可选转本轮实现）；顶层 `compute_and_store_performance_metrics` 同名保留。**以实际实现为准**（详见架构 §17「B 段实现说明」；本表 A 段草案命名不改写）。

### 6.3 发布链路影响（若进入实现阶段）

- `dashboard/backend/services/` 变更 → 按部署规则 6.6 重建 **data-backend** 与 **dashboard-api**（`FULL_COPY_SERVICES` 已有 this 映射，见 [deployment.md](../../../.trae/rules/deployment.md)）。
- `ai_tuner/allocation/` 变更 → 重建 **ai-tuner**。
- `shared/` 变更（若要抽公共「资金段」切分逻辑）→ 触发**全部策略容器**重建，需审慎评估，尽量不改 shared。

### 6.4 数据风险

1. **口径切换造成全策略历史曲线突变（非仅 HRS）**：修复后 5 策略（除 grid 不可评分）max_dd 均**显著变化**——HRS 100%→18.82%、btc_eth ≈70%→1.40%~3.22%、btc_eth_aggressive ≈9.86%→1.65%、new_coin ≈6.70%→4.77%~4.98%（架构 §10.1 预演表 / §12.1 风险 2）。部署前需导出**全部 5 策略**当前 4 桶旧值作对比基线（非仅 HRS），支持回退。
2. **段边界还原错误**：若从 `capital_allocation` / `strategy_position_snapshot_history` 还原段边界出错（如误把每日漂移当段边界），会造出大量伪段、分母失真。这是 R3 数据精度核心风险（缓解：辅锚 E3「恒定 ≥ K 天」配置化，架构 §12.1 风险 1）。
3. **104.22 静态值来源不明**：若段边界还原依赖该事件的时点，来源未定可能影响 9/11–9/21 这一段的切分（缓解：按事实值切段、不臆断修正，架构 §12.1 风险 3）。

### 6.5 回滚与安全（AC-4 已定稿 A，回填为可选路径）

- `performance_metric_snapshot` 为 UPSERT 覆盖写；**本轮定稿 A 不回填历史桶**，无需数据迁移回滚（快照是派生数据，随算法在下轮 600s 定时任务自动覆盖当前 4 桶，架构 §8.3）。
- 生产写库仅限「未来改选 AC-4 选项 B 回填」且须人工授权；本需求阶段**不写库**。
- 遵循「禁止硬编码」：段边界还原阈值（辅锚 E3 K 值）、段初分母取法、持仓级判定窗口等一律配置化或算法推导，不得写死数值。

---

## 7. 核实记录（代码出处）

| 文件/位置 | 关键结论 |
|-----------|---------|
| [data_service_docker.py L2421-2516](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2421-L2516) | 绩效预计算；一次构建、多粒度尾部切片 |
| [data_service_docker.py L2438](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2438) | 窗口起点 = today - 365d |
| [data_service_docker.py L2475](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2475) | `_PERF_WINDOW_DAYS = {day:30, week:90, month:180, year:365}` |
| [data_service_docker.py L2541-2545](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2541-L2545) | 尾部截取 `daily_returns[-window_days:]` |
| [data_service_docker.py L2617-2700](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2617-L2700) | `_build_strategy_daily_returns` 全窗口累积 + 钳 0 |
| [data_service_docker.py L2659](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2659) | `base_capital` 入参 = 365 天前（取值时点错） |
| [data_service_docker.py L2702-2787](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2702-L2787) | `_query_strategy_base_capital` 4 步回退链 |
| [data_service_docker.py L2859-2916](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2859-L2916) | `_attach_performance_metrics` 只读当前桶 |
| [daily_refresher.py L97-199](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/daily_refresher.py#L97-L199) | 每日刷新主流程 + `_recalculate_entries` 只改 allocated_amount |
| [daily_refresher.py L32](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/daily_refresher.py#L32) | cron 默认 `15 0 * * *` |
| [config_updater.py L95-157](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/config_updater.py#L95-L157) | entries 写入字段（含 initial_capital） |
| [pnl_collector.py L50-59](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/pnl_collector.py#L50-L59) | `_PNL_QUERY_TEMPLATE` 整表 BOOL_OR（无汇总组明细漏计来源，机制①） |
| [pnl_collector.py L324-379](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/pnl_collector.py#L324-L379) | `_query_base_capital` 4 步回退链 |
| [allocation_calculator.py L214-267](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/allocation_calculator.py#L214-L267) | `initial_capital = s["capital"]`、排名输入 |
| [capital_manager.py L121-173](file:///Users/yl/vscode/Binance_quantitative_trading/shared/capital_manager.py#L121-L173)、[L318-363](file:///Users/yl/vscode/Binance_quantitative_trading/shared/capital_manager.py#L318-L363) | 限额读取（不动，仅确认影响面边界） |
| [09-performance-metric-snapshot.sql L20-35](file:///Users/yl/vscode/Binance_quantitative_trading/database/postgres/init-scripts/09-performance-metric-snapshot.sql#L20-L35) | `UNIQUE(granularity,scope,strategy_id,bucket_key)` |
| [performance_reader.py L87-148](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/performance_reader.py#L87-L148) | ai_tuner 读取器可兜底读历史桶 |

---

## 8. A-review 复核回应记录（R3 设计复核，2026-10-15）

本节逐条记录需求文档 v1.1 对设计复核 5 项阻塞（B1–B5）的回应，以及在正文各处的「原表述 → 复核发现 → 修订后表述」位置。修订后需求文档与架构 v1.1 已无互相矛盾。

### 8.1 B1｜非 HRS 策略段结构未预演、「其它策略基本不变」被证伪

- **原表述**：AC-1 未给非 HRS 策略期望值；未定义「策略部分期间分母为 0」的段行为，grid 事实描述与「本就无快照行」不符；§6.4 风险 1 措辞偏「变化主要来自 HRS」。
- **复核发现**：btc_eth / new_coin / btc_eth_aggressive 与 HRS 跳变结构同构（均含 9-11 静态跳变 + 9-21 起每日漂移 + 10-09 月切换）；btc_eth_aggressive 有「9-10~9-20 `allocated_amount` 恒 0 却有成交」的零分母期；grid 在 `snapshot_history` **无任何行**（非「退化单段」）。
- **修订后表述**：§4.3 新增 **AC-1.6（5 策略期望值表）**、**AC-1.7（grid 不可评分、无回归）**、**AC-1.8（零分母期整段跳过 + 日志）**，并补「段链事实澄清（全策略显著变化）」；§6.4 风险 1 改为「全策略历史曲线突变（非仅 HRS）」；§3.1 目标 1、§6.1 附加因素同步标注。

### 8.2 B2｜段初取值方向三处矛盾

- **原表述**：§4.1 术语「段初分配额」写「来自 `initial_capital` 或段起点快照 `allocated_amount`」，未锁定「生效时刻之后向后取」。
- **复核发现**：架构内部曾三处方向不一（当日 00:00 起首条 / 向前取最近一条 / 跳变后首条），分母不唯一则 18.82% 无法落地。
- **修订后表述**：§4.1 术语表「段初分配额」改为架构 §4.1/§5.1 唯一定义（段事件生效时刻之后、段内首条正快照 `allocated_amount`；不用 `initial_capital`、不回退「全表最早一条」）；§4.3 AC-1.2 同步为「段初正快照（段 B=104.22 / 段 C=53.84 / 段 D=56.81）」。

### 8.3 B3｜AC-2 因果链无证据、绝对值断言不可复现

- **原表述**：§2.4/§4.4 用「跨月持仓双计」归因，锚定「10 月 +10.29 → income 真值 +3.43」验收。
- **复核发现**：逐笔对账证伪；差异 −5.2765 全部来自「无汇总组明细」，非跨月双计；+10.29/+3.43 已飘移、不可复现。
- **修订后表述**：§2.4 改为「三机制量化归因」（①无汇总组明细 −5.2765 ②CONDITIONAL_ORDER 同值双写 0 ③跨月 carry-in +3.03454）；§4.4 改为「三机制 + 相对断言」——**AC-2.2a**（锚 DB 精确复算 −5.2765）**AC-2.2b**（方向性收敛），并明示「本任务不修机制③跨月 carry-in（归 hrs_pnl_governance）」。

### 8.4 B4｜AC-3.1 验收语义与架构决策相反

- **原表述**：§4.5 AC-3.1「看板回撤分母与分配记录 `initial_capital` 取值一致（`initial_capital` 优先、段起点快照回退）」。
- **复核发现**：架构决策弃用 `initial_capital`（10 月 = 76.82024229 与快照最早 `open_margin` 精确相等，是旧口径残值、月粒度无法区分月内多段），原回退方向相反。
- **修订后表述**：§4.5 AC-3.1 改为架构 §5.1「分母契约」（分母 = 段事件生效时刻后段内首条正快照 `allocated_amount`；`initial_capital` 降级为历史审计字段、不再作分母，并提示字段语义注释/docstring 在 B 段同步）；AC-3.2 改为「月粒度 + 容差」同源对账；§4.1 术语表同步。

### 8.5 B5｜AC-4 待拍板未闭环

- **原表述**：§4.6/§5 #1 自标「待用户拍板」「本轮不决定」，A vs B 矛盾未闭环。
- **复核发现**：用户两问选择矛盾（第 4 项勾「回填」、专门一问选「不主动回填」），未闭环即进 B 段违反 GA 条件。
- **修订后表述**：§4.6 定稿「**选项 A（自然重算，不主动回填）**」（以专门一问为准），写 3 条可见后果为「接受声明」，并保留「改选回填」可选路径（迁移脚本 + 幂等 + 生产写库审批，需用户另行批准）；§5 #1 移出待拍板项。

### 8.6 逐条对应关系核对（需求 ↔ 架构 v1.1）

| 需求回写点 | 架构定稿处 | 是否一致 |
|-----------|-----------|---------|
| §2.4 三机制归因 | §7.1 | 一致 |
| §4.3 AC-1.6 期望值 / AC-1.7 grid / AC-1.8 零分母期 | §10.1 / §4.4 / §4.5 | 一致 |
| §4.4 AC-2.2a / 2.2b 相对断言 | §7.1 / §10.3 | 一致 |
| §4.5 AC-3.1 分母契约 | §5.1 / §5.3 | 一致 |
| §4.6 AC-4 定稿 A | §8.1 | 一致 |
| §5 待拍板项收敛（剩 2 项） | §14 | 一致 |
| §6 全策略显著变化 + R3 | §12.1 | 一致 |

> 结论：本文档 v1.1 与架构 v1.1 已无互相矛盾的表述。

---

（文档结束）