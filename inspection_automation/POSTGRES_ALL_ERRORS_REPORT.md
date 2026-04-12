# PostgreSQL 数据库错误综合报告

## 问题总结

用户报告的 5 个历史错误：

### 1. pg_stat_statements 不存在 ❌ (已解决)
```
2026-04-11 04:17:07 UTC [176531] ERROR: relation "pg_stat_statements" does not exist
```

**原因**：AI 诊断命令尝试查询未安装的扩展
**状态**：✅ **不是应用程序错误**，是诊断命令产生的，可以忽略
**解决**：无需处理

---

### 2. 权限不足错误 ❌ (已解决)
```
2026-04-11 04:17:33 UTC [176573] ERROR: must be superuser or have privileges of pg_read_all_settings to examine "log_directory"
```

**原因**：AI 诊断命令尝试查看需要 superuser 权限的配置
**状态**：✅ **不是应用程序错误**，是诊断命令产生的，可以忽略
**解决**：无需处理

---

### 3. 列 "2026-04-01" 不存在 ⚠️ (需要关注)
```
2026-04-11 04:37:55 UTC [177449] ERROR: column "2026-04-01" does not exist at character 48
```

**原因分析**：
- SQL 查询中将日期字符串当作列名
- 可能是字符串拼接错误导致的 SQL 注入
- 示例错误 SQL：`SELECT "2026-04-01" FROM table`（应该是 `SELECT * FROM table WHERE date = '2026-04-01'`）

**可能来源**：
- 回测脚本中的日期参数处理
- 动态 SQL 构造时未正确转义

**状态**：⚠️ **需要检查代码**
**建议**：
1. 检查所有使用日期参数的 SQL 查询
2. 确保使用参数化查询，而不是字符串拼接
3. 检查回测脚本的日期处理逻辑

---

### 4. 列 "exit_price" 不存在 ⚠️ (需要关注)
```
2026-04-11 07:00:32 UTC [183587] ERROR: column "exit_price" does not exist at character 138
```

**原因分析**：
- 代码中使用了 `exit_price` 字段名
- 但数据库表 `closed_positions` 中实际字段名是 `close_price`

**数据库表结构**：
```sql
-- closed_positions 表的实际字段
close_price           -- ✅ 正确的字段名
exit_price            -- ❌ 不存在
```

**代码中的使用**：
```python
# 回测脚本中使用 exit_price 作为变量
'exit_price': float(signal['entry_price'])

# 但尝试查询数据库时使用了错误的字段名
SELECT exit_price FROM closed_positions  -- ❌ 错误
SELECT close_price FROM closed_positions  -- ✅ 正确
```

**状态**：⚠️ **代码和数据库不匹配**
**解决**：
1. 统一使用 `close_price` 字段名
2. 或者在数据库中添加 `exit_price` 字段（不推荐）

---

### 5. 冒号语法错误 ⚠️ (已分析)
```
2026-04-11 18:00:48 UTC [201594] ERROR: syntax error at or near ":" at character 100
```

**原因分析**：
- 可能使用了命名参数语法 `:param`
- 或者数据中包含未转义的冒号字符

**状态**：✅ **暂时性问题**，未再出现
**建议**：继续监控

---

## 当前主要问题

### 🔴 trading_user 数据库不存在（最严重）

```
2026-04-12 13:04:20 UTC [260831] FATAL: database "trading_user" does not exist
```

**频率**：每 10 秒一次，持续不断
**影响**：
- 产生大量错误日志
- 浪费服务器资源
- 可能掩盖其他真正的问题

**原因**：
- 某个应用程序配置了错误的数据库名
- 应该连接 `trading_platform`，但配置成了 `trading_user`

**解决步骤**：

1. **查找连接源**
```bash
# 查看谁在连接
docker exec postgres-db psql -U bianace_user -d trading_platform -c \
  "SELECT client_addr, application_name, count(*) FROM pg_stat_activity GROUP BY client_addr, application_name;"
```

2. **检查应用程序配置**
```bash
# 检查 bianace 项目的 .env 文件
cat /root/trading_system/.env | grep DATABASE
```

3. **修复配置**
```bash
# 修改 .env 文件
DATABASE_URL="postgresql://bianace_user:Bianace@2024@postgres-db:5432/trading_platform"
```

4. **重启应用程序**
```bash
docker restart trading_system-app
```

---

## 修复优先级

### 🔴 高优先级（立即处理）
1. **trading_user 数据库连接错误**
   - 影响：持续产生大量错误
   - 解决：修复应用程序配置

### 🟡 中优先级（近期处理）
2. **exit_price 字段名错误**
   - 影响：部分查询失败
   - 解决：统一使用 close_price

3. **日期字符串作为列名错误**
   - 影响：可能导致 SQL 注入风险
   - 解决：检查参数化查询

### 🟢 低优先级（可忽略）
4. **pg_stat_statements 不存在**
   - 原因：诊断命令产生
   - 解决：无需处理

5. **权限不足错误**
   - 原因：诊断命令产生
   - 解决：无需处理

6. **冒号语法错误**
   - 状态：未再出现
   - 解决：继续监控

---

## 推荐行动方案

### 第一步：修复 trading_user 连接问题

1. 检查所有容器的环境变量
```bash
docker inspect trading_system-app | grep DATABASE
```

2. 检查 docker-compose.yml
```bash
cat /root/trading_system/docker-compose.yml
```

3. 修正配置并重启

### 第二步：检查代码中的 SQL 查询

1. 搜索所有 SQL 查询
```bash
grep -r "exit_price" --include="*.py" .
```

2. 检查日期参数处理
```bash
grep -r "2026-04" --include="*.py" .
```

3. 确保使用参数化查询
```python
# ✅ 正确
cursor.execute("SELECT * FROM trades WHERE date = %s", (date_str,))

# ❌ 错误
cursor.execute(f"SELECT * FROM trades WHERE date = {date_str}")
```

### 第三步：持续监控

创建监控脚本：
```bash
#!/bin/bash
# monitor_db_errors.sh

while true; do
    echo "=== $(date) ==="
    docker logs postgres-db --tail 1000 2>&1 | \
        grep -E "ERROR|FATAL" | \
        grep -v "trading_user" | \
        tail -10
    sleep 300
done
```

---

## 总结

**已解决的问题**：
- ✅ pg_stat_statements 不存在（诊断命令）
- ✅ 权限不足错误（诊断命令）
- ✅ bigint out of range（表结构已正确）

**需要关注的问题**：
- ⚠️ exit_price 字段名不匹配
- ⚠️ 日期字符串作为列名
- ⚠️ 冒号语法错误（暂时性）

**最紧急的问题**：
- 🔴 trading_user 数据库连接配置错误

---

报告生成时间：2026-04-12  
检查人：AI Assistant  
状态：等待修复 trading_user 连接问题
