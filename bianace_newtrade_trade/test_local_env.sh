#!/bin/bash

# ============================================
# 做空系统本地环境测试脚本
# ============================================

set -e

echo "============================================="
echo "做空系统 - 本地环境测试"
echo "============================================="

# 检查 Python 版本
echo "🐍 检查 Python 版本..."
python3 --version

# 检查依赖包
echo "📦 检查依赖包..."
cd /Users/yl/vscode/bianace_newtrade_trade/short_selling_system

if [ ! -f "requirements.txt" ]; then
    echo "❌ 错误：requirements.txt 不存在"
    exit 1
fi

echo "✅ 依赖文件存在"

# 检查 Docker
echo "🐳 检查 Docker..."
if command -v docker &> /dev/null; then
    docker --version
    echo "✅ Docker 已安装"
else
    echo "❌ 警告：Docker 未安装，无法进行容器化部署"
fi

# 检查 Docker Compose
echo "📋 检查 Docker Compose..."
if command -v docker-compose &> /dev/null; then
    docker-compose --version
    echo "✅ Docker Compose 已安装"
else
    echo "❌ 警告：Docker Compose 未安装"
fi

# 检查配置文件
echo "📄 检查配置文件..."
if [ -f ".env" ]; then
    echo "✅ .env 文件存在"
    # 检查是否配置了 API 密钥
    if grep -q "your_binance_api_key_here" .env; then
        echo "⚠️  警告：请配置币安 API 密钥"
    else
        echo "✅ 币安 API 密钥已配置"
    fi
else
    echo "❌ .env 文件不存在"
fi

# 运行单元测试
echo "🧪 运行单元测试..."
if [ -f "test_all_stages_v2.py" ]; then
    python3 test_all_stages_v2.py
    echo "✅ 测试完成"
else
    echo "❌ 测试文件不存在"
fi

echo "============================================="
echo "✅ 本地环境测试完成！"
echo "============================================="
echo ""
echo "下一步操作:"
echo "1. 配置 .env 文件中的 API 密钥"
echo "2. 配置 SSH 密钥认证（参考 DEPLOYMENT_GUIDE.md）"
echo "3. 执行一键部署：./one_click_deploy.sh"
echo "============================================="
