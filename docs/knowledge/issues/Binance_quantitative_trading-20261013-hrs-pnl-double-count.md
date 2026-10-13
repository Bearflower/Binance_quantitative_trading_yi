---
id: Binance_quantitative_trading-20261013-hrs-pnl-double-count
类型: issue
项目: Binance_quantitative_trading
标题: HRS 盈亏双重记账，混合求和致亏损虚增 13 倍
日期: 2026-10-13
标签: [bug, hrs, 盈亏口径, 防双计, dashboard, ai_tuner]
任务标识: hrs_pnl_dedup_2026-10-13.md
状态: 已解决（统计口径层）
审查: 待审查
---

# HRS 盈亏双重记账，混合求和致亏损虚增 13 倍

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

## 适用范围与修订
`ai_tuner/allocation/pnl_collector.py` + `dashboard/backend/services/data_service_docker.py` 两处统计口径。**写入层双路径并存未治理**：TP 未全部成交的持仓只有明细无汇总、降级匹配可能错行（56 行 CONDITIONAL_ORDER 带 realized_pnl），两个"正确"口径仍差 26.75——如需精确对账需另立任务治理 `trade_logger.py` 写入路径或与币安 income 对账。看板预计算表 `ON CONFLICT 覆盖写 + 120 天回溯`，新口径部署后历史数据自动重写（HRS 回撤从虚高 100% 回落）。

## 教训
"明细行 + 汇总行"并存的数据模型里，任何聚合统计都必须声明取哪一层，混合求和必然双计；且修复统计口径时若多个服务读同一张表，必须同步修改并互相锚定（守卫测试），否则一处修了一处照旧。（AI 归纳，仅供参考）

## 关联
- 持久证据: commit `f940a61` / `91e99c2`；生产库 9 月双计实测（-2.28 vs -29.03 vs -31.31）；SOPHUSDT 案例行
- 相关: [Binance_quantitative_trading-20261013-allocation-return-denominator-zero](./Binance_quantitative_trading-20261013-allocation-return-denominator-zero.md)（同属月度分配口径缺陷；该条修分母，本条修分子双计）
- 相关: `strategies/hrs/strategy.py` 止损标记降级注释（历史上已修过一次同类双计）
