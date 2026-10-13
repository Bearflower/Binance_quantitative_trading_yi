---
id: Binance_quantitative_trading-20261001-newcoin-zombie-reconcile
类型: issue
项目: Binance_quantitative_trading
标题: new_coin 交易所∩DB 对账自动收敛僵尸仓位与 kline 表存在性死代码
日期: 2026-10-01
标签: [bug, 对账, fail-closed, kline]
任务标识: new_coin_zombie_reconcile_kline_table_exists_2026-10-01.md
状态: 已解决
审查: 已审查
---

# new_coin 交易所∩DB 对账自动收敛僵尸仓位与 kline 表存在性死代码

## 现象
1. **僵尸 open 仓位**：new_coin 日志出现 240 条「K线服务请求失败: 400 → 计算ATR失败」（经定性为修复前历史日志，最后一条 `2026-09-30T23:09:21Z`，早于 09-30 23:xx 起的部署）。上一轮遗留「4 笔持仓无保护单」经证伪，实为 DB 有、交易所无的**僵尸 open 记录**——容器内 `/papi/v1/um/positionRisk` 实测 `AMCUSDT / APLDUSDT / CVNAUSDT / PATHUSDT` 交易所**无任何持仓**，new_coin 真实空头仅 `SECZUSDT(-1.28)`、`USDBRLUSDT(-19.54)`。
2. **kline 表存在性死代码**：kline「表已存在即放行」在线上恒不生效——表存在性判断硬编码 `information_schema + table_schema='public'`，而 K 线表实际全在 `btc_eth` schema，导致判断恒返回 False。
3. 读路径把本应报错的情况静默化为 200 空数据。

## 根因
1. **对账（P0-A）**：new_coin 补全链路以内存 `self.positions` 作唯一候选集，且为「重启后一次性补全」语义，导致 DB `open` 但交易所无仓的僵尸记录无法收敛。
2. **表存在性（P0-B）**：硬编码 `table_schema='public'` 未尊重 `search_path`，K 线表在他 schema 时判断恒 False → 上一轮「表已存在即放行」成为死代码。
3. **静默化（P0-C）**：读路径靠 `detail` 文案匹配，无法区分「服务拒绝」与「数据不足」，把真故障当「无数据」返回。

## 解决
提交 `9cc5e40`（代码+文档）与 `c7490bc`（回填报告），三项修复：
| 项 | 改动 |
|---|---|
| P0-A | `strategies/new_coin/strategy.py`：对账事实来源改为「交易所实际空头 ∩ DB `open`」；`_sync_positions_from_exchange` 被方法族替换（`_reconcile_positions_with_exchange` / `_fetch_exchange_short_symbols` / `_parse_short_symbols` / `_reconcile_zombies` / `_try_close_zombie` / `_reconfirm_symbol_absent` / `_compute_authoritative_symbols` / `_load_open_positions_from_db` / `_guard_protection_orders` / `_guard_symbol` / `_notify_protection_issue` / `_fetch_reconcile_sources`）；僵尸关闭 = 连续 N 周期确认（默认 3，带 `max(1, …)` 下限兜底）+ 关闭前交易所回查，首查 / 回查 / 响应结构异常一律**不关闭**（fail-closed）；移除 `_replenish_done` 一次性语义，改为每周期节流守卫 + 飞书告警降频 |
| P0-B | 表存在性改用 `SELECT to_regclass(:table_name) IS NOT NULL`（尊重 `search_path`、参数化）；`routes.py` / `collector.py`×2 / `src/main.py` 四处硬编码收敛到唯一助手 `services/kline_service/shared/utils/table_exists.py` |
| P0-C | 响应契约按异常类型映射：`TableNameFormatError`→400 / `SymbolNotCollectedError`→200 空 / `TableUnavailableError`→503+告警；`_calculate_atr` 区分「服务拒绝」与「数据不足」 |

新增配置（`strategies/new_coin/config.yaml`，均带默认值、禁硬编码）：`trading.reconcile.{enabled=true, zombie_confirm_cycles=3}`、`trading.replenish.{guard_interval_seconds=300, require_take_profit=true, alert_throttle_seconds=3600}`。

## 验证
- 测试（本地，禁止服务器回测）：`pytest -q tests/test_strategies tests/test_r01_r08_fixes tests/test_kline_service` → **6 failed（既有基线）/ 840 passed / 1 xfailed**（部署确认报告口径；handoff 记同批次为 **842 passed**，两处数字不一致，以报告为准并如实并列）。零新增失败。
- 部署：提交 `9cc5e40`，DEPLOY_ID=**00003101**（反解 Run **#49** attempt 1）、GIT_SHA=`9cc5e407d4e59ef46a80b112ad16cfea19f527fc`；五层防幻觉验证**全部通过**（11/11 容器 healthy / 运行镜像均 GHCR / 本轮 2 容器 `/app/VERSION` 一致 / 关键文件 MD5 5/5 一致 / 近 14 小时 new_coin 日志 error=0 且「K线服务请求失败: 400」=0）；增量只 recreate 4 个容器（kline/new_coin/hrs/dashboard-api），未发生全量停机。
- 运行实测：4 笔僵尸记录于 `2026-10-01T05:01:38Z` 自动置 `closed`（日志 `04:01:35Z`「疑似僵尸持仓，第 k/N 次确认 miss=2/required=3」→ 确认达 N 后关闭）；DB `new_coin.short_positions` 中 `AMCUSDT/APLDUSDT/CVNAUSDT/PATHUSDT` 均 `closed`；真实持仓 `SECZUSDT` / `USDBRLUSDT` 保持 `open`，**未被误关**，且保护单齐备（`SECZUSDT` SL 4 / TP 6、`USDBRLUSDT` SL 10 / TP 20，均 OPEN）。

## 适用范围与修订
`strategies/new_coin/{strategy.py,executor.py,config.yaml}`、`services/kline_service/{api/routes.py,core/table_name_guard.py,core/collector.py,src/main.py,shared/utils/table_exists.py}`；commit `9cc5e40` 已上线（push main）。来源另记一处**已移交的独立任务**（「仅缺止盈单会撤掉已有止损单」，`replenish_conditional_orders` 为全量重建语义，用户已于 2026-10-01 23:24 拍板新开改造任务），**不属本条目范围**。无替代条目。

## 教训
「DB 状态必须与交易所真实敞口对账」：以「交易所 ∩ 本策略 DB」为权威集合，对「DB 有、交易所无」的僵尸记录采用连续 N 周期确认 + 关闭前回查的 **fail-closed** 策略（任一环节异常一律不动），既收敛脏数据又不误关真实持仓。另一面是「假设必须尊重运行环境」：表存在性判断依赖 `search_path`，硬编码 schema 会让「本应放行即放行」的逻辑变成永不生效的死代码。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/new_coin_zombie_reconcile_kline_table_exists_2026-10-01.md)（不随 Git 同步）
- 持久证据: 部署确认报告 `docs/reports/new_coin对账守卫与kline表存在性_部署确认报告_20261001.md`；需求/架构 `docs/plans/fix-2026-10-01-p0-remaining-unprotected-positions-{requirements,architecture}.md`；commit 9cc5e40（+c7490bc）
- 必要证据摘要: 6 failed（既有基线）/ 840 passed / 1 xfailed（handoff 记 842 passed，两处不一致）；DEPLOY_ID 00003101 五层验证通过；4 笔僵尸 05:01:38Z 关闭、SECZUSDT/USDBRLUSDT 保持 open 且 SL/TP 齐备
- 相关: 无
