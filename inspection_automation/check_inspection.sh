#!/bin/bash

# 检查巡检日志
echo "=== 检查服务器巡检日志 ==="
if [ -f "/root/inspection/inspection.log" ]; then
    echo "巡检日志文件存在，内容如下："
    cat /root/inspection/inspection.log
else
    echo "巡检日志文件不存在"
fi

echo ""
echo "=== 检查定时任务 ==="
crontab -l

echo ""
echo "=== 检查脚本权限 ==="
ls -la /root/inspection/server_check.sh

echo ""
echo "=== 检查目录内容 ==="
ls -la /root/inspection/