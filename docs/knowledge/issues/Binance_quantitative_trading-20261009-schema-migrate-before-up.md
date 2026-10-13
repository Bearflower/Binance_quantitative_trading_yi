---
id: Binance_quantitative_trading-20261009-schema-migrate-before-up
类型: issue
项目: Binance_quantitative_trading
标题: DB schema 迁移必须早于 docker compose up -d 否则容器 crash-loop 且出口永久停用
日期: 2026-10-09
标签: [bug, grid, 部署顺序]
任务标识: grid_realtime_alert_2026-10-08.md
状态: 已解决
审查: 已审查
---

# DB schema 迁移必须早于 docker compose up -d 否则容器 crash-loop 且出口永久停用

## 现象
grid 实时风险预警影子设施（M2）上线时存在一条硬顺序约束：若 DB schema 迁移**晚于** `docker compose up -d`，`trading_system-grid-realtime` 会因缺表 crash-loop，且 `trading_system-grid` 的交接出口会**永久停用**。本次 commit `927559d` 部署顺序正确（库文件 21:25 建立、早于容器 21:26:13 启动，`restarts=0`），**未实际触发**该故障；本条记录的是由代码走读推断出的潜在故障模式（C 段审查注明为「非运行时复现」）。

## 根因
业务进程「只校验、不建表」，且失败路径 fail-soft：
- `strategies/grid/realtime/main.py` 的 `run()` 仅 `check_schema`，缺表即 `RuntimeError` → `grid-realtime` crash-loop（`reference_store.py:130-147`）；
- `strategies/grid/signal_bot.py` 的 `_init_export`（`:614`）仅在构造时 `check_schema` 一次、失败只 warning **不重试** → 出口永久停用。

建表由一次性迁移命令独占（`ensure_schema`），业务进程自身不会补建。

## 解决
commit `927559d`（M2 影子设施上线，53 files, +10709/-92）在 CI deploy job 中固化「`docker compose up -d` 前置一次性 schema 迁移」：
```
docker compose run --rm --no-deps grid-realtime python -m strategies.grid.realtime.reference_store --db-path /app/data/grid_realtime.sqlite3
```
`deploy.yml` 顺序：`docker compose pull`（:387）→ schema 迁移（:398-400）→ `up -d --remove-orphans`（:404）。

## 验证
C 段独立审查报告 `docs/reviews/grid_realtime_alert_creview_2026-10-09.md` §4.2 读代码推断 + 生产只读核对：
- 库文件 `./data/grid/grid_realtime.sqlite3` 建于 **21:25**，**早于**容器 21:26:13 启动；
- 业务进程仅 `check_schema` 且 `restarts=0`；
- 两新容器 `/app/VERSION` = `DEPLOY_ID=00003701`、`GIT_SHA=927559d54db6…4bfbb`，与部署日志 `DEPLOY_SUCCESS commit=927559d… deploy_id=00003701` 一致；
- `schema_meta version=1`；13 容器全 `Up (healthy)`，`grid-realtime`/`aggtrade-collector` `restarts=0`。
（注：C 段明确「无法从部署日志直接读步骤序」，迁移早于 `up -d` 系由上述「库文件早于容器启动 + 业务进程仅 `check_schema` 且 `restarts=0`」**间接证实**。）

## 适用范围与修订
`strategies/grid/realtime/reference_store.py`、`strategies/grid/signal_bot.py`、`.github/workflows/deploy.yml`（deploy job）；commit `927559d`（DEPLOY_ID=00003701）已上线。无替代条目。

## 教训
「业务进程只校验、迁移独占建表」的设计下，迁移必须是一次性前置步骤，且不能在业务进程里 fail-soft（只 warning 不重试会静默停用整条链路）；这类**顺序约束应固化进 CI，而非靠人工记忆**。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/grid_realtime_alert_2026-10-08.md)（不随 Git 同步）
- 持久证据: 审查报告 `docs/reviews/grid_realtime_alert_creview_2026-10-09.md` §4.2/§4.5；需求文档 `docs/requirements/grid/Grid_Trading_V2.5.4.md`；commit 927559d
- 必要证据摘要: `deploy.yml` 顺序（pull:387 → 迁移:398-400 → up -d:404）；库文件 21:25 早于容器 21:26:13；`restarts=0`、`schema_meta version=1`、`DEPLOY_ID=00003701` 与部署日志一致；13 容器 healthy
- 相关: 无
