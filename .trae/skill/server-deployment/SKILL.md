---
name: "服务器自动化部署"
description: "CI/CD 运维指南。当 GitHub Actions 部署失败、需要手动回滚、服务器运维、或者用户明确要求 SSH 服务器操作时触发。日常开发 push main 后 Actions 自动部署，不需要调用本技能。"
---

# 服务器自动化部署技能（CI/CD 运维版）

## 🎯 技能定位（已更新 2026-09-23）

**日常开发不再需要手动部署！** push main 后 GitHub Actions 自动完成云端构建 → GHCR → SSH 服务器 pull+up。

本技能仅在以下场景触发：
- GitHub Actions 部署失败，需要排查和修复
- 紧急回滚（新代码有 bug）
- 服务器运维（磁盘清理、容器状态检查等）
- 用户明确要求 SSH 服务器操作

---

## 一、CI/CD 架构总览

```
push main
  │
  ▼
GitHub Actions (.github/workflows/deploy.yml)
  ├── detect job（路径过滤）
  ├── build-all job（10 个矩阵并行，智能增量构建）
  └── deploy job（SSH 服务器 pull + up + 写日志）
  │
  ▼
GHCR 镜像仓库（ghcr.io/bearflower/trading-xxx:latest）
  │
  ▼
服务器 43.156.242.184
  cd /root/trading_system
  docker compose pull && docker compose up -d
  → 写 deploy_logs/YYYYMMDD.log
```

---

## 二、快速验证清单

### 2.1 Actions Run 全绿

```bash
# 浏览器打开
https://github.com/Bearflower/Binance_quantitative_trading_yi/actions

# 必须看到：
# ✅ detect（绿色）
# ✅ 所有"应该构建"的 build-all 子 job（绿色）
# ⚪ 所有 skip 的 build-all 子 job（灰色，正常）
# ✅ deploy（绿色）
```

### 2.2 服务器容器状态

```bash
ssh root@43.156.242.184 "docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'trading_system|ai-tuner|data-backend|dashboard-api'"
```

预期：所有容器 Image 都是 `ghcr.io/bearflower/trading-xxx:latest`。

### 2.3 部署日志

```bash
ssh root@43.156.242.184 "cat /root/trading_system/deploy_logs/$(date '+%Y%m%d').log"
```

预期：当日日志有 `DEPLOY_SUCCESS` 行。

### 2.4 VERSION 文件一致性

```bash
# 本地
cat VERSION

# 服务器
ssh root@43.156.242.184 "docker exec trading_system-btc_eth cat /app/VERSION"
```

---

## 三、故障排查手册

### 3.1 Actions YAML syntax error

**症状**：Run 秒级失败，显示 "Invalid workflow file"

**排查**：
1. 看报错行号（如 `Line 118, Col 18`）
2. 本地用 `yamllint .github/workflows/deploy.yml` 校验
3. VS Code 装 "GitHub Actions YAML" 插件实时提示

**常见坑**：
- YAML anchor `&XXX` + `*XXX` 在某些 GitHub 版本下不支持 → 删掉，硬编码
- 表达式里不能嵌套 `${{ }}` → 用 bash 变量替代
- 动态矩阵 `fromJson(needs.xxx.outputs.services)` 在 outputs 为空时会炸 → 用静态矩阵

### 3.2 build-all 某个 job 失败

**常见原因 + 排查**：

| 原因 | 排查 |
|------|------|
| COPY 了不存在的文件 | 检查 Dockerfile 的 COPY 路径 + .dockerignore |
| pip install 超时 | GitHub Runner 在美国，默认源能用但慢。Dockerfile 里 `ARG PIP_INDEX_URL` 为空即走默认源 |
| apt-get 包名错 | Runner 是 Debian 系，包名参考 Debian 文档 |
| buildx 初始化失败 | 必须有 `docker/setup-buildx-action@v3` |

### 3.3 deploy job SSH exit code 100

**排查顺序**：
```bash
# 1. 手动测试 SSH
ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem root@43.156.242.184 "echo ok"

# 2. 检查服务器日志
ssh root@43.156.242.184 "journalctl -u sshd --since '10 min ago' | grep -i publickey"

# 3. 检查 GitHub Secret
# Settings → Secrets → Actions → SERVER_SSH_KEY 值是否正确（完整 pem 内容）
```

### 3.4 服务器 docker pull GHCR 认证失败

```bash
# 重新 login
ssh root@43.156.242.184
echo "<GHCR_PULL_TOKEN>" | docker login ghcr.io -u bearflower --password-stdin
```

### 3.5 Actions 全绿但容器没更新

```bash
# 手动强制更新
ssh root@43.156.242.184
cd /root/trading_system
docker compose pull
docker compose up -d
```

---

## 四、手动运维速查

### 4.1 紧急回滚

```bash
# 方式 A：push 回滚 commit（推荐）
git revert HEAD
git push origin main
# 等 Actions 自动部署完

# 方式 B：手动紧急回滚（不 push）
ssh root@43.156.242.184
cd /root/trading_system
docker compose down
docker compose up -d
```

### 4.2 服务器磁盘清理

```bash
ssh root@43.156.242.184
df -h                              # 先看一眼哪个分区满了
docker system prune -f             # 清理悬空镜像 + 停止的容器
docker builder prune -f            # 清理构建缓存（激进）
```

### 4.3 容器健康检查

```bash
# 所有容器状态
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'

# 某个容器日志
docker logs --tail 100 trading_system-btc_eth

# 健康状态
docker inspect -f '{{.State.Health.Status}}' trading_system-btc_eth

# 进入容器调试
docker exec -it trading_system-btc_eth /bin/bash
```

### 4.4 单独重启某个服务

```bash
cd /root/trading_system

# 只重启 btc-eth
docker compose restart btc-eth

# 只更新 data-backend（如果它没被 compose 管理）
docker pull ghcr.io/bearflower/trading-data-backend:latest
docker compose up -d data-backend
```

### 4.5 服务器重启后自动恢复

服务器重启后 Docker 会自动重启容器（compose 里 `restart: unless-stopped`）。如果容器没起来：

```bash
ssh root@43.156.242.184
cd /root/trading_system
docker compose up -d
```

---

## 五、GitHub Secrets 配置参考

### 5.1 仓库级 Secrets

位置：`https://github.com/Bearflower/Binance_quantitative_trading_yi/settings/secrets/actions`

| Name | Value | 权限需求 |
|------|-------|---------|
| `SERVER_SSH_KEY` | 服务器 only.pem 完整内容（多行） | — |
| `GHCR_PULL_TOKEN` | GitHub PAT（read:packages） | 服务器 pull 认证 |

### 5.2 GHCR 相关 PAT

- **服务器 pull token**：`read:packages` + `read:org`（如果仓库属于 org）
- **Actions push token**：不需要手动配（GitHub Actions 自带 `GITHUB_TOKEN`，有 `write:packages` 权限）

### 5.3 Token 过期处理

PAT 默认有效期。过期后：
1. 去 https://github.com/settings/tokens 重新生成
2. 更新 GitHub Secret `GHCR_PULL_TOKEN`
3. 服务器手动重新 login（`echo "<新token>" | docker login ghcr.io -u bearflower --password-stdin`）

---

## 六、SSH 基础配置

```bash
# SSH 别名（~/.ssh/config）
Host trading-server
    HostName 43.156.242.184
    User root
    IdentityFile /Users/yl/vscode/inspection_automation/docs/only.pem
    IdentitiesOnly yes
    StrictHostKeyChecking no
    ServerAliveInterval 60

# 使用
ssh trading-server
```

### SSH 密钥安全

- 密钥文件权限必须 `600`
- 不要提交密钥到 git（`.pem` 文件应在 `.gitignore`）
- 定期轮换（建议每 90 天）

---

**最后更新：** 2026-09-23

**旧版（手动部署脚本 auto_package.sh / upload_to_server.sh / one_click_deploy.sh / verify_deployment.sh / docker_manage.sh）已废弃。** 如需回退到手动部署方案，参考 git 历史。
