---
id: Binance_quantitative_trading-20261027-hrs-schema-bootstrap-single-try-skip
类型: issue
项目: Binance_quantitative_trading
标题: HRS 建表链路单 try 包裹致首条 DDL 失败跳过后续全部幂等 DDL（配合 CREATE SCHEMA IF NOT EXISTS 仍校验库级权限）；叠加 universe 快照 SQL 内联 -- 注释被 _validate_sql 拦截致表长期 0 行
日期: 2026-10-27
标签: [bug, 静默失效, hrs, 数据库权限, DDL迁移, ai_tuner, SQL校验, 部署]
任务标识: schema-bootstrap-and-sql-comment_2026-10-27
状态: 待复查
审查: 待审查
---

# HRS 建表链路单 try 包裹致一条 DDL 失败即跳过全部；universe 快照 SQL 注释被校验拦截致 0 行

> 本条目合并本轮两处同主题缺陷（「单点失败/校验误判 → 整条链路静默停摆」），A 为主危险项，B 为并列独立缺陷。

## A. HRS 建表：单 try 包裹整段迁移，首条 DDL 失败即跳过后续全部

### 现象

- HRS 容器启动进入 `_ensure_db_schema()` 时，日志只出现**一条** `幂等DDL执行失败`（或直接一条建 schema 失败），随后建表/补列**全部未执行**，但**进程照常启动**、不退出。
- 表现后果：`hrs.hrs_meta` / `hrs.hrs_positions` / `hrs.hrs_signals` 缺失，或存量库该补的列（如 `algo_ids`、`entry_mode`、`signal_origin`）永远补不上，运行期查询才暴露 `relation does not exist` / `column does not exist`。
- 触发条件（生产实测口径）：库 `trading_platform` 的 owner 是 `trading_migration_admin`；应用账号 `trading_user` 的库级 ACL 仅 `Tc`（TEMP + CONNECT），`has_database_privilege(..., 'CREATE') = false`。

### 根因

1. **迁移写法的结构性缺陷（主因）**：原 `_ensure_db_schema()` 用**单个外层 `try`** 包裹整段迁移（含 schema 创建与约 20 条幂等 `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`），第一条抛异常即跳到 `except`，**后续语句一条都不执行**——即「一条失败 = 全链路停摆」，且日志只有一处报错，极易被当成单点问题忽略。
2. **`CREATE SCHEMA IF NOT EXISTS` 并不规避权限检查**：PostgreSQL 15 实测，即使 schema 已存在，语句**仍会先做库级 CREATE 权限检查**并报 `permission denied for database`。因此「加 `IF NOT EXISTS` 就安全」是错误假设；`trading_user` 无库级 CREATE 时，该语句必然失败，恰好成为第 1 点里「第一条」。
3. **缺少可执行的失败归宿**：权限类失败只留在易被淹没的日志里，没有「由谁、用哪个脚本补」的明确动作，schema 长期缺失无人处理。

### 解决

1. [`strategies/hrs/strategy.py::_ensure_schema_exists`](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L2954-L2998)：**先探后建**——用 `SELECT 1 FROM information_schema.schemata WHERE schema_name = $1`（纯 SELECT，不触发 CREATE 权限）判断；已存在直接 `return`，**不再发 `CREATE SCHEMA`**；不存在才尝试创建，捕获 `sqlstate == "42501"`（insufficient_privilege）时给出 hint「请由 DBA 执行 `database/postgres/init-scripts/14-hrs-schema.sql`」，其他异常按原文记录，均只计数不抛出。
2. [`strategies/hrs/strategy.py::_execute_ddl_safe`](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L2924-L2952)：**逐条失败隔离**——每条 DDL 独立 `try`，失败只 `logger.error`（带 80 字符 SQL 指纹）并 `_schema_ddl_failures += 1`，返回 `False`，**绝不中断后续 DDL**；`_schema_ddl_failures` 用 `getattr(self, ..., 0)` 防御性初始化（该函数可能在 `__init__` 之外被单测单独调用）。
3. [`strategies/hrs/strategy.py::_ensure_db_schema`](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3000-L3193)：调用前重置 `_schema_ddl_failures = 0`，全部 DDL 改走 `_execute_ddl_safe` / `_ensure_schema_exists`；末尾按计数分流日志——`0` → `info "数据库Schema初始化完成"`，`>0` → `error "数据库Schema初始化存在未完成项"`（附 `failed_count` 与 hint）。外层 `try/except` 保留为兜底，但不再承担「跳过后续 DDL」的副作用。
4. 新增 [`database/postgres/init-scripts/14-hrs-schema.sql`](file:///Users/yl/vscode/Binance_quantitative_trading/database/postgres/init-scripts/14-hrs-schema.sql)：`CREATE SCHEMA IF NOT EXISTS hrs` + `GRANT ALL PRIVILEGES ON SCHEMA hrs TO trading_user` + `ALTER DEFAULT PRIVILEGES`（表 `SELECT/INSERT/UPDATE/DELETE`、序列 `USAGE/SELECT`），与 `12-research-schema.sql` 同风格（幂等、自包含、无 `BEGIN/COMMIT`）。该脚本由 **DBA 账号/CI 部署**执行，**仅对全新库首启或首次部署生效**；它**不替代**运行时 `CREATE TABLE`，表结构仍由 `_ensure_db_schema` 幂等建/补。

### 验证

- [`tests/test_strategies/test_hrs_schema_bootstrap.py`](file:///Users/yl/vscode/Binance_quantitative_trading/tests/test_strategies/test_hrs_schema_bootstrap.py)：以 Fake DB 断言四条防线——① schema 已存在时**不发** `CREATE SCHEMA`；② `fetch_one` 抛错时按「schema 缺失」继续尝试创建而非中断；③ 第 N 条 DDL 失败后**后续 DDL 仍全部执行**；④ 失败计数驱动 `info` / `error` 分流日志；另覆盖空 SQL 等异常输入返回 `False` 不反噬调用方。
- 待补（上线后）：生产容器日志应见 `数据库Schema初始化完成`（0 失败）；若 `trading_user` 仍无库级 CREATE，则应在 DBA 执行 14 脚本后转为 0 失败；存量库核对 `hrs.*` 三表与新增列存在。

## B. ai_tuner universe 快照：SQL 内联 `--` 注释被 `_validate_sql` 拦截，快照表长期 0 行

### 现象

- `public.ai_tuner_universe_snapshots`（HRS 动态 universe 的历史快照）**长期 0 行**，而采集链路表面上「跑过、没报明显错误」——典型的**写入静默失效**。

### 根因

- [`ai_tuner/snapshots/repository.py::store_universe`](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/snapshots/repository.py#L126-L181) 的 `INSERT ... SELECT ... FROM UNNEST(...)` 语句里内联了 **4 行 `--` 中文注释**（用于解释 `::varchar(64)` 等显式类型转换的原因）。
- [`shared/database.py::_validate_sql`](file:///Users/yl/vscode/Binance_quantitative_trading/shared/database.py#L93-L136) 对**所有业务 SQL** 无条件拦截 SQL 注释：`if '--' in query or '/*' in query or '*/' in query: raise SQLInjectionError("检测到SQL注释注入风险")`。于是 `execute_transaction(queries)` 在提交前即抛错，整笔事务不落库 → 快照表恒为空。
- 叠加放大：`store_universe` 的调用方未把该异常升级为可见失败（表现为「快照缺失」而非「任务失败」），故长期无人发现。

### 解决

- 把这 4 行解释性文本从 SQL 内部**移到 Python `#` 注释**（`store_universe` 内 `queries.append(...)` 之前），SQL 文本变为**零注释**；参数显式类型转换、`UNNEST`、`NOT EXISTS` / `ON CONFLICT` 语义**一字未改**。

### 验证

- [`tests/test_ai_tuner_mvp/test_snapshot_repository.py`](file:///Users/yl/vscode/Binance_quantitative_trading/tests/test_ai_tuner_mvp/test_snapshot_repository.py)：新增 `test_store_universe_sql_is_comment_free_and_passes_validate_sql`——① 捕获 `store_universe` 提交的每条 SQL，断言不含 `--` / `/*` / `*/`；② 构造真实 `DatabaseManager`（不建连接，只跑安全规则）对每条 SQL 执行 `_validate_sql`，全部通过（负向对照：说明旧的含注释文本正是被该函数拒绝）。
- 同文件既有用例继续保证：空 universe 可表示（仅写 `scan_runs`、`symbol_count=0`）、多 symbol 时为**单事务两条 SQL**、`ON CONFLICT` 幂等。

## 适用范围与修订

- A 适用于 `strategies/hrs/strategy.py` 的建表/补列链路；B 适用于 `ai_tuner/snapshots/repository.py` 的全部写入 SQL。两者均为「静默失效」类，均不改变业务口径与配置。
- 与既有条目的关系：A **不替代** [schema-migrate-before-up](Binance_quantitative_trading-20261009-schema-migrate-before-up.md)（那条讲「迁移晚于 `docker up -d`」，本条讲「迁移内部单点失败吞掉后续」）；B 与 [deploy-yml-init-scripts-unsynced](Binance_quantitative_trading-20261001-deploy-yml-init-scripts-unsynced.md) 相邻（`init-scripts/` 是唯一随部署自动生效的 DDL 路径），但 B 的失败点在应用侧 SQL 校验，不在部署同步。
- 本条目**无替代条目**；决策侧无新增 decision（A 的「权限失败交 DBA 预建脚本」属既有 init-scripts 部署口径的沿用，B 属实现缺陷修复）。
- 遗留风险（本轮不修，登记为知悉项）：`strategies/hrs/Dockerfile` 为 `COPY . /app/`，故 hrs 镜像内**包含 `ai_tuner/`**；但 CI 增量构建按「各 job 只看自己目录 diff」判定，**仅改 `ai_tuner/` 时 hrs 镜像不会重建** → hrs 容器内跑的 `ai_tuner` 代码可能滞后（与 `btc_eth` ↔ `btc_eth_aggressive` 的同类隐患同源，见 INDEX 待复查清单）。
- 部署前状态（2026-10-27）：代码与测试在工作区完成，**未提交 / 未 push / 未部署**；`14-hrs-schema.sql` 需随 CI 部署同步至服务器后才对既有库生效（新库走 entrypoint）。

## 教训

（AI 归纳仅供参考）

- **迁移脚本禁止「整段单 try」**：任何 N 条幂等 DDL 的初始化方法，必须**逐条失败隔离 + 汇总计数**，末尾一次性用计数分流 `info` / `error`。否则一条失败会把「其余 20 条本该成功的修复」一起吞掉，症状表现为「什么都没发生」。
- **`IF NOT EXISTS` ≠ 不检查权限**：`CREATE SCHEMA IF NOT EXISTS` 在 schema 已存在时**仍先校验库级 CREATE 权限**（PG 15 实测 `permission denied for database`）。规避方式是**先纯 SELECT 探测 `information_schema.schemata`**，命中即返回。
- **权限类失败要给可执行动作**：只报 `42501` 等于把问题丢回给运维盲猜；必须同时给出「由谁执行哪个脚本」的 hint（本条即 `init-scripts/14-hrs-schema.sql`）。
- **项目内所有业务 SQL 都要过 `_validate_sql`**：它拦截 `--` / `/* */` 注释、多语句、`CREATE/ALTER/GRANT/DROP` 等关键字。注释一律写在 **Python 层**；需要 DDL 时走 `execute_ddl`（受信路径，绕过校验），不要指望在业务 SQL 里「加个注释说明一下」。
- **排查「表长期 0 行」的优先顺序**：先看写入是否抛错被吞（校验/权限/类型），再看 `ON CONFLICT` 是否命中，最后才怀疑上游采集。本项目已有同类教训：[databases-execute-returns-none-rowcount](Binance_quantitative_trading-20261020-databases-execute-returns-none-rowcount.md)。

## 关联

- 本地补充来源: 本轮 handoff 未入库（`docs/handoffs/` 已 gitignore）
- 持久证据: `strategies/hrs/strategy.py` `_execute_ddl_safe`（L2924-2952）/ `_ensure_schema_exists`（L2954-2998）/ `_ensure_db_schema`（L3000-3193）；`ai_tuner/snapshots/repository.py` `store_universe`（L126-181）；`shared/database.py` `_validate_sql`（L93-136，注释拦截在 L131）；`database/postgres/init-scripts/14-hrs-schema.sql`（新增）；测试 `tests/test_strategies/test_hrs_schema_bootstrap.py`、`tests/test_ai_tuner_mvp/test_snapshot_repository.py`
- 必要证据摘要: 生产库 `trading_platform` owner=`trading_migration_admin`；`trading_user` 库级 ACL 仅 `Tc`（TEMP+CONNECT）、`has_database_privilege(...,'CREATE')=false`；PG 15 实测 `CREATE SCHEMA IF NOT EXISTS` 在 schema 已存在时仍报 `permission denied for database`；`ai_tuner_universe_snapshots` 长期 0 行（`--` 注释触发 `SQLInjectionError("检测到SQL注释注入风险")`）。**未提交 / 未 push / 未部署**，上线验证待补
- 相关: [Binance_quantitative_trading-20261009-schema-migrate-before-up](Binance_quantitative_trading-20261009-schema-migrate-before-up.md)（部署顺序类）、[Binance_quantitative_trading-20261001-deploy-yml-init-scripts-unsynced](Binance_quantitative_trading-20261001-deploy-yml-init-scripts-unsynced.md)（init-scripts 生效路径）、[Binance_quantitative_trading-20261020-databases-execute-returns-none-rowcount](Binance_quantitative_trading-20261020-databases-execute-returns-none-rowcount.md)（写入静默失效排查）、[Binance_quantitative_trading-20261020-shadow-image-missing-aggr-copy-crashloop](Binance_quantitative_trading-20261020-shadow-image-missing-aggr-copy-crashloop.md)（镜像依赖与 CI diff 判定不一致，同类知悉项）

待补 INDEX 索引项：HRS 建表链路单 try 包裹致首条 DDL 失败跳过后续全部幂等 DDL（配合 CREATE SCHEMA IF NOT EXISTS 仍校验库级权限）；叠加 universe 快照 SQL 内联 -- 注释被 _validate_sql 拦截致表长期 0 行
