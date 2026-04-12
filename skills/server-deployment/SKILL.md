---
name: "服务器自动化部署"
description: "提供完整的 SSH 免密登录配置、项目打包、上传和 Docker 部署自动化流程。规范服务器目录结构、容器管理和进程管理。Invoke when 部署项目到远程服务器、管理 Docker 容器或需要自动化部署工作流时。"
---

# 服务器自动化部署技能（SSH 免密登录版）

## 🎯 技能说明

本技能提供了一套**以 SSH 免密登录为核心**的标准化部署流程，确保所有服务器连接都使用密钥认证，无需密码。

**核心功能：**
- ✅ **SSH 免密登录配置（必选第一步）**
- ✅ 项目自动打包
- ✅ 使用密钥自动上传到服务器
- ✅ Docker 容器部署与管理
- ✅ 一键部署流程
- ✅ **部署后自动验证** ⭐⭐⭐ (确保容器更新到最新版本)
- ✅ **多重验证机制** ⭐⭐⭐ (版本、状态、健康检查)
- ✅ **服务器目录结构规范** ⭐
- ✅ **容器和进程管理规范** ⭐
- ✅ **PostgreSQL 数据库统一部署** ⭐
- ✅ 故障排查

**适用场景：**
- 新项目首次部署到服务器
- 现有项目更新代码
- 快速重建 Docker 容器
- 批量部署多个服务器
- **需要 SSH 免密登录的所有场景**
- **服务器目录结构规范化** ⭐
- **容器和进程统一管理** ⭐
- **PostgreSQL 数据库部署和多应用共享** ⭐

---

## 📁 服务器部署规范 ⭐

### 核心原则

1. **单一目录原则**: 一个项目在 `/root` 下只能有一个子目录
   - ✅ `/root/trading_system/`
   - ❌ `/root/trading_system_v2/`
   - ❌ `/root/bianace_btcethbnb_trade/` (同一项目的不同命名)

2. **单一容器原则**: 一个项目在 Docker 中只能有一个运行中的容器实例
   - ✅ `trading_system-app` (只有一个实例)
   - ❌ `trading_system-app` + `trading_system-app-old` (同时运行)

3. **规范命名原则**: 目录和容器命名必须规范、清晰
   - ✅ `trading_system`
   - ❌ `trading`, `ts`, `project1`

4. **环境隔离原则**: 开发环境和生产环境严格隔离

### 目录结构示例

```
/root/
├── database/                 # ✅ 数据库目录 (统一管理)
│   └── postgres/             # PostgreSQL 数据库
│       ├── docker-compose.yml
│       ├── init-scripts/     # 初始化脚本
│       ├── scripts/          # 运维脚本
│       └── backups/          # 备份文件
│
├── trading_system/           # ✅ 项目名称 (单一子目录)
│   ├── src/                  # 源代码
│   ├── config/               # 配置文件
│   ├── logs/                 # 日志文件
│   ├── data/                 # 数据文件
│   ├── docker-compose.yml    # Docker 编排
│   ├── Dockerfile            # Docker 镜像
│   └── .env                  # 环境变量
│
├── other_project/            # ✅ 其他项目 (独立子目录)
│   └── ...
│
└── backup/                   # ✅ 备份目录
    └── trading_system_backup_20260320.tar.gz
```

### PostgreSQL 数据库部署规范 ⭐

**核心原则：**
1. **统一数据库**: 所有项目共享一个 PostgreSQL 实例，使用 Schema 隔离
2. **独立部署**: PostgreSQL 容器独立于应用容器，便于管理和备份
3. **Schema 隔离**: 每个项目使用独立的 Schema，权限分离
4. **自动备份**: 配置定时备份任务，确保数据安全

**部署流程：**
```bash
# 1. 创建 PostgreSQL 目录
mkdir -p /root/database/postgres/{init-scripts,scripts,backups}

# 2. 部署 PostgreSQL 容器
cd /root/database/postgres
docker-compose up -d

# 3. 初始化数据库（创建 Schema 和用户）
docker exec -i postgres-db psql -U trading_user -d trading_platform < init-scripts/01-create-schema.sql

# 4. 配置定时备份
cat >> /etc/crontab << 'EOF'
0 2 * * * root cd /root/database/postgres && ./scripts/backup-postgres.sh >> /var/log/postgres_backup.log 2>&1
EOF
```

**应用连接配置：**
```bash
# .env 文件配置示例
DATABASE_URL=postgresql://user:password@postgres:5432/trading_platform?schema=schema_name
```

**注意事项：**
- ✅ PostgreSQL 容器应加入统一网络（trading-network）
- ✅ 应用容器通过 Docker 网络访问 PostgreSQL（使用服务名 `postgres`）
- ✅ 每个项目使用独立的用户和 Schema
- ✅ 定期备份数据库（建议每日凌晨 2 点）
- ✅ 监控数据库资源使用（CPU、内存、磁盘）

### 容器管理示例

```bash
# ✅ 正确：只有一个容器在运行
docker ps
CONTAINER ID   NAMES
abc123         trading_system-app

# ❌ 错误：同一项目的多个容器在运行
docker ps
CONTAINER ID   NAMES
abc123         trading_system-app
def456         trading_system-app-old  # 不应该运行旧版本
```

---

## 📖 标准部署流程

### 第一步：项目目录准备

**确保服务器上的目录结构符合规范**:

```bash
# 1. 检查 /root 下的目录
ssh root@43.156.242.184 "ls -la /root/"

# 2. 确认只有一个项目目录
# ✅ 正确：/root/trading_system/
# ❌ 错误：/root/trading_system_v2/, /root/bianace_btcethbnb_trade/

# 3. 如果有多个目录，清理旧目录
ssh root@43.156.242.184 "rm -rf /root/bianace_btcethbnb_trade"
ssh root@43.156.242.184 "rm -rf /root/trading_system_v2"

# 4. 创建备份目录
ssh root@43.156.242.184 "mkdir -p /root/backup"
```

### 第二步：配置 SSH 免密登录（必须，5 分钟）

**这是部署的前提条件，必须先完成！**

#### 1.1 生成 SSH 密钥

```bash
# 生成 ED25519 密钥（推荐，更安全）
ssh-keygen -t ed25519 -C "your_email@example.com"

# 或者使用 RSA 密钥（兼容性更好）
ssh-keygen -t rsa -b 4096 -C "your_email@example.com"
```

**重要提示：**
- 直接回车，**不需要设置密码短语（passphrase）**
- 生成的私钥：`~/.ssh/id_ed25519`（保密，不要给别人）
- 生成的公钥：`~/.ssh/id_ed25519.pub`（复制到服务器）

#### 1.2 复制公钥到服务器

```bash
# 方法 1：使用 ssh-copy-id（推荐）
ssh-copy-id -i ~/.ssh/id_ed25519.pub -o StrictHostKeyChecking=no root@43.156.242.184

# 方法 2：手动复制（如果 ssh-copy-id 不可用）
cat ~/.ssh/id_ed25519.pub | ssh -o StrictHostKeyChecking=no root@43.156.242.184 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

#### 1.3 测试免密登录

```bash
# 测试登录（不需要输入密码）
ssh -o StrictHostKeyChecking=no root@43.156.242.184 "echo 成功"

# 如果成功，会直接返回"成功"，不需要输入密码
```

#### 1.4 配置 SSH 别名（推荐，简化后续操作）

编辑 `~/.ssh/config` 文件：

```bash
cat >> ~/.ssh/config << 'EOF'

# 生产服务器 - 免密登录
Host production
    HostName 43.156.242.184
    User root
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    AddKeysToAgent yes
    ServerAliveInterval 60
    ServerAliveCountMax 3

# 简短别名
Host prod
    HostName 43.156.242.184
    User root
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
EOF
```

**使用别名登录：**
```bash
ssh prod          # 使用简短别名
ssh production    # 使用完整别名
```

#### 1.5 验证配置成功

```bash
# 检查是否已配置 SSH 密钥
ssh -o StrictHostKeyChecking=no root@43.156.242.184 "echo 成功"

# 如果不需要输入密码就返回"成功"，说明已配置成功
```

**如果失败，查看故障排查章节。**

---

### 第三步：创建项目配置文件

在项目根目录创建 `.deploy_config` 文件：

```bash
cat > .deploy_config << 'EOF'
# 服务器配置
SERVER_IP="43.156.242.184"
SERVER_USER="root"
SERVER_PROJECT_PATH="/root/your-project-name"

# Docker 配置
DOCKER_CONTAINER_NAME="your-container-name"
DOCKER_IMAGE_NAME="your-image-name:latest"

# 项目配置
PROJECT_NAME="your-project-name"
DEPLOY_PACKAGE_NAME="deployment_package.tar.gz"
EOF
```

**必要文件检查：**

确保项目根目录包含以下文件：
```
your-project/
├── Dockerfile              # Docker 镜像构建文件
├── docker-compose.yml      # Docker Compose 配置
├── deploy.sh              # 部署脚本
├── requirements.txt       # Python 依赖（或其他语言的依赖文件）
├── .env                   # 环境变量文件
└── .deploy_config         # 部署配置
```

### PostgreSQL 数据库配置 ⭐

**如果项目使用 PostgreSQL 数据库，需要额外配置：**

1. **更新 .env 文件**：
```bash
# PostgreSQL 连接字符串
DATABASE_URL=postgresql://user:password@postgres:5432/trading_platform?schema=schema_name
```

2. **更新 docker-compose.yml**：
```yaml
services:
  app:
    # ...
    depends_on:
      - postgres
    networks:
      - trading-network
    environment:
      - DATABASE_URL=postgresql://user:password@postgres:5432/trading_platform?schema=schema_name

networks:
  trading-network:
    external: true  # 使用外部网络（PostgreSQL 已创建）
```

3. **安装数据库驱动**：
```bash
# Python 项目
pip install psycopg2-binary

# 或者异步驱动
pip install asyncpg
```

4. **数据库初始化**：
- 如果是新项目，PostgreSQL 会自动创建表结构
- 如果是迁移项目，需要从旧数据库导出数据并导入 PostgreSQL

---

### 第四步：创建自动打包脚本

在项目根目录创建 `auto_package.sh`：

```bash
#!/bin/bash

# ============================================
# 自动化打包脚本
# ============================================

set -e  # 遇到错误立即退出

# 加载配置
if [ -f ".deploy_config" ]; then
    source .deploy_config
    echo "✅ 已加载部署配置"
else
    echo "❌ 错误：.deploy_config 文件不存在"
    exit 1
fi

# 设置默认值
DEPLOY_PACKAGE_NAME=${DEPLOY_PACKAGE_NAME:-"deployment_package.tar.gz"}
PROJECT_NAME=${PROJECT_NAME:-$(basename "$(pwd)")}

echo "============================================="
echo "开始打包项目：$PROJECT_NAME"
echo "============================================="

# 创建临时目录
TEMP_DIR="/tmp/${PROJECT_NAME}_deploy_$$"
echo "📁 创建临时目录：$TEMP_DIR"
mkdir -p "$TEMP_DIR"

# 复制项目文件（排除不需要的文件）
echo "📋 复制项目文件..."
rsync -av \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='logs/*' \
    --exclude='data/*' \
    --exclude='reports/*' \
    --exclude='*.tar.gz' \
    --exclude='.DS_Store' \
    --exclude='._*' \
    --exclude='node_modules/*' \
    --exclude='.env.local' \
    --exclude='.trae/*' \
    ./ "$TEMP_DIR/"

# 创建压缩包
echo "📦 创建压缩包..."
cd "$TEMP_DIR"
tar -czf "$OLDPWD/$DEPLOY_PACKAGE_NAME" .

# 清理临时目录
cd "$OLDPWD"
rm -rf "$TEMP_DIR"

# 显示结果
PACKAGE_SIZE=$(ls -lh "$DEPLOY_PACKAGE_NAME" | awk '{print $5}')
echo "============================================="
echo "✅ 打包完成！"
echo "📦 压缩包：$DEPLOY_PACKAGE_NAME"
echo "📊 大小：$PACKAGE_SIZE"
echo "============================================="
```

**执行打包：**
```bash
chmod +x auto_package.sh
./auto_package.sh
```

---

### 第五步：创建上传脚本（使用 SSH 密钥）

创建 `upload_to_server.sh`：

```bash
#!/bin/bash

# ============================================
# 上传脚本 - 使用 SSH 密钥认证
# ============================================

set -e

# 加载配置
source .deploy_config

echo "============================================="
echo "上传到服务器：$SERVER_IP"
echo "============================================="

# 检查压缩包是否存在
if [ ! -f "$DEPLOY_PACKAGE_NAME" ]; then
    echo "❌ 错误：压缩包不存在，请先执行打包"
    exit 1
fi

# 使用 SSH 密钥认证（推荐）
echo "🔑 使用 SSH 密钥认证..."

# 测试 SSH 密钥是否可用
if ssh -o StrictHostKeyChecking=no -o BatchMode=yes "$SERVER_USER@$SERVER_IP" "echo 密钥可用" 2>/dev/null; then
    echo "✅ SSH 密钥可用，开始上传..."
    
    scp -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "$DEPLOY_PACKAGE_NAME" \
        "$SERVER_USER@$SERVER_IP:/root/"
    
    echo "✅ 上传成功（SSH 密钥）"
else
    echo "❌ 错误：SSH 密钥不可用，请先配置免密登录"
    echo ""
    echo "请执行以下步骤："
    echo "1. 生成 SSH 密钥：ssh-keygen -t ed25519 -C 'your_email@example.com'"
    echo "2. 复制公钥到服务器：ssh-copy-id -i ~/.ssh/id_ed25519.pub root@$SERVER_IP"
    echo "3. 测试免密登录：ssh root@$SERVER_IP 'echo 成功'"
    exit 1
fi

echo "============================================="
```

**执行上传：**
```bash
chmod +x upload_to_server.sh
./upload_to_server.sh
```

---

### 第六步：创建一键部署脚本

创建 `one_click_deploy.sh`：

```bash
#!/bin/bash

# ============================================
# 一键部署脚本 - 打包 + 上传 + 部署
# ============================================

set -e

# 加载配置
source .deploy_config

echo "============================================="
echo "一键部署 - $PROJECT_NAME"
echo "目标服务器：$SERVER_IP"
echo "============================================="

# 步骤 1：打包
echo "📦 步骤 1/4: 打包项目..."
./auto_package.sh

# 步骤 2：上传
echo "📤 步骤 2/4: 上传到服务器..."
./upload_to_server.sh

# 步骤 3：远程部署
echo "🚀 步骤 3/4: 远程部署..."

# 使用 SSH 执行远程部署命令（使用密钥认证）
ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" << 'ENDSSH'
    
# 在服务器上执行的命令
set -e

# 加载配置（需要从本地上传）
cd /root/$PROJECT_NAME

# 停止并删除旧容器
docker stop $DOCKER_CONTAINER_NAME 2>/dev/null || true
docker rm $DOCKER_CONTAINER_NAME 2>/dev/null || true

# 解压新包
cd /root
tar -xzf $DEPLOY_PACKAGE_NAME -C $PROJECT_NAME
cd $PROJECT_NAME

# 设置权限
chmod +x deploy.sh 2>/dev/null || true
chmod 600 .env 2>/dev/null || true

# 构建并启动
docker-compose build --no-cache
docker-compose up -d

# 等待启动
sleep 3

# 显示状态
echo "============================================="
echo "容器状态:"
docker ps -f name=$DOCKER_CONTAINER_NAME
echo "============================================="
echo "最近日志:"
docker logs --tail 30 $DOCKER_CONTAINER_NAME
ENDSSH

# 步骤 4：验证部署（关键步骤，确保容器更新到最新版本）⭐⭐⭐
echo "✅ 步骤 4/4: 验证部署（关键步骤，确保容器更新到最新版本）..."

# 创建验证脚本并上传
cat > /tmp/verify_deployment.sh << 'VERIFYEOF'
#!/bin/bash

DOCKER_CONTAINER_NAME="$1"
PROJECT_NAME="$2"

echo "============================================="
echo "🔍 部署验证 - 确保容器更新到最新版本"
echo "============================================="

# 1. 验证容器是否在运行
echo "1️⃣  验证容器运行状态..."
CONTAINER_STATUS=$(docker ps -f name=$DOCKER_CONTAINER_NAME --format '{{.Status}}')
if [ -z "$CONTAINER_STATUS" ]; then
    echo "❌ 容器未运行！部署失败！"
    exit 1
fi
echo "✅ 容器运行状态：$CONTAINER_STATUS"

# 2. 验证容器镜像版本（关键）⭐⭐⭐
echo ""
echo "2️⃣  验证容器镜像版本（确保是最新版本）..."
CONTAINER_IMAGE=$(docker inspect -f '{{.Config.Image}}' $DOCKER_CONTAINER_NAME 2>/dev/null)
IMAGE_CREATED=$(docker inspect -f '{{.Created}}' $DOCKER_CONTAINER_NAME 2>/dev/null)
echo "   容器镜像：$CONTAINER_IMAGE"
echo "   镜像创建时间：$IMAGE_CREATED"

# 获取本地最新镜像
LOCAL_IMAGE=$(docker images --format "{{.Repository}}:{{.Tag}}" | grep $PROJECT_NAME | head -1)
LOCAL_IMAGE_CREATED=$(docker inspect -f '{{.Created}}' $LOCAL_IMAGE 2>/dev/null)
echo "   本地最新镜像：$LOCAL_IMAGE"
echo "   本地镜像创建时间：$LOCAL_IMAGE_CREATED"

# 比较镜像创建时间
if [ "$IMAGE_CREATED" = "$LOCAL_IMAGE_CREATED" ]; then
    echo "✅ 容器使用的是最新镜像版本"
else
    echo "⚠️  警告：容器可能未使用最新镜像！"
    echo "   容器镜像创建时间：$IMAGE_CREATED"
    echo "   最新镜像创建时间：$LOCAL_IMAGE_CREATED"
fi

# 3. 验证容器健康状态
echo ""
echo "3️⃣  验证容器健康状态..."
HEALTH_STATUS=$(docker inspect -f '{{.State.Health.Status}}' $DOCKER_CONTAINER_NAME 2>/dev/null || echo "无健康检查")
if [ "$HEALTH_STATUS" = "healthy" ] || [ "$HEALTH_STATUS" = "无健康检查" ]; then
    echo "✅ 容器健康状态：$HEALTH_STATUS"
else
    echo "⚠️  容器健康状态：$HEALTH_STATUS"
fi

# 4. 验证容器端口映射
echo ""
echo "4️⃣  验证容器端口映射..."
PORT_MAPPINGS=$(docker port $DOCKER_CONTAINER_NAME 2>/dev/null)
if [ -n "$PORT_MAPPINGS" ]; then
    echo "✅ 端口映射配置："
    echo "$PORT_MAPPINGS" | sed 's/^/   /'
else
    echo "⚠️  无端口映射或容器未运行"
fi

# 5. 验证容器日志（检查是否有启动错误）
echo ""
echo "5️⃣  验证容器日志（检查启动错误）..."
ERROR_COUNT=$(docker logs --tail 100 $DOCKER_CONTAINER_NAME 2>&1 | grep -i "error\|exception\|fatal" | wc -l)
if [ "$ERROR_COUNT" -gt 0 ]; then
    echo "⚠️  发现 $ERROR_COUNT 个错误日志，请检查："
    docker logs --tail 20 $DOCKER_CONTAINER_NAME
else
    echo "✅ 未发现明显错误日志"
fi

# 6. 验证容器资源使用
echo ""
echo "6️⃣  验证容器资源使用..."
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" $DOCKER_CONTAINER_NAME

# 7. 最终验证总结
echo ""
echo "============================================="
echo "📋 验证总结"
echo "============================================="
echo "容器名称：$DOCKER_CONTAINER_NAME"
echo "运行状态：$CONTAINER_STATUS"
echo "镜像版本：$CONTAINER_IMAGE"
echo "健康状态：$HEALTH_STATUS"
echo "错误日志：$ERROR_COUNT 个"
echo "============================================="

if [ "$ERROR_COUNT" -eq 0 ] && [ -n "$CONTAINER_STATUS" ]; then
    echo "✅ 验证通过！容器已成功更新到最新版本！"
    exit 0
else
    echo "❌ 验证失败！请检查上述错误！"
    exit 1
fi
VERIFYEOF

chmod +x /tmp/verify_deployment.sh

# 上传验证脚本到服务器
scp -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    /tmp/verify_deployment.sh \
    "$SERVER_USER@$SERVER_IP:/tmp/verify_deployment.sh"

# 在服务器上执行验证
ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" \
    "bash /tmp/verify_deployment.sh '$DOCKER_CONTAINER_NAME' '$PROJECT_NAME'"

# 检查验证结果
if [ $? -eq 0 ]; then
    echo "============================================="
    echo "🎉 一键部署完成！验证通过！"
    echo "============================================="
else
    echo "============================================="
    echo "⚠️  部署完成但验证失败！请检查上述错误！"
    echo "============================================="
    exit 1
fi
```

**执行一键部署：**
```bash
chmod +x one_click_deploy.sh
./one_click_deploy.sh
```

---

## 🔍 部署后验证（关键步骤）⭐⭐⭐

**重要提示：** 部署完成后必须进行验证，确保容器已更新到最新版本！

### 验证脚本

创建 `verify_deployment.sh`：

```bash
#!/bin/bash

# ============================================
# 部署验证脚本 - 确保容器更新到最新版本
# ============================================

source .deploy_config

echo "============================================="
echo "🔍 部署验证 - 确保容器更新到最新版本"
echo "============================================="

# 在服务器上执行验证命令
ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" << ENDSSH

DOCKER_CONTAINER_NAME="$DOCKER_CONTAINER_NAME"
PROJECT_NAME="$PROJECT_NAME"

echo "============================================="
echo "验证步骤："
echo "1. 容器运行状态"
echo "2. 镜像版本验证（关键）"
echo "3. 健康状态检查"
echo "4. 端口映射验证"
echo "5. 日志错误检查"
echo "6. 资源使用检查"
echo "============================================="

# 1. 验证容器是否在运行
echo ""
echo "1️⃣  验证容器运行状态..."
CONTAINER_STATUS=$(docker ps -f name=\$DOCKER_CONTAINER_NAME --format '{{.Status}}')
if [ -z "\$CONTAINER_STATUS" ]; then
    echo "❌ 容器未运行！部署失败！"
    exit 1
fi
echo "✅ 容器运行状态：\$CONTAINER_STATUS"

# 2. 验证容器镜像版本（关键）⭐⭐⭐
echo ""
echo "2️⃣  验证容器镜像版本（确保是最新版本）..."
CONTAINER_IMAGE=\$(docker inspect -f '{{.Config.Image}}' \$DOCKER_CONTAINER_NAME 2>/dev/null)
IMAGE_CREATED=\$(docker inspect -f '{{.Created}}' \$DOCKER_CONTAINER_NAME 2>/dev/null)
echo "   容器镜像：\$CONTAINER_IMAGE"
echo "   镜像创建时间：\$IMAGE_CREATED"

# 获取服务器上最新镜像
LATEST_IMAGE=\$(docker images --format "{{.Repository}}:{{.Tag}}" | grep \$PROJECT_NAME | head -1)
LATEST_IMAGE_CREATED=\$(docker inspect -f '{{.Created}}' \$LATEST_IMAGE 2>/dev/null)
echo "   服务器最新镜像：\$LATEST_IMAGE"
echo "   最新镜像创建时间：\$LATEST_IMAGE_CREATED"

# 比较镜像创建时间
if [ "\$IMAGE_CREATED" = "\$LATEST_IMAGE_CREATED" ]; then
    echo "✅ 容器使用的是最新镜像版本"
else
    echo "⚠️  警告：容器可能未使用最新镜像！"
    echo "   容器镜像创建时间：\$IMAGE_CREATED"
    echo "   最新镜像创建时间：\$LATEST_IMAGE_CREATED"
    echo ""
    echo "   建议重新部署："
    echo "   cd /root/\$PROJECT_NAME && docker-compose down && docker-compose build --no-cache && docker-compose up -d"
fi

# 3. 验证容器健康状态
echo ""
echo "3️⃣  验证容器健康状态..."
HEALTH_STATUS=\$(docker inspect -f '{{.State.Health.Status}}' \$DOCKER_CONTAINER_NAME 2>/dev/null || echo "无健康检查")
if [ "\$HEALTH_STATUS" = "healthy" ] || [ "\$HEALTH_STATUS" = "无健康检查" ]; then
    echo "✅ 容器健康状态：\$HEALTH_STATUS"
else
    echo "⚠️  容器健康状态：\$HEALTH_STATUS"
fi

# 4. 验证容器端口映射
echo ""
echo "4️⃣  验证容器端口映射..."
PORT_MAPPINGS=\$(docker port \$DOCKER_CONTAINER_NAME 2>/dev/null)
if [ -n "\$PORT_MAPPINGS" ]; then
    echo "✅ 端口映射配置："
    echo "\$PORT_MAPPINGS" | sed 's/^/   /'
else
    echo "⚠️  无端口映射或容器未运行"
fi

# 5. 验证容器日志（检查是否有启动错误）
echo ""
echo "5️⃣  验证容器日志（检查启动错误）..."
ERROR_COUNT=\$(docker logs --tail 100 \$DOCKER_CONTAINER_NAME 2>&1 | grep -i "error\|exception\|fatal" | wc -l)
if [ "\$ERROR_COUNT" -gt 0 ]; then
    echo "⚠️  发现 \$ERROR_COUNT 个错误日志，请检查："
    docker logs --tail 20 \$DOCKER_CONTAINER_NAME
else
    echo "✅ 未发现明显错误日志"
fi

# 6. 验证容器资源使用
echo ""
echo "6️⃣  验证容器资源使用..."
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" \$DOCKER_CONTAINER_NAME

# 7. 最终验证总结
echo ""
echo "============================================="
echo "📋 验证总结"
echo "============================================="
echo "容器名称：\$DOCKER_CONTAINER_NAME"
echo "运行状态：\$CONTAINER_STATUS"
echo "镜像版本：\$CONTAINER_IMAGE"
echo "健康状态：\$HEALTH_STATUS"
echo "错误日志：\$ERROR_COUNT 个"
echo "============================================="

if [ "\$ERROR_COUNT" -eq 0 ] && [ -n "\$CONTAINER_STATUS" ]; then
    echo "✅ 验证通过！容器已成功更新到最新版本！"
    exit 0
else
    echo "❌ 验证失败！请检查上述错误！"
    exit 1
fi
ENDSSH

# 检查验证结果
if [ $? -eq 0 ]; then
    echo "============================================="
    echo "🎉 验证完成！部署成功！"
    echo "============================================="
else
    echo "============================================="
    echo "❌ 验证失败！请检查上述错误！"
    echo "============================================="
    exit 1
fi
```

**执行验证：**
```bash
chmod +x verify_deployment.sh
./verify_deployment.sh
```

### 快速验证命令

```bash
# 1. 快速检查容器状态
ssh root@SERVER_IP "docker ps -f name=CONTAINER_NAME"

# 2. 检查镜像版本
ssh root@SERVER_IP "docker inspect -f '{{.Config.Image}}' CONTAINER_NAME"

# 3. 检查镜像创建时间
ssh root@SERVER_IP "docker inspect -f '{{.Created}}' CONTAINER_NAME"

# 4. 查看最新日志
ssh root@SERVER_IP "docker logs --tail 30 CONTAINER_NAME"

# 5. 检查容器健康状态
ssh root@SERVER_IP "docker inspect -f '{{.State.Health.Status}}' CONTAINER_NAME"
```

### 验证清单 ⭐⭐⭐

部署后必须检查以下项目：

- [ ] **容器运行状态**：容器是否在运行
- [ ] **镜像版本**：容器使用的是最新镜像（检查 Created 时间）
- [ ] **健康状态**：容器健康检查是否通过
- [ ] **端口映射**：端口是否正确映射
- [ ] **日志检查**：无启动错误或异常
- [ ] **资源使用**：CPU、内存使用正常
- [ ] **功能测试**：应用功能正常

---

## 🔧 Docker 容器管理

创建 `docker_manage.sh`：

```bash
#!/bin/bash

# ============================================
# Docker 容器管理脚本
# ============================================

source .deploy_config

show_menu() {
    echo "============================================="
    echo "Docker 容器管理 - $DOCKER_CONTAINER_NAME"
    echo "============================================="
    echo "1. 查看容器状态"
    echo "2. 查看实时日志"
    echo "3. 重启容器"
    echo "4. 停止容器"
    echo "5. 启动容器"
    echo "6. 删除容器"
    echo "7. 重新构建并部署"
    echo "8. 进入容器终端"
    echo "9. 查看资源使用"
    echo "0. 退出"
    echo "============================================="
}

check_status() {
    ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" \
        "docker ps -a -f name=$DOCKER_CONTAINER_NAME"
}

view_logs() {
    ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" \
        "docker logs -f --tail 100 $DOCKER_CONTAINER_NAME"
}

restart_container() {
    echo "🔄 重启容器..."
    ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" \
        "docker restart $DOCKER_CONTAINER_NAME"
    echo "✅ 重启完成"
}

stop_container() {
    echo "🛑 停止容器..."
    ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" \
        "docker stop $DOCKER_CONTAINER_NAME"
    echo "✅ 停止完成"
}

start_container() {
    echo "🚀 启动容器..."
    ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" \
        "docker start $DOCKER_CONTAINER_NAME"
    echo "✅ 启动完成"
}

delete_container() {
    echo "⚠️  警告：此操作将删除容器！"
    read -p "确定要继续吗？(y/N): " confirm
    if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
        ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" \
            "docker stop $DOCKER_CONTAINER_NAME && docker rm $DOCKER_CONTAINER_NAME"
        echo "✅ 删除完成"
    else
        echo "❌ 操作已取消"
    fi
}

rebuild_deploy() {
    echo "🏗️  重新构建并部署..."
    ./one_click_deploy.sh
}

enter_container() {
    echo "🔌 进入容器终端..."
    ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" \
        "docker exec -it $DOCKER_CONTAINER_NAME /bin/bash"
}

view_resources() {
    echo "📊 资源使用情况:"
    ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" \
        "docker stats --no-stream $DOCKER_CONTAINER_NAME"
}

# 主循环
while true; do
    show_menu
    read -p "请选择操作 [0-9]: " choice
    
    case $choice in
        1) check_status ;;
        2) view_logs ;;
        3) restart_container ;;
        4) stop_container ;;
        5) start_container ;;
        6) delete_container ;;
        7) rebuild_deploy ;;
        8) enter_container ;;
        9) view_resources ;;
        0) echo "👋 退出"; exit 0 ;;
        *) echo "❌ 无效选择" ;;
    esac
    
    echo ""
    read -p "按回车键继续..."
done
```

---

## 🔍 故障排查

### 问题 1：SSH 密钥配置后仍然需要密码 ⭐ 重要

**症状：** 配置了 SSH 密钥，但登录时仍然提示输入密码，即使公钥已正确添加到 `~/.ssh/authorized_keys`

**常见原因：**
1. `/root` 目录的所有权不正确（**最常见**）
2. `~/.ssh` 或 `~/.ssh/authorized_keys` 权限不正确
3. 公钥内容不正确或格式有问题
4. SSH 配置限制了公钥认证

**解决方案：**

#### 1. 检查并修复 `/root` 目录所有权（**最重要**）

```bash
# 检查 /root 目录的所有权和权限
ssh root@SERVER_IP "ls -ld /root"

# ❌ 错误示例：drwxr-xr-x 27 501 games 8192 Mar 23 17:02 /root
# ✅ 正确示例：drwx------ 27 root root 8192 Mar 23 17:02 /root

# 如果所有者不是 root:root，修复它
ssh root@SERVER_IP "chown root:root /root && chmod 700 /root"

# 验证修复
ssh root@SERVER_IP "ls -ld /root"
# 应该显示：drwx------ 27 root root 8192 ... /root
```

**为什么这很重要：** SSH 对主目录的所有权和权限有严格要求。如果 `/root` 目录不是 `root:root` 所有或权限不是 `700`，SSH 会拒绝使用公钥认证，并在日志中记录：
```
Authentication refused: bad ownership or modes for directory /root
```

#### 2. 检查 SSH 密钥和 authorized_keys 权限

```bash
# 检查 .ssh 目录权限
ssh root@SERVER_IP "ls -la ~/.ssh/"

# 修复权限（必须严格）
ssh root@SERVER_IP "chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"

# 检查 authorized_keys 内容
ssh root@SERVER_IP "cat ~/.ssh/authorized_keys"
```

#### 3. 验证公钥指纹匹配

```bash
# 检查本地公钥指纹
ssh-keygen -lf ~/.ssh/id_ed25519.pub

# 检查服务器上的公钥指纹
ssh root@SERVER_IP "ssh-keygen -lf ~/.ssh/authorized_keys"

# 两个指纹必须完全相同！
```

#### 4. 使用详细模式调试

```bash
# 使用 -vvv 查看详细调试信息
ssh -vvv -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 root@SERVER_IP "echo test"

# 查看关键信息：
# - "Offering public key" - 确认正在提供正确的密钥
# - "type 51" - 服务器拒绝密钥
# - "Authenticated" - 认证成功
```

#### 5. 检查服务器 SSH 日志

```bash
# 查看最近的 SSH 认证日志
ssh root@SERVER_IP "sudo journalctl -u sshd --since '5 minutes ago' --no-pager | grep -i 'publickey\|authorized\|refused'"

# 查找关键错误：
# - "Authentication refused: bad ownership or modes"
# - "Failed publickey"
# - "Accepted publickey"
```

#### 6. 重新配置 SSH 密钥（如果以上都失败）

```bash
# 清空服务器上的 authorized_keys
ssh root@SERVER_IP "> ~/.ssh/authorized_keys"

# 重新复制公钥
ssh-copy-id -i ~/.ssh/id_ed25519.pub -o StrictHostKeyChecking=no -o PreferredAuthentications=password root@SERVER_IP

# 测试免密登录
ssh -o StrictHostKeyChecking=no -o BatchMode=yes root@SERVER_IP "echo SSH 免密登录成功！"
```

---

### 问题 2：SCP 上传失败

### 问题 2：SCP 上传失败

**症状：** `scp: Connection closed` 或 `Permission denied`

**解决方案：**
```bash
# 检查 SSH 连接
ssh -v -o StrictHostKeyChecking=no root@43.156.242.184

# 检查磁盘空间
ssh root@43.156.242.184 "df -h"

# 检查权限
ssh root@43.156.242.184 "ls -la /root/"
```

### 问题 3：Docker 构建失败

**症状：** `docker-compose build` 报错

**解决方案：**
```bash
# 清理 Docker 缓存
ssh root@43.156.242.184 "docker system prune -f"

# 检查 Docker 服务
ssh root@43.156.242.184 "systemctl status docker"

# 查看详细日志
ssh root@43.156.242.184 "docker-compose build --progress=plain"
```

### 问题 4：容器无法启动

**症状：** 容器启动后立即退出

**解决方案：**
```bash
# 查看容器日志
ssh root@43.156.242.184 "docker logs CONTAINER_NAME"

# 检查配置文件
ssh root@43.156.242.184 "cat /root/project/.env"

# 手动启动调试
ssh root@43.156.242.184 "docker-compose up"
```

---

### 问题 5：PostgreSQL 数据库连接失败 ⭐

**症状：** 应用无法连接到 PostgreSQL 数据库

**常见原因：**
1. PostgreSQL 容器未运行
2. 网络连接问题
3. 数据库配置错误
4. 用户权限不足

**解决方案：**

#### 1. 检查 PostgreSQL 容器状态
```bash
# 查看容器状态
ssh root@SERVER_IP "docker ps -f name=postgres-db"

# 如果未运行，启动它
ssh root@SERVER_IP "docker-compose -f /root/database/postgres/docker-compose.yml up -d"
```

#### 2. 检查网络连接
```bash
# 查看网络
ssh root@SERVER_IP "docker network ls"

# 检查应用容器是否在 trading-network 中
ssh root@SERVER_IP "docker network inspect trading-network"

# 如果网络不存在，创建它
ssh root@SERVER_IP "docker network create trading-network"
```

#### 3. 验证数据库配置
```bash
# 检查 .env 文件
ssh root@SERVER_IP "cat /root/project/.env | grep DATABASE_URL"

# 测试连接（从应用容器）
ssh root@SERVER_IP "docker exec -it APP_CONTAINER python -c 'import psycopg2; psycopg2.connect(\"postgresql://...\")'"
```

#### 4. 检查用户权限
```bash
# 连接到 PostgreSQL
ssh root@SERVER_IP "docker exec -it postgres-db psql -U trading_user -d trading_platform"

# 在 psql 中检查权限
\dn  # 查看 schema 列表
\du  # 查看用户列表

# 授权（如果需要）
GRANT ALL PRIVILEGES ON SCHEMA schema_name TO user_name;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA schema_name TO user_name;
```

#### 5. 查看 PostgreSQL 日志
```bash
# 查看数据库日志
ssh root@SERVER_IP "docker logs postgres-db"

# 查看应用日志
ssh root@SERVER_IP "docker logs APP_CONTAINER | grep -i database"
```

---

### 问题 6：部署后验证失败 ⭐⭐⭐ 重要

**症状：** 部署完成但验证失败，容器未更新到最新版本

**常见原因：**
1. 容器未使用最新镜像
2. 镜像构建失败
3. 容器启动失败
4. 旧容器未正确删除

**解决方案：**

#### 1. 验证镜像版本

```bash
# 检查容器使用的镜像
ssh root@SERVER_IP "docker inspect -f '{{.Config.Image}}' CONTAINER_NAME"

# 检查服务器上的最新镜像
ssh root@SERVER_IP "docker images | grep PROJECT_NAME"

# 比较镜像创建时间
ssh root@SERVER_IP "docker inspect -f '{{.Created}}' CONTAINER_NAME"
ssh root@SERVER_IP "docker inspect -f '{{.Created}}' IMAGE_ID"
```

#### 2. 强制重新部署

```bash
# 完全清理并重新部署
ssh root@SERVER_IP << 'EOF'
cd /root/PROJECT_NAME

# 停止并删除容器
docker-compose down

# 删除旧镜像
docker rmi IMAGE_NAME:latest --force

# 重新构建（不使用缓存）
docker-compose build --no-cache

# 启动新容器
docker-compose up -d

# 验证
docker ps -f name=CONTAINER_NAME
EOF
```

#### 3. 检查镜像构建日志

```bash
# 查看构建日志
ssh root@SERVER_IP "cd /root/PROJECT_NAME && docker-compose build --no-cache"

# 检查是否有构建错误
ssh root@SERVER_IP "docker images | grep PROJECT_NAME"
```

#### 4. 检查容器启动日志

```bash
# 查看完整启动日志
ssh root@SERVER_IP "docker logs CONTAINER_NAME"

# 实时查看日志
ssh root@SERVER_IP "docker logs -f CONTAINER_NAME"
```

#### 5. 手动验证步骤

```bash
# 步骤 1：确认容器状态
ssh root@SERVER_IP "docker ps -a -f name=CONTAINER_NAME"

# 步骤 2：确认镜像信息
ssh root@SERVER_IP "docker inspect CONTAINER_NAME | grep -A 5 'Config'"

# 步骤 3：确认端口映射
ssh root@SERVER_IP "docker port CONTAINER_NAME"

# 步骤 4：确认网络连接
ssh root@SERVER_IP "docker network inspect trading-network | grep -A 10 'Containers'"

# 步骤 5：测试应用功能
curl http://SERVER_IP:PORT/health
```

---

### 问题 7：PostgreSQL 性能问题 ⭐

**症状：** 数据库查询慢，应用响应延迟

**解决方案：**
```bash
# 1. 查看资源使用
ssh root@SERVER_IP "docker stats postgres-db"

# 2. 查看慢查询
ssh root@SERVER_IP "docker exec -it postgres-db psql -U trading_user -d trading_platform -c 'SELECT * FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 10;'"

# 3. 查看连接数
ssh root@SERVER_IP "docker exec -it postgres-db psql -U trading_user -d trading_platform -c 'SELECT count(*) FROM pg_stat_activity;'"

# 4. 优化配置
# 编辑 postgresql.conf（需要重启容器）
ssh root@SERVER_IP "docker exec -it postgres-db psql -U trading_user -d trading_platform -c 'SHOW shared_buffers;'"

# 5. 清理旧数据
ssh root@SERVER_IP "docker exec -it postgres-db psql -U trading_user -d trading_platform -c 'VACUUM;'"
```

---

## 📝 最佳实践建议

### 安全建议

1. **必须使用 SSH 密钥认证**，避免密码泄露
2. 定期更新 SSH 密钥
3. 限制服务器访问权限（防火墙、安全组）
4. 备份重要数据再执行部署
5. 使用非 root 用户进行日常操作

### 性能优化

1. 使用 Docker 镜像缓存（开发环境）
2. 多阶段构建减少镜像大小
3. 定期清理悬空镜像
4. 限制容器资源使用（CPU、内存）

### 版本管理

1. 使用 Git 标签标记发布版本
2. 备份旧版本以便回滚
3. 记录变更日志
4. 灰度发布到多个服务器

---

## 🎯 快速参考卡片

### 一键部署命令

```bash
# 完整流程
./auto_package.sh && ./upload_to_server.sh && ./one_click_deploy.sh

# 或者直接使用
./one_click_deploy.sh  # 已包含打包和上传
```

### 部署后验证命令 ⭐⭐⭐

```bash
# 1. 执行完整验证
./verify_deployment.sh

# 2. 快速验证容器状态
ssh root@SERVER_IP "docker ps -f name=CONTAINER_NAME"

# 3. 验证镜像版本（关键）
ssh root@SERVER_IP "docker inspect -f '{{.Config.Image}}' CONTAINER_NAME"
ssh root@SERVER_IP "docker inspect -f '{{.Created}}' CONTAINER_NAME"

# 4. 验证健康状态
ssh root@SERVER_IP "docker inspect -f '{{.State.Health.Status}}' CONTAINER_NAME"

# 5. 检查错误日志
ssh root@SERVER_IP "docker logs --tail 100 CONTAINER_NAME | grep -i error"

# 6. 验证端口映射
ssh root@SERVER_IP "docker port CONTAINER_NAME"
```

### 日常维护命令

```bash
# 查看状态
ssh root@SERVER_IP "docker ps -f name=CONTAINER_NAME"

# 查看日志
ssh root@SERVER_IP "docker logs -f CONTAINER_NAME"

# 重启
ssh root@SERVER_IP "docker restart CONTAINER_NAME"

# 更新
./one_click_deploy.sh

# 验证更新
./verify_deployment.sh
```

### 故障恢复命令

```bash
# 紧急停止
ssh root@SERVER_IP "docker stop CONTAINER_NAME"

# 强制删除
ssh root@SERVER_IP "docker rm -f CONTAINER_NAME"

# 清理空间
ssh root@SERVER_IP "docker system prune -f"

# 重新启动
ssh root@SERVER_IP "docker-compose up -d"

# 强制重新部署（验证失败时）
ssh root@SERVER_IP << 'EOF'
cd /root/PROJECT_NAME
docker-compose down
docker rmi IMAGE_NAME:latest --force
docker-compose build --no-cache
docker-compose up -d
EOF
```

---

## 🚀 5 分钟快速启动

### 前提条件

1. 服务器已安装 Docker 和 Docker Compose
2. 本地已安装 `rsync`
3. 知道服务器的 IP、用户名

### 第一步：配置 SSH 免密登录（3 分钟）

```bash
# 1. 生成 SSH 密钥
ssh-keygen -t ed25519 -C "your_email@example.com"
# 直接回车，不设置密码

# 2. 复制公钥到服务器
ssh-copy-id -i ~/.ssh/id_ed25519.pub root@43.156.242.184

# 3. 测试免密登录
ssh root@43.156.242.184 "echo 成功"
# 如果直接返回"成功"，说明配置成功
```

### 第二步：创建配置文件（1 分钟）

```bash
cat > .deploy_config << 'EOF'
SERVER_IP="43.156.242.184"
SERVER_USER="root"
SERVER_PROJECT_PATH="/root/your-project"
DOCKER_CONTAINER_NAME="your-container"
DOCKER_IMAGE_NAME="your-image:latest"
PROJECT_NAME="your-project"
DEPLOY_PACKAGE_NAME="deployment_package.tar.gz"
EOF
```

### 第三步：执行部署（1 分钟）

```bash
# 一行命令搞定（包含自动验证）
./one_click_deploy.sh

# 部署完成后会自动执行验证，确保容器更新到最新版本
```

### 第四步：验证部署（自动执行）⭐⭐⭐

```bash
# 一键部署脚本会自动执行验证，包括：
# 1. 容器运行状态
# 2. 镜像版本验证（确保是最新版本）
# 3. 健康状态检查
# 4. 端口映射验证
# 5. 日志错误检查
# 6. 资源使用检查

# 如果需要手动验证
./verify_deployment.sh
```

---

## 🔧 工具依赖

### 本地需要安装
- `rsync` - 文件同步工具
- `ssh` - SSH 客户端
- `scp` - SSH 文件传输

**macOS 安装：**
```bash
brew install rsync
```

**Linux 安装：**
```bash
apt-get install rsync  # Debian/Ubuntu
yum install rsync      # CentOS/RHEL
```

### 服务器需要安装
- Docker
- Docker Compose

---

## 🧹 清理和规范检查 ⭐

### 定期检查清单

```bash
# 1. 检查 /root 目录下的项目数量
ssh root@43.156.242.184 "ls -la /root/ | grep -E '^d'"

# 2. 检查 Docker 容器数量
ssh root@43.156.242.184 "docker ps -a"

# 3. 检查是否只有一个运行中的容器
ssh root@43.156.242.184 "docker ps"

# 4. 清理已停止的容器
ssh root@43.156.242.184 "docker container prune -f"

# 5. 清理悬空镜像
ssh root@43.156.242.184 "docker image prune -f"
```

### 常见问题处理

**问题: 多个项目目录**

```bash
# 症状
ls /root/
trading_system/
trading_system_v2/
bianace_btcethbnb_trade/

# 解决
# 1. 确认当前使用的目录
docker ps  # 确认容器使用的目录

# 2. 备份并删除旧目录
tar -czf /root/backup/old_projects.tar.gz \
    /root/trading_system_v2/ \
    /root/bianace_btcethbnb_trade/
rm -rf /root/trading_system_v2/
rm -rf /root/bianace_btcethbnb_trade/
```

**问题: 多个容器同时运行**

```bash
# 症状
docker ps
CONTAINER ID   NAMES
abc123         trading_system-app
def456         trading_system-app-old

# 解决
docker stop trading_system-app-old
docker rm trading_system-app-old
```

---

**文档版本：** v4.0（增强验证版）  
**最后更新：** 2026-04-12  
**技能类型：** 服务器部署自动化 + 规范管理 + 自动验证  
**核心增强：** 部署后自动验证机制，确保容器更新到最新版本 ⭐⭐⭐
