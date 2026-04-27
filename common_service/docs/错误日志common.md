---
title: 错误日志common
mdate: 2026-04-24 08:50:12
mdevice: Y&L的MacBook Air
doc_id: 9c75489b68ef457b88288e10f3edfc65
date: 2026-04-24 08:50
---

我来帮你查询 `common_service_kline` 项目查询不存在的表 `stock_list` 的详细日志：


        
            
toolName: run_command
            
status: success
          
command: ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem -o StrictHostKeyChecking=no root@43.156.242.184 "docker logs --since 24h postgres-db 2>&1 | grep 'stock_list' | head -30" undefined
          

        
            
toolName: run_command
            
status: success
          
command: ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem -o StrictHostKeyChecking=no root@43.156.242.184 "docker logs --since 24h postgres-db 2>&1 | grep 'stock_list' | wc -l" undefined
          

        
            
toolName: run_command
            
status: success
          
command: ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem -o StrictHostKeyChecking=no root@43.156.242.184 "docker logs postgres-db 2>&1 | grep 'stock_list'" undefined
          

        
            
toolName: run_command
            
status: success
          
command: ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem -o StrictHostKeyChecking=no root@43.156.242.184 "docker logs --since 24h common_service_kline 2>&1 | grep -E 'stock|ERROR' | head -50" undefined
          
让我获取更详细的信息，包括连接信息和完整的日志上下文：


        
            
toolName: run_command
            
status: success
          
command: ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem -o StrictHostKeyChecking=no root@43.156.242.184 "docker logs postgres-db 2>&1 | grep -B5 -A2 '936965'" undefined
          

        
            
toolName: run_command
            
status: success
          
command: ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem -o StrictHostKeyChecking=no root@43.156.242.184 "docker logs postgres-db 2>&1 | grep -B5 -A2 '936966'" undefined
          
现在让我查看 common_service_kline 服务在错误发生时间点的详细日志：


        
            
toolName: run_command
            
status: success
          
command: ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem -o StrictHostKeyChecking=no root@43.156.242.184 "docker logs --since 24h common_service_kline 2>&1 | grep -E '01:05:4[6-9]|01:05:5[0-9]' | head -30" undefined
          

        
            
toolName: run_command
            
status: success
          
command: ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem -o StrictHostKeyChecking=no root@43.156.242.184 "docker logs postgres-db 2>&1 | grep '936965\\|936966' | grep -E 'connection|user|database'" undefined
          
让我查看完整的数据库日志和 kline 服务的完整错误日志：


        
            
toolName: run_command
            
status: success
          
command: ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem -o StrictHostKeyChecking=no root@43.156.242.184 "docker logs --since 24h common_service_kline 2>&1 | grep -v health | grep -v INFO | head -100" undefined
          

        
            
toolName: run_command
            
status: success
          
command: ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem -o StrictHostKeyChecking=no root@43.156.242.184 "docker logs postgres-db 2>&1 | tail -500 | grep -E 'ERROR|STATEMENT' | tail -50" undefined
          
现在让我生成一个完整的报告：


        
            
toolName: run_command
            
status: success
          
command: ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem -o StrictHostKeyChecking=no root@43.156.242.184 "docker logs postgres-db 2>&1 | grep 'stock_list' > /tmp/stock_list_errors.log && cat /tmp/stock_list_errors.log" undefined
          
好的，我已经收集到了所有详细的日志信息。现在让我为你生成一个完整的报告：

## 📊 `common_service_kline` 查询 `stock_list` 表错误详细报告

### 错误概述

**受影响容器**：`common_service_kline`  
**数据库容器**：`postgres-db`  
**错误类型**：`relation "stock_list" does not exist`  
**错误总数**：2 次  
**发生时间**：2026-04-23 01:05:46 - 01:05:49（UTC）

---

### 详细错误日志

#### 错误 #1

```
时间：2026-04-23 01:05:46.478 UTC
进程 ID：936965
错误：ERROR: relation "stock_list" does not exist at character 31
SQL 语句：SELECT COUNT(*) as total FROM stock_list WHERE code LIKE '6%' OR code LIKE '00%' OR code LIKE '30%'
```

**分析**：
- 查询目的是统计股票代码以 `6`、`00`、`30` 开头的股票数量
- 这些代码对应中国 A 股市场：
  - `6%` → 沪市主板（600xxx, 601xxx, 603xxx 等）
  - `00%` → 深市主板（000xxx, 001xxx, 002xxx 等）
  - `30%` → 创业板（300xxx, 301xxx）

#### 错误 #2

```
时间：2026-04-23 01:05:49.317 UTC
进程 ID：936966
错误：ERROR: relation "stock_list" does not exist at character 31
SQL 语句：SELECT COUNT(*) as total FROM stock_list WHERE code LIKE '920%'
```

**分析**：
- 查询目的是统计股票代码以 `920` 开头的股票数量
- `920%` 可能对应北交所股票（920xxx）

---

### 错误上下文

#### 数据库日志上下文

```log
2026-04-23 01:03:39.930 UTC [936845] ERROR: cannot convert NaN to bigint
2026-04-23 01:03:39.930 UTC [936845] STATEMENT: ALTER TABLE klines ALTER COLUMN volume TYPE BIGINT

2026-04-23 01:04:21.157 UTC [26] LOG: checkpoint starting: wal

2026-04-23 01:05:43.296 UTC [26] LOG: checkpoint complete: wrote 612 buffers (3.7%)

2026-04-23 01:05:46.478 UTC [936965] ERROR: relation "stock_list" does not exist
2026-04-23 01:05:46.478 UTC [936965] STATEMENT: SELECT COUNT(*) as total FROM stock_list WHERE code LIKE '6%' OR code LIKE '00%' OR code LIKE '30%'

2026-04-23 01:05:49.317 UTC [936966] ERROR: relation "stock_list" does not exist
2026-04-23 01:05:49.317 UTC [936966] STATEMENT: SELECT COUNT(*) as total FROM stock_list WHERE code LIKE '920%'

2026-04-23 01:09:21.386 UTC [26] LOG: checkpoint starting: time
```

**时间线分析**：
1. **01:03:39** - 数据库正在执行表结构修改（`klines.volume` 改为 BIGINT）
2. **01:04:21** - 数据库检查点开始
3. **01:05:43** - 检查点完成（耗时 82 秒）
4. **01:05:46** - 第一次查询 `stock_list` 失败（间隔 3 秒）
5. **01:05:49** - 第二次查询 `stock_list` 失败（间隔 3 秒）

---

### 错误原因分析

#### 1. 表不存在

**可能原因**：
- ✅ `stock_list` 表确实未在数据库中创建
- ✅ 表名拼写错误（实际表名可能是其他名称）
- ✅ 使用了错误的 schema（表在其他 schema 中）
- ✅ 表已被删除或迁移

#### 2. 业务场景推测

**应用场景**：
- `common_service_kline` 服务可能需要获取股票列表
- 服务启动时或定时任务会统计各板块股票数量
- 统计结果可能用于：
  - 数据完整性校验
  - 监控股票池变化
  - 生成统计报告

#### 3. 代码位置推测

**可能代码位置**：
```python
# 可能在以下位置找到相关代码
- kline_data_service/api/routes.py
- kline_data_service/core/scheduler.py
- kline_data_service/services/stock_list_service.py
```

**可能的代码**：
```python
# 示例代码（需要修复）
async def get_stock_list_count():
    # ❌ 错误：表不存在
    query = "SELECT COUNT(*) as total FROM stock_list WHERE code LIKE '6%'"
    result = await database.fetch_one(query)
    return result['total']
```

---

### 解决方案

#### 方案 1：创建 `stock_list` 表（推荐）

```sql
-- 在 PostgreSQL 中创建 stock_list 表
CREATE TABLE IF NOT EXISTS schema_bianace.stock_list (
    id SERIAL PRIMARY KEY,
    code VARCHAR(10) NOT NULL,
    name VARCHAR(100),
    market VARCHAR(20),
    list_date DATE,
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建索引
CREATE INDEX idx_stock_code ON stock_list(code);
CREATE INDEX idx_stock_market ON stock_list(market);
```

#### 方案 2：修改代码使用正确的表名

如果实际表名不是 `stock_list`，需要修改代码：

```python
# ❌ 错误代码
query = "SELECT COUNT(*) FROM stock_list WHERE ..."

# ✅ 修正代码（假设实际表名为 securities）
query = "SELECT COUNT(*) FROM securities WHERE ..."
```

#### 方案 3：添加表存在性检查

```python
# 在查询前先检查表是否存在
async def check_table_exists(table_name):
    query = """
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_schema = 'schema_bianace'
            AND table_name = %s
        )
    """
    result = await database.fetch_one(query, [table_name])
    return result[0]

# 使用示例
if await check_table_exists('stock_list'):
    # 执行查询
    count = await get_stock_count()
else:
    # 处理表不存在的情况
    logger.warning(f"表 {table_name} 不存在")
```

---

### 验证步骤

#### 1. 检查数据库中是否存在该表

```bash
# 连接到数据库
docker exec -it postgres-db psql -U bianace_user -d trading_platform

# 检查表是否存在
\dt schema_bianace.stock_list

# 或者查询 information_schema
SELECT table_name, table_schema 
FROM information_schema.tables 
WHERE table_name LIKE '%stock%' 
  AND table_schema = 'schema_bianace';
```

#### 2. 查看实际有哪些表

```bash
docker exec -it postgres-db psql -U bianace_user -d trading_platform -c "\dt schema_bianace.*"
```

#### 3. 查找代码中的表引用

```bash
# 在代码库中搜索 stock_list
grep -r "stock_list" --include="*.py" /path/to/kline_service/

# 搜索相关的 SQL 查询
grep -r "FROM stock" --include="*.py" /path/to/kline_service/
```

---

### 总结

**错误本质**：`common_service_kline` 服务尝试查询一个不存在的 `stock_list` 表

**影响范围**：
- ✅ 仅 2 次错误，未持续发生
- ✅ 可能是初始化检查或一次性统计任务
- ✅ 未影响核心 K 线数据采集功能

**建议操作**：
1. 立即：检查代码，确认 `stock_list` 表的正确名称
2. 短期：创建缺失的表或修复代码
3. 长期：添加表存在性检查和错误处理机制

---

**报告生成时间**：2026-04-24  
**数据来源**：PostgreSQL 数据库日志  
**分析工具**：Docker 日志分析