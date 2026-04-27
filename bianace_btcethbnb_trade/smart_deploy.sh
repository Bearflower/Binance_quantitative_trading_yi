#!/bin/bash

# ============================================
# 智能部署脚本 - 自动检测网络并部署
# ============================================

SERVER_IP="43.156.242.184"
MAX_RETRIES=10
RETRY_DELAY=30

echo "============================================="
echo "🤖 智能部署 - 自动检测网络恢复"
echo "============================================="
echo "目标服务器：$SERVER_IP"
echo "最大重试次数：$MAX_RETRIES"
echo "重试间隔：${RETRY_DELAY}秒"
echo "============================================="

# 检查网络连通性
check_network() {
    ping -c 2 -W 2 "$SERVER_IP" > /dev/null 2>&1
    return $?
}

# 检查 SSH 连接
check_ssh() {
    ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes \
        root@"$SERVER_IP" "echo 'SSH 可用'" > /dev/null 2>&1
    return $?
}

# 等待网络恢复
echo "⏳ 等待服务器网络恢复..."
retry_count=0

while [ $retry_count -lt $MAX_RETRIES ]; do
    retry_count=$((retry_count + 1))
    echo -n "[$retry_count/$MAX_RETRIES] 检查网络... "
    
    if check_network; then
        echo "✅ 网络可达"
        
        # 检查 SSH
        echo -n "检查 SSH... "
        if check_ssh; then
            echo "✅ SSH 可用"
            echo ""
            echo "🎉 网络已恢复，开始部署！"
            echo "============================================="
            
            # 执行一键部署
            ./one_click_deploy.sh
            
            # 检查部署结果
            if [ $? -eq 0 ]; then
                echo "============================================="
                echo "✅ 部署成功完成！"
                echo "============================================="
                exit 0
            else
                echo "============================================="
                echo "❌ 部署失败，请手动检查"
                echo "============================================="
                exit 1
            fi
        else
            echo "❌ SSH 不可用"
        fi
    else
        echo "❌ 网络不可达"
    fi
    
    if [ $retry_count -lt $MAX_RETRIES ]; then
        echo "⏳ 等待 ${RETRY_DELAY}秒后重试..."
        sleep $RETRY_DELAY
    fi
done

echo ""
echo "============================================="
echo "⚠️  重试次数已达上限，网络仍未恢复"
echo "============================================="
echo ""
echo "建议操作："
echo "1. 检查服务器状态"
echo "2. 手动执行：./one_click_deploy.sh"
echo ""
exit 1
