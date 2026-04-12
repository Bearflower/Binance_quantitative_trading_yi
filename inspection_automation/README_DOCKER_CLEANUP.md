# Docker 清理脚本使用说明

## 📁 脚本位置

所有脚本位于服务器 `/root/inspection/` 目录下

---

## 📋 脚本列表

### 1. `server_check.sh` - 巡检脚本
**功能**: 每日服务器巡检，包含 Docker 清理检查

**执行时间**: 每日 07:30 自动执行

**清理策略**:
- 每周日自动清理 Docker 构建缓存
- 不清理悬空镜像（需手动确认）
- 在巡检报告中显示悬空镜像统计信息

**巡检报告示例**:
```
✅ 发现 5 个悬空镜像，占用空间：1200MB
✅ 悬空镜像清理提示：运行 /root/inspection/cleanup_dangling_images.sh 可清理悬空镜像
```

---

### 2. `cleanup_builder_cache.sh` - 构建缓存清理脚本
**功能**: 清理 Docker 构建缓存

**执行方式**:
- **自动**: 每周日 03:00 自动执行（crontab）
- **手动**: `/root/inspection/cleanup_builder_cache.sh`

**清理内容**:
- Docker Build Cache（构建中间层）
- 不影响运行中的容器
- 不影响现有镜像

**安全性**: ✅ 安全，可定期执行

**执行示例**:
```bash
# 手动执行
/root/inspection/cleanup_builder_cache.sh

# 查看日志
tail -f /root/inspection/cleanup.log
```

---

### 3. `cleanup_dangling_images.sh` - 悬空镜像清理脚本
**功能**: 手动清理 Docker 悬空镜像

**执行方式**: 仅支持手动执行（需要确认）

**清理内容**:
- `<none>` 标签的悬空镜像
- 不影响运行中的容器
- 可能影响快速回滚能力

**安全性**: ⚠️ 需要手动确认

**执行示例**:
```bash
# 1. 查看悬空镜像
docker images --filter "dangling=true"

# 2. 执行清理（会提示确认）
/root/inspection/cleanup_dangling_images.sh

# 3. 查看清理日志
tail -f /root/inspection/cleanup.log
```

**输出示例**:
```
=============================================
Docker 悬空镜像清理
=============================================

📊 清理前悬空镜像统计:
  悬空镜像数量：5 个
  占用空间：1200MB

📋 悬空镜像列表:
IMAGE ID          SIZE      CREATED AT
abc1234567890     200MB     2 days ago
def0987654321     300MB     3 days ago
...

⚠️  警告：此操作将删除所有悬空镜像（<none> 标签）
    这不会影响正在运行的容器，但可能影响快速回滚能力

确定要清理这些悬空镜像吗？(y/N): y

🧹 开始清理悬空镜像...
✅ 已清理悬空镜像：1200MB

=============================================
清理完成！
=============================================
```

---

## ⚙️ 自动任务配置

### Crontab 配置

```bash
# 查看当前配置
crontab -l

# 配置内容
30 7 * * *     /root/inspection/server_check.sh >> /root/inspection/server_check.log 2>&1
0 3 * * 0      /root/inspection/cleanup_builder_cache.sh >> /root/inspection/cleanup.log 2>&1
```

**说明**:
- `30 7 * * *`: 每日 07:30 执行巡检
- `0 3 * * 0`: 每周日 03:00 清理构建缓存

---

## 📊 日志文件

| 日志文件 | 说明 | 查看命令 |
|---------|------|---------|
| `server_check.log` | 每日巡检日志 | `tail -f /root/inspection/server_check.log` |
| `cleanup.log` | Docker 清理日志 | `tail -f /root/inspection/cleanup.log` |

---

## 🎯 使用流程

### 日常使用

1. **每日自动巡检**（07:30）
   - 自动检查服务器状态
   - 显示悬空镜像统计
   - 发送飞书报告

2. **每周自动清理**（周日 03:00）
   - 自动清理构建缓存
   - 记录清理日志

### 手动清理悬空镜像

当巡检报告显示有悬空镜像时：

```bash
# 1. 登录服务器
ssh root@43.156.242.184

# 2. 查看悬空镜像
docker images --filter "dangling=true"

# 3. 执行清理脚本
/root/inspection/cleanup_dangling_images.sh

# 4. 输入 'y' 确认清理

# 5. 查看清理结果
tail /root/inspection/cleanup.log
```

---

## ⚠️ 注意事项

### 构建缓存清理（每周自动）
- ✅ 安全，不影响生产
- ✅ 下次构建时会重新创建缓存
- ✅ 建议保留此自动任务

### 悬空镜像清理（手动确认）
- ⚠️ 不影响运行中的容器
- ⚠️ 但可能影响快速回滚能力
- ✅ 建议每月清理一次或按需清理
- ✅ 清理前请确认不需要回滚到旧版本

---

## 🔍 故障排查

### 脚本执行失败

```bash
# 1. 检查脚本权限
ls -la /root/inspection/*.sh

# 2. 设置执行权限
chmod +x /root/inspection/*.sh

# 3. 检查 Docker 服务
systemctl status docker

# 4. 手动测试脚本
/root/inspection/cleanup_builder_cache.sh
```

### Crontab 未执行

```bash
# 1. 检查 crontab 配置
crontab -l

# 2. 检查 cron 服务
systemctl status cron

# 3. 查看系统日志
grep CRON /var/log/syslog | tail -20
```

### 清理后空间未释放

```bash
# 1. 检查是否有停止的容器
docker ps -a

# 2. 检查 Docker 空间使用
docker system df

# 3. 检查其他大文件
du -sh /var/lib/docker/*
```

---

## 📚 相关文档

- 巡检脚本配置：`/root/inspection/server_check.sh`
- 清理日志：`/root/inspection/cleanup.log`
- 巡检日志：`/root/inspection/server_check.log`

---

**最后更新**: 2026-03-24
**版本**: v1.0
