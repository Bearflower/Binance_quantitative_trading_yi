#!/bin/bash

# 服务器登录信息
SERVER_IP="43.156.242.184"
USERNAME="root"
PASSWORD="v3U,XZy!b5A2w@R"

# 复制脚本到服务器
scp -o StrictHostKeyChecking=no server_check.sh $USERNAME@$SERVER_IP:/root/inspection/

# 执行远程命令设置定时任务
sshpass -p "$PASSWORD" ssh -o StrictHostKeyChecking=no $USERNAME@$SERVER_IP "chmod +x /root/inspection/server_check.sh && (crontab -l 2>/dev/null; echo '30 7 * * * /root/inspection/server_check.sh >> /root/inspection/inspection.log 2>&1') | crontab - && crontab -l"
