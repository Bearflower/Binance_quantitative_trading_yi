# PostgreSQL 开发指南

## 📋 概述

本指南介绍如何在项目中使用 PostgreSQL 数据库，包括本地开发、容器部署和生产环境配置。

## 🎯 数据库选型

### 为什么选择 PostgreSQL？

1. **高并发支持**: 多容器可同时访问，无文件锁问题
2. **完整 ACID 事务**: 数据一致性保证
3. **强大的查询能力**: 支持复杂查询、JOIN、窗口函数等
4. **扩展性强**: 支持分区表、读写分离、主从复制
5. **适合金融场景**: 数据安全性高，支持行级锁
6. **Docker 生态成熟**: 易于部署和管理

### SQLite vs PostgreSQL

| 特性 | SQLite | PostgreSQL |
|------|--------|-----------|
| 并发性能 | ⭐⭐ | ⭐⭐⭐⭐⭐ |
| 数据一致性 | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| 扩展性 | ⭐ | ⭐⭐⭐⭐⭐ |
| 资源占用 | 低 (<10MB) | 中 (200-500MB) |
| 适用场景 | 单用户、低并发 | 多容器、高并发 |

## 🚀 快速开始

### 本地开发环境

#### 方法 1：使用 Docker（推荐）

```bash
# 启动 PostgreSQL 容器
docker run -d \
  --name postgres-dev \
  -e POSTGRES_USER=dev_user \
  -e POSTGRES_PASSWORD=dev_password \
  -e POSTGRES_DB=dev_db \
  -p 5432:5432 \
  postgres:15-alpine

# 连接数据库
docker exec -it postgres-dev psql -U dev_user -d dev_db
```

#### 方法 2：本地安装

**macOS:**
```bash
brew install postgresql
brew services start postgresql
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt-get install postgresql postgresql-contrib
sudo systemctl start postgresql
```

### 项目配置

#### 1. 更新 .env 文件

```bash
# 本地开发
DATABASE_URL=postgresql://dev_user:dev_password@localhost:5432/dev_db

# Docker 容器内（使用 Docker 网络）
DATABASE_URL=postgresql://user:password@postgres:5432/trading_platform?schema=schema_name
```

#### 2. 安装数据库驱动

```bash
# 同步驱动（推荐用于简单项目）
pip install psycopg2-binary

# 异步驱动（推荐用于高并发项目）
pip install asyncpg

# SQLAlchemy（ORM 支持）
pip install sqlalchemy psycopg2-binary
```

#### 3. 更新 docker-compose.yml

```yaml
version: '3.8'

services:
  app:
    build: .
    container_name: my-app
    environment:
      - DATABASE_URL=postgresql://user:password@postgres:5432/trading_platform?schema=schema_name
    depends_on:
      - postgres
    networks:
      - trading-network

  postgres:
    image: postgres:15-alpine
    container_name: postgres-db
    environment:
      POSTGRES_USER: user
      POSTGRES_PASSWORD: password
      POSTGRES_DB: trading_platform
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./init-scripts:/docker-entrypoint-initdb.d
    networks:
      - trading-network
    ports:
      - "5432:5432"

volumes:
  postgres_data:

networks:
  trading-network:
    driver: bridge
```

## 📁 项目结构

### 推荐目录结构

```
my-project/
├── src/
│   ├── models/
│   │   └── database.py      # 数据库连接和模型
│   ├── services/
│   │   └── db_service.py    # 数据库服务
│   └── main.py
├── init-scripts/             # 数据库初始化脚本
│   └── 01-create-tables.sql
├── migrations/               # 数据库迁移
├── .env                      # 环境变量
├── docker-compose.yml
└── requirements.txt
```

## 💻 代码示例

### 基础连接（psycopg2）

```python
import psycopg2
from psycopg2.extras import RealDictCursor

class DatabaseManager:
    def __init__(self, db_url: str):
        self.db_url = db_url
        self.conn = None
    
    def connect(self):
        """建立数据库连接"""
        self.conn = psycopg2.connect(self.db_url)
        return self.conn
    
    def execute(self, query: str, params: tuple = None):
        """执行 SQL 查询"""
        with self.conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(query, params or ())
            if query.strip().upper().startswith('SELECT'):
                return cursor.fetchall()
            self.conn.commit()
            return cursor.rowcount
    
    def close(self):
        """关闭连接"""
        if self.conn:
            self.conn.close()

# 使用示例
db = DatabaseManager(os.getenv('DATABASE_URL'))
db.connect()
results = db.execute("SELECT * FROM trades WHERE symbol = %s", ('BTCUSDT',))
db.close()
```

### 异步连接（asyncpg）

```python
import asyncio
import asyncpg

class AsyncDatabaseManager:
    def __init__(self, db_url: str):
        self.db_url = db_url
        self.pool = None
    
    async def initialize(self):
        """初始化连接池"""
        self.pool = await asyncpg.create_pool(self.db_url)
    
    async def execute(self, query: str, *args):
        """执行 SQL"""
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)
    
    async def fetch(self, query: str, *args):
        """查询多行"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return [dict(row) for row in rows]
    
    async def close(self):
        """关闭连接池"""
        if self.pool:
            await self.pool.close()

# 使用示例
async def main():
    db = AsyncDatabaseManager(os.getenv('DATABASE_URL'))
    await db.initialize()
    trades = await db.fetch("SELECT * FROM trades WHERE symbol = $1", 'BTCUSDT')
    await db.close()

asyncio.run(main())
```

### 使用 SQLAlchemy（ORM）

```python
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

Base = declarative_base()

class Trade(Base):
    __tablename__ = 'trades'
    
    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)
    price = Column(Numeric(20, 8))
    quantity = Column(Numeric(20, 8))
    created_at = Column(DateTime, default=datetime.now)

# 创建引擎和会话
engine = create_engine(os.getenv('DATABASE_URL'))
Session = sessionmaker(bind=engine)
session = Session()

# 查询
trades = session.query(Trade).filter(Trade.symbol == 'BTCUSDT').all()

# 插入
new_trade = Trade(symbol='BTCUSDT', side='BUY', price=50000, quantity=0.001)
session.add(new_trade)
session.commit()
```

## 🗄️ 数据库设计

### Schema 设计原则

1. **按项目隔离**: 每个项目使用独立 Schema
2. **权限分离**: 每个 Schema 独立用户和权限
3. **命名规范**: 使用 `schema_projectname` 格式

### 示例 Schema 结构

```sql
-- 创建 Schema
CREATE SCHEMA schema_bianace;

-- 创建用户
CREATE USER bianace_user WITH PASSWORD 'secure_password';

-- 授权
GRANT ALL PRIVILEGES ON SCHEMA schema_bianace TO bianace_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA schema_bianace TO bianace_user;
```

### 数据类型映射

| SQLite | PostgreSQL | 说明 |
|--------|-----------|------|
| INTEGER | BIGINT / INTEGER | 整数类型 |
| REAL | DOUBLE PRECISION | 浮点数 |
| TEXT | TEXT / VARCHAR | 字符串 |
| BLOB | BYTEA | 二进制数据 |
| DATETIME | TIMESTAMP | 日期时间 |
| BOOLEAN | BOOLEAN | 布尔值 |

### 主键自增

```sql
-- SQLite
id INTEGER PRIMARY KEY AUTOINCREMENT

-- PostgreSQL
id BIGSERIAL PRIMARY KEY
-- 或
id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY
```

## 🔧 常用操作

### 数据库备份

```bash
# 备份
pg_dump -U user -d dbname --format=custom > backup.dump

# 恢复
pg_restore -U user -d dbname --clean --if-exists < backup.dump

# 导出为 SQL
pg_dump -U user -d dbname > backup.sql

# 导入 SQL
psql -U user -d dbname < backup.sql
```

### 查看数据库信息

```bash
# 连接数据库
psql -U user -d dbname

# 查看所有数据库
\l

# 查看 Schema
\dn

# 查看表
\dt schema_name.*

# 查看表结构
\d schema_name.table_name

# 查看用户
\du

# 退出
\q
```

### 性能优化

```sql
-- 创建索引
CREATE INDEX idx_trades_symbol ON trades(symbol);
CREATE INDEX idx_trades_created_at ON trades(created_at);

-- 分析表
ANALYZE trades;

-- 清理死元组
VACUUM trades;

-- 查看慢查询
SELECT * FROM pg_stat_statements 
ORDER BY mean_exec_time DESC 
LIMIT 10;

-- 查看连接数
SELECT count(*) FROM pg_stat_activity;
```

## 🚨 常见问题

### 1. 连接失败

**错误**: `connection refused`

**解决方案**:
- 检查 PostgreSQL 是否运行
- 检查端口是否被占用
- 检查防火墙设置

### 2. 权限错误

**错误**: `permission denied for schema`

**解决方案**:
```sql
GRANT ALL PRIVILEGES ON SCHEMA schema_name TO user_name;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA schema_name TO user_name;
```

### 3. 中文乱码

**解决方案**:
```sql
-- 创建数据库时指定编码
CREATE DATABASE mydb 
WITH ENCODING='UTF8' 
LC_COLLATE='zh_CN.UTF-8' 
LC_CTYPE='zh_CN.UTF-8';
```

### 4. 日期时间格式

**SQLite**: `2024-01-01 12:00:00`
**PostgreSQL**: `2024-01-01 12:00:00+00` (带时区)

**解决方案**: 使用 `TIMESTAMP` 或 `TIMESTAMPTZ` 类型

## 📚 最佳实践

### 1. 使用连接池

```python
from psycopg2 import pool

connection_pool = pool.SimpleConnectionPool(
    1, 10,
    dsn=DATABASE_URL
)
```

### 2. 使用参数化查询

```python
# ❌ 错误：SQL 注入风险
cursor.execute(f"SELECT * FROM trades WHERE id = {user_input}")

# ✅ 正确：参数化查询
cursor.execute("SELECT * FROM trades WHERE id = %s", (user_input,))
```

### 3. 使用事务

```python
try:
    cursor.execute("BEGIN")
    cursor.execute(query1, params1)
    cursor.execute(query2, params2)
    cursor.execute("COMMIT")
except Exception as e:
    cursor.execute("ROLLBACK")
    raise
```

### 4. 错误处理

```python
import psycopg2

try:
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(query)
except psycopg2.OperationalError as e:
    logger.error(f"连接失败：{e}")
except psycopg2.ProgrammingError as e:
    logger.error(f"SQL 错误：{e}")
finally:
    if conn:
        conn.close()
```

## 🌐 生产环境部署

### Docker 部署

```yaml
version: '3.8'

services:
  postgres:
    image: postgres:15-alpine
    container_name: postgres-prod
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${DB_USER}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: ${DB_NAME}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./backups:/backups
    ports:
      - "5432:5432"
    networks:
      - trading-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER}"]
      interval: 10s
      timeout: 5s
      retries: 5
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 1G

volumes:
  postgres_data:
    driver: local

networks:
  trading-network:
    driver: bridge
```

### 定时备份

```bash
# crontab -e
0 2 * * * pg_dump -U user -d dbname | gzip > /backups/db_$(date +\%Y\%m\%d).sql.gz
```

### 监控告警

- 连接数监控
- 磁盘使用率监控
- 慢查询监控
- 备份状态监控

## 📖 参考资源

- [PostgreSQL 官方文档](https://www.postgresql.org/docs/)
- [psycopg2 文档](https://www.psycopg.org/docs/)
- [asyncpg 文档](https://magicstack.github.io/asyncpg/current/)
- [SQLAlchemy 文档](https://docs.sqlalchemy.org/)

---

**最后更新**: 2026-03-30  
**版本**: 1.0.0
