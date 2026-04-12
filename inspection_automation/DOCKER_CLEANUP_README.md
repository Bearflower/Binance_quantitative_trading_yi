# Docker 清理功能说明

## 📋 功能概述

已将 Docker 清理功能集成到日常巡检脚本中，采用**智能触发**策略，避免过度清理。

---

## ⚙️ 配置说明

### 配置参数（在 `server_check.sh` 顶部）

```bash
# Docker 清理配置
DOCKER_CLEANUP_ENABLED=true  # 是否启用 Docker 自动清理
DOCKER_CLEANUP_THRESHOLD=70  # 磁盘使用率达到此阈值时触发清理 (%)
DOCKER_CLEANUP_TYPE="builder_only"  # 清理类型
```

### 清理类型说明

| 类型 | 说明 | 风险等级 | 推荐场景 |
|------|------|----------|----------|
| `builder_only` | 仅清理构建缓存 | ✅ 低风险 | 生产环境（推荐） |
| `builder_and_images` | 清理缓存 + 悬空镜像 | ⚠️ 中风险 | 开发/测试环境 |

---

## 🎯 工作原理

### 智能触发机制

```
每次巡检时检查磁盘使用率
        ↓
磁盘使用率 > 70%？
        ↓
    是 → 执行 Docker 清理
        ↓
    否 → 跳过清理
```

### 清理内容

#### 1. **Docker 构建缓存**（总是清理）
- 清理 `docker builder prune` 的内容
- 删除历史构建中间层
- **不影响**运行中的容器和镜像
- **安全**：下次构建时会重新创建缓存

#### 2. **悬空镜像**（可选清理）
- 清理 `docker image prune -f` 的内容
- 删除 `<none>` 标签的镜像
- **不影响**正在使用的镜像
- **注意**：可能影响快速回滚能力

---

## 🔍 日志示例

### 当磁盘使用率 > 70% 时

```
检查是否需要清理 Docker 空间...
当前磁盘使用率：76%
磁盘使用率超过阈值 (76% > 70%)，执行 Docker 清理...
清理 Docker 构建缓存...
✅ 已清理 Docker 构建缓存
清理悬空镜像...
✅ 已清理悬空镜像
✅ Docker 清理完成，释放空间：46G -> 21G
```

### 当磁盘使用率正常时

```
检查是否需要清理 Docker 空间...
当前磁盘使用率：35%
磁盘使用率正常，无需清理 Docker 空间
```

---

## 📊 风险评估

### ✅ 安全的清理（构建缓存）

**可以安全加入日常巡检**：
- 清理的是历史构建的中间层
- 不影响运行中的容器
- 不影响现有镜像
- 下次构建时会重新创建

**今日清理成果**：26.83GB

---

### ⚠️ 需谨慎的清理（悬空镜像）

**建议按需清理**：
- 删除 `<none>` 标签的镜像
- 不影响运行中的容器
- 但可能影响快速回滚能力
- 建议保留一周的旧镜像

---

## 🛠️ 使用方式

### 方式 1：自动清理（推荐）

配置已启用，每日巡检时自动检查并触发清理：

```bash
# 每日 07:30 自动执行
crontab -l | grep server_check.sh
```

### 方式 2：手动清理

运行独立的清理脚本：

```bash
# 仅清理构建缓存（安全）
docker builder prune -f

# 清理缓存 + 悬空镜像
docker builder prune -f && docker image prune -f

# 使用清理脚本
./docker_cleanup.sh
```

---

## 📝 配置建议

### 生产环境（推荐配置）

```bash
DOCKER_CLEANUP_ENABLED=true
DOCKER_CLEANUP_THRESHOLD=70
DOCKER_CLEANUP_TYPE="builder_only"
```

**优点**：
- ✅ 安全，不影响生产
- ✅ 智能触发，避免过度清理
- ✅ 保留悬空镜像，支持快速回滚

---

### 开发环境

```bash
DOCKER_CLEANUP_ENABLED=true
DOCKER_CLEANUP_THRESHOLD=80
DOCKER_CLEANUP_TYPE="builder_and_images"
```

**优点**：
- ✅ 最大化释放磁盘空间
- ✅ 开发环境不频繁回滚

---

### 禁用自动清理

```bash
DOCKER_CLEANUP_ENABLED=false
```

**适用场景**：
- 需要手动控制清理时机
- 对磁盘空间有严格管理要求

---

## 🎯 最佳实践

### 1. 定期检查

即使启用了自动清理，也建议：
- 每周检查一次磁盘使用情况
- 每月手动执行一次完整清理
- 监控 Docker 镜像数量

### 2. 镜像管理

- 使用明确的镜像标签（如 `v1.0.0` 而非 `latest`）
- 定期删除不用的镜像：`docker rmi <image-id>`
- 保留最近 3 个版本的镜像用于回滚

### 3. 监控告警

建议配置磁盘空间监控：
- 磁盘使用率 > 70%：告警
- 磁盘使用率 > 80%：紧急告警
- 磁盘使用率 > 90%：立即处理

---

## 📞 故障排查

### 清理失败

如果清理失败，检查：
1. Docker 服务是否运行：`systemctl status docker`
2. 是否有足够的权限：使用 root 用户
3. 网络连接是否正常

### 清理后空间未释放

检查是否有：
- 停止的容器占用空间
- 悬空卷（volumes）占用空间
- 其他大文件占用空间

```bash
# 查看所有停止的容器
docker ps -a

# 查看卷的使用情况
docker volume ls

# 查看磁盘占用最大的目录
du -sh /var/lib/docker/*
```

---

## 📚 相关文档

- [Docker 官方文档 - prune](https://docs.docker.com/config/pruning/)
- [Docker 空间管理最佳实践](https://docs.docker.com/storage/storagedriver/)
- 服务器巡检脚本：`server_check.sh`
- Docker 清理脚本：`docker_cleanup.sh`

---

**最后更新**: 2026-03-24
**版本**: v1.0
