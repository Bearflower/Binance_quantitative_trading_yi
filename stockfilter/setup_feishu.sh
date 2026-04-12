#!/bin/bash
# 飞书推送系统安装脚本

echo "=============================================================="
echo "飞书推送系统安装与配置"
echo "=============================================================="
echo ""

# 检查 Python 环境
echo "检查 Python 环境..."
if ! command -v python3 &> /dev/null; then
    echo "❌ 未找到 Python3，请先安装"
    exit 1
fi
echo "✅ Python3 已安装：$(python3 --version)"
echo ""

# 检查依赖
echo "检查依赖包..."
python3 -c "import requests" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "安装 requests..."
    pip3 install requests
fi
python3 -c "import yaml" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "安装 pyyaml..."
    pip3 install pyyaml
fi
echo "✅ 依赖包已安装"
echo ""

# 配置飞书 webhook
echo "=============================================================="
echo "配置飞书 Webhook"
echo "=============================================================="
echo ""
echo "请输入您的飞书机器人 Webhook URL:"
echo "（格式：https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx）"
echo ""
read -p "Webhook URL: " WEBHOOK_URL

if [[ ! $WEBHOOK_URL =~ "https://open.feishu.cn" ]]; then
    echo "❌ Webhook URL 格式不正确"
    exit 1
fi

# 更新 feishu_push.py 中的 webhook
echo ""
echo "更新飞书配置..."
sed -i '' "s|https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_WEBHOOK_ID|$WEBHOOK_URL|g" feishu_push.py
echo "✅ 飞书配置已更新"
echo ""

# 创建日志目录
echo "创建日志目录..."
mkdir -p logs
mkdir -p signals
echo "✅ 目录创建完成"
echo ""

# 配置 crontab
echo "=============================================================="
echo "配置定时任务"
echo "=============================================================="
echo ""
echo "将添加以下定时任务:"
echo "1. 每个交易日 15:30 - 形态扫描"
echo "2. 每个交易日 08:00 - 飞书推送"
echo ""
read -p "是否添加到 crontab？(y/n): " ADD_CRON

if [ "$ADD_CRON" = "y" ]; then
    # 备份当前 crontab
    crontab -l > crontab.bak 2>/dev/null
    
    # 获取当前脚本目录
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    
    # 添加新任务
    (crontab -l 2>/dev/null; echo "# 股票形态推送系统") | crontab -
    (crontab -l 2>/dev/null; echo "# 形态扫描：每个交易日 15:30") | crontab -
    (crontab -l 2>/dev/null; echo "30 15 * * 1-5 cd $SCRIPT_DIR && python3 daily_scan.py >> $SCRIPT_DIR/logs/daily_scan.log 2>&1") | crontab -
    (crontab -l 2>/dev/null; echo "# 飞书推送：每个交易日 08:00") | crontab -
    (crontab -l 2>/dev/null; echo "0 8 * * 1-5 cd $SCRIPT_DIR && python3 feishu_push.py >> $SCRIPT_DIR/logs/feishu_push.log 2>&1") | crontab -
    
    echo "✅ 定时任务已添加"
    echo ""
    echo "查看 crontab: crontab -l"
    echo "查看日志：tail -f logs/daily_scan.log"
else
    echo "⚠️  跳过 crontab 配置"
    echo ""
    echo "手动添加以下任务到 crontab:"
    echo "# 形态扫描：每个交易日 15:30"
    echo "30 15 * * 1-5 cd $PWD && python3 daily_scan.py >> $PWD/logs/daily_scan.log 2>&1"
    echo ""
    echo "# 飞书推送：每个交易日 08:00"
    echo "0 8 * * 1-5 cd $PWD && python3 feishu_push.py >> $PWD/logs/feishu_push.log 2>&1"
fi

echo ""
echo "=============================================================="
echo "安装完成！"
echo "=============================================================="
echo ""
echo "下一步操作:"
echo "1. 测试形态扫描：python3 daily_scan.py"
echo "2. 测试飞书推送：python3 feishu_push.py"
echo "3. 查看日志：tail -f logs/*.log"
echo ""
echo "使用说明:"
echo "- 形态扫描会在每个交易日 15:30 自动运行"
echo "- 飞书推送会在每个交易日 08:00 自动发送"
echo "- 信号文件保存在 signals/ 目录"
echo "- 日志文件保存在 logs/ 目录"
echo ""
