# 数据库统一部署方案

## 项目背景

目前 `bianace_btcethbnb_trade`、`bianace_newtrade_trade`、`Grid_Trading`、`stockfilter` 这几个项目都各自在容器内使用独立的 SQLite 数据库文件，存在以下问题：

1. **数据分散**：每个容器维护自己的数据库文件，数据孤岛严重
2. **难以备份**：需要分别备份多个数据库文件
3. **资源浪费**：每个容器都要加载 SQLite 库，占用内存
4. **不支持并发**：SQLite 在高并发场景下性能受限
5. **难以扩展**：未来如果需要多实例部署，SQLite 无法共享数据

## 一、项目数据库现状分析

### 1.1 bianace_btcethbnb_trade

**数据库类型**: SQLite
**数据库文件**: `./database/trading.db`
**主要表结构**:
- `trades` - 交易记录表（订单 ID、交易对、方向、数量、价格、状态等）
- `positions` - 持仓记录表
- `account_transfers` - 资金划转记录表
- `simple_earn_redemptions` - 理财赎回记录表
- `monitoring_logs` - 监控日志表
- `account_balance_snapshot` - 账户余额快照表
- `closed_positions` - 平仓记录表
- `tp_sl_triggers` - 止盈止损触发记录表
- `trade_statistics` - 交易统计表

**特点**:
- 数据量大（交易记录、监控日志持续增长）
- 并发写入频繁（实时监控和交易）
- 需要复杂查询（统计报表、历史数据分析）

### 1.2 Grid_Trading (adaptive_grid_trading)

**数据库类型**: SQLite (使用 aiosqlite 异步驱动)
**数据库文件**: `data/database.db`
**主要表结构**:
- `trades` - 网格交易记录表
- `grid_history` - 网格历史表
- `system_status` - 系统状态表
- `risk_events` - 风险事件表
- `grid_parameter_adjustments` - 网格参数调整历史表
- `trailing_profit_state` - 移动止盈状态表

**特点**:
- 高频写入（网格交易频繁）
- 异步操作（使用 aiosqlite）
- 需要实时状态监控

### 1.3 bianace_newtrade_trade (short_selling_system)

**数据库类型**: SQLite
**数据库文件**: `data/database.db`
**配置项**: `database_url: str = "sqlite:///data/database.db"`
**主要功能**: 做空系统交易记录和信号管理

**特点**:
- 信号评分数据
- 交易记录管理
- 风控数据

### 1.4 stockfilter

**数据库类型**: SQLite
**数据库文件**: `data/stock_scanner.db`
**主要表结构**:
- `stocks` - 股票列表表
- `klines` - K 线数据表
- `scan_results` - 筛选结果表
- `positions` - 持仓记录表
- `push_history` - 推送历史记录表

**特点**:
- 数据量大（K 线历史数据）
- 批量插入操作频繁
- 定时任务模式（每天 15:30 执行扫描）

## 二、数据库选型建议

### 2.1 选型考虑因素

1. **并发性能**: 支持多容器同时访问
2. **数据一致性**: ACID 事务支持
3. **扩展性**: 支持未来项目扩展
4. **资源占用**: 服务器资源限制
5. **运维复杂度**: 易于部署和维护
6. **性能要求**: 读写性能满足交易需求

### 2.2 候选数据库对比

| 特性 | PostgreSQL | MySQL | SQLite |
|------|-----------|-------|--------|
| 并发性能 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐ |
| 数据一致性 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
| 扩展性 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐ |
| 资源占用 | 中 (200-500MB) | 中 (200-500MB) | 低 (<10MB) |
| 运维复杂度 | 中 | 低 | 极低 |
| 性能 | 高 | 高 | 低 |
| 适用场景 | 复杂查询、高并发 | Web 应用、高并发 | 单用户、低并发 |

### 2.3 最终选型：PostgreSQL 15+

**选择理由**:

1. **并发性能优秀**: 支持多容器同时访问，解决 SQLite 文件锁问题
2. **功能强大**: 支持复杂查询、JSON 字段、全文搜索等高级功能
3. **数据一致性好**: 完整的 ACID 事务支持
4. **扩展性强**: 支持分区表、读写分离、主从复制
5. **生态完善**: Docker 镜像成熟，易于部署
6. **适合金融场景**: 数据安全性高，支持行级锁

**资源需求**:
- 内存：512MB - 1GB（初始配置）
- CPU: 0.5 - 1 核
- 存储：根据数据量动态增长（建议预留 10GB+）

## 三、服务器配置评估

### 3.1 当前服务器配置调查

需要确认的信息：
- [ ] 服务器总内存
- [ ] 服务器 CPU 核数
- [ ] 服务器存储空间
- [ ] 当前运行的容器数量和资源占用
- [ ] 网络带宽情况

### 3.2 资源评估

**PostgreSQL 资源需求**:
- 基础内存占用：200-300MB
- 每个连接额外占用：10-20MB
- 建议配置：512MB - 1GB 内存

**容器资源需求**:
1. bianace_btcethbnb_trade: CPU 0.5 核，内存 256MB
2. Grid_Trading: CPU 0.5 核，内存 256MB
3. bianace_newtrade_trade: CPU 0.5 核，内存 256MB
4. stockfilter: CPU 0.5 核，内存 256MB

**总体资源需求**:
- PostgreSQL: CPU 1 核，内存 512MB
- 4 个应用容器：CPU 2 核，内存 1GB
- **总计**: CPU 3 核，内存 1.5GB（建议预留 50% 余量）

**推荐服务器配置**:
- CPU: 4 核+
- 内存: 4GB+
- 存储：50GB+ SSD

### 3.3 后续扩展考虑

1. **数据增长**: 
   - 交易记录：预计每月 10-50 万条
   - K 线数据：预计每月 1-5GB
   - 日志数据：预计每月 100-500MB

2. **性能扩展**:
   - 垂直扩展：增加服务器资源
   - 水平扩展：主从复制、读写分离
   - 分区表：按时间分区历史数据

3. **备份策略**:
   - 每日自动备份
   - WAL 日志归档
   - 异地灾备

## 四、数据库架构设计

### 4.1 总体架构

```
┌─────────────────────────────────────────────────────┐
│                 Docker Network                      │
│                                                     │
│  ┌──────────────────┐                              │
│  │   PostgreSQL     │                              │
│  │   Container      │                              │
│  │   Port: 5432     │                              │
│  └────────┬─────────┘                              │
│           │                                        │
│  ┌────────┴─────────┐  ┌──────────┐  ┌──────────┐ │
│  │  bianace app     │  │ Grid app │  │ stock app│ │
│  └──────────────────┘  └──────────┘  └──────────┘ │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### 4.2 数据库设计

**方案一：单数据库多 Schema**（推荐）

```
postgres (PostgreSQL 实例)
├── schema_bianace (bianace_btcethbnb_trade)
│   ├── trades
│   ├── positions
│   ├── account_transfers
│   └── ...
├── schema_grid (Grid_Trading)
│   ├── trades
│   ├── grid_history
│   └── ...
├── schema_short_selling (bianace_newtrade_trade)
│   ├── signals
│   ├── trades
│   └── ...
└── schema_stockfilter (stockfilter)
    ├── stocks
    ├── klines
    └── ...
```

**优点**:
- 数据隔离清晰
- 便于权限管理
- 统一备份恢复
- 资源利用率高

**方案二：多数据库**

```
postgres (PostgreSQL 实例)
├── bianace_db
├── grid_db
├── short_selling_db
└── stockfilter_db
```

**优点**:
- 完全隔离
- 便于迁移
- 故障影响范围小

### 4.3 连接配置

所有应用通过 Docker 网络访问 PostgreSQL：

```yaml
DATABASE_URL=postgresql://user:password@postgres:5432/dbname?schema=schema_name
```

## 五、实施步骤

### 5.1 第一阶段：环境准备

1. **检查服务器配置**
   - 确认服务器资源是否满足要求
   - 检查 Docker 和 Docker Compose 版本

2. **创建 PostgreSQL Docker Compose 配置**
   - 编写 postgresql 服务配置
   - 配置数据卷持久化
   - 配置网络和端口

3. **准备初始化脚本**
   - 创建数据库和用户
   - 创建 schema
   - 配置权限

### 5.2 第二阶段：应用改造

1. **bianace_btcethbnb_trade**
   - 修改 `config/settings.py` 支持 PostgreSQL 连接字符串
   - 修改 `models/database.py` 从 SQLite 迁移到 PostgreSQL
   - 更新 `docker-compose.yml` 添加数据库依赖

2. **Grid_Trading**
   - 修改数据库配置支持 PostgreSQL
   - 将 aiosqlite 替换为 asyncpg
   - 更新 `docker-compose.yml`

3. **bianace_newtrade_trade**
   - 修改 `.env` 配置
   - 更新数据库连接代码
   - 更新 `docker-compose.yml`

4. **stockfilter**
   - 修改数据库配置
   - 更新 `docker-compose.yml`
   - 修改数据迁移脚本

### 5.3 第三阶段：数据迁移

1. **数据导出**
   - 从各 SQLite 数据库导出数据
   - 转换为 PostgreSQL 兼容格式

2. **数据导入**
   - 导入到 PostgreSQL 对应 schema
   - 验证数据完整性

3. **应用测试**
   - 功能测试
   - 性能测试
   - 并发测试

### 5.4 第四阶段：部署上线

1. **部署 PostgreSQL**
   - 启动 PostgreSQL 容器
   - 初始化数据库

2. **部署应用容器**
   - 更新应用配置
   - 启动应用容器
   - 验证连接

3. **监控和备份**
   - 配置监控告警
   - 配置自动备份
   - 制定运维手册

## 六、技术细节

### 6.1 PostgreSQL Docker 配置

```yaml
version: '3.8'

services:
  postgres:
    image: postgres:15-alpine
    container_name: postgres-db
    restart: unless-stopped
    environment:
      POSTGRES_USER: trading_user
      POSTGRES_PASSWORD: your_secure_password
      POSTGRES_DB: trading_platform
      PGDATA: /var/lib/postgresql/data/pgdata
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./postgres/init-scripts:/docker-entrypoint-initdb.d
      - ./postgres/backups:/backups
    ports:
      - "5432:5432"
    networks:
      - trading-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U trading_user"]
      interval: 10s
      timeout: 5s
      retries: 5
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 1G
        reservations:
          cpus: '0.5'
          memory: 512M

volumes:
  postgres_data:
    driver: local

networks:
  trading-network:
    driver: bridge
```

### 6.2 初始化脚本

```sql
-- 01-create-schema.sql

-- 创建各应用 schema
CREATE SCHEMA IF NOT EXISTS schema_bianace;
CREATE SCHEMA IF NOT EXISTS schema_grid;
CREATE SCHEMA IF NOT EXISTS schema_short_selling;
CREATE SCHEMA IF NOT EXISTS schema_stockfilter;

-- 创建应用用户
CREATE USER bianace_user WITH PASSWORD 'bianace_password';
CREATE USER grid_user WITH PASSWORD 'grid_password';
CREATE USER short_selling_user WITH PASSWORD 'short_selling_password';
CREATE USER stockfilter_user WITH PASSWORD 'stockfilter_password';

-- 授权
GRANT ALL PRIVILEGES ON SCHEMA schema_bianace TO bianace_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA schema_bianace TO bianace_user;

GRANT ALL PRIVILEGES ON SCHEMA schema_grid TO grid_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA schema_grid TO grid_user;

GRANT ALL PRIVILEGES ON SCHEMA schema_short_selling TO short_selling_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA schema_short_selling TO short_selling_user;

GRANT ALL PRIVILEGES ON SCHEMA schema_stockfilter TO stockfilter_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA schema_stockfilter TO stockfilter_user;
```

### 6.3 应用连接字符串

```python
# bianace_btcethbnb_trade
DATABASE_URL = "postgresql://bianace_user:bianace_password@postgres:5432/trading_platform?schema=schema_bianace"

# Grid_Trading
DATABASE_URL = "postgresql://grid_user:grid_password@postgres:5432/trading_platform?schema=schema_grid"

# bianace_newtrade_trade
DATABASE_URL = "postgresql://short_selling_user:short_selling_password@postgres:5432/trading_platform?schema=schema_short_selling"

# stockfilter
DATABASE_URL = "postgresql://stockfilter_user:stockfilter_password@postgres:5432/trading_platform?schema=schema_stockfilter"
```

### 6.4 SQLite 到 PostgreSQL 的差异处理

1. **数据类型映射**:
   ```
   SQLite INTEGER -> PostgreSQL BIGINT
   SQLite REAL -> PostgreSQL DOUBLE PRECISION
   SQLite TEXT -> PostgreSQL TEXT
   SQLite BLOB -> PostgreSQL BYTEA
   SQLite DATETIME -> PostgreSQL TIMESTAMP
   ```

2. **自增主键**:
   ```sql
   -- SQLite
   id INTEGER PRIMARY KEY AUTOINCREMENT
   
   -- PostgreSQL
   id BIGSERIAL PRIMARY KEY
   ```

3. **布尔值**:
   ```sql
   -- SQLite (使用 INTEGER 0/1)
   is_active INTEGER DEFAULT 0
   
   -- PostgreSQL
   is_active BOOLEAN DEFAULT FALSE
   ```

## 七、备份和恢复策略

### 7.1 备份策略

1. **全量备份**: 每日凌晨 2 点
2. **WAL 归档**: 实时归档
3. **备份保留**: 保留 30 天

### 7.2 备份脚本

```bash
#!/bin/bash
# backup-postgres.sh

BACKUP_DIR="/backups/postgres"
DATE=$(date +%Y%m%d_%H%M%S)
CONTAINER_NAME="postgres-db"
DB_USER="trading_user"
DB_NAME="trading_platform"

# 创建备份目录
mkdir -p $BACKUP_DIR

# 全量备份
docker exec $CONTAINER_NAME pg_dump -U $DB_USER -d $DB_NAME --format=custom \
  > $BACKUP_DIR/full_backup_$DATE.dump

# 压缩备份
gzip $BACKUP_DIR/full_backup_$DATE.dump

# 删除 30 天前的备份
find $BACKUP_DIR -name "*.dump.gz" -mtime +30 -delete

echo "Backup completed: $BACKUP_DIR/full_backup_$DATE.dump.gz"
```

### 7.3 恢复流程

```bash
# 恢复数据库
docker exec -i postgres-db pg_restore -U trading_user -d trading_platform --clean \
  < /backups/postgres/full_backup_YYYYMMDD_HHMMSS.dump.gz
```

## 八、监控和告警

### 8.1 监控指标

1. **数据库性能**:
   - 连接数
   - QPS/TPS
   - 查询延迟
   - 缓存命中率

2. **资源使用**:
   - CPU 使用率
   - 内存使用率
   - 磁盘使用率
   - I/O 等待

3. **业务指标**:
   - 各应用连接状态
   - 数据增长速度
   - 备份状态

### 8.2 告警配置

通过 Prometheus + Grafana 监控，配置以下告警：
- 连接数超过阈值
- 磁盘使用率超过 80%
- 备份失败
- 服务不可用

## 九、风险评估和应对

### 9.1 风险点

1. **数据迁移风险**: 数据丢失或不一致
2. **性能风险**: PostgreSQL 性能不如预期
3. **兼容性风险**: 应用代码需要大量修改
4. **运维风险**: 团队缺乏 PostgreSQL 运维经验

### 9.2 应对措施

1. **数据迁移**: 
   - 充分测试迁移脚本
   - 保留 SQLite 备份
   - 制定回滚方案

2. **性能优化**:
   - 性能基准测试
   - 索引优化
   - 查询优化

3. **兼容性**:
   - 逐步迁移，先测试环境
   - 充分的功能测试

4. **运维**:
   - 文档和培训
   - 自动化运维脚本
   - 监控告警完善

## 十、项目计划

### 10.1 时间安排

| 阶段 | 任务 | 预计时间 |
|------|------|----------|
| 第一阶段 | 环境准备和 PostgreSQL 部署 | 1-2 天 |
| 第二阶段 | 应用代码改造 | 3-5 天 |
| 第三阶段 | 数据迁移和测试 | 2-3 天 |
| 第四阶段 | 部署上线和监控 | 1-2 天 |
| **总计** | | **7-12 天** |

### 10.2 里程碑

1. **M1**: PostgreSQL 部署完成，初始化成功
2. **M2**: 所有应用完成代码改造，通过测试
3. **M3**: 数据迁移完成，验证通过
4. **M4**: 生产环境部署完成，运行稳定

## 十一、验收标准

1. **功能验收**:
   - [ ] 所有应用正常连接 PostgreSQL
   - [ ] 所有 CRUD 操作正常工作
   - [ ] 并发访问无异常

2. **性能验收**:
   - [ ] 查询响应时间 < 100ms
   - [ ] 写入吞吐量满足业务需求
   - [ ] 资源占用在预期范围内

3. **可靠性验收**:
   - [ ] 备份恢复测试通过
   - [ ] 故障恢复测试通过
   - [ ] 监控告警正常工作

## 十二、后续优化方向

1. **性能优化**:
   - 查询优化和索引调整
   - 连接池配置优化
   - 缓存层引入（Redis）

2. **高可用**:
   - 主从复制
   - 自动故障切换
   - 负载均衡

3. **安全加固**:
   - SSL 加密连接
   - 审计日志
   - 细粒度权限控制

4. **自动化运维**:
   - 自动备份和恢复
   - 自动扩缩容
   - 自动监控和告警

---

## 附录

### A. 相关文档

- [PostgreSQL 官方文档](https://www.postgresql.org/docs/)
- [Docker PostgreSQL 镜像文档](https://hub.docker.com/_/postgres)
- [SQLAlchemy PostgreSQL 方言文档](https://docs.sqlalchemy.org/en/14/dialects/postgresql.html)
- [asyncpg 文档](https://magicstack.github.io/asyncpg/current/)

### B. 工具推荐

- **数据库管理**: pgAdmin, DBeaver
- **备份工具**: pg_dump, pg_restore
- **监控工具**: Prometheus + Grafana
- **性能分析**: pg_stat_statements

### C. 联系方式

如有问题，请联系运维团队或参考项目文档。
