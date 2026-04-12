#!/bin/bash

# 从本地检查服务器 SSH 免密登录状态
# 用于验证 SSH 密钥是否仍然有效

# 配置
SSH_HOST="43.156.242.184"
SSH_USER="root"
SSH_IDENTITY_FILE="$HOME/.ssh/id_ed25519"

echo "======================================"
echo "SSH 免密登录状态检查"
echo "======================================"
echo ""

# 检查本地私钥是否存在
if [[ ! -f "$SSH_IDENTITY_FILE" ]]; then
    echo "❌ 错误：本地 SSH 私钥文件不存在：$SSH_IDENTITY_FILE"
    echo ""
    echo "解决方案："
    echo "1. 检查 ~/.ssh/ 目录是否存在密钥文件"
    echo "2. 如果密钥已删除，需要重新生成并配置到服务器"
    exit 1
fi

echo "✅ 本地 SSH 私钥文件存在"
echo ""

# 测试 SSH 免密登录
echo "正在测试 SSH 免密登录..."
if ssh -i "$SSH_IDENTITY_FILE" \
       -o StrictHostKeyChecking=no \
       -o BatchMode=yes \
       -o ConnectTimeout=10 \
       "$SSH_USER@$SSH_HOST" \
       "echo 'SSH 连接测试成功'" 2>/dev/null; then
    
    echo "✅ SSH 免密登录正常"
    echo ""
    
    # 检查服务器端配置
    echo "检查服务器端 SSH 配置..."
    
    # 检查 /root 目录权限
    ROOT_PERMS=$(ssh -i "$SSH_IDENTITY_FILE" \
                   -o StrictHostKeyChecking=no \
                   -o BatchMode=yes \
                   "$SSH_USER@$SSH_HOST" \
                   "stat -c '%a %U:%G' /root" 2>/dev/null || echo "unknown")
    
    if [[ "$ROOT_PERMS" == "700 root:root" ]]; then
        echo "✅ 服务器 /root 目录权限正确：$ROOT_PERMS"
    else
        echo "❌ 服务器 /root 目录权限不正确：$ROOT_PERMS (应该是 700 root:root)"
    fi
    
    # 检查 authorized_keys 文件
    AUTH_KEYS_FILE="$SSH_USER@$SSH_HOST:~/.ssh/authorized_keys"
    AUTH_KEYS_COUNT=$(ssh -i "$SSH_IDENTITY_FILE" \
                         -o StrictHostKeyChecking=no \
                         -o BatchMode=yes \
                         "$SSH_USER@$SSH_HOST" \
                         "wc -l < ~/.ssh/authorized_keys" 2>/dev/null || echo "0")
    
    if [[ "$AUTH_KEYS_COUNT" -gt 0 ]]; then
        echo "✅ authorized_keys 文件正常 ($AUTH_KEYS_COUNT 个密钥)"
    else
        echo "❌ authorized_keys 文件为空或不存在"
    fi
    
    # 检查 .ssh 目录权限
    SSH_DIR_PERMS=$(ssh -i "$SSH_IDENTITY_FILE" \
                       -o StrictHostKeyChecking=no \
                       -o BatchMode=yes \
                       "$SSH_USER@$SSH_HOST" \
                       "stat -c '%a' ~/.ssh" 2>/dev/null || echo "unknown")
    
    if [[ "$SSH_DIR_PERMS" == "700" ]]; then
        echo "✅ ~/.ssh 目录权限正确：$SSH_DIR_PERMS"
    else
        echo "❌ ~/.ssh 目录权限不正确：$SSH_DIR_PERMS (应该是 700)"
    fi
    
    echo ""
    echo "======================================"
    echo "SSH 状态：✅ 正常"
    echo "======================================"
    exit 0
    
else
    echo "❌ SSH 免密登录失败！"
    echo ""
    echo "可能的原因："
    echo "1. SSH 密钥已失效或过期"
    echo "2. 服务器 authorized_keys 被修改"
    echo "3. /root 或 ~/.ssh 目录权限被修改"
    echo "4. SSH 服务配置变更"
    echo ""
    echo "故障排查步骤："
    echo "1. 检查本地密钥：ls -la ~/.ssh/id_ed25519"
    echo "2. 手动测试连接：ssh -i ~/.ssh/id_ed25519 root@$SSH_HOST"
    echo "3. 检查服务器权限：ssh root@$SSH_HOST 'stat -c \"%a %U:%G\" /root ~/.ssh'"
    echo "4. 检查 authorized_keys: ssh root@$SSH_HOST 'cat ~/.ssh/authorized_keys'"
    echo ""
    echo "======================================"
    echo "SSH 状态：❌ 异常"
    echo "======================================"
    exit 1
fi
