# 系统服务配置指南

## 📋 概述

本指南介绍如何配置 systemd 服务，实现做空系统的开机自启和自动守护进程管理。

**配置后效果**:
- ✅ 系统重启后自动启动服务
- ✅ 容器崩溃自动重启
- ✅ 简化的服务管理命令
- ✅ 统一的日志收集
- ✅ 资源限制保护

---

## 🚀 快速配置

### 步骤 1: 部署系统

```bash
# 在服务器上执行部署脚本
bash deploy.sh
```

部署脚本会自动提示 systemd 服务配置步骤。

### 步骤 2: 配置 systemd 服务

```bash
# 1. 复制服务文件到 systemd 目录
sudo cp deploy/short-selling-system.service /etc/systemd/system/

# 2. 重新加载 systemd 配置
sudo systemctl daemon-reload

# 3. 启用开机自启
sudo systemctl enable short-selling-system

# 4. 启动服务
sudo systemctl start short-selling-system
```

### 步骤 3: 验证配置

```bash
# 查看服务状态
bash deploy/serviceManager.sh status

# 查看实时日志
bash deploy/serviceManager.sh logs
```

---

## 📖 服务管理命令

使用提供的服务管理脚本，可以方便地管理系统服务：

### 基本命令

```bash
# 查看服务状态
bash deploy/serviceManager.sh status

# 启动服务
bash deploy/serviceManager.sh start

# 停止服务
bash deploy/serviceManager.sh stop

# 重启服务
bash deploy/serviceManager.sh restart
```

### 日志管理

```bash
# 查看实时日志（类似 tail -f）
bash deploy/serviceManager.sh logs

# 查看最近 50 行日志
sudo journalctl -u short-selling-system -n 50

# 查看今天的日志
sudo journalctl -u short-selling-system --since today

# 查看指定时间的日志
sudo journalctl -u short-selling-system --since "2026-03-11 00:00:00" --until "2026-03-11 23:59:59"
```

### 开机自启管理

```bash
# 启用开机自启
bash deploy/serviceManager.sh enable

# 禁用开机自启
bash deploy/serviceManager.sh disable

# 查看是否启用自启
systemctl is-enabled short-selling-system
```

### 高级命令

```bash
# 重新加载 systemd 配置（修改服务文件后）
bash deploy/serviceManager.sh reload

# 显示帮助信息
bash deploy/serviceManager.sh help
```

---

## 🔧 systemd 服务文件详解

### 服务文件位置

```
/etc/systemd/system/short-selling-system.service
```

### 文件内容解析

```ini
[Unit]
Description=Binance New Coin Short Selling System
Documentation=https://github.com/your-repo/short-selling-system
After=network.target docker.service
Requires=docker.service
```

**解析**:
- `Description`: 服务描述
- `Documentation`: 文档链接
- `After`: 在网络和 Docker 服务之后启动
- `Requires`: 依赖 Docker 服务

```ini
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/root/short-selling-system

# 启动命令
ExecStart=/usr/bin/docker start short-selling-system

# 停止命令
ExecStop=/usr/bin/docker stop short-selling-system

# 重启命令
ExecReload=/usr/bin/docker restart short-selling-system

# 日志配置
StandardOutput=journal
StandardError=journal
SyslogIdentifier=short-selling-system

# 资源限制
MemoryLimit=512M
CPUQuota=50%
```

**解析**:
- `Type=oneshot`: 一次性任务类型
- `RemainAfterExit=yes`: 命令执行后仍视为活动状态
- `WorkingDirectory`: 工作目录
- `ExecStart`: 启动命令
- `ExecStop`: 停止命令
- `ExecReload`: 重启命令
- `MemoryLimit`: 内存限制（512MB）
- `CPUQuota`: CPU 使用限制（50%）

```ini
[Install]
WantedBy=multi-user.target
```

**解析**:
- `WantedBy`: 多用户模式下启用（开机自启）

---

## 🔍 故障排查

### 服务无法启动

```bash
# 1. 查看详细错误日志
sudo journalctl -u short-selling-system -n 100 --no-pager

# 2. 检查 Docker 容器状态
docker ps -a | grep short-selling-system

# 3. 手动启动容器测试
docker start short-selling-system

# 4. 查看容器日志
docker logs short-selling-system
```

### 服务状态异常

```bash
# 查看 systemd 服务状态
sudo systemctl status short-selling-system

# 检查服务是否启用自启
systemctl is-enabled short-selling-system

# 检查依赖服务状态
systemctl status docker
systemctl status network
```

### 日志文件位置

```bash
# systemd 日志位置
/var/log/journal/

# 查看系统日志
sudo journalctl -xe

# 清理旧日志（释放磁盘空间）
sudo journalctl --vacuum-time=7d
```

---

## 📊 监控与告警

### 服务监控

```bash
# 实时监控服务状态
watch -n 5 'systemctl is-active short-selling-system'

# 监控容器状态
watch -n 5 'docker ps -f name=short-selling-system --format "{{.Status}}"'
```

### 添加监控脚本（可选）

创建 `/usr/local/bin/monitor-short-selling.sh`:

```bash
#!/bin/bash

SERVICE="short-selling-system"

if ! systemctl is-active --quiet $SERVICE; then
    echo "⚠️ 警告：$SERVICE 服务已停止！"
    # 可以添加告警通知，如发送邮件、飞书等
    # curl -X POST -H "Content-Type: application/json" \
    #   -d '{"msg_type":"text","content":{"text":"做空系统服务已停止"}}' \
    #   YOUR_WEBHOOK_URL
fi
```

设置定时检查（crontab）:

```bash
# 每 5 分钟检查一次
*/5 * * * * /usr/local/bin/monitor-short-selling.sh
```

---

## 🔄 服务更新流程

### 标准更新流程

```bash
# 1. 停止服务
bash deploy/serviceManager.sh stop

# 2. 执行部署（会重新构建镜像和容器）
bash deploy.sh

# 3. 启动服务
bash deploy/serviceManager.sh start

# 4. 验证服务状态
bash deploy/serviceManager.sh status
```

### 快速重启（不更新代码）

```bash
# 简单重启
bash deploy/serviceManager.sh restart
```

---

## 📝 最佳实践

### 1. 定期检查服务状态

```bash
# 每天检查一次（添加到 crontab）
0 9 * * * systemctl status short-selling-system --no-pager
```

### 2. 日志轮转（防止日志过大）

创建 `/etc/logrotate.d/short-selling-system`:

```
/var/log/journal/*/short-selling-system.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0640 root root
}
```

### 3. 资源监控

```bash
# 查看容器资源使用
docker stats short-selling-system --no-stream

# 查看系统资源
systemctl show short-selling-system | grep -E "Memory|CPU"
```

### 4. 备份配置

```bash
# 备份 systemd 服务文件
sudo cp /etc/systemd/system/short-selling-system.service ~/backup/

# 备份 Docker 配置
cp docker-compose.yml ~/backup/
```

---

## 🎯 自动化程度对比

### 配置前

| 场景 | 操作 | 耗时 |
|------|------|------|
| 系统重启 | 手动 SSH 登录，执行 `python main.py start` | 2-3 分钟 |
| 容器崩溃 | 手动检查并重启容器 | 5-10 分钟 |
| 查看日志 | SSH 登录，执行 `docker logs` | 1-2 分钟 |

### 配置后

| 场景 | 操作 | 耗时 |
|------|------|------|
| 系统重启 | 自动启动，无需干预 | 0 分钟 |
| 容器崩溃 | 自动重启，无需干预 | 0 分钟 |
| 查看日志 | 本地执行 `serviceManager.sh logs` | 30 秒 |

---

## 📞 常见问题

### Q1: 为什么使用 Type=oneshot 而不是 Type=simple？

**A**: 因为我们是通过 systemd 管理 Docker 容器，而不是直接管理进程。`Type=oneshot` 适合管理一次性命令，配合 `RemainAfterExit=yes` 可以正确反映服务状态。

### Q2: 可以设置自动重启吗？

**A**: 可以！在 Docker Compose 文件中已经设置了 `restart: always`，容器崩溃会自动重启。systemd 服务也会确保 Docker 容器运行。

### Q3: 如何完全卸载服务？

**A**: 执行以下命令：

```bash
# 停止并禁用服务
bash deploy/serviceManager.sh stop
bash deploy/serviceManager.sh disable

# 删除 systemd 服务文件
sudo rm /etc/systemd/system/short-selling-system.service

# 重新加载 systemd
sudo systemctl daemon-reload

# 删除 Docker 容器和镜像
docker rm short-selling-system
docker rmi short-selling-system:latest
```

### Q4: 内存限制可以调整吗？

**A**: 可以！编辑服务文件，修改 `MemoryLimit` 参数：

```bash
sudo vim /etc/systemd/system/short-selling-system.service

# 修改为 1GB
MemoryLimit=1G

# 重新加载并重启
sudo systemctl daemon-reload
sudo systemctl restart short-selling-system
```

---

## 📚 相关文档

- [systemd 官方文档](https://www.freedesktop.org/software/systemd/man/systemd.service.html)
- [Docker 服务管理最佳实践](https://docs.docker.com/config/containers/start-containers-automatically/)
- [journalctl 使用指南](https://www.man7.org/linux/man-pages/man1/journalctl.1.html)
- [人工干预点分析](人工干预点分析.md)

---

**最后更新**: 2026-03-11  
**版本**: v1.0
