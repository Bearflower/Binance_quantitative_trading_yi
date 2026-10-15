---
id: Binance_quantitative_trading-20261015-segmented-drawdown-caliber
类型: decision
项目: Binance_quantitative_trading
标题: 看板最大回撤改为「资金段」分段口径（segmented-v1）
日期: 2026-10-15
标签: [dashboard, 指标口径, ai_tuner]
任务标识: capital_base_caliber_fix_2026-10-15.md
状态: 已采纳
审查: 待审查
---

# 看板最大回撤改为「资金段」分段口径（segmented-v1）

> **权威依据**：架构方案 [fix-2026-10-15-capital-base-caliber-architecture.md](../../plans/fix-2026-10-15-capital-base-caliber-architecture.md) **v1.1**（A 段冻结契约，已过 R3 A-review；下文括号内 §n 均指该文档章节）与需求 [fix-2026-10-15-capital-base-caliber-requirements.md](../../plans/fix-2026-10-15-capital-base-caliber-requirements.md) v1.1（AC-1..AC-4）。本条为该口径的**决策沉淀**；如与架构正文有出入，**以架构 v1.1 为准**。
> **取代**：[Binance_quantitative_trading-20261009-max-drawdown-caliber](./Binance_quantitative_trading-20261009-max-drawdown-caliber.md)（2026-10-09「全窗口单一资金基数」口径，2026-10-15 起被本条取代）。
> **实现位置**：`dashboard/backend/services/data_service_docker.py`（CS1/CS2/CS4）、`ai_tuner/allocation/pnl_collector.py`（CS3）、`database/postgres/init-scripts/09-performance-metric-snapshot.sql`（CS4 DDL）。

## 背景
看板策略级最大回撤长期异常（HRS 四粒度恒 100%）。根因不是统计 bug，而是旧设计「**全窗口单一资金基数（固定值）**」与 ai_tuner「**每日按净资产刷新分配额**」不兼容：旧口径取 `at_time = today-365d` 单点查基数（HRS 365 天前无数据 → 命中兜底「全表最早一条 allocated_amount = 58.37」），而实际分配额自 9/21 起已漂离 58.37，导致净值被钳 0、回撤顶格 100%（伪击穿）。完整根因与生产数据核实见需求 §2.1/§2.2 与架构 §2.2。

## 备选方案
- **A. 全窗口单一资金基数（旧口径，2026-10-09 决策）**：整窗口复用同一 `base_capital`，尾部切片不重基准，跨段净值继承。缺陷：与「分配额每日漂移」不兼容，前段触 0 污染后段（伪 100%），无可还原的「名义段初基数」。
- **B. 资金段（capital segment）分段口径（本决策采纳）**：按资金分配事件切段，段内以「段初分配额」为分母独立累积，跨段不比较。已在本地只读预演把 HRS 各粒度最大回撤从 100% 降到 **18.82%**（架构 §10.1）。
- 备选落败项（弃用）：`entries.initial_capital` 作段初分母（历史口径不统一、月粒度无法区分月内多段，架构 §5.1）；金额阈值切段（9/22 起金额逐日漂移会造出大量伪段，架构 §4.1）。

## 决策
采纳 B：看板策略级回撤/净值改为**资金段分段口径**，审计列标记 `caliber_version='segmented-v1'`。口径要点（与架构 v1.1 逐条对应）：

1. **段边界定义（架构 §4.1）**：段边界 = 分配计划变更事件，日内金额漂移**不算**段边界。判定采「主锚 + 辅锚」：
   - 主锚 **E1**：`capital_allocation` 新增一条 month 记录 → 事件时点 = 该记录 `created_at`；
   - 主锚 **E2**：相邻 month 记录的 `entries[x].allocated_ratio` 不同 → 事件时点**依附 E1 的 `created_at`**（表无 `effective_time`，月内手工 ratio 变更无法独立定时刻）；
   - 辅锚 **E3**：`snapshot_history.allocated_amount` 静态跳变，认定条件「跳变后**恒定 ≥ K 天**且不对齐任何 E1/E2」；**K 默认 3 天且配置化**（禁硬编码，实现为 `DataService._PERF_SEGMENT_STATIC_DAYS = int(os.getenv("PERF_SEGMENT_STATIC_DAYS", "3"))`，架构 §12.1 §14.3）。
   - 去噪特征：段边界对齐 E1/E2，或「跳变后恒定 ≥ K 天」；每日刷新漂移（北京时间 00:00 附近、幅度小、连续多日变动）被排除。
   - 派生函数：纯函数 `derive_segments(strategy_id, capital_allocation_rows, snapshots, static_days_k)` → 事件时点列表；落地为 `_derive_segments_for_strategy`（含 DB 查询 + 段初填充）。

2. **段初口径（架构 §4.1 落地规则 / §5.1 契约）**：段初分配额 = **段边界事件生效时刻（E1/E2 取 `created_at`、E3 取跳变 `snapshot_hour`）之后、该段内首条 `allocated_amount > 0` 快照**（**向后取**，`snapshot_hour >= 事件时点 ORDER BY ASC LIMIT 1`，SQL 常量 `_SEGMENT_OPENING_CAPITAL_SQL`）。**不用 `initial_capital`，也不回退「全表最早一条」**。与旧链方向相反（旧链 `snapshot_hour <= $2 ORDER BY DESC` 向前取）。快照表无任何事件/快照的历史期退化为单段（段初=首条正快照，`snapshot_debut`）。

3. **段内净值公式（架构 §3.2/§4.2）**：
   ```
   段内净值      nv = max(0, 1 + cum_pnl_in_segment / segment_opening_capital)   # 段内不复利；负值钳 0
   段内日收益率  r_t = day_pnl_t / segment_opening_capital                        # 供 sharpe
   段内最大回撤  dd_seg = max_drawdown_ratio(段内 net_values)
   总体最大回撤  max_dd = max(各段 dd_seg)                                        # 跨段不比较
   ```
   负值钳 0 = 本金亏光语义（回撤上限 100%）但只限单段，不影响其它段。

4. **跨段不比（架构 §4.2）**：每段 `dd_seg` 独立，`max_dd = max(dd_seg)`；前段触 0 不污染后段（消除伪 100%）。

5. **多粒度窗口相交 / 残缺段 cum-reset（架构 §4.3，AC-1.5）**：**不能靠尾部切片**。在完整分段序列上先按段切分、段内重基准，再按窗口 `[today-window_days, today]` 截取各段子序列：先求段-窗口交集起点索引（`_segment_window_overlap_idx`），对窗口内子序列**从 1.0 重新归一（cum 从 0 起）**重建净值（`nv = max(0, 1 + Σ r in seg_window_rets)`），该段 `opening_capital` 仍取**真实段初快照值**（不随窗口重取）。**完整段与残缺段同一套 cum-reset 公式——非比例缩放**（压缩实现曾用「按交集比例缩放」，已由阻断项修复替换为 cum-reset）。各段取最大（`_compute_strategy_perf_by_segments`）。

6. **零分母跳过（架构 §4.5，AC-1 边界表）**：某段段初分配额为 **0 或不存在**（含 `allocated_amount=0` 确有成交者）时，该段**整段跳过、不计入任何段回撤**，**不得**静默用 0 或其它段值作分母，也不得回退「全表最早一条」。等价于「首段前无数据期跳过」同一语义。对每个段打**常开 INFO 审计日志**（`strategy_id`/`segment_index`/`segment_start`/`event_type`/`opening_capital`/`skip`）。btc_eth_aggressive 9-10~9-20 零分母期即整段跳过。

7. **CS3：ai_tuner 月度口径持仓级去重（架构 §7.1，AC-2）**：`ai_tuner/allocation/pnl_collector.py` 的 `_PNL_QUERY_TEMPLATE` 由「整表 `BOOL_OR(order_type='PNL_SUMMARY')`」升级为**持仓级 CTE**：`GROUP BY strategy, symbol, side` + `IS NOT DISTINCT FROM`（JOIN 键防 NULL 丢行）；有汇总组只取 `PNL_SUMMARY`（天然防 `CONDITIONAL_ORDER` 同值双写），**无汇总组回退明细 SUM**（`NOT g.has_summary`）。修的是「无汇总组明细漏计」（机制①，10 月 HRS 实测 −5.2765），**不**负责跨月 carry-in（机制③，+3.03454，归 `hrs_pnl_governance`）。不抽 `shared/`（避免触发全部策略容器重建），以 SQL 文本守卫测试 + 注释互指保证与 dashboard `_DAILY_PNL_QUERY` 语义一致。

8. **审计列 `caliber_version`（架构 §6.1 / CS4）**：`public.performance_metric_snapshot` 幂等追加可空 `caliber_version VARCHAR(16)`（`ADD COLUMN IF NOT EXISTS`）；新口径 UPSERT 写入 `_CALIBER_VERSION = "segmented-v1"`（UPSERT 11 列含 `caliber_version`）。旧代码/旧消费方不读此列，无回归。段边界本身**不落地新表/新字段**（实时从 `snapshot_history` + `capital_allocation` 推导）。

**边界事实（架构 §4.4）**：grid 在 `strategy_position_snapshot_history` **无任何行**，新口径下标记「**不可评分**」，**不产生任何新行、不覆盖旧值**（无回归）；其 `performance_metric_snapshot` 残留行维持现状。账户级（`scope='total'`）口径**不变**（以 `equity_snapshot` 净值曲线为准，仍走尾部切片 `_compute_one_perf_entry`）。

## 理由
- 段边界「可确定性还原」：`snapshot_history` 有小时级 `allocated_amount` 留痕，段边界可从「跳变后是否逐日漂移」的时间模式特征稳健区分（架构 §4.1/§2.2）；`capital_allocation` 是 JSONB 单值、每日覆盖写无逐日历史，故段边界不能从它还原。
- 与 ai_tuner「每日按净资产刷新分配额」机制兼容：段内固定分母 = 段的名义资金基数，消除「固定分母 vs 漂移额度」的错误配对。
- 段内不复利、负值钳 0，继承 2026-10-09 决策的三项原则（不复利/上限 100%/不取未来快照），避免复利放大与未来值幻觉。
- 生产只读预演已验证：HRS 各粒度 100% → 18.82%（架构 §10.1），段 D（10-09 起、段初 56.81）贡献整体值。

## 后果
- **眼下收益**：策略级回撤不再恒等 1.0（HRS ≈18.82%）；5 策略（除 grid 不可评分）分母由「固定单值」切换为「分段分母」，数值全部显著变化（架构 §10.1 期望值表，部署后按 §10.3 只读 SQL 对账，容差 ±2%）。
- **技术债 / 待偿还**：
  1. **历史桶不回填（AC-4 定稿选项 A）**：仅改算法，历史（非当前）桶残留旧口径 `max_drawdown`（如 HRS 历史 = 1.0）；当前无历史回看界面时不可见，接受该声明（架构 §8.1）。回滚路径 §8.3 保留。
  2. **E3 阈值 K / 段边界还原精度（R3 核心风险）**：K 不当可能误判段或漏段；已配置化（默认 3）并用 104.22（恒 10 天）与每日漂移样本做测试守门（架构 §12.1/§12.2）。
  3. **E2 时点不可得**：月内手工 ratio 变更若无静态留痕则无法识别（依附 E1，架构 §4.1 局限 N6）。
  4. **段初取值 3 候选差异**：段 D 段初有 56.81（采纳）/57.92（month 记录初值）/76.82（`initial_capital` open_margin 残值）三候选，架构锁定 56.81，B 段加断言防漂移（架构 §10.4）。
  5. **机制③跨月 carry-in 未消**：`_PNL_QUERY_TEMPLATE` 持仓级化**不**消除 `PNL_SUMMARY` 累计语义跨月 carry-in（+3.03454），归 `hrs_pnl_governance` 后续处理（架构 §7.1）。

## 适用范围与修订
`dashboard/backend/services/data_service_docker.py`（CS1/CS2：`derive_segments`/`_derive_segments_for_strategy`/`_build_strategy_segmented_series`/`_compute_strategy_perf_by_segments`/`_segment_window_overlap_idx`/`_SEGMENT_OPENING_CAPITAL_SQL`/`_PERF_SEGMENT_STATIC_DAYS`）；`ai_tuner/allocation/pnl_collector.py`（CS3：`_PNL_QUERY_TEMPLATE`）；`database/postgres/init-scripts/09-performance-metric-snapshot.sql`（CS4：`caliber_version`）。K 值 `PERF_SEGMENT_STATIC_DAYS` 可调；`_CALIBER_VERSION` 更换即视为**新口径批次**（审计列用途，架构 §6.1）。

- **替代**：[Binance_quantitative_trading-20261009-max-drawdown-caliber](./Binance_quantitative_trading-20261009-max-drawdown-caliber.md) 被本条取代（2026-10-15；原因：其「全窗口单一基数」与分配额漂移不兼容）；2026-10-09 决策的三项原则（不复利/上限 100%/不取未来快照）被本条继承。
- **本条至今有效、无进一步替代条目**；如未来 K 或段定义再变，应另立 decision 并双向链接。
- **审查说明**：内容取自 A 段 architecture v1.1（已过 R3 A-review 与 v1.1 复审）与 B 段实现（已过 C 段独立审查、无阻断项）；代码引用已按仓库相对路径 + 函数名/常量名核对。按知识库规范默认「待审查」，待 code-document-curator 放下时复核转正。

## 关联与证据
- 本地补充来源: [handoff](../../handoffs/capital_base_caliber_fix_2026-10-15.md)（不随 Git 同步）
- 持久证据: 架构 [fix-2026-10-15-capital-base-caliber-architecture.md](../../plans/fix-2026-10-15-capital-base-caliber-architecture.md) v1.1（§3/§4.1-§4.5/§5.1/§6.1/§7.1/§9/§10.1/§13.1）+ 需求 [fix-2026-10-15-capital-base-caliber-requirements.md](../../plans/fix-2026-10-15-capital-base-caliber-requirements.md) v1.1（AC-1..AC-4）；代码 `dashboard/backend/services/data_service_docker.py`、`ai_tuner/allocation/pnl_collector.py`、`database/postgres/init-scripts/09-performance-metric-snapshot.sql`
- 必要证据摘要: 旧口径 HRS 四粒度 = 1.0（伪击穿，分母 58.37）→ 分段口径 HRS 各粒度 = 0.1882（18.82%，段 D 段初 56.81）；grid 不可评分不产新行；`caliber_version='segmented-v1'`；CS3 恒等式「持仓级 − 整表 == 无汇总组明细」（10 月 == −5.2765）
- 相关: [Binance_quantitative_trading-20261013-allocation-return-denominator-zero](../issues/Binance_quantitative_trading-20261013-allocation-return-denominator-zero.md)（同为分母口径主题；其修复时参照的看板 `_query_strategy_base_capital` 已随本条删除）；[Binance_quantitative_trading-20261013-hrs-pnl-double-count](../issues/Binance_quantitative_trading-20261013-hrs-pnl-double-count.md)（CS3 与其同源，机制③ carry-in 归其后续）
