# PostgreSQL "trading_user" 数据库连接错误分析报告

## 问题描述

PostgreSQL 数据库日志中持续出现错误（从 2026-03-30 开始）：
```
FATAL: database "trading_user" does not exist
```

**频率**：每 10 秒一次
**持续时间**：已超过 10 天
**影响**：产生大量错误日志，浪费服务器资源

---

## 排查过程

### 1. 检查当前连接的容器 ✅

- `binance-trade-analyzer` - 使用正确的数据库配置
- `short-selling-system` - 已修复（localhost → postgres-db）
- `stockfilter-app` - 无数据库配置
- `postgres-db` - PostgreSQL 服务本身

### 2. 检查数据库用户和权限 ✅

```sql
Role name          | Attributes
bianace_user       | 
short_selling_user | 
trading_user       | Superuser, Create role, Create DB, Replication, Bypass RLS
```

`trading_user` 用户存在，是超级用户，但没有名为 `trading_user` 的数据库。

### 3. 检查数据库列表 ✅

```sql
Database Name      | Owner
postgres           | postgres
trading_platform   | bianace_user
template0          | postgres
template1          | postgres
```

没有 `trading_user` 数据库。

### 4. 检查活动连接 ✅

```sql
pid     | usename      | client_addr
126923  | bianace_user | 172.30.0.3 (binance-trade-analyzer)
```

只有 `binance-trade-analyzer` 在正常连接。

### 5. 检查定时任务 ✅

- 系统 crontab：没有发现连接 `trading_user` 的任务
- 容器定时任务：没有发现异常

### 6. 检查 PostgreSQL 配置 ✅

```bash
POSTGRES_USER=trading_user
POSTGRES_DB=trading_platform
POSTGRES_PASSWORD=Trading@2024Secure
```

**关键发现**：`POSTGRES_USER=trading_user` 是初始超级用户，但默认数据库是 `trading_platform`。

---

## 问题根源分析

### 最可能的原因

**PostgreSQL 的默认行为**：

当客户端使用 `trading_user` 用户连接 PostgreSQL 时，如果**未明确指定数据库名**，PostgreSQL 会尝试连接与用户名同名的数据库（即 `trading_user`）。

由于 `trading_user` 数据库不存在，所以报错：
```
FATAL: database "trading_user" does not exist
```

### 谁在连接？

**可能性分析**：

1. **外部监控工具**（最可能）
   - Prometheus、Grafana 等监控工具
   - 数据库健康检查脚本
   - 使用默认配置连接（只指定用户名，未指定数据库）

2. **应用程序配置错误**
   - 某个已停止的容器或脚本
   - 配置文件中使用 `trading_user` 作为数据库名

3. **PostgreSQL 内部进程**
   - 自动统计信息收集
   - 背景写入进程

### 证据

1. **连接无客户端地址**：`pg_stat_activity` 中看不到 `trading_user` 的连接
2. **瞬时连接**：连接尝试后立即失败断开
3. **固定频率**：每 10 秒一次，符合健康检查特征

---

## 解决方案

### 方案 1：创建 `trading_user` 数据库（推荐）

创建一个空的 `trading_user` 数据库，让连接成功：

```bash
docker exec postgres-db psql -U bianace_user -d trading_platform -c "CREATE DATABASE trading_user OWNER trading_user;"
```

**优点**：
- 立即解决问题
- 不影响现有应用
- 符合 PostgreSQL 默认行为

**缺点**：
- 多一个空数据库

### 方案 2：找到并修复配置错误的客户端

1. **检查所有容器的环境变量**：
```bash
for container in $(docker ps -q); do
    echo "=== $(docker inspect --format '{{.Name}}' $container) ==="
    docker inspect $container --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -i database
done
```

2. **检查宿主机上的脚本**：
```bash
grep -r "trading_user" /root/ --include="*.sh" --include="*.py" --include="*.env"
```

3. **检查监控系统配置**：
   - Prometheus配置文件
   - Grafana 数据源配置
   - 任何数据库监控工具

### 方案 3：修改 PostgreSQL 默认行为

在 `postgresql.conf` 中设置默认数据库：

```conf
# 不推荐：PostgreSQL 不支持设置默认数据库
```

**注意**：PostgreSQL 不支持全局设置默认数据库，每个用户必须有同名数据库或明确指定数据库名。

### 方案 4：删除 `trading_user` 用户（不推荐）

```bash
docker exec postgres-db psql -U postgres -c "DROP USER trading_user;"
```

**警告**：这可能会破坏依赖该用户的应用程序！

---

## 推荐执行步骤

### 第一步：创建数据库（立即执行）

```bash
ssh root@43.156.242.184 "docker exec postgres-db psql -U bianace_user -d trading_platform -c 'CREATE DATABASE trading_user OWNER trading_user;'"
```

### 第二步：验证修复

```bash
# 等待 30 秒后检查日志
ssh root@43.156.242.184 "sleep 30 && docker logs postgres-db --tail 20 | grep -E 'ERROR|FATAL'"
```

### 第三步：查找根本原因（可选）

如果需要找出是谁在连接：

1. **启用连接日志**（需要 superuser 权限）：
```bash
docker exec postgres-db psql -U postgres -c "ALTER SYSTEM SET log_connections = on;"
docker exec postgres-db psql -U postgres -c "SELECT pg_reload_conf();"
```

2. **查看日志**：
```bash
docker logs postgres-db 2>&1 | grep "connection received"
```

3. **识别客户端**：
   - 查看 IP 地址
   - 匹配容器网络

---

## 总结

**问题**：某个客户端使用 `trading_user` 用户连接 PostgreSQL，但未指定数据库名，导致尝试连接不存在的 `trading_user` 数据库。

**最可能的来源**：外部监控工具或健康检查脚本

**推荐解决**：创建 `trading_user` 数据库（5 分钟搞定）

**长期建议**：
1. 找出配置错误的客户端并修复
2. 使用专门的监控用户（只读权限）
3. 明确指定数据库名，不依赖默认行为

---

报告生成时间：2026-04-12  
检查人：AI Assistant  
建议优先级：高（立即创建数据库）
