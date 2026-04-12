# 做空系统部署指南

## 📋 部署前准备

### 1. 服务器信息

- **服务器 IP**: 43.156.242.184
- **用户名**: root
- **项目路径**: /root/short_selling_system
- **容器名称**: short-selling-system

### 2. 配置 SSH 密钥认证（推荐）

**强烈建议使用 SSH 密钥认证，而不是密码认证**

#### 步骤 1: 生成 SSH 密钥（如果没有）

```bash
# 检查是否已有 SSH 密钥
ls -la ~/.ssh/id_rsa.pub

# 如果没有，生成新的 SSH 密钥
ssh-keygen -t rsa -b 4096 -C "your_email@example.com"
# 按回车接受默认设置
```

#### 步骤 2: 复制公钥到服务器

```bash
# 方法 1：使用 ssh-copy-id（推荐）
ssh-copy-id -o StrictHostKeyChecking=no root@43.156.242.184
# 输入服务器密码

# 方法 2：手动复制
cat ~/.ssh/id_rsa.pub | ssh root@43.156.242.184 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

#### 步骤 3: 测试 SSH 连接

```bash
ssh -o StrictHostKeyChecking=no root@43.156.242.184 "echo 连接成功"
```

---

## 🚀 一键部署

### 方法 1: 使用一键部署脚本（推荐）

```bash
cd /Users/yl/vscode/bianace_newtrade_trade

# 执行一键部署（包含打包、上传、部署）
./one_click_deploy.sh
```

### 方法 2: 分步部署

#### 步骤 1: 打包项目

```bash
./auto_package.sh
```

#### 步骤 2: 上传到服务器

```bash
./upload_to_server.sh
```

#### 步骤 3: SSH 到服务器执行部署

```bash
ssh root@43.156.242.184

# 在服务器上执行
cd /root/short_selling_system
./deploy.sh
```

---

## 🔧 手动部署（备选方案）

### 如果自动部署失败，可以使用手动部署

#### 1. 本地打包

```bash
cd /Users/yl/vscode/bianace_newtrade_trade/short_selling_system

# 创建部署包
tar -czf ../deployment_package.tar.gz \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='logs/*' \
    --exclude='data/*' \
    --exclude='.pytest_cache/*' \
    .
```

#### 2. 上传到服务器

```bash
# 使用 scp 上传
scp deployment_package.tar.gz root@43.156.242.184:/root/
```

#### 3. 服务器上部署

```bash
ssh root@43.156.242.184

# 解压部署包
cd /root
rm -rf short_selling_system
mkdir -p short_selling_system
tar -xzf deployment_package.tar.gz -C short_selling_system
cd short_selling_system

# 设置权限
chmod +x deploy.sh

# 配置环境变量
cp .env.example .env
vim .env  # 编辑配置，填入 API 密钥等

# 构建并启动
docker-compose build --no-cache
docker-compose up -d

# 检查状态
docker ps -f name=short-selling-system
docker logs --tail 30 short-selling-system
```

---

## 📊 部署验证

### 1. 检查容器状态

```bash
ssh root@43.156.242.184 "docker ps -f name=short-selling-system"
```

### 2. 查看实时日志

```bash
ssh root@43.156.242.184 "docker logs -f short-selling-system"
```

### 3. 测试系统功能

```bash
# SSH 到服务器
ssh root@43.156.242.184

# 进入容器
docker exec -it short-selling-system /bin/bash

# 运行测试
python3 test_all_stages_v2.py
```

---

## 🔍 故障排查

### 问题 1: SSH 连接失败

**症状**: `Permission denied (publickey,password)`

**解决方案**:
```bash
# 检查 SSH 密钥
ls -la ~/.ssh/id_rsa.pub

# 重新配置 SSH 密钥
ssh-copy-id root@43.156.242.184

# 检查 SSH 配置
vim ~/.ssh/config
# 添加：
# Host 43.156.242.*
#     StrictHostKeyChecking no
#     UserKnownHostsFile /dev/null
```

### 问题 2: Docker 构建失败

**症状**: `docker-compose build` 报错

**解决方案**:
```bash
# SSH 到服务器
ssh root@43.156.242.184

# 清理 Docker 缓存
docker system prune -f

# 重新构建
cd /root/short_selling_system
docker-compose build --no-cache
```

### 问题 3: 容器无法启动

**症状**: 容器启动后立即退出

**解决方案**:
```bash
# 查看详细日志
ssh root@43.156.242.184 "docker logs short-selling-system"

# 检查配置文件
ssh root@43.156.242.184 "cat /root/short_selling_system/.env"

# 手动启动调试
ssh root@43.156.242.184 "cd /root/short_selling_system && docker-compose up"
```

### 问题 4: API 密钥配置错误

**症状**: 日志显示 API 认证失败

**解决方案**:
```bash
# SSH 到服务器
ssh root@43.156.242.184

# 编辑环境变量
cd /root/short_selling_system
vim .env

# 确保以下配置正确：
# BINANCE_API_KEY=your_actual_api_key
# BINANCE_API_SECRET=your_actual_secret
# FEISHU_WEBHOOK=your_actual_webhook

# 重启容器
docker-compose restart
```

---

## 🛠️ 日常管理

### 查看容器状态

```bash
ssh root@43.156.242.184 "docker ps -f name=short-selling-system"
```

### 查看实时日志

```bash
ssh root@43.156.242.184 "docker logs -f short-selling-system"
```

### 重启容器

```bash
ssh root@43.156.242.184 "docker restart short-selling-system"
```

### 停止容器

```bash
ssh root@43.156.242.184 "docker stop short-selling-system"
```

### 启动容器

```bash
ssh root@43.156.242.184 "docker start short-selling-system"
```

### 更新部署

```bash
cd /Users/yl/vscode/bianace_newtrade_trade
./one_click_deploy.sh
```

### 进入容器终端

```bash
ssh root@43.156.242.184 "docker exec -it short-selling-system /bin/bash"
```

### 查看资源使用

```bash
ssh root@43.156.242.184 "docker stats short-selling-system"
```

---

## 📝 配置说明

### .env 文件配置

```bash
# 币安 API 配置（必填）
BINANCE_API_KEY=your_binance_api_key_here
BINANCE_API_SECRET=your_binance_api_secret_here

# 飞书通知配置（可选）
FEISHU_WEBHOOK=your_feishu_webhook_url_here

# 数据库配置（默认即可）
DATABASE_URL=sqlite:///data/database.db

# 日志配置（默认即可）
LOG_LEVEL=INFO
LOG_FILE=logs/app.log

# 交易配置
DEFAULT_POSITION_SIZE=4.0        # 默认仓位大小（USDT）
DEFAULT_LEVERAGE=5               # 默认杠杆倍数
MAX_POSITION_SIZE=10.0           # 最大仓位
MIN_POSITION_SIZE=2.0            # 最小仓位

# 风控配置
DEFAULT_STOP_LOSS_PERCENT=0.05   # 止损比例（5%）
DEFAULT_TAKE_PROFIT_PERCENT_1=0.20  # 第一止盈（20%）
DEFAULT_TAKE_PROFIT_PERCENT_2=0.30  # 第二止盈（30%）
MAX_HOLDING_HOURS=24             # 最大持仓时间（小时）

# 评分配置
MIN_SIGNAL_SCORE=7.0             # 最小开仓评分
SIGNAL_EXPIRE_HOURS=1            # 信号有效期（小时）

# 监控配置
NEW_COIN_HIGH_FREQ_INTERVAL=60          # 新币高频监控间隔（秒）
NEW_COIN_NORMAL_FREQ_INTERVAL=300       # 新币普通监控间隔（秒）
NO_NEW_COIN_INTERVAL=3600               # 无新币监控间隔（秒）
```

---

## ⚠️ 安全提示

1. **保护 API 密钥**: 不要将 `.env` 文件提交到版本控制
2. **定期更新密码**: 如果使用密码认证，定期更新服务器密码
3. **备份重要数据**: 定期备份 `data/` 目录下的数据库和信号文件
4. **监控资源使用**: 定期检查容器的 CPU、内存使用情况
5. **日志轮转**: 配置日志轮转避免磁盘空间占满

---

## 📞 技术支持

如遇到问题，请提供以下信息：

1. 部署日志：`deployment_log.txt`
2. 容器日志：`docker logs short-selling-system`
3. 系统信息：`uname -a`
4. Docker 版本：`docker --version`
5. 错误截图或详细描述

---

**文档版本**: v1.0  
**最后更新**: 2026-03-10  
**适用版本**: short_selling_system v0.7.0
