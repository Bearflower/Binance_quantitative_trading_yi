#!/bin/bash

# SSH 密钥自动修复脚本
# 当 SSH 免密登录失效时，使用密码修复

set -e

SERVER_IP="43.156.242.184"
USERNAME="root"
PASSWORD="v3U,XZy!b5A2w@R"
SSH_KEY_FILE="/Users/yl/vscode/inspection_automation/docs/only.pem"
SSH_KEY_PUB_FILE="/Users/yl/vscode/inspection_automation/docs/only.pem.pub"

echo "=========================================="
echo "SSH 密钥自动修复脚本"
echo "=========================================="
echo ""

# 检查本地密钥文件是否存在
if [[ ! -f "$SSH_KEY_FILE" ]]; then
    echo "❌ 错误：SSH 私钥文件不存在：$SSH_KEY_FILE"
    echo "请先创建 SSH 密钥：ssh-keygen -t ed25519 -f /Users/yl/vscode/inspection_automation/docs/only.pem"
    exit 1
fi

if [[ ! -f "$SSH_KEY_PUB_FILE" ]]; then
    echo "❌ 错误：SSH 公钥文件不存在：$SSH_KEY_PUB_FILE"
    exit 1
fi

echo "✅ SSH 密钥文件存在"
echo ""

# 获取公钥内容
SSH_PUB_KEY=$(cat "$SSH_KEY_PUB_FILE")
echo "公钥：$SSH_PUB_KEY"
echo ""

# 检查 sshpass 是否可用
if ! command -v sshpass &> /dev/null; then
    echo "ℹ️  提示：sshpass 未安装，将尝试使用 expect 或手动输入密码"
    echo ""
fi

# 尝试使用 sshpass
if command -v sshpass &> /dev/null; then
    echo "步骤 1：修复服务器权限..."
    sshpass -p "$PASSWORD" ssh -o StrictHostKeyChecking=no "$USERNAME@$SERVER_IP" << 'ENDSSH'
        # 修复 /root 目录权限
        chown root:root /root
        chmod 700 /root

        # 创建 .ssh 目录（如果不存在）
        mkdir -p ~/.ssh
        chmod 700 ~/.ssh

        # 修复 authorized_keys 文件权限
        if [[ -f ~/.ssh/authorized_keys ]]; then
            chmod 600 ~/.ssh/authorized_keys
        fi

        echo "✅ 权限修复完成"
ENDSSH

    echo ""
    echo "步骤 2：重新配置 SSH 公钥..."

    # 检查公钥是否已存在于 authorized_keys
    KEY_EXISTS=$(sshpass -p "$PASSWORD" ssh -o StrictHostKeyChecking=no "$USERNAME@$SERVER_IP" "grep -F '$SSH_PUB_KEY' ~/.ssh/authorized_keys || echo 'NOT_FOUND'")

    if [[ "$KEY_EXISTS" == "NOT_FOUND" ]]; then
        echo "公钥不存在，添加到 authorized_keys..."
        sshpass -p "$PASSWORD" ssh -o StrictHostKeyChecking=no "$USERNAME@$SERVER_IP" "echo '$SSH_PUB_KEY' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
        echo "✅ 公钥已添加"
    else
        echo "✅ 公钥已存在，无需重复添加"
    fi

    echo ""
    echo "步骤 3：测试免密登录..."
    if ssh -o StrictHostKeyChecking=no -o BatchMode=yes "$USERNAME@$SERVER_IP" "echo '✅ SSH 免密登录成功！'" 2>/dev/null; then
        echo ""
        echo "=========================================="
        echo "✅ SSH 密钥修复成功！"
        echo "=========================================="
        exit 0
    else
        echo ""
        echo "❌ SSH 免密登录测试失败"
        exit 1
    fi
else
    # 如果没有 sshpass，使用交互式方式
    echo "=========================================="
    echo "手动修复步骤（请按顺序执行）："
    echo "=========================================="
    echo ""
    echo "1. 修复服务器权限："
    echo "   ssh $USERNAME@$SERVER_IP"
    echo "   chown root:root /root && chmod 700 /root"
    echo "   chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
    echo ""
    echo "2. 添加公钥（如果不存在）："
    echo "   echo '$SSH_PUB_KEY' >> ~/.ssh/authorized_keys"
    echo ""
    echo "3. 测试免密登录："
    echo "   ssh $USERNAME@$SERVER_IP 'echo success'"
    echo ""

    # 尝试启动 ssh 会话（用户手动操作）
    echo "正在启动 SSH 会话..."
    ssh -o StrictHostKeyChecking=no "$USERNAME@$SERVER_IP"
fi
