# 自动化打包部署标准化文档

## 📋 文档说明

本文档提供了一套完整的自动化打包、上传、Docker 部署标准化流程，适用于任何需要部署到远程服务器的项目。

**使用场景：**
- 新项目首次部署到服务器
- 现有项目更新代码
- 需要快速重建 Docker 容器
- 批量部署多个服务器

---

## 🚀 一、准备工作

### 1.1 服务器信息配置

在本地项目根目录创建 `.deploy_config` 文件：

```bash
# 服务器配置
SERVER_IP="43.156.242.184"
SERVER_USER="root"
SERVER_PASSWORD="your_password_here"
SERVER_PROJECT_PATH="/root/your-project-name"

# Docker 配置
DOCKER_CONTAINER_NAME="your-container-name"
DOCKER_IMAGE_NAME="your-image-name:latest"

# 项目配置
PROJECT_NAME="your-project-name"
DEPLOY_PACKAGE_NAME="deployment_package.tar.gz"
```

### 1.2 必要文件检查

确保项目根目录包含以下文件：

```
your-project/
├── Dockerfile              # Docker 镜像构建文件
├── docker-compose.yml      # Docker Compose 配置
├── deploy.sh              # 部署脚本
├── requirements.txt       # Python 依赖（或其他语言的依赖文件）
├── .env                   # 环境变量文件
└── .deploy_config         # 部署配置（刚创建的）
```

---

## 📦 二、自动化打包流程

### 2.1 创建打包脚本

在项目根目录创建 `auto_package.sh`：

```bash
#!/bin/bash

# ============================================
# 自动化打包脚本 - 适用于任何项目
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

### 2.2 执行打包

```bash
# 添加执行权限
chmod +x auto_package.sh

# 执行打包
./auto_package.sh
```

---

## 📤 三、上传到服务器

### 3.1 解决 Trae SSH 通信问题

**重要：** 在 Trae IDE 中执行 SSH/SCP 命令时，需要使用以下方法避免 `trae-sandbox` 错误：

#### 方法 1：使用 SSH 选项（推荐）

```bash
# 添加 StrictHostKeyChecking=no 避免首次连接提示
trae-sandbox 'scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null deployment_package.tar.gz root@43.156.242.184:/root/'
```

#### 方法 2：使用 SSH 配置文件

创建 `~/.ssh/config`：

```
Host 43.156.242.*
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
```

#### 方法 3：使用 expect 脚本自动化

创建 `scp_with_expect.sh`：

```bash
#!/usr/bin/expect -f

set server_ip [lindex $argv 0]
set username [lindex $argv 1]
set password [lindex $argv 2]
set file [lindex $argv 3]
set remote_path [lindex $argv 4]

spawn scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $file $username@$server_ip:$remote_path
expect {
    "*assword:" {
        send "$password\r"
    }
    "*yes/no*" {
        send "yes\r"
        exp_continue
    }
}
expect eof
```

### 3.2 完整的上传脚本

创建 `upload_to_server.sh`：

```bash
#!/bin/bash

# ============================================
# 上传脚本 - 支持 Trae IDE
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

# 方法 1：直接使用 scp（适用于已配置 SSH 密钥的情况）
if [ -f ~/.ssh/id_rsa.pub ]; then
    echo "🔑 使用 SSH 密钥认证..."
    scp -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "$DEPLOY_PACKAGE_NAME" \
        "$SERVER_USER@$SERVER_IP:/root/"
    echo "✅ 上传成功（SSH 密钥）"
else
    # 方法 2：使用密码（需要 expect）
    echo "🔑 使用密码认证..."
    
    if ! command -v expect &> /dev/null; then
        echo "❌ 错误：expect 未安装，请安装或使用 SSH 密钥"
        echo "安装命令：brew install expect (macOS) 或 apt-get install expect (Linux)"
        exit 1
    fi
    
    # 使用 expect 脚本
    expect << EOF
set timeout 60
spawn scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $DEPLOY_PACKAGE_NAME $SERVER_USER@$SERVER_IP:/root/
expect {
    "*assword:" {
        send "$SERVER_PASSWORD\r"
    }
    "*yes/no*" {
        send "yes\r"
        exp_continue
    }
}
expect eof
EOF
    
    echo "✅ 上传成功（密码认证）"
fi

echo "============================================="
```

### 3.3 执行上传

```bash
# 添加执行权限
chmod +x upload_to_server.sh

# 执行上传
./upload_to_server.sh
```

---

## 🐳 四、Docker 部署流程

### 4.1 服务器端部署脚本

在服务器端创建 `remote_deploy.sh`（或通过 SSH 执行）：

```bash
#!/bin/bash

# ============================================
# 服务器端 Docker 部署脚本
# ============================================

set -e

# 加载配置（从本地上传的配置文件读取）
source .deploy_config

echo "============================================="
echo "Docker 部署 - $PROJECT_NAME"
echo "============================================="

# 进入项目目录
cd "$SERVER_PROJECT_PATH" || exit 1

# 1. 停止旧容器
echo "🛑 停止旧容器..."
docker stop "$DOCKER_CONTAINER_NAME" 2>/dev/null || echo "容器不存在，跳过停止"

# 2. 删除旧容器
echo "🗑️  删除旧容器..."
docker rm "$DOCKER_CONTAINER_NAME" 2>/dev/null || echo "容器不存在，跳过删除"

# 3. 删除旧镜像（可选）
echo "🗑️  删除旧镜像..."
docker rmi "$DOCKER_IMAGE_NAME" 2>/dev/null || echo "镜像不存在，跳过删除"

# 4. 解压部署包
echo "📦 解压部署包..."
cd /root
tar -xzf "$DEPLOY_PACKAGE_NAME" -C "$PROJECT_NAME"
cd "$PROJECT_NAME"

# 5. 设置权限
echo "🔐 设置文件权限..."
chmod +x deploy.sh 2>/dev/null || true
chmod 600 .env 2>/dev/null || true

# 6. 构建新镜像
echo "🏗️  构建 Docker 镜像..."
docker-compose build --no-cache

# 7. 启动新容器
echo "🚀 启动新容器..."
docker-compose up -d

# 8. 检查容器状态
echo "📊 检查容器状态..."
sleep 3  # 等待容器启动
docker ps -f name="$DOCKER_CONTAINER_NAME"

# 9. 查看日志
echo "📋 最近日志:"
docker logs --tail 20 "$DOCKER_CONTAINER_NAME"

echo "============================================="
echo "✅ 部署完成！"
echo "============================================="
```

### 4.2 一键部署脚本（本地执行）

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

# 使用 SSH 执行远程部署命令
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

# 步骤 4：验证
echo "✅ 步骤 4/4: 验证部署..."
ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" \
    "docker ps -f name=$DOCKER_CONTAINER_NAME --format '容器 {{.Names}} 状态：{{.Status}}'"

echo "============================================="
echo "🎉 一键部署完成！"
echo "============================================="
```

---

## 🔧 五、Docker 容器管理

### 5.1 常用管理命令

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

### 5.2 快速命令参考

```bash
# 查看容器状态
ssh root@SERVER_IP "docker ps -f name=CONTAINER_NAME"

# 查看实时日志
ssh root@SERVER_IP "docker logs -f CONTAINER_NAME"

# 重启容器
ssh root@SERVER_IP "docker restart CONTAINER_NAME"

# 停止容器
ssh root@SERVER_IP "docker stop CONTAINER_NAME"

# 启动容器
ssh root@SERVER_IP "docker start CONTAINER_NAME"

# 删除容器
ssh root@SERVER_IP "docker stop CONTAINER_NAME && docker rm CONTAINER_NAME"

# 删除镜像
ssh root@SERVER_IP "docker rmi IMAGE_NAME"

# 重新构建
ssh root@SERVER_IP "cd PROJECT_PATH && docker-compose build --no-cache && docker-compose up -d"

# 进入容器
ssh root@SERVER_IP "docker exec -it CONTAINER_NAME /bin/bash"

# 查看资源使用
ssh root@SERVER_IP "docker stats CONTAINER_NAME"

# 清理悬空镜像
ssh root@SERVER_IP "docker image prune -f"
```

---

## ⚠️ 六、Trae IDE SSH 通信注意事项

### 6.1 常见问题：trae-sandbox 错误

**问题描述：**
在 Trae IDE 中执行 SSH/SCP 命令时，经常出现 `trae-sandbox` 相关的错误。

**解决方案：**

#### 方案 1：添加 SSH 选项（最常用）

```bash
# 错误写法
scp file.txt root@43.156.242.184:/root/

# 正确写法
trae-sandbox 'scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null file.txt root@43.156.242.184:/root/'
```

#### 方案 2：使用 SSH 配置文件

编辑 `~/.ssh/config`：

```
Host *
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
    ServerAliveInterval 60
    ServerAliveCountMax 3
```

#### 方案 3：在命令前添加 trae-sandbox

```bash
# SSH 连接
trae-sandbox 'ssh -o StrictHostKeyChecking=no root@43.156.242.184 "docker ps"'

# SCP 上传
trae-sandbox 'scp -o StrictHostKeyChecking=no file.txt root@43.156.242.184:/root/'

# 多命令执行
trae-sandbox 'ssh -o StrictHostKeyChecking=no root@43.156.242.184 "cd /root && ls -la"'
```

### 6.2 SSH 密钥认证配置（强烈推荐）

**为什么要配置 SSH 密钥？**
- ✅ 免密码登录，更安全
- ✅ 自动化部署不需要输入密码
- ✅ 避免密码泄露风险
- ✅ 部署速度更快

#### 步骤 1：生成 SSH 密钥

```bash
# 生成 ED25519 密钥（推荐，更安全）
ssh-keygen -t ed25519 -C "your_email@example.com"

# 或者使用 RSA 密钥（兼容性更好）
ssh-keygen -t rsa -b 4096 -C "your_email@example.com"
```

**提示**：直接回车，不需要设置密码短语（passphrase）

**生成的文件**：
- 私钥：`~/.ssh/id_ed25519`（保密，不要给别人）
- 公钥：`~/.ssh/id_ed25519.pub`（复制到服务器）

#### 步骤 2：复制公钥到服务器

```bash
# 方法 1：使用 ssh-copy-id（推荐）
ssh-copy-id -i ~/.ssh/id_ed25519.pub -o StrictHostKeyChecking=no root@43.156.242.184

# 方法 2：手动复制（如果 ssh-copy-id 不可用）
cat ~/.ssh/id_ed25519.pub | ssh -o StrictHostKeyChecking=no root@43.156.242.184 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

#### 步骤 3：测试免密登录

```bash
# 测试登录（不需要输入密码）
ssh -o StrictHostKeyChecking=no root@43.156.242.184 "echo 成功"

# 如果成功，会直接返回"成功"，不需要输入密码
```

#### 步骤 4：配置 SSH 别名（可选，但推荐）

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

**使用别名登录**：
```bash
ssh prod          # 使用简短别名
ssh production    # 使用完整别名
```

#### 验证配置成功

```bash
# 检查是否已配置 SSH 密钥
ssh -o StrictHostKeyChecking=no root@43.156.242.184 "echo 成功"

# 如果不需要输入密码就返回"成功"，说明已配置成功
```

#### 故障排查

**问题**：配置后仍然需要密码

**解决方案**：
```bash
# 1. 检查公钥是否正确复制到服务器
ssh root@43.156.242.184 "cat ~/.ssh/authorized_keys"

# 2. 检查权限（必须严格）
ssh root@43.156.242.184 "chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"

# 3. 检查 SSH 日志
ssh -v -o StrictHostKeyChecking=no root@43.156.242.184
```

### 6.3 使用 expect 处理密码

如果必须使用密码认证，创建通用 expect 脚本：

```bash
#!/usr/bin/expect -f

# generic_ssh_command.expect
# 用法：./generic_ssh_command.expect <server_ip> <username> <password> <command>

set timeout 60
set server_ip [lindex $argv 0]
set username [lindex $argv 1]
set password [lindex $argv 2]
set command [lindex $argv 3]

spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $username@$server_ip "$command"
expect {
    "*assword:" {
        send "$password\r"
    }
    "*yes/no*" {
        send "yes\r"
        exp_continue
    }
}
expect eof
```

---

## 📊 七、多服务器批量部署

### 7.1 服务器列表配置

创建 `servers.list`：

```
# 服务器列表
# 格式：IP,用户名，密码，项目路径，容器名称
43.156.242.184,root,password1,/root/project1,container1
43.156.242.185,root,password2,/root/project2,container2
43.156.242.186,root,password3,/root/project3,container3
```

### 7.2 批量部署脚本

创建 `batch_deploy.sh`：

```bash
#!/bin/bash

# ============================================
# 批量部署脚本
# ============================================

set -e

SERVER_LIST="servers.list"
DEPLOY_PACKAGE="deployment_package.tar.gz"

if [ ! -f "$DEPLOY_PACKAGE" ]; then
    echo "❌ 部署包不存在，请先打包"
    exit 1
fi

echo "============================================="
echo "批量部署开始"
echo "============================================="

while IFS=',' read -r ip user password project_path container_name; do
    # 跳过注释行
    [[ "$ip" =~ ^#.*$ ]] && continue
    
    echo ""
    echo "============================================="
    echo "部署到服务器：$ip"
    echo "容器：$container_name"
    echo "============================================="
    
    # 1. 上传
    echo "📤 上传中..."
    expect << EOF
set timeout 60
spawn scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $DEPLOY_PACKAGE $user@$ip:/root/
expect {
    "*assword:" { send "$password\r" }
    "*yes/no*" { send "yes\r"; exp_continue }
}
expect eof
EOF
    
    # 2. 远程部署
    echo "🚀 部署中..."
    expect << EOF
set timeout 120
spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $user@$ip "
    cd /root
    rm -rf $project_path
    mkdir -p $project_path
    tar -xzf $DEPLOY_PACKAGE -C $project_path
    cd $project_path
    chmod +x deploy.sh 2>/dev/null || true
    chmod 600 .env 2>/dev/null || true
    docker stop $container_name 2>/dev/null || true
    docker rm $container_name 2>/dev/null || true
    docker-compose build --no-cache
    docker-compose up -d
    sleep 3
    docker ps -f name=$container_name
"
expect {
    "*assword:" { send "$password\r" }
    "*yes/no*" { send "yes\r"; exp_continue }
}
expect eof
EOF
    
    echo "✅ $ip 部署完成"
    
done < "$SERVER_LIST"

echo ""
echo "============================================="
echo "🎉 批量部署完成！"
echo "============================================="
```

---

## 🔍 八、故障排查

### 8.1 常见问题及解决方案

#### 问题 1：SCP 上传失败

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

#### 问题 2：Docker 构建失败

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

#### 问题 3：容器无法启动

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

#### 问题 4：trae-sandbox 错误

**症状：** `trae-sandbox: command not found` 或其他相关错误

**解决方案：**
```bash
# 方法 1：直接使用 SSH（不通过 trae-sandbox）
# 在终端中直接执行，不要在 Trae 的 RunCommand 中执行

# 方法 2：使用完整路径
/usr/bin/ssh -o StrictHostKeyChecking=no root@43.156.242.184 "command"

# 方法 3：创建别名
alias safe-ssh='ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
```

### 8.2 日志查看命令

```bash
# 查看容器日志
ssh root@43.156.242.184 "docker logs CONTAINER_NAME"

# 实时查看日志
ssh root@43.156.242.184 "docker logs -f CONTAINER_NAME"

# 查看最近 100 行
ssh root@43.156.242.184 "docker logs --tail 100 CONTAINER_NAME"

# 查看特定时间
ssh root@43.156.242.184 "docker logs --since 2024-01-01 CONTAINER_NAME"

# 查看 Docker 服务日志
ssh root@43.156.242.184 "journalctl -u docker -f"
```

---

## 📝 九、最佳实践建议

### 9.1 安全建议

1. **使用 SSH 密钥认证**，避免密码泄露
2. **定期更新密码**和 SSH 密钥
3. **限制服务器访问权限**（防火墙、安全组）
4. **备份重要数据**再执行部署
5. **使用非 root 用户**进行日常操作

### 9.2 性能优化

1. **使用 Docker 镜像缓存**（开发环境）
2. **多阶段构建**减少镜像大小
3. **定期清理悬空镜像**
4. **限制容器资源使用**（CPU、内存）

### 9.3 版本管理

1. **使用 Git 标签**标记发布版本
2. **备份旧版本**以便回滚
3. **记录变更日志**
4. **灰度发布**到多个服务器

### 9.4 监控告警

1. **配置容器健康检查**
2. **设置日志轮转**避免磁盘占满
3. **监控资源使用**（CPU、内存、磁盘）
4. **配置告警通知**（邮件、短信、飞书）

---

## 🎯 十、快速参考卡片

### 10.1 一键部署命令

```bash
# 完整流程
./auto_package.sh && ./upload_to_server.sh && ./one_click_deploy.sh

# 或者直接使用
./one_click_deploy.sh  # 已包含打包和上传
```

### 10.2 日常维护命令

```bash
# 查看状态
ssh root@SERVER_IP "docker ps -f name=CONTAINER_NAME"

# 查看日志
ssh root@SERVER_IP "docker logs -f CONTAINER_NAME"

# 重启
ssh root@SERVER_IP "docker restart CONTAINER_NAME"

# 更新
./one_click_deploy.sh

# 回滚（需要保留旧版本）
ssh root@SERVER_IP "cd /root/project && git checkout PREVIOUS_VERSION && docker-compose restart"
```

### 10.3 故障恢复命令

```bash
# 紧急停止
ssh root@SERVER_IP "docker stop CONTAINER_NAME"

# 强制删除
ssh root@SERVER_IP "docker rm -f CONTAINER_NAME"

# 清理空间
ssh root@SERVER_IP "docker system prune -f"

# 重新启动
ssh root@SERVER_IP "docker-compose up -d"
```

---

## 📚 附录

### A. 完整文件结构示例

```
your-project/
├── .deploy_config           # 部署配置
├── auto_package.sh          # 自动打包脚本
├── upload_to_server.sh      # 上传脚本
├── one_click_deploy.sh      # 一键部署脚本
├── docker_manage.sh         # Docker 管理脚本
├── batch_deploy.sh          # 批量部署脚本
├── Dockerfile               # Docker 镜像
├── docker-compose.yml       # Docker 编排
├── deploy.sh                # 部署脚本
├── requirements.txt         # 依赖列表
├── .env                     # 环境变量
└── .gitignore              # Git 忽略文件
```

### B. 环境变量示例

```bash
# .env 示例
DEEPSEEK_API_KEY=your_api_key
LARK_WEBHOOK_URL=your_webhook
SCHEDULE_TIME=08:30
TIMEZONE=Asia/Shanghai
```

### C. Docker Compose 示例

```yaml
version: '3.8'

services:
  app:
    build: .
    image: your-image:latest
    container_name: your-container
    restart: unless-stopped
    volumes:
      - ./logs:/app/logs
    environment:
      - TZ=Asia/Shanghai
    networks:
      - app-network

networks:
  app-network:
    driver: bridge
```

---

## 📞 支持与反馈

如遇到问题，请检查：
1. 服务器网络连接
2. Docker 服务状态
3. 磁盘空间
4. 日志文件

**文档版本：** v1.0  
**最后更新：** 2026-03-09  
**维护者：** [你的名字]
