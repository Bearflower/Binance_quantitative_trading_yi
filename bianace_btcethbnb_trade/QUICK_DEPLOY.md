# 🚀 快速部署指南

## ⚡ 一键部署（推荐）

当服务器网络恢复后，只需执行一条命令：

```bash
cd /Users/yl/vscode/bianace_btcethbnb_trade
./one_click_deploy.sh
```

这将自动完成：
1. ✅ 打包项目（已准备好）
2. 🔄 上传到服务器
3. 🔄 停止旧容器
4. 🔄 解压新包
5. 🔄 启动新容器
6. 🔄 验证部署

---

## 🔧 分步部署（备选）

如果一键部署失败，可以分步执行：

### 步骤 1：打包项目
```bash
./auto_package.sh
```

### 步骤 2：上传到服务器
```bash
./upload_to_server.sh
```

### 步骤 3：SSH 到服务器手动部署
```bash
ssh root@43.156.242.184
```

在服务器上执行：
```bash
# 停止旧容器
docker stop trading_system-app
docker rm trading_system-app

# 解压新包
cd /root
tar -xzf deployment_package.tar.gz -C trading_system

# 启动新容器
cd trading_system
docker-compose up -d

# 查看日志
docker logs -f trading_system-app
```

---

## 📋 部署检查清单

部署前确认：

- [ ] 服务器网络正常（ping 通）
- [ ] SSH 免密登录已配置
- [ ] 本地已打包（deployment_package.tar.gz）
- [ ] 部署脚本有执行权限（chmod +x *.sh）

部署后验证：

- [ ] 容器正常运行（docker ps）
- [ ] 日志无错误（docker logs）
- [ ] 调度器配置正确（每小时 25 分执行）
- [ ] 限价单生效（日志显示"限价单"）

---

## 🔍 常见问题

### Q1: SSH 连接超时
**A:** 服务器网络故障，等待网络恢复或联系服务器管理员

### Q2: 上传失败
**A:** 检查 SSH 密钥配置：
```bash
ssh-copy-id -i /Users/yl/vscode/inspection_automation/docs/only.pem.pub root@43.156.242.184
```

### Q3: 容器启动失败
**A:** 查看详细日志：
```bash
docker logs trading_system-app
```

### Q4: 仍然是市价单
**A:** 代码未更新，需要重新部署或重启容器

---

## 📞 需要帮助？

查看完整部署文档：
- [部署报告](docs/reports/deployment_report_20260423.md)
- [服务器自动化部署技能](skills/服务器自动化部署技能.md)
