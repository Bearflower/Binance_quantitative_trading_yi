---
id: Binance_quantitative_trading-20261018-db-api-hallucination-asyncpg-vs-wrapper
类型: issue
项目: Binance_quantitative_trading
标题: 审计只读查询误用 asyncpg 原生 API 名（fetch/fetchrow/fetchval/close），项目 DatabaseManager 仅暴露 fetch_one/fetch_all/disconnect
日期: 2026-10-18
标签: [数据库, API幻觉, ai_tuner, 只读审计, 静默失效]
任务标识: ai_tuner_mvp_stage1_safety_readiness_2026-10-17
状态: 已解决
审查: 待审查
---

# 审计只读查询误用 asyncpg 原生 API 名，项目 DatabaseManager 仅暴露 fetch_one/fetch_all/disconnect

## 现象

阶段一「90 天准备度审计器」（`ai_tuner/readiness/auditor.py`）与 CLI（`scripts/ai_tuner_mvp.py`）在只读查库时，代码一度按 asyncpg 习惯调用 `db.fetch(...)` / `db.fetchrow(...)` / `db.fetchval(...)` / `db.close()`，但项目封装层并无这些方法——属「方法不存在」型 API 幻觉（假幻觉），运行时 `AttributeError`，查询拿不到结果。

## 根因

项目的 `DatabaseManager`（`shared/database.py`）是 asyncpg 连接池的**封装层**，只暴露以下公开协程方法：

- `connect()` / `disconnect()`
- `execute(query, *args)`（写）
- `fetch_one(query, *args)` → 返回 `Optional[dict]`
- `fetch_all(query, *args)` → 返回 `List[dict]`

`fetch` / `fetchrow` / `fetchval` / `close` 是 **asyncpg `Connection` 的方法名**，并未在封装层透出。开发者按底层库直觉书写，即产生幻觉调用。

## 解决

统一使用封装层方法：查询单条用 `await db.fetch_one(...)`、多条用 `await db.fetch_all(...)`、收尾用 `await db.disconnect()`；CLI 中 `connect()` 失败显式返回退出码 1。新增**回归守卫** `tests/test_ai_tuner_mvp/test_db_hallucination_guard.py`，断言审计/CLI 代码不再引用不存在的 DB 方法名（防回归）。

## 验证

- `tests/test_ai_tuner_mvp/test_db_hallucination_guard.py` 全绿；阶段一累计 **219 用例通过**。
- 审计器实际使用 `fetch_one`（trade_records / trade_signals / regime / K 线覆盖、information_schema 探测）与 `fetch_all`（信号 schema 探测）。

## 适用范围与修订

- 模块：`ai_tuner/readiness/auditor.py`、`scripts/ai_tuner_mvp.py`；依赖 `shared/database.py` 的 `DatabaseManager`。
- 当前适用性：已修复。
- 替代条目：无。
- 日期与原因：2026-10-18 沉淀（阶段一收口）。

## 教训

- 调用项目封装层前，**先 `Grep` 其 `def` 公开方法**确认签名与返回类型，不要套用底层库（asyncpg/psycopg）的 API 名。
- 只读审计查询应先用 mock/fake（`fetch_one`/`fetch_all`）验证方法存在与返回结构，再连真库；只读探测的「方法不存在」应在回归用例里被固定下来。
- 审计器用 **f-string 注入 schema/表名**拼 SQL（K 线 schema/表名/时间戳列），其取值**只能来自 `information_schema` 的可信查询结果**（`_KNOWN_SCHEMAS` 白名单 + `information_schema`），严禁来自任何外部/用户输入，避免注入面。
- （AI 归纳仅供参考）

## 关联

- 本地补充来源: [handoff](../../handoffs/)（不随 Git 同步）
- 持久证据: `shared/database.py`（`fetch_one` / `fetch_all` / `disconnect`）、`ai_tuner/readiness/auditor.py`、`scripts/ai_tuner_mvp.py`、`tests/test_ai_tuner_mvp/test_db_hallucination_guard.py`
- 必要证据摘要: 审计器与 CLI 全量改用封装层方法，219 用例通过；无剩余未验证项。
- 相关: [income-history-silent-empty-non-unified](../issues/Binance_quantitative_trading-20261015-income-history-silent-empty-non-unified.md)（同为「调用层与实现层行为不一致致静默」主题）
