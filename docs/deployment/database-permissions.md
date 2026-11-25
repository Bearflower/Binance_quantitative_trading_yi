# PostgreSQL 权限一致性

权限清单：`database/postgres/permissions.json`。工具：`scripts/database_permissions.py`。规则：`.claude/rules/database-permissions.md`。

## 检查与修复

在具备 Docker 访问能力的服务器仓库目录执行：

```bash
python3 scripts/database_permissions.py audit
python3 scripts/database_permissions.py apply
```

默认容器 `trading_system-postgres`，数据库 `trading_platform`，账号 `trading_migration_admin`。工具不接受或输出密码。使用本地 PostgreSQL 时添加 `--local`，通过 libpq 的 PGHOST、PGPORT、PGDATABASE、PGUSER 和已有认证配置连接。可使用 `--manifest` 指定已核验的清单路径。

退出码：0 表示通过；1 表示清单不符，JSON 的 `issues` 描述问题；2 表示工具、清单、连接、SQL 或事务错误，不能当作通过。JSON 包含核验时间、清单 SHA256 和对象数量。

`audit` 只在事务内建立临时检查函数和临时结果表，结束后回滚；不改变持久权限和业务数据。`lock` 探针以运行账号执行 EXPLAIN 锁行查询，并用 `UPDATE ... WHERE false` 验证语句级不可变触发器拒绝修改；不会更新业务行，要求触发器函数本身没有事务外副作用。

`apply` 先完整检查，再补清单规定的缺权，最后复查；任意未知对象、越权、owner 漂移、角色提权或触发器异常都会阻止补权。修复及复查在同一事务内，复查失败则回滚；不自动撤销存量权限或改变 owner、角色属性和默认权限。并发 apply 使用事务 advisory lock 串行；这把锁不替代其他迁移工具的部署锁。

## 清单约定

- `schemas`：本系统管理的命名空间；不检查系统 catalog、临时对象和扩展所属对象。
- `roles`：运行账号，包含其 schema 权限；`required` 角色不存在时报错，未设置 required 的专用账号在未安装相关组件时可不存在。一旦账号存在，其所有受管对象权限都检查。当前运行账号不允许任何角色成员关系，含 NOINHERIT，避免通过 SET ROLE 绕过权限上限。
- `profiles`：`required` 是必须具备的最小集合，`allowed` 是允许保留的集合。简写数组表示两个集合相同。普通历史 owner 表允许保留固有权限，但只补实际需要的 CRUD，不额外补 TRUNCATE、REFERENCES、TRIGGER。
- `objects`：逐项声明表/视图/序列、owner、各运行角色的 profile 和列级权限；未列出的运行角色默认无权限。检查有效权限，包含 PUBLIC、继承和授权转授能力。
- `required: true`：对象必须存在，适用于部署迁移创建的必需对象。未设置时仅在存在时检查，适用于运行时表、历史备份和当前 checkout 尚不包含建表迁移的生产专属表。当前清单盘点于 2026-11-19，这些生产专属表仍须保留各自发布流程的建表迁移；清单不负责替代 DDL。
- 仅动态 K 线对象使用受限模式；现有中文币种表逐项登记，新中文表出现时需登记。精确对象规则覆盖动态模式；多个动态模式冲突会报错。
- 不可变锁行表登记 `lock`，要求对应的 BEFORE UPDATE/DELETE/TRUNCATE 语句级触发器启用，函数身份匹配并通过行为探针。本次 `trading.hrs_equity_segment` 只允许 `UPDATE(account_id)` 用于锁行。

存量兼容例外：`trading_user` 仍是部分旧业务表的 owner，且线上已有 BYPASSRLS。清单显式记录并保留这些能力，工具不会新增或扩大这些角色属性。它们不是新组件默认权限；本轮不做 owner/RLS 重构，新增受保护表应由独立 owner 持有。

## 发布接入

GitHub Actions 按提交上传权限契约、检查工具和账户切换迁移至 `.deploy_tmp/account-mode-<SHA>/`。宿主 `.env` 设置 `BINANCE_CLASSIC_CUTOVER_MS` 后，常规 full 发布在原有数据库与专用账号检查后执行迁移，再执行 apply、audit，均成功后更新业务服务。受限发布在既有受控入口前执行 audit。若当前生产使用签名发布锁，须审查新的继任发布，在自身迁移后、激活前重新审计；不可复用旧签名。未配置切点的原有 PM 发布保持原流程。

## 新增对象示例

```json
{
  "schema": "trading",
  "name": "new_evidence",
  "kind": "table",
  "owner": "trading_migration_admin",
  "required": true,
  "grants": {"trading_user": "append"}
}
```

同时提交建表迁移，明确 GRANT SELECT,INSERT，并为自动生成 ID 所需的序列建立单独条目。按受影响 SQL 编写真实 PostgreSQL 回归测试，测试拒绝越权和允许合法操作。不要给混合用途 schema 配置一揽子默认读写授权；若确需默认权限，要限定创建角色和用途，已有对象单独处理。

## 测试

```bash
python3 -m pytest tests/test_database_permissions tests/test_scripts -q
bash -n scripts/remote_deploy_runtime.sh
```

集成测试在本机使用 initdb/pg_ctl 创建独立 Unix socket PostgreSQL 临时实例，不读取生产凭据，不接触生产库。缺少这些二进制会显示 skip；发布前必须在具备 PostgreSQL 的环境实际跑过，不能把 skip 当作数据库行为验证。

本工具覆盖 schema、运行角色能力、受管关系对象和列级 ACL；不检查函数 EXECUTE、RLS 策略内容或默认 ACL 是否符合其他业务要求，相关迁移仍需单独审查。告警日志窗口去重属于监控模块的职责，本轮没有修改监控调度或删除历史日志。
