---
id: Binance_quantitative_trading-20261013-hrs-pnl-double-count
类型: issue
项目: Binance_quantitative_trading
标题: HRS 盈亏双重记账，混合求和致亏损虚增 13 倍
日期: 2026-10-13
标签: [bug, hrs, 盈亏口径, 防双计, dashboard, ai_tuner]
任务标识: hrs_pnl_dedup_2026-10-13.md
状态: 已解决（统计层已部署 f940a61/91e99c2；写入层已修复待部署；回填待授权）
审查: 已审查
---

# HRS 盈亏双重记账，混合求和致亏损虚增 13 倍

> **后续治理更新（2026-10-14）**：统计层修复只压住双计、未解决写入层漏写汇总，本文多处"-2.28"为**统计层修复后的混合值，非真值**。HRS 各月真值以币安 income 对账为准（2026-09 为 **-39.6538**），详见下文「后续治理」章节。

## 现象
2026-10 月度资金分配报告中 HRS 收益率 **-40.76%** 排名垫底（分配 10%），但用户对 HRS 实际亏损体量存疑。生产库核对：HRS 2026-09 全月下单 313 行、`SUM(realized_pnl) = -31.31`，其中 `PNL_SUMMARY` 汇总行合计仅 **-2.28**、非汇总明细行合计 **-29.03**，两个"正确"口径之间相差 26.75 USDT。

## 根因
HRS 独有的**双路径记账**并存（全库仅 HRS 有 PNL_SUMMARY 行，75 条）：

1. `shared/trade_logger.py` `update_realized_pnl`：每笔**部分平仓**（TP1/TP2/止损）的盈亏 **UPDATE 回填**到已有成交行（TAKE_PROFIT/STOP_MARKET/CONDITIONAL_ORDER 等）；
2. `insert_pnl_summary`：整笔持仓全部平仓后 **INSERT** 一条 `PNL_SUMMARY` 汇总行，金额 = 各部分之和。

任何"混合 `SUM(realized_pnl)`"统计都把同一笔平仓记**两次**（明细一遍 + 汇总一遍）。铁证：SOPHUSDT 9 月 `+1.014966 + (-3.827694) = -2.812728`，其中 -2.812728 又以 PNL_SUMMARY 行落库；9 月 36 组 (symbol, side) 中 **11 组两类行金额 8 位小数完全相同**。代码注释早有前车之鉴（`strategies/hrs/strategy.py` 止损场景"同一笔平仓被记两次，HRS 回撤因此虚高到 100%"），但"累计 + 明细并存"的结构性双计一直未在统计口径层治理。看板绩效（gross_pnl/closed_count/wins/losses）、日盈亏曲线、月度分配盈亏采集共用同一混合求和，全部失真。

## 解决
**统计口径层防双计条件 SQL**（数据写入层治理另行排期）：

```sql
CASE WHEN BOOL_OR(order_type = 'PNL_SUMMARY')          -- 统计组内存在汇总行
     THEN COALESCE(SUM(realized_pnl) FILTER (WHERE order_type = 'PNL_SUMMARY'), 0)  -- 只算汇总
     ELSE COALESCE(SUM(realized_pnl), 0)               -- 无汇总回退全行（其他策略行为不变）
END
```

不能一刀切"只取 PNL_SUMMARY"：全库其他策略汇总行为 0，一刀切会把它们收益率全部算成 0，必须做组内条件回退。

| 文件 | 改动 |
|---|---|
| `ai_tuner/allocation/pnl_collector.py` | `_PNL_QUERY_TEMPLATE` 改防双计条件口径（commit `f940a61`） |
| `ai_tuner/allocation/tests/test_allocation.py` | 新增守卫测试防口径回退 |
| `dashboard/backend/services/data_service_docker.py` | `_TRADE_RECORD_AGG_COLUMNS` 单源列模板 + `_DAILY_PNL_QUERY` 日盈亏查询同款条件（commit `91e99c2`）；wins/losses 从"分笔正负"演进为"整笔正负" |
| `tests/test_performance_metrics.py` | 双侧守卫测试（与 ai_tuner 锚定对齐） |

跨服务（ai-tuner / dashboard 独立镜像）无法共享模块，采用**同款 SQL 条件 + 双侧守卫测试 + 互相锚定注释**保证口径一致。

## 验证
- `pytest ai_tuner/allocation/tests/` = 48 passed；`tests/test_performance_metrics.py` = 29 passed
- 生产库只读验证（2026-09）：hrs **-31.31 → -2.28**；btc_eth +11.88 / new_coin -4.58 / aggressive +35.02 不变
- 看板新口径：hrs closed_count=39（= 汇总笔数）、wins=21、losses=17、gross_pnl=-2.28
- 部署：`f940a61` deploy_id=**00004501**（ai-tuner）、`91e99c2` deploy_id=**00004601**（dashboard-api），VERSION/MD5 五层验证通过
- 修复后口径重算 2026-10 期分配（分母用 05888cc 回退链）：aggressive +32.52% > btc_eth +13.57% > **hrs -3.91% > new_coin -3.92%**——hrs/new_coin 排名会翻转（差距仅 0.009pp），经用户决策**本期分配维持现状**，新口径自 2026-11 分配生效
- **口径说明（2026-10-14 修正）**：上述"-31.31 → -2.28"中，**-2.28 是统计层防双计条件 SQL 修复后的混合值，并非 9 月真值**。写入层仍漏写汇总（第三平仓路径），真值见「后续治理」章节的 income 对账结果（9 月 **-39.6538**）。

## 适用范围与修订
`ai_tuner/allocation/pnl_collector.py` + `dashboard/backend/services/data_service_docker.py` 两处统计口径。**写入层双路径并存未治理**：TP 未全部成交的持仓只有明细无汇总、降级匹配可能错行（56 行 CONDITIONAL_ORDER 带 realized_pnl），两个"正确"口径仍差 26.75——如需精确对账需另立任务治理 `trade_logger.py` 写入路径或与币安 income 对账。看板预计算表 `ON CONFLICT 覆盖写 + 120 天回溯`，新口径部署后历史数据自动重写（HRS 回撤从虚高 100% 回落）。（2026-10-14 更新：该治理任务已立项 `hrs_pnl_governance` 并完成 income 对账 + 写入层修复，见「后续治理」章节。）

## 后续治理（2026-10-14，income 对账立项）

统计层修复（f940a61/91e99c2）后 HRS 看板日最大回撤仍 100%，深挖确认：**统计层只压住了双计，未解决写入层漏写汇总（第三平仓路径）**，且 trade_records 内部任一聚合都无法自证真值。遂立项 R3 任务 `hrs_pnl_governance`，以**币安 PM 账户 income（REALIZED_PNL）流水为唯一真值**。

### 真值定案（income 逐笔匹配归属 HRS）

| 月份 | 旧混合 SUM（C） | 统计层修复后（A 仅汇总） | **E income 真值（HRS）** | E' income 全量 |
|------|----------------|------------------------|------------------------|---------------|
| 2026-07 | `6.3560` | `0.0000` | **`+6.3560`** | `111.3269` |
| 2026-08 | `-45.6020` | `1.1255` | **`-54.5305`** | `-115.6006` |
| 2026-09 | `-31.3143` | `-2.2833` | **`-39.6538`** | `-0.3359` |
| 2026-10 | `12.8473` | `10.2858` | **`+3.4292`** | `19.3803` |

**关键修正**：本文此前的"-31.31 → -2.28"表述中，**-2.28 是统计层修复后的混合值**（防双计条件 SQL 之结果），仍非真值——写入层存在漏写汇总 + 降级匹配错行两类缺陷（详见下文）。真值为 **2026-09 的 -39.6538**。2026-09 五口径对照：A 汇总 `-2.2833` / B 明细 `-29.0310` / C 混合 `-31.3143` / D 持仓级去重 `-35.9978` / **E income 真值 `-39.6538`**。

对账脚本 `ai_tuner/reconciler/hrs_income_reconcile.py`（7 天窗口 + 先金额容差后时间窗 + Decimal 全路径），income 1002 条（去重后），匹配率 60.1%（602/1002）；报告 `docs/reports/hrs_income_reconcile_report.md`（含 §八 SOLUSDT 归属验证：9 月 SOLUSDT +42.31 无 trade_records 明细支撑，判定为 orphan、E 口径已正确排除），40 项单测全绿。

### H6：第三平仓路径结构漏写汇总（9 月 -33.71 的成因）

时间止损 / 移动止盈经 `trading_executor.close_position(close_percent=1.0)` → `strategies/hrs/strategy.py::_finalize_close_if_filled` → `_writeback_pnl`（明细 UPDATE）→ `remove_position`，**结构性不写 `insert_pnl_summary`**，且删仓后主循环持仓为 None、**永不触发全平回写**。2026-09 有 114 行明细属"从未写过汇总"的 (symbol,side) 组，合计 **-33.71**。BTRUSDT 三笔实证（`-6.7658` / `-9.8343` / `-10.7540` = **`-27.35405`**）经 income 匹配确认归属 HRS，但**无任何 PNL_SUMMARY 行**。→ 独立沉淀 issue：[20261014-hrs-third-close-path-missing-summary](./Binance_quantitative_trading-20261014-hrs-third-close-path-missing-summary.md)。

### 写入层修复清单（代码完成，178 测试通过，GC 审查通过，待部署）

| 文件 | 改动 | 对应根因 |
|------|------|---------|
| `strategies/hrs/strategy.py::_finalize_close_if_filled`（L3574-3670 附近） | 全平分支在 `remove_position` 前补调 `_writeback_pnl_for_full_close`（复用"实际值优先→理论兜底→since 幂等"取值链） | H6 |
| `strategies/hrs/strategy.py` 时间止损路径（L1997-2001 附近） | 移除冗余 `mark_stop_loss`（与汇总行 `close_reason=STOP_LOSS` 双写）；副作用：盈利的时间止损不再计止损次数（如实记录） | H6 次生 |
| `shared/trade_logger.py::insert_pnl_summary`（L496-510 附近） | H1 去重收紧：移除 `AND realized_pnl = $4`（`since=entry_time` 已保证幂等，避免金额巧合误挡） | H1 |
| `shared/trade_logger.py::update_realized_pnl`（L349-416 附近） | L1 降级匹配守卫：时间窗上限 `_MAX_FALLBACK_WINDOW_SECONDS`（默认 1800s）+ SQL 排除 `order_type IN ('LIMIT','CONDITIONAL_ORDER')` 错行 | L1 |

### 回填概况（dry-run 就绪，待用户授权执行）

`ai_tuner/reconciler/hrs_pnl_backfill.py`：识别 **21 组待回填、0 组未决**，金额**锚 income 真值合计 `-52.920`**（≠ 明细 SUM `-56.003`；EPICUSDT 反转 `-0.806 → +0.948`）。回填行 `order_id` 写批次号（`backfill-YYYYMMDD-NN`）便于回滚，`executed_at` 取该组最后一笔平仓成交时间（禁止取执行时刻）；默认 dry-run，执行需人工授权。

### 真实交易对更正（用户核实）

`牛来USDT`、`SAMSUNGUSDT`、`MUUSDT`、`DRAMUSDT` 等经用户在币安 APP 核实**均为真实币安交易对**（策略开过订单）。C 组 `-33.71` **全部为真实成交、仅汇总缺失**；此前若将其判为"测试脏数据 / 假交易对"的判断**作废**。

## 教训
"明细行 + 汇总行"并存的数据模型里，任何聚合统计都必须声明取哪一层，混合求和必然双计；且修复统计口径时若多个服务读同一张表，必须同步修改并互相锚定（守卫测试），否则一处修了一处照旧。（AI 归纳，仅供参考）

## 关联
- 持久证据: commit `f940a61` / `91e99c2`；生产库 9 月双计实测（-2.28 vs -29.03 vs -31.31）；SOPHUSDT 案例行
- 持久证据（后续治理）: 对账报告 `docs/reports/hrs_income_reconcile_report.md`；对账脚本 `ai_tuner/reconciler/hrs_income_reconcile.py`（+ `tests/test_hrs_income_reconcile.py` 40 项）；回填工具 `ai_tuner/reconciler/hrs_pnl_backfill.py`；设计文档 `docs/plans/hrs_pnl_governance_design.md`
- 相关: [Binance_quantitative_trading-20261014-hrs-third-close-path-missing-summary](./Binance_quantitative_trading-20261014-hrs-third-close-path-missing-summary.md)（本条写入层延续：第三平仓路径结构漏写 PNL_SUMMARY）
- 相关: [Binance_quantitative_trading-20261013-allocation-return-denominator-zero](./Binance_quantitative_trading-20261013-allocation-return-denominator-zero.md)（同属月度分配口径缺陷；该条修分母，本条修分子双计）
- 相关: `strategies/hrs/strategy.py` 止损标记降级注释（历史上已修过一次同类双计）
