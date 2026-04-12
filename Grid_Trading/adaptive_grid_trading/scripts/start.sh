#!/bin/bash
# 自适应网格策略系统 - 启动脚本

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 项目根目录
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

echo -e "${GREEN}================================${NC}"
echo -e "${GREEN}自适应网格策略系统${NC}"
echo -e "${GREEN}================================${NC}"

# 检查 Python 版本
echo -e "${YELLOW}检查 Python 环境...${NC}"
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}错误：未找到 Python 3${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
echo "Python 版本：$PYTHON_VERSION"

# 检查虚拟环境
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}创建虚拟环境...${NC}"
    python3 -m venv venv
fi

# 激活虚拟环境
echo -e "${YELLOW}激活虚拟环境...${NC}"
source venv/bin/activate

# 检查并安装依赖
echo -e "${YELLOW}检查依赖...${NC}"
if [ ! -f "venv/bin/activate" ]; then
    echo -e "${RED}错误：虚拟环境未正确创建${NC}"
    exit 1
fi

# 安装依赖
pip install -q -r requirements.txt

# 检查配置文件
echo -e "${YELLOW}检查配置文件...${NC}"
if [ ! -f "config/config.yaml" ]; then
    if [ -f "config/config.yaml.template" ]; then
        echo -e "${YELLOW}复制配置模板...${NC}"
        cp config/config.yaml.template config/config.yaml
        echo -e "${RED}请编辑 config/config.yaml 填入实际配置${NC}"
        exit 1
    else
        echo -e "${RED}错误：配置文件不存在${NC}"
        exit 1
    fi
fi

# 检查环境变量文件
if [ ! -f "config/.env" ]; then
    if [ -f "config/.env.template" ]; then
        echo -e "${YELLOW}复制环境变量模板...${NC}"
        cp config/.env.template config/.env
        echo -e "${RED}请编辑 config/.env 填入 API 密钥${NC}"
        exit 1
    fi
fi

# 创建必要目录
echo -e "${YELLOW}创建必要目录...${NC}"
mkdir -p logs data/history

# 启动程序
echo -e "${GREEN}启动系统...${NC}"
python src/main.py
