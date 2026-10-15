# 架构方案：看板资金基数 / 最大回撤口径修复

## 1. 文档信息

| 项 | 内容 |
|----|------|
| 文档名称 | 架构方案：看板资金基数 / 最大回撤口径修复 |
| 版本 | v1.1（A 段修订版，已闭环 R3 阻塞 B1–B5） |
| 作者 | backend-architect（DeepSeek-V4-Pro） |
| 上游需求 | `docs/plans/fix-2026-10-15-capital-base-caliber-requirements.md`（AC 编号以其为准） |
| 下游环节 | A-review（R3 设计复核，回本复核者）→ python-engineer（B 段实现） |
| 风险级别 | R3（资金精度 + 数据迁移 + 发布时间，见需求 §6.1） |
| 本轮边界 | 只出架构方案 + 只读数据预演；**不写实现代码、不写迁移脚本、不部署、不 push、不改容器** |

**AC 映射总表**：

| AC | 结论/设计位置 | 本轮是否改码 |
|----|---------------|-------------|
| AC-1（看板分段回撤） | §3 段定义 + §4 分段净值/回撤算法 + §5 段初取值 + §6 接口契约 | 是 |
| AC-2（ai_tuner 持仓级去重） | §7（因果链已按数据重归因，AC-2.2 改相对断言，需求 §2.4/§4.4 需回写） | 是 |
| AC-3（分母同源统一） | §5.3（最终分母口径契约已写死，需求 AC-3.1 需回写） | 是（口径对齐，不改 10 月已落地数据） |
| AC-4（历史快照处置） | §8（**选项 A 已定稿**，需回写需求 AC-4.2） | 否（本轮只出定稿结论，不执行回填） |

---

## 2. 方案概述

### 2.1 一句话结论

根因不是「统计 bug」，而是旧设计「**全窗口单一资金基数（固定值）**」与 ai_tuner「**每日按净资产刷新分配额**」机制不兼容：看板 `_build_strategy_daily_returns` 取 `at_time = today - 365d` 做单点查基数，HRS 首快照在 2026-08，365 天前无数据 → 命中兜底「全表最早 allocated_amount = 58.37」并**四个粒度共用这一个 58.37**（[data_service_docker.py L2659](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2659)、[L2702-2787](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2702-L2787)），而 HRS 实际分配额自 9/21 起已漂离 58.37（9/11~9/21 恒 104.22、9/22 起 52~58），错误的固定分母使策略「段内累计盈亏」多次击穿本金、净值被钳 0、回撤顶格 100%。

**修复方向**：以「资金段（capital segment）」为单位切分，段内用「段初分配额」作分母，段内独立累积、跨段不比较。已在本地只读数据预演中把 HRS 各粒度最大回撤从 100% 降到 **18.82%**（§10）。

### 2.2 核心事实（已读代码 + 生产只读核实，作为方案前提）

1. **段边界的历史留痕是充分且可还原的**：`public.strategy_position_snapshot_history` 有小时级 `snapshot_hour + allocated_amount`，生产实测 HRS `allocated_amount` 有 **18 个跳变点**（下表），段边界与每日漂移可从「跳变后的时间模式」区分；而 `public.capital_allocation.entries` 是 JSONB 单值、每日刷新 UPDATE **覆盖写、无逐日历史**（[daily_refresher.py L53-57](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/daily_refresher.py#L53-L57)）——所以段边界**不能**从 `capital_allocation` 还原逐日漂移，但**能**从 `snapshot_history` 还原。

```
HRS allocated_amount 跳变点（生产只读，snapshot_hour → allocated_amount）：
  2026-09-10 17:00 →  58.37    （首条，2026-08 首月 fallback ratio=0.2）
  2026-09-11 14:00 → 104.22    （静态值，来源未定论，9-11~9-20 恒定）
  2026-09-21 13:00 →  53.84    （2026-09 记录 ratio=0.1 生效 + 每日刷新启动）
  2026-09-22 起 00:00 逐日小幅漂移 52.67→53.17→…→53.77（每日刷新，不算段边界）
  2026-10-09 14:00 →  56.81    （2026-10 记录 month 切换，ratio 仍 0.1）
  2026-10-10 起 00:00 逐日漂移 56.55→…→57.92（每日刷新，不算段边界）
```

2. **`initial_capital` 字段历史口径不一致，不能作段初权威源**：`capital_allocation.entries` 里 HRS 的 `initial_capital` 9 月=58.37（allocated_amount 口径）、10 月=**76.82**。生产实测 76.82 = 快照表最早一条 `open_margin`（9-10 17:00 的 `open_margin=76.82024229`），即旧口径 `_query_month_start_margin` 的残值——因为 10 月补生成记录（`created_at=2026-10-09 14:48`）早于分母修复 commit `05888cc`（2026-10-13，见 [issue allocation-return-denominator-zero](../../knowledge/issues/Binance_quantitative_trading-20261013-allocation-return-denominator-zero.md)）。详见 §5.3。

3. **看板绩效遍历 5 个策略**（含 grid）：`PERFORMANCE_STRATEGY_IDS = [btc_eth, btc_eth_aggressive, new_coin, hrs, grid]`（[strategy_ids.py L12-18](file:///Users/yl/vscode/Binance_quantitative_trading/shared/strategy_ids.py#L12-L18)）；`trade_records.strategy` 落库英文短名，`_db_strategy_names = _db_strategy_ids`（[data_service_docker.py L64](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L64)）。grid 的事实（生产只读）：`snapshot_history` **无任何行**、`trade_records` 自 2026-09 起**零成交**，`performance_metric_snapshot` 的 day/week 桶只到 10-09 后停更、month/year 桶残留 `max_drawdown=0.0`（详见 §4.4，非「退化单段」，而应标记「不可评分」）。

---

## 3. 现状 vs 目标口径对照（含公式）

### 3.1 公式对照

| 维度 | 现状（旧口径，缺陷） | 目标（新口径） |
|------|---------------------|----------------|
| 资金基数取值时点 | `at_time = today - 365d`（[data_service_docker.py L2659](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2659)）单点查一次 | 每个资金段取「段起点」快照 `allocated_amount` |
| 基数粒度 | 全窗口一个值，四粒度共用 | 每段一个值，段内固定 |
| 净值构建 | 完整 365 天序列累积 cum_pnl，`net_value = (base + cum)/base`（[L2687-2692](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2687-L2692)） | 段内独立累积，段初 cum 重置 0 |
| 多粒度切片 | 尾部切片 `daily_returns[-window_days:]` 不重基准（[L2541-2545](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2541-L2545)），窗口峰值继承全序列 | 先分段、段内重基准，再按窗口取各段 max_dd |
| 回撤语义 | 跨段净值继承 → 前段触 0 污染后段（伪 100%） | 分段各算取最大，跨段不比 |
| 无数据期（首段前） | 回退「全表最早一条 58.37」兜底（[L2745-2756](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2745-L2756)） | 跳过（不可评分），不退化为未来值 |

### 3.2 目标口径公式（策略级）

```
对每个策略，按其「资金段」边界切分：
  段内净值  nv_t = max(0, 1 + cum_pnl_in_segment_t / segment_opening_capital)
  段内日收益 r_t = day_pnl_t / segment_opening_capital        # 供 sharpe
  段内最大回撤 dd_seg = max_drawdown_ratio(段内 net_values)
  总体最大回撤 max_dd = max(各段 dd_seg)                        # 跨段不比较
```

其中 `segment_opening_capital` = 段起点的 `allocated_amount`（快照值，不是 `initial_capital`，理由见 §5.3）。

---

## 4. 问题 1 + 问题 3：资金段定义与分段回撤算法

### 4.1 段边界判定源（问题 1）

**结论：段边界 = 分配计划变更事件，日内金额漂移不算段边界。** 判定来源采用「主锚 + 辅锚」两层：

| 锚 | 事件 | 数据来源 | 判定依据 |
|----|------|---------|---------|
| 主锚 E1 | month 切换（`capital_allocation` 新增一条 month 记录） | `public.capital_allocation.month` | month 值变化 |
| 主锚 E2 | `allocated_ratio` 变更（相邻 month 记录的 `entries[x].allocated_ratio` 不同） | `capital_allocation.entries.allocated_ratio` | ratio 值变化 |
| 辅锚 E3 | 「非逐日漂移」的显著跳变（无法归入 E1/E2，如 104.22） | `snapshot_history.allocated_amount` 跳变点 | 跳变后**保持恒定 ≥ K 天**（K 配置化），且跳变发生在非每日刷新时刻 |

**去噪规则（区分「段边界」vs「每日漂移」）**：
- 每日漂移特征：跳变发生在每日刷新时刻（北京时间 00:00 附近 → UTC 前一日 16:00 附近），单日幅度小，且**连续多日逐日变动**（9/22 起 52.67→53.17→…→53.77）。
- 段边界特征：① 能对齐到 E1/E2（month/ratio 变更），或 ② 跳变后**恒定 ≥ K 天**（104.22 恒 10 天）。

**为什么不用金额阈值切段**：需求 §3.1 已明确，9/22 起 `allocated_amount` 本身逐日随净资产漂移，金额阈值法会造出大量伪段。本方案用「跳变后是否逐日漂移」这一**时间模式特征**替代金额阈值，可稳健区分两者。

**E2 依附 E1 的局限（B2 明确记录）**：`public.capital_allocation.entries` 表**只有 `created_at`、无 `effective_time`**（[monthly_job.py L94-105](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/monthly_job.py#L94-L105)），且月内手工 ratio 变更走 `UPDATE` **不改 `created_at`**。故 E2（ratio 变更）的**精确时点不可得**，本方案 E2 只能依附 E1（月记录 `created_at`）作出「相邻月 ratio 不同」的判断；E2 无法作为独立时点在月内切段。月内手工 ratio 变更导致的漂移由辅锚 E3（静态跳变或逐日漂移）兜底，但**若该月内变更本身无静态留痕则 E3 亦无法识别**（局限记录见 N6/§12.2）。

**伪码**：

```
derive_segments(strategy_id):
    events = []
    # 主锚：capital_allocation 记录（字段为 created_at；E2 依附 E1 同刻）
    for each capital_allocation month record (按月递增):
        events.append((record.created_at, E1_month_switch))
        if ratio 与上月不同:
            events.append((record.created_at, E2_ratio_change))   # 时点 = E1 的 created_at
    # 辅锚：snapshot_history 静态跳变（不被 E1/E2 覆盖的）
    jumps = snapshot_history 中 allocated_amount 变化的时刻
    for jump in jumps:
        if jump 后恒定 >= K 天 且 不对齐任何 E1/E2:
            events.append((jump.snapshot_hour, E3_static_jump))
    # 段起点 = 事件时点之后该策略首个 allocated_amount > 0 快照
    return 按时间排序、去重后的段边界列表
```

**段边界时点 → 段初分配额的落地规则（B2 唯一定义，取代 v1.0 的三处分歧）**：段边界事件的「生效时刻」统一取 `created_at`（E1/E2）或跳变 `snapshot_hour`（E3），段初分配额 = **该生效时刻之后、该段内的首条 `allocated_amount > 0` 快照**（向后取，不是「含当日 00:00 起首条」、也不是「向前取最近一条」）。对段 D 而言，生效时刻 = 10-09 14:00 跳变后，首条正快照 = 10-09 14:00 的 56.81（**不是** 10-09 00:00 的 53.77 旧段尾值，也不是 10-08 的 53.77）。§5.2 的查询语义、§10.1 预演采用值均与本条一致。

### 4.2 段内净值与回撤（问题 3 前半）

见 §3.2 公式。要点：
- 段内不复利：`nv = 1 + cum/opening`，与旧口径「固定基数 + 累加」哲学一致（避免旧口径复利导致的 >100% 回撤回归）。
- 负值钳 0：`nv < 0 → 0`（本金亏光语义，回撤上限 100%），单段内可触 0，但不影响其它段。
- 跨段不比：每段 `dd_seg` 独立，`max_dd = max(dd_seg)`。

### 4.3 多粒度窗口与段的相交规则（问题 3 后半，确定性算法）

**结论：不能靠尾部切片。必须在完整序列上先按段切分、段内重基准，再按窗口截取各段的子序列算回撤。**

```
compute_perf(strategy_id, granularity):
    window_days = _PERF_WINDOW_DAYS[granularity]   # day30/week90/month180/year365
    window_start = today - window_days 天
    segments = derive_segments(strategy_id)
    dd_list = []
    for seg in segments:
        if seg 与 [window_start, today] 不相交:
            continue
        # 段与窗口相交部分的净值序列：从 max(seg.start, window_start) 起累积
        cum = 0
        net_values = []
        for d in [max(seg.start, window_start) .. seg.end]:
            cum += day_pnl[d]
            net_values.append(max(0, 1 + cum / seg.opening_capital))
        dd_list.append(max_drawdown_ratio(net_values))
    return max(dd_list)  # 各段取最大
```

**关键语义（回应 AC-1.5）**：段与窗口相交的「残缺段」（窗口起点落在段中间）时，该段净值**从窗口起点重新归一（cum 从 0 起）**，不继承段起点之前已发生的盈亏。段初分配额 `seg.opening_capital` 仍是该段的**真实段初快照值**（不随窗口重取），因为它是段的名义资金基数。

> 说明：这与需求 §4.2「跨段不比较」是两层语义——「跨段不比」隔离**段间**峰值；「段内重新归一」处理**窗口切段**，两者叠加后，任一粒度窗口内的回撤只反映「近 N 天内、该策略各资金段各自由段初基数出发的跌幅」，不跨段、不继承更早峰值。

### 4.4 非分配策略（grid）——按事实改为「不可评分」（B1 修正）

生产只读事实（2026-10-15）：grid 在 `strategy_position_snapshot_history` **无任何行**（`allocated_amount` / `open_margin` 均无）、`trade_records` 自 2026-09 起**零成交**、`capital_allocation` 无记录。故 grid 无 E1/E2/E3 任何事件，且 4 步回退链三步（allocated_amount 最早、open_margin 最近、open_margin 最早）全部无值可退。

- **v1.0 误述**：原 §4.4 称 grid「段边界退化到单段、段初退化 open_margin」，与「本就无快照行」的事实不符。
- **修订**：grid 在新口径下 **标记「不可评分」**（与「首段前无数据期跳过」同一语义），其 `performance_metric_snapshot` 残留行（day/week 至 10-09、month/year 为 `0.0`）**维持现状不重写**，新算法对 grid **不产生任何新行、不覆盖任何旧值** → **无回归**。grid 不参与资金分配，回撤口径问题对 grid 无业务影响。

### 4.5 零分母期（allocated_amount = 0 却有成交）的处理（B1 补充）

生产只读事实：`btc_eth_aggressive` 在 **9-10 ~ 9-20** 的 `allocated_amount` 恒为 0.00，但同期有真实成交（9 月该策略 320 笔成交中的前段部分，`_DAILY_PNL_QUERY` 有对应日盈亏行）。

**定义（一句话契约）**：某资金段若其段初分配额（生效时刻后首条正快照）**为 0 或不存在**，则该段**整段跳过、不计入任何段的回撤**，也不得静默用 0 或其它段的值作分母——与「首段前无数据期跳过」完全同一语义（需求 AC-1 边界表第 3 行「分母为 0 / 无快照 → 跳过或不可评分」）。

- 对 btc_eth_aggressive：9-10~9-20 零分母期整段跳过；其首个可评分段自 9-21（段初 107.69）起。9-10~9-20 期间的日盈亏（含 +7.33、+15.51 等我方策略实绩）**不计入**回撤口径（因分母为 0 无法归一同基）。
- 该行为是「段初分配额作分母」的自然推论：分母缺失即不可评分，不得回退「全表最早一条」或「未来值」（消除旧口径兜底 58.37 的同类退化风险）。

---

## 5. 问题 2 + 问题 4：段初分配额取值 与 base_capital 时点修正

### 5.1 段初分配额取值（问题 2）——最终分母口径契约（B4 写死）

**一段话契约（这是看板策略级回撤分母的唯一口径，AC-3.1 验收据此改写）**：

> **看板某策略某资金段的回撤分母 = 该段边界事件生效时刻（E1/E2 取 `capital_allocation` 记录 `created_at`、E3 取跳变 `snapshot_hour`）之后、该段内首条 `allocated_amount > 0` 的快照值。** 该值来自 `public.strategy_position_snapshot_history.allocated_amount`，**不用 `entries.initial_capital`，也不回退「全表最早一条」**；段初分配额为 0 或不存在时，该段标记「不可评分」（§4.5）。

| 候选 | 语义 | 缺陷/取舍 |
|------|------|-----------|
| `snapshot_history.allocated_amount`（生效时刻后首条正快照） | 名义资金基数（净资产 × ratio，不含杠杆），**小时级、与段边界对齐** | **采纳**（方向=生效时刻之后向后取，见 §4.1 落地规则） |
| `entries.initial_capital` | 月度 ranking 分母，整月不变 | **弃用**：历史口径不统一（9 月=allocated 58.37，10 月=open_margin 残值 76.82），且**月粒度无法区分月内多段**（HRS 9 月内有 58.37/104.22/53.84 三段，initial_capital 只有一个 58.37） |

> **需求回写坐标（B4）**：需求 AC-3.1「看板回撤分母与分配记录 `initial_capital` 取值一致（允许 `initial_capital` 优先、段起点快照回退）」的**回退方向与本节决策相反**（本方案弃用 `initial_capital`、以段初快照为唯一分母）。需求应回写为「分母 = 段起点首条正快照 `allocated_amount`」；与 `capital_allocation` 的同源性改由「记录创建时刻初值，月粒度和容差对账」（§5.3）验收，不再要求等于 `initial_capital`。

### 5.2 `_query_strategy_base_capital` 改造（问题 4）

**结论：废弃「全窗口单一基数 + at_time 单点查询」，改为「按段取段初 allocated_amount」。**

- 现有 4 步回退链（[L2702-2787](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2702-L2787)）改造后的语义：**只在单个资金段内部**查询「该段边界事件生效时刻**之后**、段内首条 `allocated_amount > 0`」（即旧链第 1 步的**反方向**：旧链是 `snapshot_hour <= $2 ORDER BY DESC` 向前取，[L2731-2740](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2731-L2740)，改造后为 `snapshot_hour >= 事件时点 ORDER BY ASC LIMIT 1` 向后取）。段事件时点之后无正快照（首段前无数据期 / 零分母期）→ **返回「不可评分」而非回退第 2 步「最早一条 58.37」**。
- 兜底「最早一条」不再用于无数据期（消除需求待拍板项 4 的退化风险）；`open_margin` 兜底（旧链第 3/4 步）仍保留，仅用于「该策略**所有**段均无 allocated_amount、但快照表有 open_margin」的极端退化（当前 5 策略无一命中；grid 连 open_margin 也无不适用，见 §4.4）。
- `at_time` 参数语义从「窗口起点」改为「段边界事件生效时刻」；`compute_and_store_performance_metrics` 不再传 `start_date`（`today-365d`）而传「每个段的段事件时点」。当前 `_build_strategy_daily_returns` 的签名 `(strategy_id, start_date, end_date)` 相应扩展为「段列表」或内部自行推导段边界。

### 5.3 AC-3 同源统一（看板 vs 分配记录）

**结论：看板段初分配额与 `capital_allocation` 在「month 粒度」上可对账一致，但不要求逐日相等，也不要求等于 `initial_capital`。**

- 同源点：两者最终都来自「分配额 = 净资产 × allocated_ratio」这一记账口径；段初快照 `allocated_amount` 与当月 `entries.allocated_amount`（每日刷新产物）同源。
- 对齐点（AC-3.1 回写后的可验收口径）：对同一策略同一月，看板该月第一个资金段的段初 `allocated_amount` 应**与当月 `capital_allocation.entries[x].allocated_amount` 在「记录创建时刻」的初值在月粒度、容差内对账一致**（而非等于被每日刷新漂移后的当前值，更非等于 `initial_capital`）。实测 HRS 10 月：记录创建于 10-09 14:48、初始 `allocated_amount=57.92`（=579.17×0.1）；段 D 段初快照 10-09 14:00=56.81（当日 00:15 每日刷新值），二者差 1.9%，同源可对账（差异来自记录创建与每日刷新两个不同时点的净资产）。
- **不越界**：`initial_capital` 是 ai_tuner 月度 ranking 的既有分母字段，本轮**不改其历史值**（AC-3.3：10 月已落地记录不变）；仅在看板侧不再把它当段初分母。`initial_capital` 口径不统一的根因（10 月是 open_margin 残值）已在 2026-10-13 修复（05888cc），11 月起新记录 `initial_capital` 将回到 allocated_amount 口径，与段初快照进一步收敛；本轮不追溯改 10 月。**10 月 `initial_capital`=76.82 与段初快照 56.81 的数值差异为已文档化的既成残值，不作为本条验收对象（B4）。**

---

## 6. 问题 5：快照表 schema 与调用方影响、CI 映射

### 6.1 schema 是否变更

**结论：核心不改 schema**（`max_drawdown` 仍是 0~1 单值，只是计算方式变），并**建议追加一个可选审计列 `caliber_version`**（非必需）。

| 项 | 决策 | 理由 |
|----|------|------|
| `max_drawdown` 等既有列 | 不改类型/语义 | 仍是比例值，消费方无需改 |
| `UNIQUE(granularity,scope,strategy_id,bucket_key)` | 不变 | UPSERT 仍按桶覆盖 |
| 可选 `caliber_version`（`VARCHAR(16)`，如 `'segmented-v1'`） | 建议加（ADD COLUMN IF NOT EXISTS，可空） | 用于审计「当前桶由哪个口径计算」，为 AC-4 回滚留批次标记；不加也能正确跑 |

> 段边界**不**落地新表/新字段（需求待拍板项 3）：段边界可从 `snapshot_history` + `capital_allocation` 确定性实时推导（§4.1 已证明可还原），无需持久化；持久化反而引入「段表与快照表双写一致性」的新风险。如需调试，用日志打印每段的 (段起点, 段初分配额, 段内 dd)。

### 6.2 调用方全量清单

| 容器 | 文件/函数 | 角色 | 影响 |
|------|-----------|------|------|
| **data-backend** | `services/data_backend/src/main.py` → `run_performance_metrics` → `DataService.compute_and_store_performance_metrics`（[main.py L111-120](file:///Users/yl/vscode/Binance_quantitative_trading/services/data_backend/src/main.py#L111-L120)） | **写**快照 | 新分段算法在此生效 |
| **dashboard-api** | `routes_docker.py L173` `_attach_performance_metrics`（[routes_docker.py L170-173](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/api/routes_docker.py#L170-L173)）；`get_performance_metrics`（[data_service_docker.py L2807-2816](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2807-L2816)） | **读**当前桶 | 只读，无需改 |
| **ai-tuner** | `ai_tuner/allocation/performance_reader.py::get_latest`（[L87-148](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/performance_reader.py#L87-L148)） | **读**（`bucket_key <= target ORDER BY DESC LIMIT 1`） | 只读，无需改 |
| 前端 | 读 dashboard-api overview 的 `total_max_drawdown / strategies[].max_drawdown` | 展示 | 无需改 |

### 6.3 CI 重建映射（问题 5 后半）

**结论：现有 deploy.yml 已覆盖，无需新增规则。**

- 本次改 `dashboard/backend/services/data_service_docker.py` → 命中 6.6 规则（`dashboard/backend/` 变更 → **data-backend 重建**，[deploy.yml L178-L183](file:///Users/yl/vscode/Binance_quantitative_trading/.github/workflows/deploy.yml#L178-L183)），且因 dashboard-api 是 FULL_COPY_SERVICES（[L160-L168](file:///Users/yl/vscode/Binance_quantitative_trading/.github/workflows/deploy.yml#L160-L168)）会**同时重建 dashboard-api**——恰好覆盖「data-backend 写 + dashboard-api 读」两个容器，正是上一轮 8cf1ae4 部署遗漏后补上的正确映射。
- 本次改 `ai_tuner/allocation/pnl_collector.py`（AC-2）→ 触发 **ai-tuner** 重建（目录匹配）。
- **不触及 `shared/`**，避免触发全部策略容器重建（§7 越界结论）。

---

## 7. 问题 7：ai_tuner 月度口径持仓级去重

### 7.1 缺陷归因（B3 数据重归因）与改造方案

**原归因作废**：需求 §2.4 / §4.4 将 AC-2 缺陷表述为「判定粒度不一致 → 跨月持仓（上月有明细、本月补 `PNL_SUMMARY`）在两个月各计一次」，并以 10 月旧口径 +10.29 vs income 真值 +3.43（差约 6.86）作验收锚点。本轮生产只读逐笔对账（2026-10-15）证实该因果链不成立，改按数据证据重写。

**10 月 HRS 各口径实测（生产只读，截至 2026-10-15）**：

| 口径 | 数值(USDT) | 统计目标 |
|------|-----------|---------|
| A 整表 BOOL_OR（旧 ai_tuner `f940a61` 现状） | **+17.90** | 整月窗口只要任一持仓有 `PNL_SUMMARY`，就对全表只 `SUM(order_type='PNL_SUMMARY')`，**丢弃**无汇总组明细 |
| D 持仓级 CTE（dashboard `91e99c2` / 本节升级目标） | **+12.62** | 按 (strategy,symbol,side) 分组，有汇总组取 summary、无汇总组回退明细 SUM |
| D − A | **−5.28** | **全部**来自「无汇总组明细」（见三机制①），非跨月双计 |
| E income 真值（币安 REALIZED_PNL 归 HRS） | **+3.4292**（10-13 对账值） | 外部唯一真值；10-15 未重拉、随新增成交漂移（[double-count issue](../../knowledge/issues/Binance_quantitative_trading-20261013-hrs-pnl-double-count.md) 真值表） |

**三机制归因（差异构成逐笔量化）**：

① **无汇总组明细漏计（统计层，本节 AC-2 可修，量 = −5.2765）**——这是 D 与 A 的**全部**差异。`_PNL_QUERY_TEMPLATE` 的缺陷并非「跨月双计」，而是「整表任一持仓有 summary 即全表只看 summary」，使「该月内真实发生、但未写 `PNL_SUMMARY`」的持仓明细被**漏计**。生产实测 10 月有 4 组无汇总持仓：LITEUSDT −2.6035、ONDOUSDT −3.2190、NBISUSDT +0.3861、QQQUSDT +0.1599，合计 −5.2765。这些组的明细行是 income 真值里**已包含**的已实现盈亏，旧口径漏计导致 10 月 PnL 被**高估** 5.28。

② **CONDITIONAL_ORDER 同值双写（有汇总组不双计，量 = 0）**——12 个有汇总组存在 `summary == detail`（如 ARBUSDT 10-12 `CONDITIONAL_ORDER` −2.874080 与 `PNL_SUMMARY` −2.874080 同值；1000SHIBUSDT 明细合计 3.057761 == summary）。持仓级 CTE 对有汇总组只取 summary、不取明细，故这些组**不会**双计；这是「持仓级 CTE 对有汇总组正确」的实证，**非差异来源**。

③ **PNL_SUMMARY 累计语义跨月 carry-in（写入层，非 AC-2 可修，量 = +3.03454）**——`insert_pnl_summary` 写的是「整笔平仓的累计盈亏」（[trade_logger.py L461-519](file:///Users/yl/vscode/Binance_quantitative_trading/shared/trade_logger.py#L461-L519)，docstring「整笔平仓的累计盈亏」、`since=entry_time` 幂等去重），自开仓起累计、可跨月。生产实测 2 组跨月持仓：SEIUSDT（10 月 summary +1.91109 vs 当月明细 +1.04345，carry-in +0.86764）、牛来USDT（summary +3.73105 vs 当月明细 +1.56415，carry-in +2.1669），合计 **+3.03454**。持仓级 CTE **无法**消除此 carry-in（它只区分 summary/明细层取值，不重定义 summary 的累计语义），且「重做 HRS 写入层」已被需求 §3.2 非目标 4 排外，归 [hrs_pnl_governance](../../knowledge/issues/Binance_quantitative_trading-20261013-hrs-pnl-double-count.md) 后续处理。

**修正后结论**：AC-2 的正确方向仍是「整表 BOOL_OR → 持仓级 BOOL_OR（与 dashboard `_DAILY_PNL_QUERY` 对齐）」，但它修复的是机制①（无汇总组明细漏计）并顺带统一 ai_tuner/dashboard 两侧口径；它**不能**、也不负责消除机制③（跨月 carry-in）与 income 以外的已有系统性差异（手续费、symbol 污染、孤儿 income 等，均已文档化于 [hrs_income_reconcile_report.md](../../reports/hrs_income_reconcile_report.md)）。

**AC-2.2 改为相对断言（B3，替代 +10.29/+3.43 历史时点绝对值）**：

- **AC-2.2a（精确复算，锚 DB，不依赖 income API）**：持仓级口径 − 整表 BOOL_OR 口径 == 无汇总组明细 `SUM(realized_pnl)`（10 月 == −5.2765），可由 §10.3 生产对账 SQL 逐分精确复算，两者必须一致。
- **AC-2.2b（方向性收敛）**：持仓级口径比整表口径更接近 income 真值（补回旧口径漏计的、income 确已发生的无汇总组真实盈亏）；残余差异 = 跨月 carry-in（+3.03454）+ income 对账已有系统性差异，**不以绝对值锚定验收、不由 AC-2 消除**。

**改造 SQL（目标形态不变：持仓级整月聚合，与 dashboard 持仓级 CTE 对齐）：**

```
-- 目标形态（持仓级整月聚合，与 dashboard 持仓级 CTE 对齐）
WITH grp AS (
    SELECT strategy, symbol, side,
           BOOL_OR(order_type = 'PNL_SUMMARY') AS has_summary
    FROM trading.trade_records
    WHERE strategy = ANY($1::text[]) AND executed_at >= $2 AND executed_at < $3
    GROUP BY strategy, symbol, side
)
SELECT COALESCE(SUM(t.realized_pnl)
                 FILTER (WHERE g.has_summary AND t.order_type = 'PNL_SUMMARY'), 0)
     + COALESCE(SUM(t.realized_pnl) FILTER (WHERE NOT g.has_summary), 0) AS total_pnl
FROM trading.trade_records t
JOIN grp g
  ON g.strategy IS NOT DISTINCT FROM t.strategy
 AND g.symbol   IS NOT DISTINCT FROM t.symbol
 AND g.side     IS NOT DISTINCT FROM t.side
WHERE t.strategy = ANY($1::text[]) AND t.executed_at >= $2 AND t.executed_at < $3
```

- 判定窗口 = 整月（采集范围 `[month_start, month_end)`，与现状一致），不向前后延展——与 dashboard `_DAILY_PNL_QUERY` 的「窗口内持仓级 BOOL_OR」语义一致（需求待拍板项 5 锁定「与 dashboard 一致」）。
- 无汇总组（含非 HRS 策略）→ `has_summary=false` → 回退 `SUM(realized_pnl)`，与旧口径一致（AC-2.3）；对有汇总组则只取 summary、天然不双计（机制②）。

**需求回写坐标（B3）**：需求 §2.4「问题三：跨月持仓双计」、§4.4 AC-2 的目标 / 输入影响 / AC-2.1 / AC-2.2 / AC-2.4 中「跨月双计」及「+10.29→+3.43」表述，需回写为「三机制归因 + AC-2.2 相对断言（AC-2.2a 精确复算 −5.2765、AC-2.2b 方向性收敛，残余 carry-in +3.03454 归 hrs_pnl_governance）」。§10.3 已补 AC-2 生产对账命令（原方案生产验收只有 max_drawdown SQL，缺 AC-2）。

### 7.2 是否抽公共模块 / 是否越界改 shared（问题 7 关键判断）

**结论：不抽 shared/，ai_tuner 侧独立实现持仓级 CTE，并加「SQL 文本守卫测试 + 注释互指」保证与 dashboard 语义一致。**

理由（明确记录「禁止重复代码」与「部署成本」的权衡）：
1. `_DAILY_PNL_QUERY` 在 `dashboard/backend/services/data_service_docker.py`，被 data-backend 与 dashboard-api 两个容器 COPY（data-backend Dockerfile `COPY dashboard/backend/{core,config,services}`）；`ai_tuner` 是独立容器，不 COPY dashboard 代码。
2. 若抽到 `shared/`，会触发**全部策略容器重建**（R3 资金风险 × 部署范围扩大），代价远高于「两份同构 SQL」的维护成本。
3. 两份 SQL 是**数据契约**（非可复用函数），语义一致性由测试 SQL 文本断言 + 注释互指保证，属可接受的「跨容器契约重复」，不适用「连续 5 行重复代码即违规」的机械判据（两者 GROUP BY 粒度不同：dashboard 按天、ai_tuner 按月）。

---

## 8. 问题 6 + 问题 8：历史快照处置 + 兼容与回退

### 8.1 历史快照处置（问题 6，AC-4 定稿结论）

**结论：定稿选项 A（自然重算，不主动回填历史桶）。** 以用户在需求 §4.6「另一问」的专门选择「只改算法，快照随定时任务自然重算（不主动回填）」为准（B5 闭环）；第 4 项「重算并回填」勾选作为矛盾对栏、不采纳。

已核实的判断依据（全部来自需求 §4.6，本方案复核实为真）：
1. 前端只读当前桶：`_attach_performance_metrics` 查 `WHERE granularity=$1 AND bucket_key=$2`（今日桶，[L2875-2885](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2875-L2885)）。
2. 定时任务只 UPSERT 当前桶（[L2478-2484](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2478-L2484)），从不重写历史桶。
3. `ai_tuner performance_reader.get_latest` 用 `bucket_key <= target ORDER BY DESC LIMIT 1`，target 默认是**当前**周/月/年/天 → 会命中当前桶（自然重算后即新值），不依赖历史桶。

**不主动回填的可见后果（本方案明确接受，构成 AC-4.2 的「接受声明」）**：
1. 历史桶（非当前桶）残留旧口径 `max_drawdown`（如 HRS 历史 = 1.0），在**当前无历史回看界面的前提下永不可见**。
2. 前端总览 + dashboard-api 读的都是当前桶；定时任务在 **600s 内**用新口径 UPSERT 覆盖当前 4 个桶（[main.py L117](file:///Users/yl/vscode/Binance_quantitative_trading/services/data_backend/src/main.py#L117) 默认 `PERFORMANCE_METRIC_INTERVAL_SECONDS=600`）→ 用户在页面看到的即修复后的值，无感知差异。
3. 若未来新增历史回看能力，历史桶旧值会被展示，届时需另立任务按选项 B 回填（§8.3 的安全方案已给出，**保留改选回填路径**，无需重做架构）。

**技术依据（为何不主动回填）**：
- 主动回填历史桶是生产写操作（R3 数据迁移），收益仅在「未来出现历史回看」时体现，当前无此收益。
- 定稿 A 后仍可改选 B：§8.3 的选项 B 安全方案（逐桶重算 + UPSERT + 幂等 / 批次标记 / 可回滚）已就绪，改选不阻塞本轮实现。

**需求回写坐标（B5）**：需求 AC-4.2（若选 A 须明示「历史桶旧值残留、且当前无历史回看界面」的接受声明）已由本节满足——本轮架构已明确接受上述 3 条可见后果并定稿 A；需求 §4.6「待用户拍板项」的矛盾表述宜回写为「已定稿选项 A（自然重算不回填）」，并保留选项 B 的可选回填路径说明。

### 8.2 兼容性

- **读兼容**：不改 schema（或仅 `ADD COLUMN IF NOT EXISTS`），旧代码仍能读新桶（结构不变，`max_drawdown` 值变）。
- **字段兼容**：`caliber_version` 可空，旧代码/旧消费方不读它不受影响。
- **写兼容**：UPSERT 复合同一 UNIQUE，新算法覆盖同一 bucket_key，无冲突。

### 8.3 回滚方式

1. **回滚代码**：revert 相关 commit → 推 main → Actions 重建 data-backend + dashboard-api → 下一轮 600s 定时任务按旧口径 UPSERT 覆盖当前 4 个桶。**无需数据迁移回滚**（快照是派生数据，随算法自动覆盖）。
2. **口径对比**：部署前后保留旧口径 `max_drawdown` 快照（可在部署前用只读 SQL 导出当前 4 桶值作对比基线），一旦新口径异常可回滚到旧口径代码 + 旧桶自然还原。
3. 若曾加 `caliber_version`：回滚后旧代码不写该列，旧桶 `caliber_version` 为新值残留，但因消费方不读该列，无影响；如要彻底清，可`UPDATE ... SET caliber_version=NULL WHERE bucket_key 属于历史`（仅选项 B 场景）。

---

## 9. 问题 9：任务分工（可独立评审的变更集）

| 变更集 | 内容 | 修改文件 | 依赖 | 独立评审边界 |
|--------|------|---------|------|-------------|
| **CS1 段定义与分段净值/回撤** | 段边界推导 + `_build_strategy_daily_returns` 分段累积 + `_query_strategy_base_capital` 段初取值 | `dashboard/backend/services/data_service_docker.py` | 无 | 看板侧核心，改码 |
| **CS2 多粒度窗口相交** | `compute_and_store_performance_metrics` / `_compute_one_perf_entry` 改为「先分段再按窗口取各段 max_dd」 | `dashboard/backend/services/data_service_docker.py`（同 CS1 文件，**必须串行**） | CS1 | 看板侧 |
| **CS3 ai_tuner 持仓级去重** | `_PNL_QUERY_TEMPLATE` 改持仓级 CTE + SQL 文本守卫测试 | `ai_tuner/allocation/pnl_collector.py` + `ai_tuner/allocation/tests/*` | 无（与 CS1/CS2 可并行） | ai_tuner 侧 |
| **CS4（可选）caliber 审计列** | DDL 追加 `caliber_version` + 写入 | `database/postgres/init-scripts/09-performance-metric-snapshot.sql` + data_service 写入点 | CS2 | 可选 |

**完成条件 / 测试场景**（每变更集）：
- CS1：单测构造多段资金序列（含「段 A 净值触 0、段 B 重新开始」），断言段 B 回撤不受段 A 峰值影响（AC-1.4）；断言段内负值钳 0。
- CS2：四粒度（30/90/180/365）窗口分别求各段 max_dd 取最大；构造「窗口起点落在段中间」的残缺段，断言段内重新归一（AC-1.5）。
- CS3：mock「上月仅明细、本月补 PNL_SUMMARY」持仓，断言不双计；SQL 文本断言含 `(strategy,symbol,side)` + `IS NOT DISTINCT FROM`；非 HRS 无汇总组回退明细 SUM（AC-2.1~2.3）。
- CS4：ADD COLUMN IF NOT EXISTS 幂等；旧代码不读新列无回归。

**串行关系**：CS1 与 CS2 改同一文件 `data_service_docker.py`，**必须串行**（先 CS1 后 CS2）；CS3 独立、可与 CS1/CS2 并行。

---

## 10. 问题 10：测试设计与关卡命令 + 生产只读预演

### 10.1 生产只读预演结果（本地复现，禁止服务器回测）

已本地对 **5 个策略**（btc_eth / btc_eth_aggressive / new_coin / hrs / grid）拉取生产 `snapshot_history` 段结构 + 持仓级日盈亏序列，跑 `derive_segments` 分段回撤预演（B1 闭环）。

**各策略段清单（生产只读，段初分配额 = 段事件生效时刻后首条正快照，§4.1 落地规则）**：

| 策略 | 段清单（段起点日 → 段初 allocated_amount） | 说明 |
|------|------------------------------------------|------|
| btc_eth | 9-10 87.56 → 9-11 208.44 → 9-21 161.53 → 10-09 142.03 | trades 止于 9-23；9-21 后每日漂移 |
| btc_eth_aggressive | 9-10~9-20 全 0（零分母期）→ 9-21 107.69 → 10-09 170.44 | 零分母期整段跳过（§4.5） |
| new_coin | 9-10 116.75 → 9-11 156.33 → 9-21 134.61 → 10-09 113.63 | 9-21 后每日漂移 |
| hrs | 9-10 58.37 → 9-11 104.22 → 9-21 53.84 → 10-09 56.81 | 详 §2.2 / 本节「HRS 细分」 |
| grid | 无快照行 | 无任何段（§4.4「不可评分」） |

**各粒度新口径回撤期望值（本地预演，部署后生产验收对照用）**：

| 策略 | day(30d) | week(90d) | month(180d) | year(365d) |
|------|---------|---------|---------|---------|
| btc_eth | 1.40% | 3.22% | 3.22% | 3.22% |
| btc_eth_aggressive | 1.65% | 1.65% | 1.65% | 1.65% |
| hrs | 18.82% | 18.82% | 18.82% | 18.82% |
| new_coin | 4.77% | 4.98% | 4.98% | 4.98% |
| grid | 不可评分 | 不可评分 | 不可评分 | 不可评分 |

> 表内数值来自本地分段回撤预演（`derive_segments` 结果）；生产部署后按 §10.3 只读 SQL 对账，允许段初取值 ±2% 内的微小浮动（§10.4）。btc_eth_aggressive / hours 零分母期与首段前无数据期均按 §4.5 跳过，不计入任何段回撤。

**旧口径现状对照（生产只读，部署前需导出全量基线）**：

| 策略 | 旧口径现状（month/year 桶近似值） |
|------|---------------------------------|
| btc_eth | ≈ 0.70 |
| btc_eth_aggressive | ≈ 0.0986 |
| hrs | 1.0（day/week/month/year 全 1.0） |
| new_coin | ≈ 0.0670 |
| grid | 0.0（day/week 至 10-09 后停更，month/year 残留 0.0） |

> **修正（B1）**：架构 §12.1 风险 2 旧版称「其它策略无每日刷新、单段、基本不变」**与生产数据相反**——btc_eth / new_coin / btc_eth_aggressive 与 HRS 跳变结构同构（均有 9-11 静态跳变 + 9-21 起每日漂移 + 10-09 月切换），修复后 5 策略（除 grid 不可评分外）的 max_dd 都由「固定单值分母」切换到「分段分母」，数值**全部显著变化**（见上表），不是「基本不变」。

---

**HRS 细分（本地复现，B2 采用段初 = 段事件生效时刻后首条正快照）**：

```
HRS 各粒度最大回撤（今天=2026-10-15）：
  新口径 overall = 0.1882（18.82%），四个粒度一致（day/week/month/year）
  旧口径（单一 58.37） = 1.0（100%）
细分（窗口内各段）：
  段A 2026-09-10  58.37   样本 1 天 → max_dd=None（样本不足）
  段B 2026-09-11 104.22   段内 max_dd = 0.1298
  段C 2026-09-21  53.84   段内 max_dd = 0.0316
  段D 2026-10-09  56.81   段内 max_dd = 0.1882 ← 决定整体值
```

- 整体 max_dd 由**段 D（10-09 起，段初 56.81）**贡献：10-09 冲高 +8.03 后，10-10~10-13 累计 -12.20，净值 1.211 → 0.926，回撤 18.82%。
- 四个粒度一致的原因：段 D 是最近段且回撤最大，day/week/month/year 窗口（30/90/180/365 天）都含段 D 全段。
- **结论**：AC-1.1 成立——HRS 各粒度不再恒等 1.0，预期显著 < 1.0（≈18.82%，与「段内累计盈亏 ÷ 段初分配额」一致；数值会随段初分配额的精确取法 ±2% 内浮动，详 §10.4）。

### 10.2 本地单测/集成测试设计

环境与既有约定一致（本地 macOS、Python 3.9.6、pytest 8.4.2 + pytest-cov、`asyncio_mode=auto`）。

- **纯函数单测**（新增，落 `dashboard/backend/services/` 或对应 tests 目录）：
  - `derive_segments`：构造「month 切换 / ratio 变更 / 静态跳变 / 每日漂移」四类事件，断言段边界 = 前三类并集、每日漂移被排除。
  - `segment_max_drawdown`：构造多段序列（含「跨段触 0 后新段」），断言 `max_dd < 1.0` 且等于逐段手算值（AC-1.4）。
  - 残缺段重归一：窗口起点落在段内，断言净值从 1.0 起、不继承段起点前盈亏（AC-1.5）。
- **SQL 文本守卫测试**：断言 `_PNL_QUERY_TEMPLATE` 改后含 `GROUP BY strategy, symbol, side` + `IS NOT DISTINCT FROM` + `NOT g.has_summary` 回退分支（AC-2.1~2.3）。
- 非 HRS 策略回归（AC-2.3）：btc_eth / new_coin / grid 无汇总组走明细 SUM。

### 10.3 关卡命令

```bash
# 看板侧（CS1/CS2）单测
python3 -m pytest dashboard/backend -q -k "segment or capital or perf"

# ai_tuner 侧（CS3）单测（含既有 97 passed 回归）
python3 -m pytest ai_tuner/allocation/tests/ -q

# 变更范围覆盖率（分段/段初/持仓级去重相关）
python3 -m pytest dashboard/backend ai_tuner/allocation/tests \
  --cov=dashboard.backend.services.data_service_docker \
  --cov=ai_tuner.allocation.pnl_collector \
  --cov-report=term-missing
```

**门槛**：变更范围（分段净值/回撤、段初取值、持仓级去重）行/分支覆盖 100%；既有 dashboard 与 ai_tuner allocation 用例全绿（回归）。

**生产只读验收（部署后，只读 SELECT，禁止写库/服务器回测）**：
```sql
-- AC-1: HRS 各粒度 max_drawdown 不再为 1.0
SELECT granularity, max_drawdown FROM public.performance_metric_snapshot
WHERE scope='strategy' AND strategy_id='hrs' AND bucket_key = (当前桶) ORDER BY granularity;
-- AC-1: 段初分配额应为段起点快照值（非 58.37）
```
预期：day/week/month/year 均 ≈0.188 ± 0.02（含段 D 段初取值的微小浮动）。

**AC-2 生产对账（部署后，只读，验证「持仓级 − 整表 == 无汇总组明细」构成）**：

```sql
-- 整表 BOOL_OR 口径（旧 f940a61）
SELECT COALESCE(SUM(realized_pnl) FILTER (WHERE order_type='PNL_SUMMARY'), 0) AS whole_table
FROM trading.trade_records
WHERE strategy='hrs' AND executed_at >= '2026-10-01' AND executed_at < '2026-11-01';

-- 持仓级 CTE 口径（本节升级目标）
WITH grp AS (
  SELECT strategy, symbol, side,
         BOOL_OR(order_type='PNL_SUMMARY') AS has_summary
  FROM trading.trade_records
  WHERE strategy='hrs' AND executed_at >= '2026-10-01' AND executed_at < '2026-11-01'
  GROUP BY strategy, symbol, side
)
SELECT COALESCE(SUM(t.realized_pnl) FILTER (WHERE g.has_summary AND t.order_type='PNL_SUMMARY'),0)
     + COALESCE(SUM(t.realized_pnl) FILTER (WHERE NOT g.has_summary),0) AS position_level
FROM trading.trade_records t
JOIN grp g
  ON g.strategy IS NOT DISTINCT FROM t.strategy
 AND g.symbol   IS NOT DISTINCT FROM t.symbol
 AND g.side     IS NOT DISTINCT FROM t.side
WHERE t.strategy='hrs' AND t.executed_at >= '2026-10-01' AND t.executed_at < '2026-11-01';

-- 无汇总组明细合计（应 == position_level - whole_table == -5.2765）
SELECT symbol, side, SUM(realized_pnl) AS detail_sum
FROM trading.trade_records t
WHERE t.strategy='hrs' AND t.executed_at >= '2026-10-01' AND t.executed_at < '2026-11-01'
  AND NOT EXISTS (
    SELECT 1 FROM trading.trade_records s
    WHERE s.strategy=t.strategy AND s.symbol=t.symbol AND s.side=t.side
      AND s.order_type='PNL_SUMMARY'
      AND s.executed_at >= '2026-10-01' AND s.executed_at < '2026-11-01'
  )
GROUP BY symbol, side ORDER BY detail_sum;
```
预期（2026-10-15 快照）：`whole_table`=+17.90、`position_level`=+12.62、`position_level - whole_table` == 无汇总组明细合计 == −5.2765（LITEUSDT −2.6035 / ONDOUSDT −3.2190 / NBISUSDT +0.3861 / QQQUSDT +0.1599）。

> 说明：此对账**不依赖币安 income API**（AC-2.2a 锚 DB 可精确复算），income 真值仅作方向性参照（AC-2.2b）。各口径绝对值随 10-14/10-15 新增成交漂移，但「持仓级 − 整表 == 无汇总组明细」这一恒等关系不随新增成交改变（除非新增成交改变无汇总组构成，此时应重跑第三条 SQL 对齐新值）。

### 10.4 预演数值的边界说明（证据缺口级）

段 D 段初分配额有 3 个候选：快照 10-09 14:00=56.81（预演采用）、month 记录初始 allocated_amount=57.92、initial_capital=76.82。三者算出的回撤分别为 18.82% / ~18.5% / ~14%（前两者接近，第三方因 open_margin 口径偏大而显小）。架构锁定**取 56.81（段起点快照）**，B 段实现时需以「段起点首条快照」为唯一取值口径并加断言防漂移。

---

## 11. 对需求文档第 5 节 6 项待拍板项的逐项技术建议

| # | 待拍板项 | 技术建议 | 依据 |
|---|---------|---------|------|
| 1 | 历史快照回填（A vs B） | **已定稿 A（自然重算，不回填）** | §8.1（B5 闭环）：前端只读当前桶、任务只写当前桶、无历史回看界面；B 是生产写操作（R3）无当前收益；改选 B 路径保留于 §8.3 |
| 2 | 104.22 静态值来源 | **不臆断来源；如实识别为段边界（辅锚 E3 静态跳变）** | 生产证实 9-11~9-20 恒 104.22、无 month/ratio 留痕；按「变更事件切段」原则将其列段，段初=104.22。此来源究责**不阻塞本修复** |
| 3 | 段的持久化粒度 | **不落地新表/新字段，实时推导** | §6.1：snapshot_history + capital_allocation 可确定性还原段边界；落表引入双写一致性风险 |
| 4 | 首段之前无数据期处理 | **跳过（不可评分）** | 预演中 HRS 7-1~9-9 有日盈亏但无快照，直接跳过、不退化为「未来最早一条 58.37」（对应 AC-1.2） |
| 5 | AC-2 持仓级判定窗口边界 | **整月窗口内持仓级 GROUP BY（与 dashboard `_DAILY_PNL_QUERY` 一致），不做前后延展** | §7.1；与既有 ai_tuner 月窗口采集范围一致 |
| 6 | 账户级回撤是否随之调整 | **不改账户级** | 账户级以 `equity_snapshot` 净值曲线为准（[L2561-2615](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2561-L2615)），恒为正、回撤 ≤100%，与「分配额漂移」无关；本次仅策略级 |

---

## 12. 风险与未闭环证据缺口

### 12.1 风险

1. **段边界还原错误 → 分母失真（R3 核心风险）**：若辅锚 E3 的「恒定 ≥ K 天」阈值 K 不当，可能把「两日漂移」误判为段，或漏掉短暂静态跳变。**缓解**：K 配置化（默认建议 3 天），并用 104.22（恒 10 天）与每日漂移（逐日变）的已知样本做测试守门。
2. **历史口径切换造成看板曲线突变（全策略，非仅 HRS）**：修复后 5 个策略（除 grid 不可评分）max_dd 均显著变化——HRS 100%→18.82%、btc_eth ≈70%→1.40%~3.22%、btc_eth_aggressive ≈9.86%→1.65%、new_coin ≈6.70%→4.77%~4.98%（§10.1 预演表）。部署前导出**全部 5 策略**当前 4 桶旧值作对比基线（非仅 HRS）。grid 维持现状（无回归，§4.4）。
3. **104.22 段初值偏大稀释回撤**：若 104.22 实为误配（ratio 错写），段 B 回撤被「分母放大」而低估。**缓解**：本方案按事实值切段、不臆断修正；若后续查明 104.22 为错账，需另立数据修正任务，不在本任务范围。

### 12.2 证据缺口（本轮 A 段已大部分闭环，剩余注明）

| # | 缺口 | 影响 | 当前状态/建议 |
|---|------|------|------|
| 1 | **104.22（9-11~9-20 静态值）的写入来源未定论**（月度 job / 手工 `manual_allocation_trigger.py` / config 残留） | 段 B 段边界「语义」无法 100% 归因 E1/E2，只能按辅锚 E3 静态跳变切段 | **保留（不阻塞）**：按事实切段即可；来源究责另立查证 |
| 2 | **`initial_capital`=76.82 为 open_margin 残值（05888cc 修复前产物），10 月记录不追溯修正**（AC-3.3） | 看板新口径（段初快照 56.81）与 10 月 `initial_capital`（76.82）数值仍有差异 | **保留（B4 已定为「不追溯」）**：接受现状；11 月起新记录趋于一致 |
| 3 | **段初分配额取值 ±2% 浮动**：段起点快照（56.81）vs month 记录初始 allocated_amount（57.92） | 回撤 18.82% vs ~18.5% | **保留**：B 段锁死「段起点首条快照」唯一取值并加断言防漂移（§10.4） |
| 4 | ~~非 HRS 策略（btc_eth/new_coin/grid）段边界未预演~~ | ~~可能误切段~~ | **已闭环（B1）**：本轮已对 5 策略跑 `derive_segments`，段清单见 §10.1；btc_eth/new_coin/btc_eth_aggressive 与 HRS 同构 |
| 5 | ~~grid 的 allocated_amount 快照是否有值~~ | ~~grid 是否需同步分段~~ | **已闭环（B1）**：grid 无任何快照行，标记「不可评分」、维持现状（§4.4） |
| 6 | **income 真值 10-15 未重拉**：知识库 +3.4292 为 10-13 对账值 | AC-2.2b 只能做方向性参照，无法锚定 10-15 绝对 income 值 | **不阻塞**：AC-2.2a 锚 DB 可精确复算、不依赖 income API；如需绝对对账，重跑 `python -m ai_tuner.reconciler.hrs_income_reconcile`（非 AC-2 验收必须） |
| 7 | **跨月 carry-in +3.03454 写入层未修**（PNL_SUMMARY 累计语义） | 持仓级口径仍含跨月累计、不等于 income 当月真值 | **已归口**：归 hrs_pnl_governance，需求 §3.2 非目标 4 已排外（§7.1 机制③） |

---

## 13. 影响面分析

### 13.1 涉及的文件/容器/数据库表

| 类别 | 明细 |
|------|------|
| 修改文件 | `dashboard/backend/services/data_service_docker.py`（CS1/CS2）；`ai_tuner/allocation/pnl_collector.py`（CS3）；可选 `database/postgres/init-scripts/09-performance-metric-snapshot.sql`（CS4） |
| 不改文件 | `dashboard/backend/api/routes_docker.py`（读逻辑不变）；`shared/capital_manager.py`（限额执行链不动）；`shared/` 任何文件（不越界） |
| 重建容器 | **data-backend**（6.6）、**dashboard-api**（FULL_COPY）、**ai-tuner**（CS3） |
| 数据库表 | 读 `public.strategy_position_snapshot_history` / `public.capital_allocation` / `trading.trade_records`；写 `public.performance_metric_snapshot`（仍是 UPSERT） |

### 13.2 需同步的文档

- 本架构文档 + 需求文档（AC 结论回填）；
- `docs/knowledge/decisions/Binance_quantitative_trading-20261009-max-drawdown-caliber.md` 补「后续口径修正（2026-10-15）」段；
- 若 AC-2 落地，更新 `docs/knowledge/issues/Binance_quantitative_trading-20261013-allocation-return-denominator-zero.md` 的「待验证」项。

---

## 14. 待确认事项（提交 A-review）

1. ~~AC-4 是否采纳选项 A（自然重算不回填）~~ → **已定稿（B5）**：选项 A，见 §8.1；改选 B 的可选回填路径保留于 §8.3。
2. ~~段初分配额取「段起点首条快照」还是 initial_capital~~ → **已锁定（B2/B4）**：段事件生效时刻后首条正快照 `allocated_amount`（§4.1 落地规则 / §5.1 契约），不再待用户确认；B 段只需以该口径加断言防漂移（§10.4）。
3. 辅锚 E3「恒定 ≥ K 天」的去噪阈值 K 默认值（建议 3 天）与配置落点（`dashboard` 配置 or 环境变量）——留 B 段定案（禁止硬编码，必须配置化）。
4. 是否采纳可选 `caliber_version` 审计列（§6.1）——可选，待用户/A-review 确认。
5. **需求文档回写（转 requirements-document-expert，非本次架构师职责）**：AC-2（§2.4/§4.4 三机制归因 + AC-2.2 相对断言）、AC-3.1（分母口径契约）、AC-4.2（选项 A 接受声明）按本架构 §5.1 / §7.1 / §8.1 已标注的「需求回写坐标」执行。

---

## 附：关键代码出处索引

| 位置 | 结论 |
|------|------|
| [data_service_docker.py L88-102](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L88-L102) | 聚合列 `_TRADE_RECORD_AGG_COLUMNS` 持仓级 BOOL_OR |
| [data_service_docker.py L112-134](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L112-L134) | `_DAILY_PNL_QUERY` 持仓级 CTE |
| [data_service_docker.py L2438](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2438) | 窗口起点 = today - 365d |
| [data_service_docker.py L2475](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2475) | `_PERF_WINDOW_DAYS` |
| [data_service_docker.py L2541-2545](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2541-L2545) | 尾部切片 |
| [data_service_docker.py L2617-2700](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2617-L2700) | `_build_strategy_daily_returns` 全窗口累积 |
| [data_service_docker.py L2659](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2659) | `base_capital` 入参 = 365 天前 |
| [data_service_docker.py L2702-2787](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2702-L2787) | 4 步回退链 |
| [data_service_docker.py L2807-2916](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2807-L2916) | 读当前桶 |
| [pnl_collector.py L50-59](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/pnl_collector.py#L50-L59) | `_PNL_QUERY_TEMPLATE` 整表 BOOL_OR |
| [pnl_collector.py L77-112](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/pnl_collector.py#L77-L112) | ai_tuner 4 步回退链 SQL |
| [daily_refresher.py L53-57](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/daily_refresher.py#L53-L57) | 每日刷新 UPDATE 覆盖 entries |
| [config_updater.py L95-157](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/config_updater.py#L95-L157) | entries 写入字段 |
| [allocation_calculator.py L214-267](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/allocation_calculator.py#L214-L267) | `initial_capital = capital`、`return_rate = pnl/capital` |
| [main.py L111-120](file:///Users/yl/vscode/Binance_quantitative_trading/services/data_backend/src/main.py#L111-L120) | data-backend 绩效任务注册（600s） |
| [09-performance-metric-snapshot.sql L20-35](file:///Users/yl/vscode/Binance_quantitative_trading/database/postgres/init-scripts/09-performance-metric-snapshot.sql#L20-L35) | UNIQUE 约束 |
| [routes_docker.py L170-173](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/api/routes_docker.py#L170-L173) | `_attach_performance_metrics` 调用 |
| [deploy.yml L160-184](file:///Users/yl/vscode/Binance_quantitative_trading/.github/workflows/deploy.yml#L160-L184) | 6.5/6.6 重建规则 |

---

## 15. A-review 复核记录（R3 设计复核，2026-10-15）

### 15.1 复核者与独立性

| 项 | 内容 |
|----|------|
| 复核者 | design-review-local，实际模型 GLM-5.3 |
| 独立性 | 本复核者未参与本需求/架构的编写；需求作者 requirements-document-expert-local、架构作者 backend-architect-local（自述 DeepSeek-V4-Pro，平台未确认实际模型），与复核者不同 |
| 复核范围 | 三个问题聚焦复核：无证据前提、未定义失败/恢复状态、测试证明不了的验收条件；不重做方案、不编码 |
| 结论 | **GA 不通过**，5 项阻塞（B1~B5），交回 A 段修订后回本复核者复核 |

### 15.2 实际检查范围

- 读代码：[data_service_docker.py](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py)（`_DAILY_PNL_QUERY` L112-134、`compute_and_store_performance_metrics` L2421-2516、`_build_strategy_daily_returns` L2617-2700、`_query_strategy_base_capital` L2702-2787、快照对账写入 L1640-1763、`_get_active_capital_allocation_raw` L1950-1975）；[pnl_collector.py](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/pnl_collector.py)（L40-112）；[daily_refresher.py](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/daily_refresher.py)（全文）；[monthly_job.py](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/monthly_job.py)（DDL L94-105）；[performance_reader.py](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/performance_reader.py) L87-148；[06-position-snapshot.sql](file:///Users/yl/vscode/Binance_quantitative_trading/database/postgres/init-scripts/06-position-snapshot.sql)；[tests/test_performance_metrics.py](file:///Users/yl/vscode/Binance_quantitative_trading/tests/test_performance_metrics.py)（测试标题扫描）。
- 生产只读 SELECT（trading_system-postgres / trading_platform）：capital_allocation 全部 3 条月记录及 HRS entry；snapshot_history 全部 4 策略的 allocated_amount 跳变点（71 行）、覆盖范围（36 天）、btc_eth_aggressive 断档分布；HRS 最早快照 open_margin；HRS 最早成交与快照期前累计盈亏；performance_metric_snapshot 当前桶现状（HRS 四粒度全 1.0、grid 残留行）；10 月 HRS 旧口径 vs 持仓级口径实测；HRS 按月按 order_type 盈亏分布；跨月持仓组（TRUMPUSDT/ONDOUSDT/ARBUSDT/BZUSDT）明细与 SUMMARY 行逐笔分布。
- 未核实部分（如实说明）：币安 income API 真值（需外部 API，无法从 DB 裁定，见 B3）；104.22 写入者的分钟级时序还原（capital_allocation 无变更留痕，无法从 DB 裁定，架构已按 E3 事实切段，可接受）；deploy.yml CI 映射行号（采信架构引用，未逐行复核）。

### 15.3 已证实成立的关键前提（架构主张核对为真）

1. snapshot_history 确有小时级 allocated_amount，HRS 18 个跳变点与 §2.2 表逐行一致（含 9-10 17:00 首条 58.37、9-11 14:00 静态 104.22、9-21 13:00 月记录切换、10-09 14:00 月记录切换、9-22 起 00:00 每日漂移）。
2. capital_allocation 无逐日历史：表仅有 created_at（[monthly_job.py L94-105](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/monthly_job.py#L94-L105)），daily_refresher UPDATE 覆写 entries、失败保留旧值——均与架构 §2.2 一致。
3. `initial_capital` 弃用理由成立：10 月记录 initial_capital=76.82024229 与快照表最早一条 open_margin（9-10 17:00，76.82024229）**精确相等**，证实其为 open_margin 残值；9 月记录 initial_capital=58.37（created_at=2026-09-21 13:58:45，回退链"最早一条"产物）。
4. E1 主锚时点可行：9 月记录 created_at=9-21 13:58、10 月记录 created_at=10-09 14:48，与段 C/段 D 起点同日。
5. HRS 现状当前桶四粒度 max_drawdown 全部 = 1.0；快照覆盖期仅 2026-09-10 起 36 天；HRS 最早成交 2026-07-07（快照期前累计盈亏 -121.77，见 N2）。
6. 回滚兼容性论证成立：不改 schema（或 ADD COLUMN IF NOT EXISTS）+ UPSERT 覆盖当前桶，旧代码可读新值、回滚后旧算法自然覆盖（§8.2/8.3 已定义）。

### 15.4 阻塞项清单（B1~B5）

**B1｜非 HRS 策略段结构未预演，架构"基本不变"断言被生产数据证伪**
- 证据：生产实测 btc_eth / new_coin / btc_eth_aggressive 与 HRS 跳变结构同构——btc_eth：9-10 首条 87.56 → 9-11 静态跳变 208.44（+138%）→ 9-21 161.53 → 每日漂移 → 10-09 142.03 → 漂移；new_coin：116.75 → 156.33 → 134.61 → 漂移 → 113.63；btc_eth_aggressive：9-10~9-20 全为 0.00（期间有 88 笔非排除成交）→ 9-21 107.69 → 漂移 → 10-09 170.44。架构 §12.1 风险 2"其它策略无每日刷新、单段、基本不变"与事实相反。
- 为何阻塞：AC-1.3/AC-1.5 对全部策略生效；btc_eth 旧分母 87.56 vs 新段初 208.44/161.53/142.03，回撤数值必然显著变化，但无预演期望值，部署后生产验收无法判定对错（只能判定"变了"）。btc_eth_aggressive 存在"有成交但 allocated_amount=0 期间（9-10~9-20）"的真实边界，需求 AC-1 边界表第 4 行只给了原则（跳过/不可评分），架构 §4.4 只写了 grid 场景，未定义"策略部分期间分母为 0"的段切分与盈亏归属行为。grid 事实与 §4.4 描述不符：grid 在 snapshot_history **无任何行**（非"退化单段"），其 performance_metric_snapshot 的 month/year 桶残留 10-09 前旧代码写的 max_drawdown=0.0 行，day/week 桶已停止更新——新口径维持该现状（无回归），但文档描述需按事实修正。
- 建议闭环：A 段对 5 策略跑 derive_segments 只读预演并给出各策略各粒度新口径期望回撤（部署后核对需要）；明确 btc_eth_aggressive 零分母期的段行为（建议：跳过该期、不计入任何段回撤，与"首段前无数据期跳过"同一语义）；修正 grid 与 §12.1 的事实描述。

**B2｜段初取值方向在架构内部矛盾，"段起点"时点未唯一定义**
- 证据：§4.1 落地规则"段初分配额取该日期（含当日）的首条 allocated_amount > 0 快照"（按字面取当日 00:00 起首条——对段 D 会取到 10-09 00:00 的 53.77 旧段尾值，因为跳变发生在 14:00）；§5.2"只在单个资金段内部做「段起点及之前最近一条 allocated_amount > 0」查询（即旧链第 1 步）"——旧链第 1 步 SQL 是 `snapshot_hour <= $2 ORDER BY DESC`（向前取，[data_service_docker.py L2731-2740](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L2731-L2740)），若 at_time 取段起点日 00:00 会取到 10-08 的 53.77。而预演（§10.1）实际采用 10-09 14:00 跳变后的 56.81，对应语义是"段边界事件生效时刻之后的段内首条正快照"（向后取）。三处给出三个方向，B 段照 §5.2 实现将得到分母 53.77（段 D 回撤 ≈22.7%）而非预演 18.82%。另：§4.1 伪码引用 `record.effective_time` 字段在 capital_allocation 表中不存在（仅有 created_at），月内手工 ratio 变更（UPDATE 不改 created_at）的时点不可得，E2 实际只能依附 E1。
- 为何阻塞：这是分母取值的唯一口径定义，方向不唯一则实现结果不唯一，预演 18.82% 与断言（§10.4"锁死段起点首条快照"）无法落地。
- 建议闭环：统一定义为"段边界事件生效时刻（E1 取记录 created_at、E3 取跳变 snapshot_hour）之后、该段内的首条 allocated_amount > 0 快照"；§5.2 的查询语义描述同步改为">= 事件时点、段内首条"（新增 SQL 或明确改造）；伪码字段改 created_at 并记录 E2 依附 E1 的局限。

**B3｜AC-2 修复方案与"消除跨月双计、收敛 income 真值"之间的因果链无证据**
- 证据：实测（2026-10-15）10 月 HRS 旧口径 = +17.90（= 10 月 PNL_SUMMARY 行合计，与需求引用的 +10.29 已漂移——新增 10-14/10-15 成交所致）、持仓级月度 CTE 口径 = +12.62，两者差异 -5.28 全部来自"无汇总组的明细行"（持仓级比旧口径多计，含与 PNL_SUMMARY 同值双写的 CONDITIONAL_ORDER 行，如 ARBUSDT 10-12 CONDITIONAL_ORDER -2.87408 与 PNL_SUMMARY -2.87408）。**跨月持仓组在两个口径下行为完全相同**（9 月窗口计 9 月 SUMMARY、10 月窗口计 10 月 SUMMARY，PNL_SUMMARY 为增量式多条，executed_at 落各自平仓时刻，如 TRUMPUSDT 分布于 8-23/9-03/9-04/9-09/10-07）。即：月度窗口的持仓级 CTE 改变的是"窗口内无汇总组是否计明细"，**不触及跨月归属**；需求 §2.4 的归因（"判定粒度不一致 → 跨月各计一次"）与数据证据（SUMMARY 增量式、月度归属自洽）不吻合，income 真值 +3.43 与旧口径 +10.29 的 ~6.86 差异的确切构成（哪些行、哪个机制）**未经逐笔对账定论**——10 月大额平仓 PONS(+4.97)/1000SHIB(+3.06) 均为 10 月内开平（非跨月持仓），无法用"跨月双计"解释 income 差异。
- 为何阻塞：按 §7.1 实现后，可能出现"SQL 文本守卫与 mock 测试全绿、但 10 月数值仍不接近 income 真值"——AC-2.2 的验收无法成立。且 AC-2.2 写死的 +10.29/+3.43 是历史时点值（现已漂移为 17.90/未知），断言不可复现；架构 §10.3 的生产验收命令只有 max_drawdown 的 SQL，**缺 AC-2 的生产对账命令**。
- 建议闭环：A 段先用币安 income API 明细与 DB 的 PNL_SUMMARY 行逐笔对账（10 月样本），把 income 差异归因到具体行与机制，再定修复方案；若归因后确认持仓级 CTE 是正确方向，AC-2.2 改为相对断言（如"新口径 − income 差异 ≤ 阈值"或固定数据快照集上的断言），并在 §10.3 补 AC-2 生产对账 SQL。

**B4｜AC-3.1 验收语义与架构决策相反，需求未回写**
- 证据：需求 AC-3.1"看板回撤分母与分配记录 initial_capital 取值一致（允许「段起点快照无 initial_capital 时回退段起点快照 allocated_amount」的同源回退）"——回退方向为 initial_capital 优先；架构 §5.1/§5.3 决策弃用 initial_capital（理由已证实成立：历史口径污染 + 月粒度无法区分月内多段），10 月 initial_capital=76.82 与段初快照 56.81 数值不一致且不追溯。架构 §5.3 把 AC-3.1 重新解释为"段初快照 = 记录创建时刻的 allocated_amount 初值（57.92 vs 56.81，差 1.9%）"，实质修改了验收定义但需求文档未同步。
- 为何阻塞：AC 是测试与 C 段审查的依据，按 AC-3.1 字面写断言（分母 == initial_capital）必然失败；B 段实现者面临两份矛盾语义。
- 建议闭环：需求 AC-3.1 回写为架构实际口径（分母 = 段起点首条正快照 allocated_amount；与 capital_allocation 的同源性以"记录创建时刻初值对账（月粒度、容差）"验收），或明确声明 10 月 initial_capital 残值例外。

**B5｜AC-4 待用户拍板未闭环**
- 证据：需求 §5.1 自标"必须拍板"，用户第 4 项选择与另一问答案矛盾（回填 vs 自然重算）；架构 §8.1 建议 A 但 §14.1 承认"需用户最终确认"。
- 为何阻塞：选项 B 涉及生产库写操作（回填历史桶），A/B 的实现范围与测试设计差异大，带未决项进 B 段违反 GA 条件（实施者无需猜测关键业务语义）。
- 建议闭环：用户拍板。复核者意见：A 的技术依据充分（前端只读当前桶、任务只写当前桶、ai_tuner reader 默认当前桶均已核实），若选 B 须按 AC-4.2 补安全方案。

### 15.5 非阻塞项（建议，可延后 B 段）

- **N1 分母不可观察 + 容差过宽**：performance_metric_snapshot 不落分母值，生产验收只能靠 max_drawdown 反推，§10.3 的 ±0.02 容差无法区分 56.81 vs 57.92（18.82% vs 18.5%）。建议：单测锁死精确分母断言 + 段初值默认 INFO 日志输出（§6.1 的"调试日志"升级为常开审计输出，作为部署后验收证据来源）。
- **N2 无快照期跳过的效应未隔离**：HRS 7-07~9-09 累计 -121.77 在新口径下不计入任何回撤（旧口径 100% 回撤主要由这段造成），18.82% 的改善大部分来自"跳过"而非"分段"。建议：测试设计增加"无数据期巨额亏损 + 有数据期小额回撤"用例断言隔离两效应；文档明示"回撤仅覆盖有分配快照期间"，避免用户将 18.82% 与 equity 净值 0.064 并读误解。
- **N3 既有测试失效清单未列**：[test_performance_metrics.py](file:///Users/yl/vscode/Binance_quantitative_trading/tests/test_performance_metrics.py) 的 TestBaseCapitalQueryNoFutureSnapshot（断言"最早一条兜底"SQL 存在）、TestStrategyCurveBaseCapital（断言旧口径净值构建）在新口径下必然失败，应列明修改计划，防止 B 段为迁就旧测试扭曲实现。
- **N4 两侧黄金用例交叉验证缺失**：ai_tuner 月度与 dashboard 日度的持仓级口径一致性目前仅靠 SQL 文本断言 + 注释互指（§7.2），建议增加同一组不跨窗口边界的黄金数据在两侧聚合结果相等的交叉断言（dashboard 日和 = ai_tuner 月和）。
- **N5 快照断档无告警**：实测对账任务非整点满勤（btc_eth_aggressive 单日 3~24 行不等，9-16 仅 3 行），history 写失败仅 warning（[data_service_docker.py L1748-1750](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L1748-L1750)）。段初取值依赖该表质量，建议另立任务加断档告警。
- **N6 E3 局限记录**：月内手工 UPDATE 改 ratio（不建新月记录）后每日刷新立即按新 ratio 漂移，E3 无法识别该段边界（capital_allocation 无变更留痕）——建议在文档记录该已知局限；若未来需要可另立留痕机制任务。
- **N7 9-11 静态跳变为全局事件**：btc_eth/new_coin/hrs 同日（9-11 14:00）发生静态跳变，多策略同日跳变可互证，可作为 E3 判定的鲁棒性增强（非必须）。

### 15.6 复核结论

GA **不通过**。B1（非 HRS 策略预演与边界行为）、B2（段初取值方向矛盾）、B3（AC-2 因果链与验收数字）、B4（AC-3.1 需求回写）交回 A 段（实际负责人：backend-architect-local，自述 DeepSeek-V4-Pro）修订，B5（AC-4 拍板）需用户决策；修订后回到本复核者（design-review-local / GLM-5.3）复核，不直接进入 B 段。本复核不替代编码后 C 段对实际实现的独立审查。

### 15.7 修订闭环记录（v1.1，backend-architect 回写）

本节记录本轮 A 段对 B1~B5 的闭环处理及 N1~N7 的采纳/延后，供复核者核对。

| 阻塞 | 原结论要点 | 修订后结论 | 证据/落点 |
|------|-----------|-----------|----------|
| B1 | 非 HRS 策略未预演、「其它策略基本不变」被证伪；零分母期/grid 行为未定义 | 5 策略跑 `derive_segments` 预演，给出段清单 + 各粒度回撤期望值；定义零分母期整段跳过；grid 改「不可评分」；修正 §12.1 风险 2 | §10.1 预演表、§4.4、§4.5、§12.1 |
| B2 | 段初取值方向三处矛盾（§4.1/§5.2/§10.1）+ 伪码字段 `effective_time` 不存在 | 统一为「段边界事件生效时刻（created_at/snapshot_hour）之后、段内首条正快照」；§5.2 改向后取；伪码改 `created_at`；记录 E2 依附 E1 局限 | §4.1 落地规则、§5.2、§4.1 E2 局限 |
| B3 | AC-2 因果链无证据、绝对值断言不可复现、缺生产对账命令 | 三机制归因量化（①无汇总组明细 −5.2765 ②CONDITIONAL_ORDER 同值双写 0 ③跨月 carry-in +3.03454）；AC-2.2 改相对断言（2a 精确复算 + 2b 方向性收敛）；补 §10.3 对账 SQL；标需求回写坐标 | §7.1、§10.3、§12.2 #6/#7 |
| B4 | AC-3.1 语义与架构决策相反 | 写死最终分母口径契约（段起点首条正快照，弃 initial_capital）；注释需求 AC-3.1 回写 | §5.1、§5.3 |
| B5 | AC-4 两问矛盾未闭环 | 按用户「专门一问」定稿选项 A；写清不主动回填 3 条可见后果；保留改选 B 路径 | §8.1、§11 #1、§14 #1 |

**非阻塞项采纳/延后（N1~N7）**：
- N1（分母不可观察 + 容差过宽）→ 留 B 段：单测锁死分母断言 + 段初值 INFO 常开审计日志（§6.1）。
- N2（无快照期跳过效应未隔离）→ 留 B 段：测试加「无数据期巨额亏损 + 有数据期小额回撤」用例；文档已明示「回撤仅覆盖有分配快照期间」（§4.5）。
- N3（既有测试失效清单）→ 留 B 段：`test_performance_metrics.py` 的 TestBaseCapitalQueryNoFutureSnapshot / TestStrategyCurveBaseCapital 需按新口径改写。
- N4（两侧黄金用例交叉验证）→ 留 B 段：增加「dashboard 日和 == ai_tuner 月和」交叉断言（不跨窗口边界）。
- N5（快照断档无告警）→ 另立任务（本次不含）。
- N6（E3 局限记录）→ 已记入 §4.1「E2 依附 E1 的局限」。
- N7（9-11 静态跳变为全局事件）→ 已体现于 §10.1「各策略段清单」（btc_eth/new_coin/hrs 同日 9-11 跳变）。

**修订范围自检（本轮 A 段只改文档，未触碰代码/生产库）**：
- 只读数据预演：5 策略 `derive_segments` 分段回撤（本地 `/tmp/derive_segments.py`）+ 10 月 HRS 三机制对账（生产只读 SELECT）。
- 未写实现代码、未写迁移脚本、未部署、未 push、未改容器、未写生产库。
- 未改需求文档（需求由 requirements-document-expert 按最终口径回写，坐标见 §14 #5）。

**是否可送回复核**：是。B1/B2/B3/B4 已闭环，B5 已按用户选择定稿 A，N1~N7 均已标注纳入或延后 B 段；剩余非阻塞项（K 阈值、caliber_version 审计列、income 绝对对账）已明确归口，不阻塞进入 B 段设计复核。

---

## 16. A-review 复核确认记录（v1.1 复审，2026-10-15，design-review-local）

> 本节为 R3 设计复核的**第二轮（复审确认）**记录：对 v1.1 修订后的需求/架构两文档做聚焦复核（不重做整套方案），含两项独立抽验证据。复核者与 §15.1 相同（design-review-local，实际模型 GLM-5.3），独立于文档作者（backend-architect-local / requirements-document-expert-local），独立性成立。

### 16.1 检查范围

- **通读**：需求文档 v1.1 全文（412 行）、架构文档 v1.1 全文（693 行），逐条核对 §15.4 阻塞项 B1~B5 的修订落点与两文档一致性。
- **读代码**（复核口径依据）：[data_service_docker.py L112-134](file:///Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py#L112-L134)（`_DAILY_PNL_QUERY` 持仓级 CTE）、L2421-2516（窗口语义）、L2617-2700（净值构建）、L2702-2787（旧回退链）；[performance_metrics.py L53-73](file:///Users/yl/vscode/Binance_quantitative_trading/shared/performance_metrics.py#L53-L73)（running peak 回撤）。
- **生产只读**（2026-10-15，trading_system-postgres / trading_platform，用户 trading_user）：QQQUSDT 全部成交行（order_type / realized_pnl / executed_at）；持仓级与整表 BOOL_OR 两口径 10 月 HRS 合计；无汇总组逐组合计；两策略 365 天窗口持仓级日盈亏全序列；capital_allocation 三条 created_at；两策略 snapshot_history 跳变点。
- **本地复算**：`/tmp/review_verify_v11.py`（独立实现段切分 + 段初取值 + 分段净值/回撤 + 残缺段重归一 + 四粒度窗口，与本文档 §4 口径逐条对齐）。
- **未核实部分（如实声明）**：btc_eth_aggressive 与 new_coin 的期望值**未独立复算**（未拉全量日盈亏明细；仅核对其段清单结构与此前拉取的跳变点数据一致）；grid 未复算（无快照行，本文档 §4.4 已定性「不可评分」）；币安 income API 真值仍无法从 DB 裁定（v1.1 的 AC-2.2 已改为相对断言 2a/2b，不再依赖 income 绝对值，该缺口不构成阻塞）。

### 16.2 独立抽验一：预演期望值复算（用户硬性要求）

**方法**：不采信作者自述，用生产拉取的日盈亏 + 跳变点数据，按本文档 §4 口径（段初=事件生效时刻后段内首条正快照；`nv = max(0, 1+cum/段初)`；残缺段重归一；running peak）独立实现并复算。

**结果（btc_eth，精确复现）**：

| 粒度 | 独立复算 | 文档 §10.1 期望 | 判定 |
|------|---------|----------------|------|
| day(30d) | 0.013953（1.40%，段B 残缺重归一主导） | 0.0140 | PASS（Δ<0.0002） |
| week(90d) | 0.032187（3.22%，段B 全段主导） | 0.0322 | PASS |
| month(180d) | 0.032187 | 0.0322 | PASS |
| year(365d) | 0.032187 | 0.0322 | PASS |

**结果（HRS，容差内复现 + 漂移机制闭环）**：

| 项 | 独立复算 | 文档 §10.1 | 判定 |
|----|---------|-----------|------|
| 段B 全段 dd | 0.129832 | 0.1298 | 精确一致 |
| 段C dd | 0.031563 | 0.0316 | 精确一致 |
| 段D dd（当前数据） | 0.190702 | 0.1882 | 漂移 +0.0025（见下） |
| 段D dd（QQQUSDT 还原后） | **0.188235** | 0.1882 | PASS |
| 四粒度 | 均由段D 主导（day 窗口段B 残缺 0.135206 < 段D） | 18.82×4 | 一致 |

**漂移机制（行级证据）**：QQQUSDT 于 10-13 09:23:49 有明细 `CONDITIONAL_ORDER +0.1599`（当时无 PNL_SUMMARY，属无汇总组，计入 10-13 持仓级日盈亏）；10-15 12:23:49 平仓明细 `+0.7564`；**10-15 13:24:13 写入 `PNL_SUMMARY +0.9163`**（= 0.1599 + 0.7564，逐笔自洽）→ QQQUSDT 从无汇总组移入有汇总组，其 +0.1599 从 10-13 的日盈亏中消失。将 10-13 还原为预演时点值（−7.105850 + 0.1599 = −6.945950）后，段D dd = 0.188235 ≈ 文档 0.1882。**同一机制同时解释抽验二的锚值漂移，量级分毫不差（均为 0.1599）**。

**B2 方向性验证**：按 v1.1 统一口径（段D 段初 = 10-09 事件后首条正快照 56.81）得 ~18.8%~19.1%，**不是 22.7%**（22.7% 来自被 B2 修正的「向前取 53.77」错误口径）——修订后口径可实现且与预演一致。

### 16.3 独立抽验二：AC-2.2a 锚值复算（用户硬性要求）

**方法**：按 §7.1/§10.3 的 SQL 口径（可改写、口径须一致）在生产库只读复算恒等式 `持仓级口径 − 整表 BOOL_OR 口径 == 无汇总组明细 SUM`。

**结果（2026-10-15 当前数据）**：

| 量 | 值（USDT） |
|----|-----------|
| Q1 整表 BOOL_OR 口径 | 18.816139 |
| Q2 持仓级 CTE 口径 | 13.379739 |
| 差（Q2−Q1） | **−5.436400** |
| 无汇总组明细合计（LITEUSDT −2.6035 / NBISUSDT +0.3861 / ONDOUSDT −3.2190） | **−5.4364** |

**恒等式精确成立**（分毫不差）。文档锚值 −5.2765 与当前复算 −5.4364 的差 **−0.1599 恰为 QQQUSDT 的量**（其已获得 PNL_SUMMARY 移入有汇总组，见 §16.2 行级证据）——即 §10.3 注释预见的「新增成交改变无汇总组构成」情形。**结论：SQL 口径可复现、恒等式成立；锚值绝对值随数据快照漂移属文档已声明的预期行为，生产验收必须断言恒等式而非字面值 −5.2765（已列入 §16.6 B 段必做）。**

### 16.4 B1~B5 闭环判定

| 阻塞 | 判定 | 依据 |
|------|------|------|
| B1（5 策略预演 + 零分母期 + grid + §12.1） | **闭环** | §10.1 已给出 5 策略段清单与各粒度期望值（btc_eth 经独立复算精确复现、HRS 容差内复现且漂移机制闭环）；§4.5 零分母期整段跳过 + INFO 日志；§4.4 grid 按事实改「无快照行 / 不可评分」；§12.1 风险 2 已改「全策略显著变化」 |
| B2（段初取值方向统一） | **闭环** | §4.1/§5.2/§10.1 三处统一为「生效时刻之后段内首条正快照（向后取）」；伪码改 created_at；E2 依附 E1 局限已记录（§4.1）；独立复算按此口径得 18.8%（非 22.7%）证实可实现 |
| B3（AC-2 因果链 + 相对断言 + 对账 SQL） | **闭环** | §7.1 三机制量化归因（①−5.2765 / ②0 / ③+3.03454）；AC-2.2 改相对断言（2a 恒等式精确复算 + 2b 方向性收敛）；§10.3 补对账 SQL。本复核独立复算恒等式精确成立（§16.3） |
| B4（AC-3.1 回写分母契约） | **闭环** | 需求 §4.5 AC-3.1 已回写为分母契约（段起点首条正快照，initial_capital 降级历史审计字段）；两文档通读无相反表述 |
| B5（AC-4 拍板） | **闭环** | 已按用户「专门一问」定稿选项 A（自然重算不回填）；§8.1 写清 3 条可见后果 + 改选 B 路径保留 |

### 16.5 新增不自洽项检查

通读两文档 v1.1 全文，**未发现新的阻塞性不自洽**。记录一项事实（非阻塞）：**§10.1 期望值与 §7.1/AC-2.2a 锚值均为预演时点数据快照值，随新增成交漂移**——本复核已实证两处漂移（HRS 18.82%→19.07%、锚值 −5.2765→−5.4364，同源 QQQUSDT、同量 0.1599）；文档已有 ±0.02/±2% 容差与 §10.3「部署时重跑」注释覆盖，属可接受，但 **B 段与 D 段验收不得对绝对快照值做字面断言**（已列入 §16.6）。

### 16.6 最终结论与 B 段交接

**GA 通过，可送 B 段（python-engineer）实施。** B1~B5 全部闭环，两项硬性独立抽验均已完成且证据成立（§16.2/§16.3）。本复核不替代编码后 C 段对实际实现的独立审查。

**B 段执行者必做事项（开工前逐条确认）**：

1. **N3 失效测试清单先行**：[test_performance_metrics.py](file:///Users/yl/vscode/Binance_quantitative_trading/tests/test_performance_metrics.py) 的 `TestBaseCapitalQueryNoFutureSnapshot`（断言旧「最早一条兜底」SQL）、`TestStrategyCurveBaseCapital`（断言旧口径净值构建）在新口径下必然失败——先列明并改写，**不得为迁就旧测试扭曲实现**。
2. **N1 精确断言**：单测锁死「分母 == 段初快照值」的精确断言（非 ±0.02 容差反推）；段初取值打 INFO 常开审计日志（§6.1），作为部署后验收证据来源。
3. **E3 阈值 K 必须配置化**（默认建议 3 天，§12.1/待决清单），禁止硬编码；用 104.22（恒 10 天）与每日漂移样本做测试守门。
4. **AC-2.2a 生产验收断言恒等式**（持仓级 − 整表 == 无汇总组明细合计），**禁止字面断言 −5.2765**（本复核已实证漂移至 −5.4364）。
5. **§10.1 期望值为预演时点快照**：部署后生产验收前按 §10.3 命令重导出期望值再比对（本复核已实证 HRS 18.82%→19.07% 漂移）。
6. **N4 交叉断言**：增加「dashboard 日和 == ai_tuner 月和」黄金用例（不跨窗口边界数据集）。
7. **N2 效应隔离用例**：「无快照期巨额亏损 + 有数据期小额回撤」用例，断言跳过效应与分段效应隔离；文档口径明示「回撤仅覆盖有分配快照期间」。
8. **实施顺序按 §9**：CS1→CS2 串行（同文件 `data_service_docker.py`），CS3（ai_tuner）可并行，CS4（caliber_version 审计列）可选——是否本轮做 CS4 与 K 默认值，开工前向用户确认。
9. 遵循工作区编码规范：禁硬编码、中文注释、无重复代码、无幽灵参数。

---

## 17. B 段实现说明（实际实现命名，2026-10-15）

> **性质**：本节为**实现注记**，仅记录 B 段实际落地后的函数/常量命名与 A 段草案命名（§6.2 等）的差异，供文档对照追溯；**不改写上文 A 段冻结契约正文**（§1–§16 仍为设计基线，凡与 B 段实际实现有出入者以本节为准）。

**实现位置**：`dashboard/backend/services/data_service_docker.py`（CS1/CS2/CS4）、`ai_tuner/allocation/pnl_collector.py`（CS3）、`database/postgres/init-scripts/09-performance-metric-snapshot.sql`（CS4 DDL）。

| 环节 | A 段草案命名 | B 段实际实现命名 | 差异说明 |
|------|--------------|------------------|----------|
| 段切分（纯函数） | `derive_segments(...)` | `derive_segments(strategy_id, capital_allocation_rows, snapshots, static_days_k)` | 一致 |
| 段切分（落库取数） | —（草案未单独命名） | `_derive_segments_for_strategy`（DB 查询 + 段初填充 + 逐段 INFO 审计日志） | 新增落地方法 |
| 策略级净值序列 | `_build_strategy_daily_returns` | `_build_strategy_segmented_series` | **旧名已删除**，改名体现分段语义 |
| 段初取值 | `_query_strategy_base_capital` | **已删除**；`_SEGMENT_OPENING_CAPITAL_SQL`（段初「向后取」）+ `_derive_segments_for_strategy` 内填充 | 旧 4 步回退链不再用于看板 |
| 多粒度绩效 | —（草案在 `compute_and_store_performance_metrics` 内展开） | `_compute_strategy_perf_by_segments`（策略级）+ `_segment_window_overlap_idx`（段-窗口交集起点）；账户级仍 `_compute_one_perf_entry` | 拆分为策略级/账户级两条路径 |
| 顶层入口 | `compute_and_store_performance_metrics` | `compute_and_store_performance_metrics` | 同名保留 |
| K（辅锚 E3 静态阈值） | 配置化（默认建议 3，禁硬编码） | `_PERF_SEGMENT_STATIC_DAYS = int(os.getenv("PERF_SEGMENT_STATIC_DAYS", "3"))` | 环境变量配置化 |
| 审计列写入 | `caliber_version`（原为可选，§5 待拍板 2） | `_CALIBER_VERSION = "segmented-v1"`；`_PERF_METRIC_UPSERT_SQL`（11 列含 `caliber_version`） | CS4 本轮已实现（由「可选」转「本轮实现」） |

**与 A 段草案的实质差异**：①旧 `_build_strategy_daily_returns` 与 `_query_strategy_base_capital` **均已从 `data_service_docker.py` 删除**，改为「分段序列构建 + 段初向后取」；②账户级（`scope='total'`）绩效路径**未变**（仍走尾部切片 `_compute_one_perf_entry`），仅策略级（`scope='strategy'`）切换到分段口径；③CS4（审计列）由「可选」转为**本轮实现**。

**交叉引用**：[decision 20261015-segmented-drawdown-caliber](../knowledge/decisions/Binance_quantitative_trading-20261015-segmented-drawdown-caliber.md)（口径决策沉淀）。

---

（文档结束）