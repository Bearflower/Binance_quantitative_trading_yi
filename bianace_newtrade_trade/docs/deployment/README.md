# 📚 自动化部署技能文档

## 🎯 文档说明

这套文档提供了一套完整的、可复用的自动化打包、上传、Docker 部署标准化流程。

**适用场景：**
- ✅ 新项目首次部署到服务器
- ✅ 现有项目更新代码
- ✅ 快速重建 Docker 容器
- ✅ 批量部署多个服务器
- ✅ Trae IDE 中的 SSH 通信问题

---

## 📖 文档目录

### 1. 🚀 [快速启动指南](QUICK_START.md) ⭐ **推荐先看**

**5 分钟完成部署**，适合快速上手。

**内容：**
- 1 分钟创建配置文件
- 2 分钟创建脚本
- 2 分钟执行部署
- 常用命令速查
- 故障排查

**适合：** 想要快速部署，不想看长篇文档的用户

---

### 2. 📋 [完整部署规范文档](DEPLOYMENT_SKILL.md) 📚

**完整的标准化文档**，包含所有细节和最佳实践。

**内容：**
- 一、准备工作（配置文件、必要文件检查）
- 二、自动化打包流程（打包脚本、执行打包）
- 三、上传到服务器（Trae SSH 通信问题解决方案）
- 四、Docker 部署流程（服务器端部署、一键部署）
- 五、Docker 容器管理（管理脚本、快速命令）
- 六、Trae IDE SSH 通信注意事项 ⭐ **重点**
- 七、多服务器批量部署
- 八、故障排查
- 九、最佳实践建议
- 十、快速参考卡片

**适合：** 需要深入了解、建立标准化流程的团队

---

## 🎯 快速开始

### 方式 1：使用快速启动指南（推荐新手）

```bash
# 1. 查看快速启动指南
cat QUICK_START.md

# 2. 按照步骤操作
# 创建 .deploy_config
# 创建 auto_package.sh
# 创建 one_click_deploy.sh
# 执行 ./one_click_deploy.sh
```

### 方式 2：直接使用现有脚本（如果已有）

```bash
# 如果项目已有这些脚本，直接执行
./one_click_deploy.sh
```

---

## 🔑 核心文件

### 配置文件
- `.deploy_config` - 部署配置（服务器信息、Docker 配置等）

### 脚本文件
- `auto_package.sh` - 自动打包脚本
- `upload_to_server.sh` - 上传脚本
- `one_click_deploy.sh` - 一键部署脚本
- `docker_manage.sh` - Docker 容器管理菜单
- `batch_deploy.sh` - 批量部署脚本

### Docker 文件
- `Dockerfile` - Docker 镜像构建文件
- `docker-compose.yml` - Docker 编排文件
- `deploy.sh` - 服务器端部署脚本

---

## 💡 Trae IDE SSH 通信重点

在 Trae IDE 中执行 SSH/SCP 命令时，**必须**使用以下方法之一：

### 方法 1：添加 SSH 选项（推荐）

```bash
trae-sandbox 'scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null file.txt root@IP:/root/'
```

### 方法 2：配置 SSH 配置文件

编辑 `~/.ssh/config`：
```
Host *
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
```

### 方法 3：使用 expect 脚本

```bash
#!/usr/bin/expect -f
spawn scp -o StrictHostKeyChecking=no file.txt root@IP:/root/
expect "*assword:" { send "password\r" }
expect eof
```

**详细说明请查看：** [DEPLOYMENT_SKILL.md 第六章节](DEPLOYMENT_SKILL.md)

---

## 📊 部署流程图

```
┌─────────────┐
│  本地项目   │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  auto_      │  步骤 1: 打包
│  package.sh │  - 排除不需要的文件
└──────┬──────┘  - 创建 tar.gz 压缩包
       │
       ▼
┌─────────────┐
│ deployment_ │
│ package.tar │
│    .gz      │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  upload_    │  步骤 2: 上传
│  to_        │  - 使用 SCP + expect
│  server.sh  │  - 解决 Trae SSH 问题
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  服务器     │
│  /root/     │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ SSH 远程     │  步骤 3: 部署
│ 执行部署    │  - 停止旧容器
│  命令       │  - 删除旧容器
└──────┬──────┘  - 解压新包
       │         - 构建新镜像
       ▼         - 启动新容器
┌─────────────┐
│  Docker     │
│  容器运行   │
└─────────────┘
```

---

## 🛠️ 工具依赖

### 本地需要安装
- `rsync` - 文件同步工具
- `expect` - 自动化交互工具
- `ssh` - SSH 客户端
- `scp` - SSH 文件传输

**macOS 安装：**
```bash
brew install rsync expect
```

**Linux 安装：**
```bash
apt-get install rsync expect  # Debian/Ubuntu
yum install rsync expect      # CentOS/RHEL
```

### 服务器需要安装
- Docker
- Docker Compose

---

## 🎓 学习路径

1. **新手入门**
   - 阅读 [QUICK_START.md](QUICK_START.md)
   - 按照步骤完成首次部署
   - 使用常用命令查看状态

2. **进阶使用**
   - 阅读 [DEPLOYMENT_SKILL.md](DEPLOYMENT_SKILL.md) 第六章节
   - 理解 Trae SSH 通信问题
   - 配置 SSH 密钥认证

3. **团队标准化**
   - 完整阅读 [DEPLOYMENT_SKILL.md](DEPLOYMENT_SKILL.md)
   - 根据团队需求调整脚本
   - 建立部署规范和流程

4. **批量部署**
   - 学习第七章节
   - 创建 `servers.list`
   - 使用 `batch_deploy.sh`

---

## 📝 常见问题

### Q1: 为什么在 Trae 中执行 SSH 命令会报错？
**A:** Trae IDE 使用 sandbox 环境，需要使用 `trae-sandbox` 命令或添加 SSH 选项。详见第六章节。

### Q2: 每次都要输入密码很麻烦，怎么办？
**A:** 建议配置 SSH 密钥认证。详见 [QUICK_START.md](QUICK_START.md) 底部说明。

### Q3: 如何回滚到旧版本？
**A:** 详见 [DEPLOYMENT_SKILL.md](DEPLOYMENT_SKILL.md) 第十章节的快速参考卡片。

### Q4: 如何同时部署到多个服务器？
**A:** 使用第七章节的批量部署脚本 `batch_deploy.sh`。

### Q5: 部署失败了如何排查？
**A:** 详见第八章节的故障排查指南。

---

## 🔗 相关资源

- [Docker 官方文档](https://docs.docker.com/)
- [Docker Compose 官方文档](https://docs.docker.com/compose/)
- [SSH 最佳实践](https://www.ssh.com/academy/ssh/)
- [Expect 官方文档](https://core.tcl-lang.org/expect/)

---

## 📞 支持与反馈

如遇到问题：
1. 查看 [DEPLOYMENT_SKILL.md](DEPLOYMENT_SKILL.md) 第八章节
2. 检查服务器日志：`ssh root@IP "docker logs CONTAINER_NAME"`
3. 检查本地网络：`ping SERVER_IP`
4. 检查 Docker 状态：`ssh root@IP "systemctl status docker"`

---

## 📄 文档版本

- **版本：** v1.0
- **创建日期：** 2026-03-09
- **最后更新：** 2026-03-09
- **维护者：** [你的名字]

---

## 🎉 总结

这套文档包含了：

✅ **完整的部署流程** - 从打包到部署的一条龙服务  
✅ **Trae SSH 通信解决方案** - 专门针对 Trae IDE 的优化  
✅ **一键部署脚本** - 自动化所有步骤  
✅ **批量部署支持** - 同时部署到多个服务器  
✅ **故障排查指南** - 常见问题及解决方案  
✅ **最佳实践建议** - 安全、性能、版本管理、监控告警  

**开始使用：** 请从 [QUICK_START.md](QUICK_START.md) 开始！

---

**祝你部署顺利！🚀**
