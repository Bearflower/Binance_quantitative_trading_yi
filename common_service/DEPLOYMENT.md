# Common Service 部署指南

## 📋 部署前检查清单

### 1. 服务器环境要求

- ✅ 已安装 Docker (版本 20.10+)
- ✅ 已安装 Docker Compose (版本 2.0+)
- ✅ 服务器内存 ≥ 4GB
- ✅ 服务器磁盘空间 ≥ 20GB

### 2. SSH 免密登录配置（必须）

**在本地执行：**

```bash
# 1. 生成 SSH 密钥（如果没有）
ssh-keygen -t ed25519 -C "your_email@example.com"
# 直接回车，不设置密码

# 2. 复制公钥到服务器
ssh-copy-id -i /Users/yl/vscode/inspection_automation/docs/only.pem.pub root@43.156.242.184

# 3. 测试免密登录
ssh root@43.156.242.184 "echo 成功"
# 应该直接返回"成功"，不需要输入密码
```

### 3. 环境变量配置

**创建 `.env` 文件：**

```bash
cp .env.example .env
```

**编辑 `.env` 文件，配置以下内容：**

```bash
# ============================================
# 数据库配置
# ============================================
POSTGRES_USER=binance
POSTGRES_DB=binance_data
DB_PASSWORD=你的强密码_至少 16 位

# ============================================
# Redis 配置
# ============================================
REDIS_PASSWORD=你的强密码_至少 16 位

# ============================================
# 飞书 Webhook 配置（5 个项目）
# ============================================
# BTC/ETH 交易系统
BTC_ETH_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/你的 webhook

# 新币做空系统
NEW_COIN_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/你的 webhook

# 网格交易系统
GRID_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/你的 webhook

# 检查自动化系统
INSPECTION_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/你的 webhook

# 股票筛选系统
STOCK_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/你的 webhook

# ============================================
# K 线数据服务配置
# ============================================
SYMBOLS=BTCUSDT,ETHUSDT,BNBUSDT
COLLECT_INTERVALS=15m,1h,4h,1d

# ============================================
# 通知服务配置
# ============================================
WORKER_COUNT=3
RATE_LIMIT_PER_MINUTE=60

# ============================================
# 日志配置
# ============================================
LOG_LEVEL=INFO
```

### 4. 获取飞书 Webhook

**步骤：**

1. 打开飞书，进入需要推送的群聊
2. 点击右上角「设置」→「群机器人」
3. 点击「添加机器人」
4. 选择「自定义机器人」
5. 设置机器人名称（如：BTC/ETH 交易通知）
6. 复制 Webhook 地址

**重复以上步骤，为 5 个项目分别创建机器人。**

---

## 🚀 部署步骤

### 方法一：一键部署（推荐）

```bash
# 1. 确保已配置 SSH 免密登录
ssh root@43.156.242.184 "echo 成功"

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填入实际配置

# 3. 执行一键部署
./one_click_deploy.sh
```

### 方法二：手动部署

```bash
# 1. 打包项目
./auto_package.sh

# 2. 上传到服务器
./upload_to_server.sh

# 3. 登录服务器，手动部署
ssh root@43.156.242.184

# 在服务器上执行：
cd /root/common_service
docker-compose build
docker-compose up -d

# 4. 查看状态
docker-compose ps
docker-compose logs -f
```

---

## 🔍 验证部署

### 1. 检查容器状态

```bash
# 查看所有容器
docker-compose ps

# 应该看到 5 个容器都在运行：
# - common_service_postgres
# - common_service_redis
# - common_service_kline
# - common_service_notification
# - common_service_nginx
```

### 2. 检查服务日志

```bash
# 查看所有服务日志
docker-compose logs -f

# 查看单个服务日志
docker-compose logs -f kline_service
docker-compose logs -f notification_service
```

### 3. 测试 API 接口

```bash
# 测试通知服务健康检查
curl http://43.156.242.188:8766/api/v1/health

# 测试 K 线数据服务健康检查
curl http://43.156.242.188:8000/api/v1/health

# 测试通知发送
curl -X POST http://43.156.242.188:8766/api/v1/send \
  -H "Content-Type: application/json" \
  -d '{
    "project": "btc_eth",
    "message": "测试消息",
    "type": "text",
    "level": "info"
  }'
```

### 4. 检查数据库

```bash
# 连接到数据库
docker-compose exec postgres psql -U binance -d binance_data

# 查看表结构
\dt

# 应该看到类似这样的表：
# - kline_btcusdt_15m
# - kline_btcusdt_1h
# - kline_btcusdt_4h
# - kline_btcusdt_1d
# ... (其他币种和周期)
```

---

## 📊 服务端口说明

| 服务 | 端口 | 说明 |
|------|------|------|
| K 线数据服务 | 8765 | 提供 K 线数据查询 API |
| 通知服务 | 8766 | 提供通知发送 API |
| PostgreSQL | 5432 | 数据库（内部网络） |
| Redis | 6379 | 消息队列（内部网络） |
| Nginx | 80 | 反向代理（可选） |

**访问示例：**

```bash
# K 线数据服务
http://43.156.242.188:8765/api/v1/klines/latest?symbol=BTCUSDT&interval=1h

# 通知服务
http://43.156.242.188:8766/api/v1/send

# 通过 Nginx（如果启用）
http://43.156.242.188/kline/api/v1/klines/latest
http://43.156.242.188/notification/api/v1/send
```

---

## 🔧 常用运维命令

### 查看服务状态

```bash
# 查看所有容器
docker-compose ps

# 查看容器资源使用
docker stats

# 查看服务日志
docker-compose logs -f
```

### 重启服务

```bash
# 重启所有服务
docker-compose restart

# 重启单个服务
docker-compose restart kline_service
docker-compose restart notification_service
```

### 停止服务

```bash
# 停止所有服务（保留数据）
docker-compose down

# 停止并删除数据（危险！）
docker-compose down -v
```

### 更新服务

```bash
# 重新部署
./one_click_deploy.sh

# 或者手动更新
docker-compose pull
docker-compose up -d --build
```

---

## ⚠️ 常见问题

### 问题 1：容器无法启动

**症状：** `docker-compose up` 报错

**解决方案：**

```bash
# 1. 查看详细日志
docker-compose logs

# 2. 检查配置文件
cat .env

# 3. 检查端口占用
netstat -tlnp | grep 8765
netstat -tlnp | grep 8766

# 4. 清理并重建
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

### 问题 2：数据库连接失败

**症状：** 服务日志显示 `database connection failed`

**解决方案：**

```bash
# 1. 等待数据库启动完成
sleep 10

# 2. 检查数据库健康状态
docker-compose ps postgres

# 3. 查看数据库日志
docker-compose logs postgres

# 4. 重启服务
docker-compose restart kline_service
```

### 问题 3：飞书通知不发送

**症状：** 通知服务正常，但飞书收不到消息

**解决方案：**

```bash
# 1. 检查 Webhook 配置
docker-compose exec notification_service env | grep WEBHOOK

# 2. 测试 Webhook
curl -X POST https://open.feishu.cn/open-apis/bot/v2/hook/你的 webhook \
  -H "Content-Type: application/json" \
  -d '{"msg_type":"text","content":{"text":"测试"}}'

# 3. 查看通知服务日志
docker-compose logs -f notification_service
```

### 问题 4：K 线数据未采集

**症状：** 数据库中没有 K 线数据

**解决方案：**

```bash
# 1. 检查采集器日志
docker-compose logs -f kline_service

# 2. 手动触发采集
curl -X POST "http://localhost:8765/api/v1/collect/manual?symbol=BTCUSDT&interval=1h&minutes=5"

# 3. 检查定时任务
docker-compose exec kline_service python3 -c "from kline_data_service.core.scheduler import TaskScheduler; print('调度器正常')"
```

---

## 📝 下一步

部署完成后，可以进行：

1. **业务系统改造** - 将 3 个交易系统接入通用服务
2. **性能监控** - 配置 Prometheus + Grafana 监控
3. **日志收集** - 配置 ELK 日志分析系统
4. **自动备份** - 配置数据库定时备份

---

**文档版本**: v1.0  
**更新日期**: 2026-04-20  
**维护者**: Common Service Team
