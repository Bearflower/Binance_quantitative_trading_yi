#!/bin/bash

# ============================================
# V2.4 上传脚本 - 使用 SSH 密钥认证
# ============================================

set -e

# 加载配置
source .deploy_config

DEPLOY_PACKAGE_NAME=${DEPLOY_PACKAGE_NAME:-"deployment_package.tar.gz"}

echo "============================================="
echo "上传 V2.4 到服务器：$SERVER_IP"
echo "============================================="

# 检查压缩包是否存在
if [ ! -f "$DEPLOY_PACKAGE_NAME" ]; then
    echo "❌ 错误：压缩包不存在，请先执行打包"
    exit 1
fi

# 使用 SSH 密钥认证
echo "🔑 使用 SSH 密钥认证..."

# 测试 SSH 密钥是否可用
if ssh -o StrictHostKeyChecking=no -o BatchMode=yes -i "$SSH_KEY_FILE" "$SERVER_USER@$SERVER_IP" "echo 密钥可用" 2>/dev/null; then
    echo "✅ SSH 密钥可用，开始上传..."
    
    scp -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -i "$SSH_KEY_FILE" \
        "$DEPLOY_PACKAGE_NAME" \
        "$SERVER_USER@$SERVER_IP:/root/"
    
    echo "✅ 上传成功（SSH 密钥）"
else
    echo "❌ 错误：SSH 密钥不可用"
    exit 1
fi

echo "============================================="
