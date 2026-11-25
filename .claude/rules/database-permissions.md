---
paths:
  - "database/**"
  - "scripts/**"
  - "shared/database.py"
  - "strategies/**"
  - "services/**"
  - ".github/workflows/**"
---

# PostgreSQL 权限一致性（强制）

权威清单：`database/postgres/permissions.json`。统一工具：`scripts/database_permissions.py`。使用说明：`docs/deployment/database-permissions.md`。

1. 新建/修改表、序列、owner、运行角色、SQL 锁语句时，必须同时检查并更新权限清单、迁移和集成测试。生产发布目录里的临时授权也必须回补仓库，禁止只在线上修权限。
2. 迁移账号负责 DDL，业务账号负责运行。新增受保护表由迁移管理员或 NOLOGIN owner 持有，禁止为解决权限错误提升业务账号为超级用户或让其拥有受保护表。保留存量 owner 是兼容性例外，不是新表模板。
3. 权限按实际用途明确登记：普通读写、只读、追加、状态更新、序列。`required` 是必须具备的权限，`allowed` 是允许保留的上限；不能把当前 ACL 未经分类直接当作授权需求。
4. `SELECT ... FOR UPDATE/SHARE/NO KEY UPDATE/KEY SHARE` 也需要更新权限。不可变表的锁行例外使用显式列级 UPDATE，并登记 `lock` 触发器约束；不能改成整表 UPDATE。权限检查必须覆盖列级、PUBLIC、角色继承和 GRANT OPTION。当前受管运行账号禁止角色成员关系，含 NOINHERIT；不能通过 SET ROLE 绕过清单上限。
5. 禁止对 `public` / `trading` / `hrs` 混合用途 schema 执行一揽子 GRANT ALL 或读写补权。未知对象先分类登记，不自动授权。只读、追加和不可变边界不可被通用修复覆盖。
6. 已有对象使用幂等 GRANT；`ALTER DEFAULT PRIVILEGES` 仅影响指定创建角色未来创建的对象，不能代替已有对象修复。使用时显式指定 `FOR ROLE`，不得假设覆盖其他建表角色。
7. 新增对象默认应设 `required: true`；只有运行时按需创建、历史备份、当前本地版本尚不具备的生产发布专属表可标为可选并说明原因。可选对象一旦存在仍受完整检查。动态命名只允许经过审查的受限规则，精确规则优先。
8. 发布顺序：数据库可用 → 迁移 → `apply`（只补清单允许的缺权）→ 权限检查通过 → 更新业务服务。受限发布只执行 `audit`，保持既有发布范围，并在自身迁移之后、激活之前再次检查。历史签名清单不能自动重签；修改执行器须通过新的受控发布审查。任何越权、未知对象、owner 漂移或探针失败都必须停止发布，禁止吞掉退出码。
9. 排障先保存 SQL、账号、schema、对象 owner 和有效权限，再决定补权。`audit` 默认不改变持久权限；`apply` 不能自动撤销权限、修改 owner、改角色属性或修改默认权限。禁止根据日志文本自动提权。
10. 修复后以业务角色验证原 SQL；不可变表同时验证禁止更新的约束。告警应区分历史窗口汇总与修复后新增错误，不能仅凭旧日志再次授权。

验证入口：`python3 -m pytest tests/test_database_permissions tests/test_scripts -q`。
