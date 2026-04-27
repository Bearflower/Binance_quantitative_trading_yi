#!/bin/bash

# ============================================
# 部署前检查脚本
# ============================================

echo "============================================="
echo "股票形态筛选系统 - 部署前检查"
echo "============================================="
echo ""

# 加载配置
source .deploy_config

# 检查项计数
PASS=0
FAIL=0

# 检查函数
check_item() {
    local name="$1"
    local command="$2"
    
    if eval "$command" > /dev/null 2>&1; then
        echo "✅ $name"
        ((PASS++))
    else
        echo "❌ $name"
        ((FAIL++))
    fi
}

echo "1. 本地环境检查"
echo "-------------------------------------------"
check_item "Python3 已安装" "command -v python3"
check_item "rsync 已安装" "command -v rsync"
check_item "ssh 已安装" "command -v ssh"
check_item "scp 已安装" "command -v scp"
check_item "tar 已安装" "command -v tar"
echo ""

echo "2. 项目文件检查"
echo "-------------------------------------------"
check_item "daily_scan.py 存在" "test -f daily_scan.py"
check_item "feishu_push.py 存在" "test -f feishu_push.py"
check_item "config_v21_final.yaml 存在" "test -f config_v21_final.yaml"
check_item "Dockerfile 存在" "test -f Dockerfile"
check_item "docker-compose.yml 存在" "test -f docker-compose.yml"
check_item ".deploy_config 存在" "test -f .deploy_config"
check_item "auto_package.sh 存在" "test -f auto_package.sh"
check_item "upload_to_server.sh 存在" "test -f upload_to_server.sh"
check_item "one_click_deploy.sh 存在" "test -f one_click_deploy.sh"
echo ""

echo "3. 飞书配置检查"
echo "-------------------------------------------"
if grep -q "955aced6-5b07-42a6-a714-4c5f4726b003" feishu_push.py; then
    echo "✅ 飞书 webhook URL 已配置"
    ((PASS++))
else
    echo "❌ 飞书 webhook URL 未配置"
    ((FAIL++))
fi
echo ""

echo "4. SSH 免密登录检查"
echo "-------------------------------------------"
echo "服务器：$SERVER_IP"
echo "用户：$SERVER_USER"
echo ""

# 检查 SSH 密钥
if [ -f /Users/yl/vscode/inspection_automation/docs/only.pem.pub ]; then
    echo "✅ SSH 密钥文件存在 (/Users/yl/vscode/inspection_automation/docs/only.pem.pub)"
    ((PASS++))
else
    echo "⚠️  未找到 SSH 密钥文件 (/Users/yl/vscode/inspection_automation/docs/only.pem.pub)"
    echo "   请执行：ssh-keygen -t ed25519 -C 'your_email@example.com'"
    ((FAIL++))
fi

# 测试免密登录
echo ""
echo "测试 SSH 免密登录..."
if ssh -o StrictHostKeyChecking=no -o BatchMode=yes "$SERVER_USER@$SERVER_IP" "echo 成功" 2>/dev/null; then
    echo "✅ SSH 免密登录成功"
    ((PASS++))
else
    echo "❌ SSH 免密登录失败"
    echo ""
    echo "   请执行以下步骤配置免密登录："
    echo "   1. ssh-keygen -t ed25519 -C 'your_email@example.com'"
    echo "   2. ssh-copy-id -i /Users/yl/vscode/inspection_automation/docs/only.pem.pub $SERVER_USER@$SERVER_IP"
    echo "   3. ssh -o StrictHostKeyChecking=no $SERVER_USER@$SERVER_IP 'echo 成功'"
    ((FAIL++))
fi
echo ""

echo "5. 服务器 Docker 检查"
echo "-------------------------------------------"
if ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" "command -v docker" 2>/dev/null; then
    echo "✅ Docker 已安装"
    ((PASS++))
else
    echo "❌ Docker 未安装"
    echo "   请在服务器上执行："
    echo "   curl -fsSL https://get.docker.com | sh"
    ((FAIL++))
fi

if ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" "command -v docker-compose" 2>/dev/null; then
    echo "✅ Docker Compose 已安装"
    ((PASS++))
else
    echo "❌ Docker Compose 未安装"
    echo "   请在服务器上执行："
    echo "   apt-get update && apt-get install docker-compose"
    ((FAIL++))
fi

# 检查服务器上的项目目录
if ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" "test -d $SERVER_PROJECT_PATH" 2>/dev/null; then
    echo "✅ 服务器项目目录存在 ($SERVER_PROJECT_PATH)"
    ((PASS++))
else
    echo "⚠️  服务器项目目录不存在 ($SERVER_PROJECT_PATH)"
    echo "   部署时会自动创建"
fi
echo ""

echo "============================================="
echo "检查结果汇总"
echo "============================================="
echo "通过：$PASS"
echo "失败：$FAIL"
echo ""

if [ $FAIL -eq 0 ]; then
    echo "🎉 所有检查通过！可以开始部署"
    echo ""
    echo "执行一键部署："
    echo "  ./one_click_deploy.sh"
    exit 0
else
    echo "⚠️  有 $FAIL 项检查未通过，请先解决上述问题"
    echo ""
    echo "查看部署指南："
    echo "  cat 服务器部署指南.md"
    exit 1
fi
