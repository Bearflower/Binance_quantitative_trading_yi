# 快速启动指南

**项目**: common_service  
**版本**: v1.0  
**更新日期**: 2026-04-20

---

## 🚀 5 分钟快速启动（本地开发）

### 前置要求

- Docker 20+
- Docker Compose 2.0+
- Git

### 第一步：配置环境变量

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑配置文件
vim .env
```

**必须配置的环境变量**:

```bash
# 数据库配置
DB_PASSWORD=your_secure_password_here

# 飞书 Webhook（5 个项目）
BTC_ETH_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx_btc_eth
NEW_COIN_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx_new_coin
GRID_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx_grid
INSPECTION_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx_inspection
STOCK_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx_stock

# K 线数据服务配置
SYMBOLS=BTCUSDT,ETHUSDT,BNBUSDT
COLLECT_INTERVALS=15m,1h,4h,1d
```

### 第二步：启动服务

```bash
# 一键启动所有服务
docker-compose up -d

# 查看日志
docker-compose logs -f

# 查看服务状态
docker-compose ps
```

### 第三步：验证部署

```bash
# 健康检查
curl http://localhost:8765/api/v1/health  # K 线数据服务
curl http://localhost:8766/api/v1/health  # 通知服务

# 查看数据库
docker-compose exec postgres psql -U binance -d binance_data -c "\dt"
```

### 第四步：停止服务

```bash
# 停止所有服务
docker-compose down

# 停止并删除数据卷（谨慎使用）
docker-compose down -v
```

---

## 📦 部署到服务器

### 前置要求

- 服务器已安装 Docker 和 Docker Compose
- 已配置 SSH 免密登录

### 第一步：配置 SSH 免密登录（3 分钟）

```bash
# 1. 生成 SSH 密钥
ssh-keygen -t ed25519 -C "your_email@example.com"
# 直接回车，不设置密码

# 2. 复制公钥到服务器
ssh-copy-id -i /Users/yl/vscode/inspection_automation/docs/only.pem.pub root@SERVER_IP

# 3. 测试免密登录
ssh root@SERVER_IP "echo 成功"
# 如果直接返回"成功"，说明配置成功
```

### 第二步：配置部署文件

```bash
# 复制部署配置模板
cp .deploy_config.example .deploy_config

# 编辑配置
vim .deploy_config
```

**配置说明**:

```bash
SERVER_IP="43.156.242.184"           # 你的服务器 IP
SERVER_USER="root"
SERVER_PROJECT_PATH="/root/common_service"
DOCKER_CONTAINER_NAME="common_service-app"
PROJECT_NAME="common_service"
DEPLOY_PACKAGE_NAME="deployment_package.tar.gz"
```

### 第三步：一键部署

```bash
# 执行一键部署
./one_click_deploy.sh
```

**部署流程**:
1. ✅ 自动打包项目
2. ✅ 上传到服务器
3. ✅ 远程构建和启动
4. ✅ 验证部署成功

---

## 🔧 常用命令

### Docker 相关

```bash
# 查看所有容器
docker-compose ps

# 查看日志
docker-compose logs -f

# 重启服务
docker-compose restart

# 重新构建
docker-compose build --no-cache

# 进入容器
docker-compose exec kline_service /bin/bash
```

### 数据库相关

```bash
# 进入数据库
docker-compose exec postgres psql -U binance -d binance_data

# 查看表
\dt

# 查看 K 线数据
SELECT symbol, interval, COUNT(*) FROM klines GROUP BY symbol, interval;

# 查看指标数据
SELECT indicator_type, COUNT(*) FROM indicators GROUP BY indicator_type;
```

### 日志查看

```bash
# K 线数据服务日志
docker-compose logs -f kline_service

# 通知服务日志
docker-compose logs -f notification_service

# 数据库日志
docker-compose logs -f postgres
```

---

## 📊 API 测试

### K 线数据服务

```bash
# 查询 K 线数据
curl "http://localhost:8765/api/v1/klines?symbol=BTCUSDT&interval=1h&limit=10"

# 查询指标数据
curl "http://localhost:8765/api/v1/indicators?symbol=BTCUSDT&interval=1h&indicator_type=RSI"

# 健康检查
curl http://localhost:8765/api/v1/health
```

### 通知服务

```bash
# 发送消息
curl -X POST http://localhost:8766/api/v1/send \
  -H "Content-Type: application/json" \
  -d '{
    "project": "btc_eth",
    "message": "测试消息",
    "type": "text",
    "level": "info"
  }'

# 查询队列状态
curl http://localhost:8766/api/v1/queue/status

# 健康检查
curl http://localhost:8766/api/v1/health
```

---

## 🐛 故障排查

### 问题 1：服务无法启动

```bash
# 查看详细日志
docker-compose logs kline_service

# 检查数据库连接
docker-compose exec postgres psql -U binance -d binance_data -c "SELECT 1"

# 检查 Redis 连接
docker-compose exec redis redis-cli ping
```

### 问题 2：数据库连接失败

```bash
# 重启数据库
docker-compose restart postgres

# 查看数据库状态
docker-compose exec postgres pg_isready

# 重新初始化数据库（谨慎使用，会删除数据）
docker-compose down -v
docker-compose up -d postgres
```

### 问题 3：端口冲突

```bash
# 查看端口占用
lsof -i :8765
lsof -i :8766

# 修改 docker-compose.yml 中的端口映射
# 例如：8765:8000 -> 8767:8000
```

### 问题 4：SSH 免密登录失败

```bash
# 检查 SSH 密钥
ls -la ~/.ssh/

# 重新配置
ssh-keygen -t ed25519 -C "your_email@example.com"
ssh-copy-id -i /Users/yl/vscode/inspection_automation/docs/only.pem.pub root@SERVER_IP

# 测试
ssh -v root@SERVER_IP
```

---

## 📁 项目结构

```
common_service/
├── docker-compose.yml          # Docker 编排
├── .env.example               # 环境变量模板
├── .deploy_config.example     # 部署配置模板
├── auto_package.sh            # 自动打包脚本
├── upload_to_server.sh        # 上传脚本
├── one_click_deploy.sh        # 一键部署脚本
├── docker/
│   ├── init.sql               # 数据库初始化
│   └── nginx.conf             # Nginx 配置
├── kline_data_service/        # K 线数据服务
│   ├── Dockerfile
│   ├── requirements.txt
│   └── src/
├── notification_service/      # 通知服务
│   ├── Dockerfile
│   ├── requirements.txt
│   └── src/
└── logs/                      # 日志目录
```

---

## 📞 获取帮助

- **完整文档**: `/docs/` 目录
- **API 文档**: `http://localhost:8765/docs` (Swagger)
- **问题反馈**: GitHub Issues

---

**最后更新**: 2026-04-20
