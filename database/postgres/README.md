# PostgreSQL 数据库部署说明

## 目录结构

```
database/postgres/
├── docker-compose.yml          # PostgreSQL Docker 配置
├── init-scripts/               # 初始化 SQL 脚本
│   ├── 01-create-schema.sql           # 创建 schema 和用户
│   ├── 02-create-tables-bianace.sql   # bianace 项目表结构
│   ├── 03-create-tables-grid.sql      # Grid_Trading 表结构
│   ├── 04-create-tables-short-selling.sql  # 做空系统表结构
│   └── 05-create-tables-stockfilter.sql    # stockfilter 表结构
├── scripts/                    # 运维脚本
│   ├── backup-postgres.sh      # 备份脚本
│   └── restore-postgres.sh     # 恢复脚本
├── backups/                    # 备份文件目录（自动创建）
└── README.md                   # 本文档
```

## 快速开始

### 1. 启动 PostgreSQL

```bash
cd /Users/yl/vscode/database/postgres
docker-compose up -d
```

### 2. 查看日志

```bash
docker-compose logs -f
```

### 3. 检查数据库状态

```bash
docker exec postgres-db psql -U trading_user -d trading_platform -c "\l"
```

## 数据库配置

### 连接信息

- **主机**: localhost
- **端口**: 5432
- **主数据库**: trading_platform
- **主用户**: trading_user
- **主密码**: Trading@2024Secure

### Schema 和用户

| Schema | 用户 | 密码 | 用途 |
|--------|------|------|------|
| schema_bianace | bianace_user | Bianace@2024 | bianace 交易项目 |
| schema_grid | grid_user | Grid@2024 | Grid_Trading 项目 |
| schema_short_selling | short_selling_user | ShortSell@2024 | 做空系统 |
| schema_stockfilter | stockfilter_user | Stock@2024 | 股票筛选项目 |

### 连接字符串示例

```python
# bianace_btcethbnb_trade
DATABASE_URL = "postgresql://bianace_user:Bianace@2024@localhost:5432/trading_platform?schema=schema_bianace"

# Grid_Trading
DATABASE_URL = "postgresql://grid_user:Grid@2024@localhost:5432/trading_platform?schema=schema_grid"

# bianace_newtrade_trade
DATABASE_URL = "postgresql://short_selling_user:ShortSell@2024@localhost:5432/trading_platform?schema=schema_short_selling"

# stockfilter
DATABASE_URL = "postgresql://stockfilter_user:Stock@2024@localhost:5432/trading_platform?schema=schema_stockfilter"
```

## 运维操作

### 备份数据库

```bash
# 手动备份
cd /Users/yl/vscode/database/postgres
chmod +x scripts/backup-postgres.sh
./scripts/backup-postgres.sh
```

### 恢复数据库

```bash
# 从备份恢复
chmod +x scripts/restore-postgres.sh
./scripts/restore-postgres.sh /backups/postgres/full_backup_20240101_120000.dump.gz
```

### 定时备份

编辑 crontab：

```bash
crontab -e
```

添加每日备份任务（每天凌晨 2 点）：

```cron
0 2 * * * /Users/yl/vscode/database/postgres/scripts/backup-postgres.sh >> /var/log/postgres_backup.log 2>&1
```

## 常用命令

### 连接到数据库

```bash
# 使用 psql 连接
docker exec -it postgres-db psql -U trading_user -d trading_platform

# 连接到特定 schema
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "SET search_path TO schema_bianace"
```

### 查看表结构

```bash
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "\dt schema_bianace.*"
```

### 查看数据

```bash
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "SELECT * FROM schema_bianace.trades LIMIT 10;"
```

### 重启数据库

```bash
docker-compose restart
```

### 停止数据库

```bash
docker-compose down
```

### 查看资源使用

```bash
docker stats postgres-db
```

## 监控和诊断

### 查看连接数

```bash
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "SELECT count(*) FROM pg_stat_activity;"
```

### 查看数据库大小

```bash
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "SELECT pg_size_pretty(pg_database_size('trading_platform'));"
```

### 查看慢查询

```bash
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "SELECT * FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 10;"
```

## 安全建议

1. **修改默认密码**: 生产环境请修改默认密码
2. **限制网络访问**: 仅允许信任的 IP 访问 5432 端口
3. **定期备份**: 确保备份策略有效执行
4. **监控告警**: 配置数据库监控和告警
5. **日志审计**: 定期检查数据库日志

## 故障排查

### 数据库无法启动

```bash
# 查看日志
docker-compose logs postgres

# 检查端口占用
lsof -i :5432

# 检查磁盘空间
df -h
```

### 连接失败

```bash
# 检查容器状态
docker ps | grep postgres

# 测试连接
docker exec postgres-db pg_isready -U trading_user

# 检查网络
docker network inspect postgres_trading-network
```

### 性能问题

```bash
# 查看 CPU 和内存使用
docker stats postgres-db

# 查看慢查询
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "SELECT * FROM pg_stat_statements WHERE mean_exec_time > 1000 ORDER BY mean_exec_time DESC;"
```

## 升级和维护

### 升级 PostgreSQL

```bash
# 停止当前容器
docker-compose down

# 备份数据
./scripts/backup-postgres.sh

# 修改 docker-compose.yml 中的镜像版本
# 例如：postgres:15-alpine -> postgres:16-alpine

# 重新启动
docker-compose up -d
```

### 清理数据

```bash
# 删除所有数据（⚠️ 危险操作）
docker-compose down -v

# 重新初始化
docker-compose up -d
```

## 相关文档

- [PostgreSQL 官方文档](https://www.postgresql.org/docs/)
- [Docker PostgreSQL 镜像文档](https://hub.docker.com/_/postgres)
- [项目规划文档](../../../.trae/documents/database_unified_deployment_plan.md)

## 联系方式

如有问题，请参考项目文档或联系运维团队。
