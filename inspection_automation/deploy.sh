#!/bin/bash

# 检查目录是否存在
if [ ! -d "/root/inspection" ]; then
    mkdir -p /root/inspection
fi

# 给脚本添加执行权限
chmod +x /root/inspection/server_check.sh

# 设置定时任务，每天07:30执行巡检脚本
(crontab -l 2>/dev/null; echo "30 7 * * * /root/inspection/server_check.sh >> /root/inspection/inspection.log 2>&1") | crontab -

# 显示当前定时任务
crontab -l

# 显示脚本权限
ls -la /root/inspection/
