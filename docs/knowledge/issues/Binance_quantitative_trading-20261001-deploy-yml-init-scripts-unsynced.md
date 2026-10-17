---
id: Binance_quantitative_trading-20261001-deploy-yml-init-scripts-unsynced
类型: issue
项目: Binance_quantitative_trading
标题: deploy.yml 未同步 init-scripts 致新表迁移失效
日期: 2026-10-01
标签: [配置, 部署, postgres]
任务标识: deploy_yml_init_scripts_sync_2026-10-01.md
状态: 已解决
审查: 已审查
---

# deploy.yml 未同步 init-scripts 致新表迁移失效

## 现象
新增的 SQL 迁移脚本（表/索引/schema 变更）随 GitHub Actions 部署不生效，业务侧报 **`relation does not exist`**。事故根因定性于 `perf_metric_relation_bug`：`deploy.yml` 未同步 `database/postgres/init-scripts/`，导致新表脚本从未上服务器。

## 根因
两个叠加缺陷：
1. **未同步目录**：GitHub Actions `deploy.yml` 未 `scp` `database/postgres/init-scripts/` 到服务器 → 新增脚本永远到不了服务器，即使容器挂载正确也无从执行。
2. **entrypoint 不回跑**：postgres 官方 entrypoint **仅对空库（首次初始化）**执行 `/docker-entrypoint-initdb.d/` 下的脚本；生产库已初始化 → 后来新增的脚本永不重跑。实例：`btc_eth_aggressive` schema 后来被加进 `01-create-schema.sql`，但线上 postgres 初始化时用的是旧版 01，entrypoint 不会重跑，故线上缺该 schema。

## 解决
提交 `644cde0`，部署增强：
| 位置 | 改动 |
|---|---|
| `.github/workflows/deploy.yml` deploy job | 新增「📦 同步数据库 init-scripts」step，紧接上传 docker-compose.yml 之后，`retry_connect scp -r database/postgres/init-scripts` 到服务器 `/root/trading_system/database/postgres/`；目录不存在/为空则 `exit 0` 安全跳过（空值守卫） |
| 远程 `deploy.sh` | 新增 step 4.5：`docker compose up -d` 之后 → `pg_isready` 每 2s 轮询最多 60 次（120s，未就绪即 `exit 1`）→ 取容器内 `/docker-entrypoint-initdb.d/*.sql` 排序后按 `01→02→…→NN` 顺序 `psql --set ON_ERROR_STOP=on -f` 执行全部脚本；每步打印 `▶ xxx.sql ... ✅ 完成` |
| `database/postgres/init-scripts/02-init-tables.sql` | 补 `CREATE INDEX IF NOT EXISTS`：02 原有 **25 个** `CREATE INDEX` 缺 `IF NOT EXISTS`（CREATE TABLE/SCHEMA 本就有） |

关键设计：`ON_ERROR_STOP=on` + 远程脚本 `set -e` 构成**双重 CI 守门**——漏加 `IF NOT EXISTS` 的脚本在第二次 deploy 时必然炸掉；psql stderr 不吞（报错时 Actions 日志显示 ERROR 详情），正常时仅吞 stdout。另含生产**一次性手动修复（不入库）**：补建 `btc_eth_aggressive` schema；手动 scp 08/09 SQL。

## 验证
- commit `644cde0` 部署后（部署 ID **00002B01**）：init-scripts 目录 **9 个文件全同步**到服务器 + postgres 容器挂载。
- `ON_ERROR_STOP=on` 执行 **零 ERROR**（02 补 `IF NOT EXISTS` 前有 6 个 ERROR，补后消失）；已验证 `ON_ERROR_STOP=on` 只炸 ERROR/FATAL，不炸 NOTICE（`relation already exists, skipping`）。
- 仓库内**全部 37 个 `CREATE INDEX` 已 100% 带 `IF NOT EXISTS`**（03-09 本来就有，仅 02 缺）。
- 业务面：data-backend 绩效落库 `rows=24` 正常，API 全 200；**11 个容器全 healthy**。
- 全局 skill `/Users/yl/.trae-cn/skills/服务器自动化部署/SKILL.md` 增补「PostgreSQL init-scripts 自动同步（GitHub Actions CI/CD）」小节。

## 适用范围与修订
`.github/workflows/deploy.yml`（commit `644cde0`）、`database/postgres/init-scripts/02-init-tables.sql`。对开发者的新增约束：① 新增 SQL 脚本数字前缀递增（如 `10-xxx.sql`）保证执行顺序；② 所有 `CREATE` 语句（TABLE/SCHEMA/INDEX/VIEW/FUNCTION）带 `IF NOT EXISTS`；③ 放入 `database/postgres/init-scripts/` 并提交 git。注意：`deploy.yml` 改动会触发 **10 个镜像全量重建**（`仅 .github/` 变更 → 防死循环机制判定全量重建，约 5 分钟）。无替代条目。

**同类复发（2026-10-15，C1/D1 只读采集交付）——约束的边界场景**：本轮新增 `database/postgres/migrations/2026-10-15-decision-evidence.sql`、`2026-10-15-execution-facts.sql` **放在 `migrations/`（手动迁移目录）而非 `init-scripts/`**，故 **CI 部署不会自动 apply**。与 2026-10-01 事故的差别在于：本轮是**有意的**——消费方（决策证据消费、会计/资金流核对）尚未在同一部署流程迁入，贸然执行会让生产库多出**无业务方消费的空表**（「孤儿 schema 对象」）。教训边界补充：**`migrations/` 目录不是自动生效路径**；新表若要随部署生效，**唯一办法是迁入 `init-scripts/`（数字前缀 + `IF NOT EXISTS`）**，否则须由人工迁移并留痕。

## 教训
「已初始化库不回跑 entrypoint」是 PostgreSQL 的固有行为，任何新增迁移脚本都必须由**部署流程显式按序执行**（本题以 deploy.yml 同步目录 + deploy.sh `psql` 按序执行为解），不能指望 entrypoint 自动重跑。同时**幂等性必须闭环**：所有 `CREATE` 语句带 `IF NOT EXISTS`，否则第二次部署即破幂等，一旦叠加 `ON_ERROR_STOP=on` 会立即炸——可用该开关反作 CI 守门人。同步步骤要带空目录守卫，手动修复（schema 补建、scp）须固化为自动流程，避免只留在会话里。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/deploy_yml_init_scripts_sync_2026-10-01.md)（不随 Git 同步）
- 持久证据: `.github/workflows/deploy.yml`（commit `644cde0`「📦 同步数据库 init-scripts」step；远程 `deploy.sh` step 4.5）；`database/postgres/init-scripts/02-init-tables.sql`（commit `644cde0`）
- 必要证据摘要: init-scripts 9 文件全同步；`ON_ERROR_STOP=on` 零 ERROR（02 原 6 ERROR 消失）；11 容器 healthy、API 200、绩效落库 rows=24；全仓库 37 个 CREATE INDEX 100% 带 IF NOT EXISTS；部署 ID 00002B01
- 相关: [schema-migrate-before-up](./Binance_quantitative_trading-20261009-schema-migrate-before-up.md)（同属部署阶段 DB schema 迁移时序/幂等问题）
