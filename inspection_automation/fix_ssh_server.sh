#!/bin/bash

# 服务器端 SSH 密钥修复脚本
# 在服务器上运行此脚本以修复 SSH 免密登录

set -e

echo "=========================================="
echo "服务器端 SSH 密钥修复脚本"
echo "=========================================="
echo ""

# 1. 修复目录权限
echo "步骤 1：修复目录权限..."
chown root:root /root
chmod 700 /root

mkdir -p ~/.ssh
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys 2>/dev/null || true

echo "✅ 权限修复完成"
echo ""

# 2. 显示当前 authorized_keys 内容
echo "步骤 2：检查 authorized_keys 文件..."
if [[ -f ~/.ssh/authorized_keys ]]; then
    echo "当前 authorized_keys 内容："
    cat ~/.ssh/authorized_keys
    echo ""
    echo "密钥数量：$(wc -l < ~/.ssh/authorized_keys) 个"
else
    echo "❌ authorized_keys 文件不存在，将创建新文件"
    touch ~/.ssh/authorized_keys
    chmod 600 ~/.ssh/authorized_keys
fi
echo ""

# 3. 显示 SSH 服务状态
echo "步骤 3：检查 SSH 服务状态..."
if command -v systemctl &> /dev/null; then
    systemctl status sshd --no-pager | head -20
else
    service ssh status 2>&1 | head -20
fi
echo ""

# 4. 显示 SSH 配置
echo "步骤 4：检查 SSH 配置..."
echo "SSHD 配置文件权限："
ls -la /etc/ssh/sshd_config 2>&1

echo ""
echo "SSH 服务监听状态："
netstat -tuln | grep ssh 2>&1 || ss -tuln | grep ssh 2>&1

echo ""

# 5. 检查 /root 目录权限
echo "步骤 5：验证权限设置..."
echo "/root 目录：$(stat -c '%a %U:%G' /root)"
echo "~/.ssh 目录：$(stat -c '%a' ~/.ssh)"
echo "authorized_keys：$(stat -c '%a' ~/.ssh/authorized_keys 2>/dev/null || echo '文件不存在')"
echo ""

# 6. 生成修复建议
echo "=========================================="
echo "修复建议"
echo "=========================================="
echo ""
echo "1. 请在本地运行以下命令添加公钥："
echo "   ssh-copy-id -i /Users/yl/vscode/inspection_automation/docs/only.pem.pub root@43.156.242.184"
echo ""
echo "2. 测试免密登录："
echo "   ssh root@43.156.242.184 'echo 成功'"
echo ""
echo "3. 如果仍然失败，请检查："
echo "   - 本地私钥文件是否存在：/Users/yl/vscode/inspection_automation/docs/only.pem"
echo "   - 服务器防火墙是否允许 SSH 连接"
echo "   - SSH 服务是否正常运行"
echo ""
echo "=========================================="
echo "修复脚本执行完成！"
echo "=========================================="
