# 服务器自动化巡检系统

## 📋 项目简介

这是一套服务器自动化巡检系统，用于每日定时检查服务器运行状态、Docker 容器状态、系统资源使用情况等，并通过飞书机器人发送巡检报告。

## ✨ 主要功能

### 1. Docker 容器监控
- 检查所有 Docker 容器的运行状态
- 监控容器健康状态（healthy/unhealthy/unknown）
- 检查容器内进程运行情况
- 分析最近 24 小时日志中的错误信息

### 2. 系统资源监控
- CPU 使用率监控（阈值：80%）
- 内存使用率监控（阈值：80%）
- 磁盘使用率监控（阈值：80%）

### 3. 系统服务检查
- 关键系统服务状态（sshd、docker、NetworkManager）
- 系统负载监控
- 网络连接测试
- 系统更新检查

### 4. 巡检报告
- 通过飞书 Webhook 自动发送巡检结果
- 支持正常报告和异常告警
- 详细的错误信息和统计

## 📁 项目结构

```
inspection_automation/
├── server_check.sh        # 主巡检脚本
├── check_inspection.sh    # 辅助检查脚本
├── deploy.sh              # 部署脚本
├── deploy_full.sh         # 完整部署脚本
├── setup_cron.sh          # 定时任务配置脚本
└── README.md              # 项目文档
```

## 🚀 快速开始

### 1. 配置巡检脚本

编辑 `server_check.sh` 配置文件：

```bash
# 飞书 Webhook URL
FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/your-webhook-url"

# Docker 容器过滤模式（空字符串表示监控所有容器）
DOCKER_CONTAINER_PATTERN=""

# 资源使用阈值
CPU_THRESHOLD=80      # CPU 使用率阈值 (%)
MEMORY_THRESHOLD=80   # 内存使用率阈值 (%)
DISK_THRESHOLD=80     # 磁盘使用率阈值 (%)
```

### 2. 部署到服务器

#### 方法一：使用部署脚本

```bash
# 一键部署
./deploy.sh
```

#### 方法二：手动上传

```bash
# 上传脚本到服务器
scp server_check.sh root@your-server:/root/inspection/

# 设置执行权限
ssh root@your-server "chmod +x /root/inspection/server_check.sh"
```

### 3. 配置定时任务

系统默认每天 07:30 自动执行巡检。

```bash
# 配置定时任务
./setup_cron.sh

# 或手动配置
ssh root@your-server "crontab -e"
# 添加以下行：
0 7 * * * /root/inspection/server_check.sh >> /root/inspection/cron.log 2>&1
```

### 4. 测试运行

```bash
# 在服务器上手动运行巡检
ssh root@your-server "/root/inspection/server_check.sh"
```

## 📊 巡检结果示例

### 正常报告
```
✅ 服务器巡检报告 (一切正常)
服务器巡检正常
时间：2026-03-13 07:30:00
所有检查项都正常
服务器运行状态良好
```

### 异常报告
```
🚨 服务器巡检报告 (发现 2 个问题)
服务器巡检发现问题
时间：2026-03-13 07:30:00
共发现 2 个问题

问题详情：
- Docker 容器未运行：binance-monitor (状态：Exited)
- CPU 使用率过高：85.50% (阈值：80%)
```

## 🔧 配置说明

### DOCKER_CONTAINER_PATTERN 配置

此配置用于过滤需要监控的 Docker 容器：

- **空字符串 `""`**：监控所有 Docker 容器（推荐）
- **`"binance"`**：只监控名称包含 "binance" 的容器
- **`"myapp"`**：只监控名称包含 "myapp" 的容器

**示例：**
```bash
# 监控所有容器
DOCKER_CONTAINER_PATTERN=""

# 只监控特定项目
DOCKER_CONTAINER_PATTERN="binance"
```

### 告警阈值配置

根据实际需求调整资源使用阈值：

```bash
# 严格模式（更早发现潜在问题）
CPU_THRESHOLD=60
MEMORY_THRESHOLD=60
DISK_THRESHOLD=70

# 标准模式（推荐）
CPU_THRESHOLD=80
MEMORY_THRESHOLD=80
DISK_THRESHOLD=80

# 宽松模式（减少告警）
CPU_THRESHOLD=90
MEMORY_THRESHOLD=90
DISK_THRESHOLD=90
```

## 📝 定时任务管理

### 查看定时任务
```bash
ssh root@your-server "crontab -l"
```

### 编辑定时任务
```bash
ssh root@your-server "crontab -e"
```

### 删除定时任务
```bash
ssh root@your-server "crontab -r"
```

### 查看巡检日志
```bash
# 查看最新日志
ssh root@your-server "tail -f /root/inspection/server_check.log"

# 查看历史日志
ssh root@your-server "cat /root/inspection/server_check.log"
```

## 🔍 故障排查

### 1. 飞书消息发送失败

**检查 Webhook URL 是否正确：**
```bash
ssh root@your-server "grep FEISHU_WEBHOOK /root/inspection/server_check.sh"
```

**测试网络连接：**
```bash
ssh root@your-server "curl -I https://open.feishu.cn"
```

### 2. 容器未被监控

**检查 DOCKER_CONTAINER_PATTERN 配置：**
```bash
ssh root@your-server "grep DOCKER_CONTAINER_PATTERN /root/inspection/server_check.sh"
```

**查看服务器上的所有容器：**
```bash
ssh root@your-server "docker ps -a --format '{{.Names}}'"
```

### 3. 定时任务未执行

**检查 cron 服务状态：**
```bash
ssh root@your-server "systemctl status crond"
```

**查看 cron 日志：**
```bash
ssh root@your-server "tail -f /var/log/cron"
```

## 📈 更新日志

### v1.1.0 (2026-03-13)
- ✅ 支持监控所有 Docker 容器（不仅仅是特定模式）
- ✅ 优化容器状态检测逻辑
- ✅ 改进错误提示信息
- ✅ 修复 short-selling-system 容器未被监控的问题

### v1.0.0 (2026-03-07)
- ✅ 初始版本发布
- ✅ Docker 容器状态监控
- ✅ 系统资源监控
- ✅ 飞书消息推送
- ✅ 定时任务自动执行

## 📞 技术支持

如有问题，请检查：
1. 服务器网络连接是否正常
2. Docker 服务是否运行
3. 飞书 Webhook URL 是否正确
4. 脚本执行权限是否正确设置

## 📄 许可证

本项目仅供内部使用。
