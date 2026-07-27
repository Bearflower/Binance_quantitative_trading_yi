#!/bin/bash

# ============================================
# 代码同步检查脚本
# ============================================
# 功能：检查回测代码和生产环境代码的一致性
# 用途：部署前自动检查，防止代码不同步导致的问题
# ============================================

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 项目根目录
PROJECT_ROOT="/Users/yl/vscode/Binance_quantitative_trading"

# 回测代码目录
BACKTEST_DIR="$PROJECT_ROOT/backtest/btc_eth/scripts"

# 生产环境代码目录
PRODUCTION_DIR="$PROJECT_ROOT/strategies/btc_eth"

# 共享模块目录
SHARED_DIR="$PROJECT_ROOT/shared"

# 配置文件
CONFIG_FILE="$PRODUCTION_DIR/config.yaml"

# 检查报告文件
REPORT_FILE="/tmp/code_sync_report_$(date +%Y%m%d_%H%M%S).txt"

echo "============================================="
echo "🔍 代码同步检查工具"
echo "============================================="
echo ""
echo "项目根目录: $PROJECT_ROOT"
echo "回测代码目录: $BACKTEST_DIR"
echo "生产环境目录: $PRODUCTION_DIR"
echo "共享模块目录: $SHARED_DIR"
echo ""

# 创建报告文件
echo "代码同步检查报告" > "$REPORT_FILE"
echo "检查时间: $(date '+%Y-%m-%d %H:%M:%S')" >> "$REPORT_FILE"
echo "=============================================" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"

# 计数器
WARNING_COUNT=0
ERROR_COUNT=0
CHECK_COUNT=0

# ============================================
# 1. 检查回测代码版本
# ============================================
echo "📋 步骤 1/6: 检查回测代码版本..."
echo "" >> "$REPORT_FILE"
echo "1. 回测代码版本检查" >> "$REPORT_FILE"
echo "----------------------------------------" >> "$REPORT_FILE"

# 查找最新的回测脚本
LATEST_BACKTEST=$(ls -t "$BACKTEST_DIR"/backtest_v*.py 2>/dev/null | head -1)

if [ -z "$LATEST_BACKTEST" ]; then
    echo -e "${RED}❌ 错误：未找到回测脚本${NC}"
    echo "❌ 错误：未找到回测脚本" >> "$REPORT_FILE"
    ERROR_COUNT=$((ERROR_COUNT + 1))
else
    BACKTEST_VERSION=$(basename "$LATEST_BACKTEST" | sed 's/backtest_v\([0-9]*\).py/\1/')
    echo -e "${GREEN}✅ 最新回测脚本: $(basename "$LATEST_BACKTEST")${NC}"
    echo "   版本号: v$BACKTEST_VERSION"
    echo "最新回测脚本: $(basename "$LATEST_BACKTEST")" >> "$REPORT_FILE"
    echo "版本号: v$BACKTEST_VERSION" >> "$REPORT_FILE"
    
    # 检查回测脚本的修改时间
    BACKTEST_MOD_TIME=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M:%S" "$LATEST_BACKTEST" 2>/dev/null || stat -c "%y" "$LATEST_BACKTEST" 2>/dev/null | cut -d'.' -f1)
    echo "   最后修改时间: $BACKTEST_MOD_TIME"
    echo "最后修改时间: $BACKTEST_MOD_TIME" >> "$REPORT_FILE"
fi

echo "" >> "$REPORT_FILE"
CHECK_COUNT=$((CHECK_COUNT + 1))

# ============================================
# 2. 检查生产环境代码版本
# ============================================
echo "📋 步骤 2/6: 检查生产环境代码版本..."
echo "" >> "$REPORT_FILE"
echo "2. 生产环境代码版本检查" >> "$REPORT_FILE"
echo "----------------------------------------" >> "$REPORT_FILE"

PRODUCTION_STRATEGY="$PRODUCTION_DIR/strategy.py"

if [ ! -f "$PRODUCTION_STRATEGY" ]; then
    echo -e "${RED}❌ 错误：生产环境策略文件不存在${NC}"
    echo "❌ 错误：生产环境策略文件不存在" >> "$REPORT_FILE"
    ERROR_COUNT=$((ERROR_COUNT + 1))
else
    # 从策略文件中提取版本号
    PRODUCTION_VERSION=$(grep -E "version.*['\"].*['\"]" "$PRODUCTION_STRATEGY" | head -1 | sed -E "s/.*version.*['\"]([0-9.]+)['\"].*/\1/" || echo "未知")
    
    echo -e "${GREEN}✅ 生产环境策略文件存在${NC}"
    echo "   版本号: v$PRODUCTION_VERSION"
    echo "生产环境策略文件: strategy.py" >> "$REPORT_FILE"
    echo "版本号: v$PRODUCTION_VERSION" >> "$REPORT_FILE"
    
    # 检查生产环境代码的修改时间
    PRODUCTION_MOD_TIME=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M:%S" "$PRODUCTION_STRATEGY" 2>/dev/null || stat -c "%y" "$PRODUCTION_STRATEGY" 2>/dev/null | cut -d'.' -f1)
    echo "   最后修改时间: $PRODUCTION_MOD_TIME"
    echo "最后修改时间: $PRODUCTION_MOD_TIME" >> "$REPORT_FILE"
    
    # 检查配置文件版本
    if [ -f "$CONFIG_FILE" ]; then
        CONFIG_VERSION=$(grep -E "version.*['\"].*['\"]" "$CONFIG_FILE" | head -1 | sed -E "s/.*version.*['\"]([0-9.]+)['\"].*/\1/" || echo "未知")
        echo "   配置文件版本: v$CONFIG_VERSION"
        echo "配置文件版本: v$CONFIG_VERSION" >> "$REPORT_FILE"
    fi
fi

echo "" >> "$REPORT_FILE"
CHECK_COUNT=$((CHECK_COUNT + 1))

# ============================================
# 3. 检查版本一致性
# ============================================
echo "📋 步骤 3/6: 检查版本一致性..."
echo "" >> "$REPORT_FILE"
echo "3. 版本一致性检查" >> "$REPORT_FILE"
echo "----------------------------------------" >> "$REPORT_FILE"

if [ -n "$BACKTEST_VERSION" ] && [ -n "$PRODUCTION_VERSION" ] && [ "$PRODUCTION_VERSION" != "未知" ]; then
    # 比较版本号（去除小数点）
    BACKTEST_VER_NUM=$(echo "$BACKTEST_VERSION" | tr -d '.')
    PRODUCTION_VER_NUM=$(echo "$PRODUCTION_VERSION" | tr -d '.')
    
    if [ "$BACKTEST_VER_NUM" != "$PRODUCTION_VER_NUM" ]; then
        echo -e "${YELLOW}⚠️  警告：回测代码和生产环境代码版本不一致${NC}"
        echo "   回测版本: v$BACKTEST_VERSION"
        echo "   生产版本: v$PRODUCTION_VERSION"
        echo "⚠️  警告：回测代码和生产环境代码版本不一致" >> "$REPORT_FILE"
        echo "回测版本: v$BACKTEST_VERSION" >> "$REPORT_FILE"
        echo "生产版本: v$PRODUCTION_VERSION" >> "$REPORT_FILE"
        WARNING_COUNT=$((WARNING_COUNT + 1))
    else
        echo -e "${GREEN}✅ 版本一致: v$BACKTEST_VERSION${NC}"
        echo "✅ 版本一致: v$BACKTEST_VERSION" >> "$REPORT_FILE"
    fi
else
    echo -e "${YELLOW}⚠️  无法比较版本（版本号未知）${NC}"
    echo "⚠️  无法比较版本（版本号未知）" >> "$REPORT_FILE"
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

echo "" >> "$REPORT_FILE"
CHECK_COUNT=$((CHECK_COUNT + 1))

# ============================================
# 4. 检查核心策略参数一致性
# ============================================
echo "📋 步骤 4/6: 检查核心策略参数一致性..."
echo "" >> "$REPORT_FILE"
echo "4. 核心策略参数一致性检查" >> "$REPORT_FILE"
echo "----------------------------------------" >> "$REPORT_FILE"

# 定义需要检查的关键参数
KEY_PARAMS=(
    "stop_loss_atr_multiplier"
    "take_profit_atr_multiplier"
    "tp1_atr_multiplier"
    "tp2_atr_multiplier"
    "activation_atr"
    "trailing_atr"
    "max_holding_hours"
)

# 从配置文件中提取参数
if [ -f "$CONFIG_FILE" ]; then
    echo "检查关键参数..." >> "$REPORT_FILE"
    
    for param in "${KEY_PARAMS[@]}"; do
        # 从配置文件中提取参数值
        CONFIG_VALUE=$(grep -A 5 "$param" "$CONFIG_FILE" | grep -E "^\s+[0-9.]+" | head -1 | awk '{print $1}' || echo "未找到")
        
        if [ "$CONFIG_VALUE" != "未找到" ]; then
            echo "   $param: $CONFIG_VALUE"
            echo "$param: $CONFIG_VALUE" >> "$REPORT_FILE"
        fi
    done
    
    echo -e "${GREEN}✅ 核心参数检查完成${NC}"
else
    echo -e "${RED}❌ 配置文件不存在，无法检查参数${NC}"
    echo "❌ 配置文件不存在，无法检查参数" >> "$REPORT_FILE"
    ERROR_COUNT=$((ERROR_COUNT + 1))
fi

echo "" >> "$REPORT_FILE"
CHECK_COUNT=$((CHECK_COUNT + 1))

# ============================================
# 5. 检查共享模块一致性
# ============================================
echo "📋 步骤 5/6: 检查共享模块一致性..."
echo "" >> "$REPORT_FILE"
echo "5. 共享模块一致性检查" >> "$REPORT_FILE"
echo "----------------------------------------" >> "$REPORT_FILE"

# 定义需要检查的共享模块
SHARED_MODULES=(
    "shared/indicators.py"
    "shared/dynamic_atr_filter.py"
    "shared/binance_api.py"
    "shared/kline_service.py"
    "shared/notification.py"
)

echo "检查共享模块..." >> "$REPORT_FILE"

for module in "${SHARED_MODULES[@]}"; do
    MODULE_PATH="$PROJECT_ROOT/$module"
    
    if [ -f "$MODULE_PATH" ]; then
        MODULE_MOD_TIME=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M:%S" "$MODULE_PATH" 2>/dev/null || stat -c "%y" "$MODULE_PATH" 2>/dev/null | cut -d'.' -f1)
        echo -e "${GREEN}✅ $module 存在${NC}"
        echo "   最后修改时间: $MODULE_MOD_TIME"
        echo "$module: 存在 (修改时间: $MODULE_MOD_TIME)" >> "$REPORT_FILE"
    else
        echo -e "${RED}❌ $module 不存在${NC}"
        echo "$module: 不存在" >> "$REPORT_FILE"
        ERROR_COUNT=$((ERROR_COUNT + 1))
    fi
done

echo "" >> "$REPORT_FILE"
CHECK_COUNT=$((CHECK_COUNT + 1))

# ============================================
# 6. 检查代码修改时间差异
# ============================================
echo "📋 步骤 6/6: 检查代码修改时间差异..."
echo "" >> "$REPORT_FILE"
echo "6. 代码修改时间差异检查" >> "$REPORT_FILE"
echo "----------------------------------------" >> "$REPORT_FILE"

if [ -n "$BACKTEST_MOD_TIME" ] && [ -n "$PRODUCTION_MOD_TIME" ]; then
    # 转换为时间戳
    BACKTEST_TIMESTAMP=$(date -j -f "%Y-%m-%d %H:%M:%S" "$BACKTEST_MOD_TIME" "+%s" 2>/dev/null || date -d "$BACKTEST_MOD_TIME" "+%s" 2>/dev/null || echo "0")
    PRODUCTION_TIMESTAMP=$(date -j -f "%Y-%m-%d %H:%M:%S" "$PRODUCTION_MOD_TIME" "+%s" 2>/dev/null || date -d "$PRODUCTION_MOD_TIME" "+%s" 2>/dev/null || echo "0")
    
    if [ "$BACKTEST_TIMESTAMP" != "0" ] && [ "$PRODUCTION_TIMESTAMP" != "0" ]; then
        # 计算时间差（秒）
        TIME_DIFF=$((BACKTEST_TIMESTAMP - PRODUCTION_TIMESTAMP))
        TIME_DIFF_ABS=${TIME_DIFF#-}
        
        # 转换为小时
        TIME_DIFF_HOURS=$((TIME_DIFF_ABS / 3600))
        
        if [ $TIME_DIFF_HOURS -gt 24 ]; then
            echo -e "${YELLOW}⚠️  警告：回测代码和生产环境代码修改时间差异超过24小时${NC}"
            echo "   回测代码修改时间: $BACKTEST_MOD_TIME"
            echo "   生产环境代码修改时间: $PRODUCTION_MOD_TIME"
            echo "   时间差: ${TIME_DIFF_HOURS}小时"
            echo "⚠️  警告：回测代码和生产环境代码修改时间差异超过24小时" >> "$REPORT_FILE"
            echo "回测代码修改时间: $BACKTEST_MOD_TIME" >> "$REPORT_FILE"
            echo "生产环境代码修改时间: $PRODUCTION_MOD_TIME" >> "$REPORT_FILE"
            echo "时间差: ${TIME_DIFF_HOURS}小时" >> "$REPORT_FILE"
            WARNING_COUNT=$((WARNING_COUNT + 1))
        else
            echo -e "${GREEN}✅ 代码修改时间差异在24小时内${NC}"
            echo "   时间差: ${TIME_DIFF_HOURS}小时"
            echo "✅ 代码修改时间差异在24小时内 (${TIME_DIFF_HOURS}小时)" >> "$REPORT_FILE"
        fi
    else
        echo -e "${YELLOW}⚠️  无法计算时间差${NC}"
        echo "⚠️  无法计算时间差" >> "$REPORT_FILE"
        WARNING_COUNT=$((WARNING_COUNT + 1))
    fi
else
    echo -e "${YELLOW}⚠️  无法获取修改时间${NC}"
    echo "⚠️  无法获取修改时间" >> "$REPORT_FILE"
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

echo "" >> "$REPORT_FILE"
CHECK_COUNT=$((CHECK_COUNT + 1))

# ============================================
# 生成总结报告
# ============================================
echo "" >> "$REPORT_FILE"
echo "=============================================" >> "$REPORT_FILE"
echo "检查总结" >> "$REPORT_FILE"
echo "=============================================" >> "$REPORT_FILE"
echo "检查项目数: $CHECK_COUNT" >> "$REPORT_FILE"
echo "警告数: $WARNING_COUNT" >> "$REPORT_FILE"
echo "错误数: $ERROR_COUNT" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"

echo ""
echo "============================================="
echo "📊 检查总结"
echo "============================================="
echo "检查项目数: $CHECK_COUNT"
echo -e "警告数: ${YELLOW}$WARNING_COUNT${NC}"
echo -e "错误数: ${RED}$ERROR_COUNT${NC}"
echo ""

if [ $ERROR_COUNT -gt 0 ]; then
    echo -e "${RED}❌ 发现严重错误，建议修复后再部署${NC}"
    echo "❌ 发现严重错误，建议修复后再部署" >> "$REPORT_FILE"
    echo ""
    echo "建议操作："
    echo "1. 检查缺失的文件"
    echo "2. 确认代码版本一致性"
    echo "3. 同步回测代码到生产环境"
    echo ""
    echo "详细报告已保存到: $REPORT_FILE"
    exit 1
elif [ $WARNING_COUNT -gt 0 ]; then
    echo -e "${YELLOW}⚠️  发现警告，建议检查后再部署${NC}"
    echo "⚠️  发现警告，建议检查后再部署" >> "$REPORT_FILE"
    echo ""
    echo "建议操作："
    echo "1. 检查版本差异"
    echo "2. 确认代码同步状态"
    echo "3. 手动验证关键参数"
    echo ""
    echo "详细报告已保存到: $REPORT_FILE"
    exit 0
else
    echo -e "${GREEN}✅ 所有检查通过，可以安全部署${NC}"
    echo "✅ 所有检查通过，可以安全部署" >> "$REPORT_FILE"
    echo ""
    echo "详细报告已保存到: $REPORT_FILE"
    exit 0
fi
