# PostgreSQL Syntax Error 诊断报告

## 问题描述

在 PostgreSQL 数据库日志中发现 2 个错误：
```
2026-04-10 00:39:44.093 UTC [105490] ERROR:  syntax error at or near ":" at character 100
2026-04-10 00:40:53.362 UTC [105542] ERROR:  syntax error at or near ":" at character 100
```

## 问题分析

### 1. 错误特征

- **错误类型**：SQL 语法错误
- **错误位置**：在冒号 `:` 附近（character 100）
- **发生时间**：2026-04-10 00:39:44 和 00:40:53（两次）
- **当前状态**：已不再出现

### 2. 可能的原因

#### 原因 1：应用程序使用了命名参数语法 ❌

某些 ORM 或数据库库使用 `:param` 语法作为命名参数占位符：

```python
# 错误示例（PostgreSQL 不支持这种语法）
cursor.execute("SELECT * FROM trades WHERE order_id = :order_id", {"order_id": 123})

# 正确示例（使用 %s 占位符）
cursor.execute("SELECT * FROM trades WHERE order_id = %s", (123,))
```

**检查的代码库**：
- ✅ `models/database.py` - 使用 psycopg2 的 `%s` 占位符
- ✅ 其他 Python 文件 - 未发现命名参数语法

#### 原因 2：SQL 语句中包含冒号字符

某些数据值可能包含冒号字符（如时间戳 `2026-04-10T00:39:00`），如果未正确转义可能导致语法错误。

**检查点**：
- 时间戳格式化：`datetime.isoformat()` 会产生 `:` 字符
- URL 或路径：可能包含 `:` 字符

#### 原因 3：PostgreSQL 的 PL/pgSQL 代码

PostgreSQL 的存储过程或函数中使用 `:` 作为变量前缀，但普通 SQL 语句不支持。

### 3. 数据库检查

#### 表结构检查 ✅

所有表字段类型正确：
- `trades.create_time` - bigint (int8)
- `trades.update_time` - bigint (int8)
- `trades.order_id` - bigint (int8)

#### 当前查询检查 ✅

没有活跃的异常查询：
```sql
SELECT pid, usename, application_name, state, query 
FROM pg_stat_activity 
WHERE state != 'idle';
```

#### 日志配置

- `logging_collector` = off（日志输出到 stderr）
- `log_min_error_statement` = error（记录错误 SQL）

### 4. 代码检查

检查了以下文件，未发现使用 `:param` 语法的 SQL 语句：
- ✅ `models/database.py` - 使用 `%s` 占位符
- ✅ `scheduler_new.py` - 使用 `%s` 占位符
- ✅ `services/*.py` - 使用 `%s` 占位符

## 解决方案

### 方案 1：持续监控（推荐）

由于错误已不再出现，可能是暂时性问题。建议：

1. **监控日志**：继续观察是否再次出现
2. **记录上下文**：如果再次出现，记录：
   - 哪个应用程序在访问数据库
   - 执行的具体操作
   - 完整的错误日志

### 方案 2：启用详细日志

如果问题频繁出现，可以启用更详细的日志：

```sql
-- 在 postgresql.conf 中添加或修改
logging_collector = on
log_min_error_statement = DEBUG5
log_statement = 'all'  -- 记录所有 SQL 语句（谨慎使用）
```

**注意**：`log_statement = 'all'` 会产生大量日志，仅用于调试。

### 方案 3：检查应用程序代码

如果知道哪个应用程序在访问数据库，检查：

1. **SQL 语句构造**：确保使用参数化查询
2. **字符串转义**：确保特殊字符正确转义
3. **ORM 配置**：确保 ORM 使用正确的参数语法

## 预防措施

### 1. 使用参数化查询

```python
# ✅ 正确：使用 psycopg2 的参数化查询
cursor.execute("SELECT * FROM trades WHERE order_id = %s", (order_id,))

# ❌ 错误：字符串拼接（SQL 注入风险 + 语法错误）
cursor.execute(f"SELECT * FROM trades WHERE order_id = {order_id}")

# ❌ 错误：命名参数语法（PostgreSQL 不支持）
cursor.execute("SELECT * FROM trades WHERE order_id = :order_id", {"order_id": order_id})
```

### 2. 时间戳处理

```python
# ✅ 正确：使用 BIGINT 存储毫秒时间戳
timestamp_ms = int(datetime.now().timestamp() * 1000)
cursor.execute("INSERT INTO trades (create_time) VALUES (%s)", (timestamp_ms,))

# ✅ 正确：使用 TIMESTAMP 类型
cursor.execute("INSERT INTO trades (create_time) VALUES (%s)", (datetime.now(),))

# ❌ 错误：直接拼接 ISO 格式（包含冒号）
cursor.execute(f"INSERT INTO trades (create_time) VALUES ('{datetime.now().isoformat()}')")
```

### 3. 代码审查要点

- [ ] 所有 SQL 语句使用 `%s` 占位符
- [ ] 不使用字符串拼接构造 SQL
- [ ] 不使用 `:param` 命名参数语法
- [ ] 时间戳转换为 BIGINT 或使用 TIMESTAMP 类型
- [ ] 特殊字符正确转义

## 监控脚本

创建监控脚本检测语法错误：

```bash
#!/bin/bash
# monitor_postgres_errors.sh

while true; do
    docker logs postgres-db --tail 1000 2>&1 | \
        grep "syntax error at or near" | \
        tail -10
    
    sleep 60
done
```

## 总结

**当前状态**：错误已不再出现，可能是暂时性问题

**可能原因**：
1. 某个应用程序使用了不正确的 SQL 语法
2. 数据中包含未正确转义的冒号字符
3. 临时性的网络或连接问题

**建议**：
1. 继续监控数据库日志
2. 如果再次出现，记录完整的错误信息和上下文
3. 检查所有访问数据库的应用程序代码

---

生成时间：2026-04-10  
检查人：AI Assistant  
状态：待观察
