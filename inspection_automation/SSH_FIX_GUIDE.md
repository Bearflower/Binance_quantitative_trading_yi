# SSH 免密登录修复指南

## 问题描述
SSH 免密登录失效，需要修复服务器端配置。

## 修复步骤

### 1. 上传修复脚本到服务器
在本地终端执行：
```bash
# 上传修复脚本
scp -o StrictHostKeyChecking=no fix_ssh_server.sh root@43.156.242.184:/root/

# 如果上面命令失败，使用密码登录上传
sftp -o StrictHostKeyChecking=no root@43.156.242.184 << 'EOF'
put fix_ssh_server.sh /root/
EOF
```

### 2. 在服务器上运行修复脚本
```bash
# 登录服务器
ssh root@43.156.242.184

# 运行修复脚本
chmod +x /root/fix_ssh_server.sh
/root/fix_ssh_server.sh
```

### 3. 重新配置 SSH 密钥
在本地终端执行：
```bash
# 复制公钥到服务器
ssh-copy-id -i /Users/yl/vscode/inspection_automation/docs/only.pem.pub root@43.156.242.184

# 测试免密登录
ssh root@43.156.242.184 'echo 成功'
```

### 4. 验证修复结果
```bash
# 运行 SSH 检查脚本
./check_ssh_from_local.sh
```

## 常见问题排查

### 1. 权限问题
- **/root 目录**：应为 `700 root:root`
- **~/.ssh 目录**：应为 `700`
- **authorized_keys**：应为 `600`

### 2. 网络问题
- 确保服务器能正常访问
- 检查防火墙是否允许 SSH 端口（22）
- 验证服务器 IP 地址正确

### 3. 密钥问题
- 本地私钥文件存在：`/Users/yl/vscode/inspection_automation/docs/only.pem`
- 公钥已添加到服务器：`~/.ssh/authorized_keys`
- 密钥权限正确

## 紧急情况
如果以上方法都失败，使用密码登录服务器后手动检查：

```bash
# 手动修复权限
chown root:root /root
chmod 700 /root
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys

# 手动添加公钥（在本地复制公钥内容后）
echo "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDuIrMIvgru4iUHdciFXCNew9B1B+8FwS81kgSb2w/dH your_email@example.com" >> ~/.ssh/authorized_keys
```
