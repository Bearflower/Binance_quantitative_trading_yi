---
id: Binance_quantitative_trading-20261014-adhoc-sql-guess-columns
类型: issue
项目: Binance_quantitative_trading
标题: 临时 psql 会话猜列名/窗口函数误用，postgres 日志报 5 条 ERROR（非生产缺陷）
日期: 2026-10-14
标签: [运维, postgres, ad-hoc SQL, 窗口函数, 日志排查]
任务标识: postgres_error_triage_2026-10-14
状态: 已解决（无需代码修复）
审查: 待审查
---

# 临时 psql 会话猜列名/窗口函数误用，postgres 日志报 5 条 ERROR（非生产缺陷）

## 现象
用户提交 postgres 容器日志 5 条 ERROR（均发生在 2026-10-13 UTC 12:55–15:09）：
1. `column "position_value" does not exist at character 49`
2. `column "pnl" does not exist at character 86`
3. `column "closed_count" does not exist at character 54`
4. `aggregate function calls cannot contain window function calls at character 436`
5. `window functions are not allowed in WHERE at character 494`

日志连接标记均为 `trading_user@psql [local]`。**注意语义**：`@psql` 是 log_prefix 里的 application_name（psql CLI 默认应用名），不是数据库名——实际数据库是 `trading_platform`（容器 env `POSTGRES_DB` 已核实），数据库 "psql" 并不存在。`[local]` = unix socket，即 **docker exec 进 postgres 容器手跑的 psql 临时会话**，非任何容器的应用连接（应用走 TCP）。

STATEMENT 回溯（服务器日志 12807/12846/12863/12867/12869 行）：查询对象为 `public.strategy_position_snapshot_history`、`trading.trade_records`、`public.performance_metric_snapshot` 及 HRS 日盈亏窗口查询，硬编码时间戳、分号结尾，与当天 HRS 盈亏双计调查（见关联条目）场景吻合。

## 根因
三类错误，全部来自临时会话直接执行未经验证的探索性 SQL：

1. **猜列名 ×3**（对线上表结构没有先行核对）：
   - `strategy_position_snapshot_history` 无 `position_value`——真实列为 `strategy_id, strategy_name, open_position_count, open_margin, allocated_amount, occupied_ratio, snapshot_hour, updated_at`（线上 information_schema 已核对，与 [06-position-snapshot.sql](../../../database/postgres/init-scripts/06-position-snapshot.sql) 一致）；
   - `trade_records` 无 `pnl`——真实列为 `id, strategy, symbol, order_id, side, order_type, quantity, price, commission, status, executed_at, realized_pnl, close_reason, algo_id`；
   - `performance_metric_snapshot` 无 `closed_count`——该列在 `public.metric_snapshot`（[05-metric-snapshot.sql](../../../database/postgres/init-scripts/05-metric-snapshot.sql)），而 [09-performance-metric-snapshot.sql](../../../database/postgres/init-scripts/09-performance-metric-snapshot.sql) 的绩效表只有 `sharpe/max_drawdown/sample_count` 等风险调整指标。两张快照表名字相近易混。
2. **窗口函数语法 ×2**：`MIN(SUM(day_pnl) OVER (...))`（聚合内嵌窗口）与 `WHERE SUM(...) OVER (...) < -25`（窗口进 WHERE）均为 PostgreSQL 语法禁止项。

## 解决
**生产代码零缺陷、零改动**（部署 N/A）：全库 grep 确认生产 SQL（`ai_tuner/allocation/pnl_collector.py`、`dashboard/backend/services/data_service_docker.py`、`ai_tuner/allocation/performance_reader.py` 等）使用的表名列名全部正确。5 条 SQL 均在 parse/analyze 阶段失败，**未执行、无数据变更**。

沉淀正确写法（如该分析仍需要）：
```sql
-- 窗口函数修正：先在子查询算窗口列，外层再过滤/聚合
SELECT * FROM (
    SELECT DATE(executed_at) d, SUM(realized_pnl) day_pnl,
           SUM(SUM(realized_pnl)) OVER (ORDER BY DATE(executed_at)) cum
    FROM trading.trade_records
    WHERE strategy='hrs' AND order_type='PNL_SUMMARY' AND realized_pnl IS NOT NULL
    GROUP BY 1
) t WHERE cum < -25 ORDER BY d;

-- closed_count 在 metric_snapshot 表
SELECT granularity, scope, bucket_key, net_pnl, closed_count, snapshot_at
FROM public.metric_snapshot WHERE strategy_id='hrs'
ORDER BY snapshot_at DESC LIMIT 8;
```

## 验证
- SSH 线上 `information_schema.columns` 核对三张表真实列结构（trading_platform 库），与仓库 DDL 一致，无漂移
- `trade_records` 列清单线上核对：有 `realized_pnl` 无 `pnl`
- 5 条 ERROR 均伴随 STATEMENT 回溯定位，确认 parse 失败、无执行
- grep 全库：`position_value`/`closed_count` 的生产命中均为 Python 变量名或正确表（`metric_snapshot`）的 SQL，无错误 SQL 来源

## 适用范围与修订
仅适用于 ad-hoc 数据排查场景（docker exec psql 临时会话），不涉及任何部署容器代码。三张快照表列结构以 `database/postgres/init-scripts/05/06/09` 三个 DDL 为准（2026-10-14 线上核对一致）。无替代条目。

## 教训
1. **临时会话写探索性 SQL，先查结构再写查询**：`\d 表名` 或 `SELECT column_name FROM information_schema.columns WHERE table_name='...'`，不要凭记忆/猜测列名；尤其 `metric_snapshot`（盈亏类，含 closed_count）与 `performance_metric_snapshot`（风险调整类）两表易混。
2. **窗口函数两条硬规则**：不能嵌在聚合函数内部；不能出现在 WHERE（先子查询物化，外层过滤）。
3. **postgres 日志排查要点**：`user@xxx` 里的 `xxx` 要对照 log_prefix 确认是 application_name 还是 database——本例 `@psql` 是应用名；`[local]` + 大额离散后端 PID = 容器内手工会话，据此可立即区分「生产代码缺陷」与「人工误操作」。
（AI 归纳，仅供参考）

## 关联
- 本地补充来源: 无 handoff（本次为轻量诊断，未落交接文件）
- 持久证据: 服务器 postgres 容器日志 12807–12869 行（含 5 条完整 STATEMENT）；线上 `information_schema.columns` 核对输出（2026-10-14）；`database/postgres/init-scripts/05-metric-snapshot.sql` / `06-position-snapshot.sql` / `09-performance-metric-snapshot.sql`
- 必要证据摘要: 5 条 ERROR 均 `[local]`+psql 会话、parse 阶段失败；三表列结构与仓库 DDL 一致；生产代码 grep 无错误 SQL。待验证项：无
- 相关: [Binance_quantitative_trading-20261013-hrs-pnl-double-count](./Binance_quantitative_trading-20261013-hrs-pnl-double-count.md)（同一调查场景产生本批临时 SQL；该条治理盈亏双计口径）
