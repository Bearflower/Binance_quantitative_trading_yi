# 🚀 快速启动指南 - 5 分钟完成部署

## 前提条件

1. 服务器已安装 Docker 和 Docker Compose
2. 本地已安装 `rsync` 和 `expect`（macOS: `brew install rsync expect`）
3. 知道服务器的 IP、用户名、密码
4. **推荐**：配置 SSH 密钥认证（免密登录）

---

## 📝 零、配置 SSH 密钥认证（推荐，5 分钟）

**为什么要配置 SSH 密钥？**
- ✅ 免密码登录，更安全
- ✅ 自动化部署不需要输入密码
- ✅ 避免密码泄露风险

### 步骤 1：生成 SSH 密钥

```bash
# 生成 ED25519 密钥（推荐）
ssh-keygen -t ed25519 -C "your_email@example.com"

# 或者使用 RSA 密钥（兼容性更好）
ssh-keygen -t rsa -b 4096 -C "your_email@example.com"
```

**提示**：直接回车，不需要设置密码短语（passphrase）

### 步骤 2：复制公钥到服务器

```bash
# 方法 1：使用 ssh-copy-id（推荐）
ssh-copy-id -i /Users/yl/vscode/inspection_automation/docs/only.pem.pub root@43.156.242.184

# 方法 2：手动复制（如果 ssh-copy-id 不可用）
cat /Users/yl/vscode/inspection_automation/docs/only.pem.pub | ssh root@43.156.242.184 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

### 步骤 3：测试免密登录

```bash
# 测试登录
ssh root@43.156.242.184 "echo 成功"

# 如果成功，会直接返回"成功"，不需要输入密码
```

### 步骤 4：配置 SSH 别名（可选，但推荐）

编辑 `~/.ssh/config` 文件：

```bash
cat >> ~/.ssh/config << 'EOF'

# 生产服务器 - 免密登录
Host production
    HostName 43.156.242.184
    User root
    IdentityFile /Users/yl/vscode/inspection_automation/docs/only.pem
    IdentitiesOnly yes
    AddKeysToAgent yes
    ServerAliveInterval 60

# 简短别名
Host prod
    HostName 43.156.242.184
    User root
    IdentityFile /Users/yl/vscode/inspection_automation/docs/only.pem
    IdentitiesOnly yes
EOF
```

**使用别名登录**：
```bash
ssh prod          # 使用简短别名
ssh production    # 使用完整别名
```

---

## 第一步：创建配置文件（1 分钟）

在项目根目录创建 `.deploy_config`：

```bash
cat > .deploy_config << 'EOF'
# 服务器配置
SERVER_IP="43.156.242.184"
SERVER_USER="root"
SERVER_PASSWORD="your_password"  # 如果配置了 SSH 密钥，此项可留空
SERVER_PROJECT_PATH="/root/your-project"

# Docker 配置
DOCKER_CONTAINER_NAME="your-container"
DOCKER_IMAGE_NAME="your-image:latest"

# 项目配置
PROJECT_NAME="your-project"
DEPLOY_PACKAGE_NAME="deployment_package.tar.gz"
EOF
```

**编辑配置文件：**
```bash
vim .deploy_config
# 修改为你的实际配置
```

---

## 第二步：创建脚本（2 分钟）

### 2.1 自动打包脚本

```bash
cat > auto_package.sh << 'SCRIPT'
#!/bin/bash
set -e
source .deploy_config
DEPLOY_PACKAGE_NAME=${DEPLOY_PACKAGE_NAME:-"deployment_package.tar.gz"}
PROJECT_NAME=${PROJECT_NAME:-$(basename "$(pwd)")}

echo "📦 打包项目：$PROJECT_NAME"
TEMP_DIR="/tmp/${PROJECT_NAME}_deploy_$$"
mkdir -p "$TEMP_DIR"

rsync -av \
    --exclude='*.pyc' --exclude='__pycache__' --exclude='.git' \
    --exclude='logs/*' --exclude='data/*' --exclude='reports/*' \
    --exclude='*.tar.gz' --exclude='.DS_Store' --exclude='._*' \
    ./ "$TEMP_DIR/"

cd "$TEMP_DIR" && tar -czf "$OLDPWD/$DEPLOY_PACKAGE_NAME" .
rm -rf "$TEMP_DIR"

echo "✅ 打包完成：$DEPLOY_PACKAGE_NAME ($(ls -lh $DEPLOY_PACKAGE_NAME | awk '{print $5}'))"
SCRIPT
chmod +x auto_package.sh
```

### 2.2 一键部署脚本

```bash
cat > one_click_deploy.sh << 'SCRIPT'
#!/bin/bash
set -e
source .deploy_config

echo "🚀 一键部署到：$SERVER_IP"

# 1. 打包
./auto_package.sh

# 2. 上传
echo "📤 上传中..."
expect << EOF
set timeout 60
spawn scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $DEPLOY_PACKAGE_NAME $SERVER_USER@$SERVER_IP:/root/
expect {
    "*assword:" { send "$SERVER_PASSWORD\r" }
    "*yes/no*" { send "yes\r"; exp_continue }
}
expect eof
EOF

# 3. 远程部署
echo "🚀 部署中..."
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $SERVER_USER@$SERVER_IP << ENDSSH
cd /root
rm -rf $PROJECT_NAME
mkdir -p $PROJECT_NAME
tar -xzf $DEPLOY_PACKAGE_NAME -C $PROJECT_NAME
cd $PROJECT_NAME
chmod +x deploy.sh 2>/dev/null || true
chmod 600 .env 2>/dev/null || true
docker stop $DOCKER_CONTAINER_NAME 2>/dev/null || true
docker rm $DOCKER_CONTAINER_NAME 2>/dev/null || true
docker-compose build --no-cache
docker-compose up -d
sleep 3
echo "============================================="
docker ps -f name=$DOCKER_CONTAINER_NAME
echo "============================================="
docker logs --tail 20 $DOCKER_CONTAINER_NAME
ENDSSH

echo "🎉 部署完成！"
SCRIPT
chmod +x one_click_deploy.sh
```

---

## 第三步：执行部署（2 分钟）

```bash
# 一行命令搞定
./one_click_deploy.sh
```

---

## 常用命令速查

### 查看容器状态
```bash
ssh root@SERVER_IP "docker ps -f name=CONTAINER_NAME"
```

### 查看日志
```bash
ssh root@SERVER_IP "docker logs -f CONTAINER_NAME"
```

### 重启容器
```bash
ssh root@SERVER_IP "docker restart CONTAINER_NAME"
```

### 再次部署（更新代码）
```bash
./one_click_deploy.sh
```

---

## 故障排查

### 问题：`expect: command not found`
**解决：** `brew install expect` (macOS) 或 `apt-get install expect` (Linux)

### 问题：`Permission denied`
**解决：** 检查密码是否正确，或使用 SSH 密钥认证

### 问题：Docker 构建失败
**解决：** `ssh root@SERVER_IP "docker system prune -f"` 清理空间

---

## 进阶使用

详细文档请查看：[DEPLOYMENT_SKILL.md](DEPLOYMENT_SKILL.md)

包含：
- Trae IDE SSH 通信注意事项
- 批量部署多个服务器
- Docker 容器管理菜单
- 故障排查指南
- 最佳实践建议

---

**提示：** 如果已配置 SSH 密钥认证，部署脚本会自动使用密钥，无需输入密码。

---

## 🔐 SSH 密钥认证详解

### 已配置 SSH 密钥

如果您已经按照**步骤零**配置了 SSH 密钥，那么：

1. **自动部署无需密码**：`./one_click_deploy.sh` 会自动使用密钥认证
2. **更安全**：不需要在配置文件中存储明文密码
3. **更便捷**：一键部署，无需人工值守

### 未配置 SSH 密钥

如果您还没有配置 SSH 密钥：

1. 需要在 `.deploy_config` 中配置 `SERVER_PASSWORD`
2. 部署时会提示输入密码（通过 expect 自动输入）
3. 建议尽快配置 SSH 密钥认证

### 检查是否已配置 SSH 密钥

```bash
# 测试免密登录
ssh root@43.156.242.184 "echo 成功"

# 如果不需要输入密码就返回"成功"，说明已配置成功
```

### 相关文档

- [SSH 密钥配置完整指南](README.md#ssh-密钥配置指南)
- [故障排查](README.md#故障排查)
