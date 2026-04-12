# PostgreSQL 数据库统一部署 - 快速部署指南

## 📋 概述

本指南帮助您快速部署统一的 PostgreSQL 数据库，并将 4 个项目（bianace_btcethbnb_trade、Grid_Trading、bianace_newtrade_trade、stockfilter）迁移到 PostgreSQL。

## 🚀 快速开始

### 步骤 1：部署 PostgreSQL

```bash
# 进入 PostgreSQL 目录
cd /Users/yl/vscode/database/postgres

# 启动 PostgreSQL 容器
docker-compose up -d

# 查看日志
docker-compose logs -f

# 验证部署
docker exec postgres-db psql -U trading_user -d trading_platform -c "\dn"
```

预期输出应该显示 4 个 schema：
- schema_bianace
- schema_grid
- schema_short_selling
- schema_stockfilter

### 步骤 2：更新项目配置

#### 2.1 bianace_btcethbnb_trade

编辑 `.env` 文件：

```bash
DATABASE_URL=postgresql://bianace_user:Bianace@2024@postgres:5432/trading_platform?schema=schema_bianace
```

#### 2.2 Grid_Trading

编辑 `config/.env` 文件：

```bash
DATABASE_URL=postgresql://grid_user:Grid@2024@postgres:5432/trading_platform?schema=schema_grid
```

#### 2.3 bianace_newtrade_trade

编辑 `.env` 文件：

```bash
DATABASE_URL=postgresql://short_selling_user:ShortSell@2024@postgres:5432/trading_platform?schema=schema_short_selling
```

#### 2.4 stockfilter

编辑 `.env` 文件：

```bash
DATABASE_URL=postgresql://stockfilter_user:Stock@2024@postgres:5432/trading_platform?schema=schema_stockfilter
```

### 步骤 3：安装依赖

#### bianace_btcethbnb_trade

```bash
cd /Users/yl/vscode/bianace_btcethbnb_trade
pip install psycopg2-binary
```

#### Grid_Trading

```bash
cd /Users/yl/vscode/Grid_Trading/adaptive_grid_trading
pip install asyncpg
```

#### 其他项目

```bash
# bianace_newtrade_trade
cd /Users/yl/vscode/bianace_newtrade_trade/short_selling_system
pip install psycopg2-binary

# stockfilter
cd /Users/yl/vscode/stockfilter
pip install psycopg2-binary
```

### 步骤 4：测试连接

#### 测试 bianace_btcethbnb_trade

```bash
cd /Users/yl/vscode/bianace_btcethbnb_trade
python models/database.py
```

#### 测试 Grid_Trading

```bash
cd /Users/yl/vscode/Grid_Trading/adaptive_grid_trading
python -c "import asyncio; from src.data.database import init_db; asyncio.run(init_db())"
```

### 步骤 5：启动应用

#### 启动 bianace_btcethbnb_trade

```bash
cd /Users/yl/vscode/bianace_btcethbnb_trade
docker-compose up -d
```

#### 启动 Grid_Trading

```bash
cd /Users/yl/vscode/Grid_Trading/adaptive_grid_trading
docker-compose up -d
```

#### 启动其他项目

```bash
# bianace_newtrade_trade
cd /Users/yl/vscode/bianace_newtrade_trade/short_selling_system
docker-compose up -d

# stockfilter
cd /Users/yl/vscode/stockfilter
docker-compose up -d
```

## 📊 数据库管理

### 连接数据库

```bash
# 连接到主数据库
docker exec -it postgres-db psql -U trading_user -d trading_platform

# 连接到特定 schema
docker exec -it postgres-db psql -U bianace_user -d trading_platform -c "SET search_path TO schema_bianace"
```

### 查看表结构

```bash
# 查看所有表
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "\dt schema_bianace.*"

# 查看特定表结构
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "\d schema_bianace.trades"
```

### 查看数据

```bash
# 查询交易记录
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "SELECT * FROM schema_bianace.trades LIMIT 10;"
```

### 备份数据库

```bash
cd /Users/yl/vscode/database/postgres
chmod +x scripts/backup-postgres.sh
./scripts/backup-postgres.sh
```

### 恢复数据库

```bash
./scripts/restore-postgres.sh /backups/postgres/full_backup_YYYYMMDD_HHMMSS.dump.gz
```

## 🔍 故障排查

### 问题 1：无法连接数据库

**症状**: 应用启动时报数据库连接错误

**解决方案**:
```bash
# 检查 PostgreSQL 容器状态
docker ps | grep postgres

# 检查网络
docker network ls
docker network inspect postgres_trading-network

# 测试连接
docker exec postgres-db pg_isready -U trading_user
```

### 问题 2：schema 不存在

**症状**: 查询时报表或 schema 不存在

**解决方案**:
```bash
# 查看 schema 列表
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "\dn"

# 如果 schema 不存在，重新运行初始化脚本
docker exec -i postgres-db psql -U trading_user -d trading_platform < init-scripts/01-create-schema.sql
```

### 问题 3：权限错误

**症状**: 用户无权限访问表

**解决方案**:
```bash
# 重新授权
docker exec -i postgres-db psql -U trading_user -d trading_platform << EOF
GRANT ALL PRIVILEGES ON SCHEMA schema_bianace TO bianace_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA schema_bianace TO bianace_user;
GRANT ALL PRIVILEGES ON SCHEMA schema_grid TO grid_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA schema_grid TO grid_user;
EOF
```

## 📈 监控和维护

### 查看数据库大小

```bash
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "SELECT pg_size_pretty(pg_database_size('trading_platform'));"
```

### 查看连接数

```bash
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "SELECT count(*) FROM pg_stat_activity;"
```

### 查看慢查询

```bash
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "SELECT * FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 10;"
```

### 清理旧数据

```bash
# 清理 90 天前的数据
docker exec -it postgres-db psql -U trading_user -d trading_platform -c "DELETE FROM schema_bianace.monitoring_logs WHERE check_time < NOW() - INTERVAL '90 days';"
```

## 🔐 安全建议

1. **修改默认密码**: 生产环境请修改所有用户的默认密码
2. **限制网络访问**: 使用 Docker 网络隔离，不暴露 5432 端口到公网
3. **定期备份**: 确保备份策略有效执行
4. **监控告警**: 配置数据库监控和告警
5. **日志审计**: 定期检查数据库日志

## 📚 相关文档

- [PostgreSQL 详细使用文档](./postgres/README.md)
- [实施总结](./IMPLEMENTATION_SUMMARY.md)
- [项目规划文档](../../.trae/documents/database_unified_deployment_plan.md)

## ✅ 验证清单

部署完成后，请验证以下项目：

- [ ] PostgreSQL 容器正常运行
- [ ] 4 个 schema 创建成功
- [ ] 4 个用户可以正常连接
- [ ] 所有表结构创建完成
- [ ] bianace 应用可以连接数据库
- [ ] Grid_Trading 应用可以连接数据库
- [ ] bianace_newtrade_trade 应用可以连接数据库
- [ ] stockfilter 应用可以连接数据库
- [ ] 备份脚本正常运行

## 🆘 获取帮助

如有问题，请：
1. 查看 PostgreSQL 日志：`docker-compose logs postgres`
2. 查看应用日志：`docker-compose logs <service_name>`
3. 参考 PostgreSQL 官方文档
4. 联系运维团队

---

**最后更新时间**: 2024-03-30
**版本**: 1.0.0
