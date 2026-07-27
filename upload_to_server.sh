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

# 检查密钥文件是否存在
if [ ! -f "$SSH_KEY_PATH" ]; then
    echo "❌ 错误：SSH 密钥文件不存在：$SSH_KEY_PATH"
    echo ""
    echo "请检查以下内容："
    echo "1. 确认密钥文件路径是否正确"
    echo "2. 如果使用云平台密钥，请从云平台控制台下载"
    exit 1
fi

# 检查密钥文件权限
KEY_PERMISSION=$(stat -f "%OLp" "$SSH_KEY_PATH" 2>/dev/null || stat -c "%a" "$SSH_KEY_PATH" 2>/dev/null)
if [ "$KEY_PERMISSION" != "600" ]; then
    echo "⚠️  警告：密钥文件权限不正确（当前：$KEY_PERMISSION）"
    echo "   正在修复权限..."
    chmod 600 "$SSH_KEY_PATH"
    echo "✅ 权限已修复为 600"
fi

# 测试 SSH 密钥是否可用
echo "🔍 测试 SSH 连接..."
if ssh -i "$SSH_KEY_PATH" \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o BatchMode=yes \
    -o ConnectTimeout=10 \
    "$SERVER_USER@$SERVER_IP" "echo 'SSH 连接成功'" 2>/dev/null; then
    echo "✅ SSH 密钥可用，开始上传..."

    # 上传压缩包
    echo "📤 上传压缩包..."
    scp -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "$DEPLOY_PACKAGE_NAME" \
        "$SERVER_USER@$SERVER_IP:/root/"

    echo "✅ 上传成功"

    # 验证文件完整性
    echo "🔍 验证文件完整性..."
    REMOTE_SIZE=$(ssh -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "$SERVER_USER@$SERVER_IP" \
        "ls -l /root/$DEPLOY_PACKAGE_NAME | awk '{print \$5}'")

    LOCAL_SIZE=$(ls -l "$DEPLOY_PACKAGE_NAME" | awk '{print $5}')

    if [ "$REMOTE_SIZE" = "$LOCAL_SIZE" ]; then
        echo "✅ 文件大小验证通过（本地：$LOCAL_SIZE 字节，远程：$REMOTE_SIZE 字节）"
    else
        echo "⚠️  警告：文件大小不一致！"
        echo "   本地大小：$LOCAL_SIZE 字节"
        echo "   远程大小：$REMOTE_SIZE 字节"
        exit 1
    fi

else
    echo "❌ 错误：SSH 密钥不可用，请检查以下内容："
    echo ""
    echo "1. 确认密钥文件路径：$SSH_KEY_PATH"
    echo "2. 确认密钥文件权限：chmod 600 $SSH_KEY_PATH"
    echo "3. 确认密钥已绑定到服务器（云平台控制台）"
    echo "4. 测试密钥登录："
    echo "   ssh -i $SSH_KEY_PATH $SERVER_USER@$SERVER_IP 'echo 成功'"
    exit 1
fi

echo "============================================="
echo "✅ 上传完成！"
echo "============================================="
