#!/bin/bash

# ============================================
# 上传脚本 - Common Service
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
    echo "2. 复制公钥到服务器：ssh-copy-id -i /Users/yl/vscode/inspection_automation/docs/only.pem.pub root@$SERVER_IP"
    echo "3. 测试免密登录：ssh root@$SERVER_IP 'echo 成功'"
    exit 1
fi

echo "============================================="
