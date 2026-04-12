# 🗄️ libs/database：数据库适配层（预留）

`libs/database/` 预留给未来的“存储适配层”。目标是把数据库的细节（连接、迁移、事务、查询）封装在一个清晰边界内，避免业务代码到处散落 SQL/ORM。

## 设计边界（先写清楚再实现）

- 这里负责：连接管理、迁移脚本、ORM/SQL 模型、统一的查询/事务封装
- 这里不负责：业务规则、HTTP/API 逻辑、领域对象的复杂编排

## 推荐目录结构（落地时按需取舍）

```
libs/database/
├── README.md
├── __init__.py
├── connection.py             # 连接与池化
├── migrations/               # 迁移脚本（Alembic/Flyway/自研均可）
├── repositories/             # 数据访问层（可选）
└── models/                   # ORM 模型或 SQL schema（可选）
```

## 何时开始实现

当仓库出现“需要长期保存/查询的数据”且 **文件系统不够用** 时，再把这一层落地；否则保持为空，避免过早引入复杂度。
