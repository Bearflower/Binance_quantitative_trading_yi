# 部署规则（GitHub Actions + GHCR 自动化方案）

## 部署触发条件（只有一个）

- **push 代码到 main 分支** → GitHub Actions 自动触发构建 + 部署
- **workflow_dispatch** → 在 Actions 页面手动触发

**不再需要**手动打包、手动 SCP、手动 SSH 部署脚本。

---

## 一、整体架构

```
┌──────────────┐     ┌──────────────┐     ┌───────────────────┐
│ 本地 Mac     │     │ GitHub       │     │ GitHub Actions    │
│ git push main│────▶│ 仓库代码    │────▶│ Runner (云端)     │
└──────────────┘     └──────────────┘     │                   │
                                           │ 1. git diff 增量   │
                                           │ 2. docker build    │
                                           │ 3. push GHCR       │
                                           └────────┬──────────┘
                                                    │
                                           ┌────────▼──────────┐
                                           │ GHCR 镜像仓库     │
                                           │ ghcr.io/bearflower│
                                           └────────┬──────────┘
                                                    │
                                           ┌────────▼──────────┐
                                           │ 你的交易服务器    │
                                           │ 43.156.242.184   │
                                           │                   │
                                           │ SSH docker pull   │
                                           │ docker compose up │
                                           │ 写部署日志       │
                                           └───────────────────┘
```

---

## 二、GitHub Actions 配置

### 2.1 workflow 文件位置

`.github/workflows/deploy.yml`

### 2.2 工作流设计（智能增量构建）

```yaml
# 三个 Job
detect       # 路径过滤（纯诊断，输出变更清单）
build-all    # 静态 10 个矩阵并行
             # → 每个 job 自己 git diff ${{ github.event.before }}..${{ github.sha }}
             #   （整段推送范围，checkout fetch-depth: 0；before 全 0 时回退 HEAD~1）
             # → shared/ 或 docker-compose.yml / VERSION 变了？→ 全部重建
             # → 变更全部落在 .github/？→ 全部重建（打破"改 CI 即全 skip"死循环）
             # → 自己目录变了？→ 构建自己
             # → 范围为空 / before 本地不可达？→ fail-safe 全量重建
             # → 否则 skip（秒级）
deploy       # SSH 服务器 pull + up（幂等）
```

**为什么用静态矩阵 + 每个 job 自己判断 skip？**

- 从 GitHub Runner 启动到 docker build 完成：3-5 min
- 从 skip 判断到 exit 0：3 秒
- 改 1 个策略 ≈ 3 min（只构建那个）
- 改 shared/ ≈ 10 min（全部 10 个构建）
- 只改 .github/ ≈ 10 min（无法判断历史镜像是否已构建，全部 10 个重建，用于打破死循环）

### 2.3 服务器需要的 GitHub Secrets

| Secret | 值 | 说明 |
|--------|-----|------|
| `SERVER_SSH_KEY` | 服务器 only.pem 的完整内容 | Actions SSH 服务器认证 |
| `GHCR_PULL_TOKEN` | GitHub PAT（read:packages） | 服务器 pull GHCR 私有镜像认证 |

### 2.4 GHCR 镜像命名规范

```
ghcr.io/bearflower/trading-btc-eth:latest
ghcr.io/bearflower/trading-btc-eth-aggr:latest
ghcr.io/bearflower/trading-new-coin:latest
ghcr.io/bearflower/trading-grid:latest
ghcr.io/bearflower/trading-hrs:latest
ghcr.io/bearflower/trading-kline-service:latest
ghcr.io/bearflower/trading-kline-monitor:latest
ghcr.io/bearflower/trading-ai-tuner:latest
ghcr.io/bearflower/trading-data-backend:latest
ghcr.io/bearflower/trading-dashboard-api:latest
```

### 2.5 docker-compose.yml 镜像引用

```yaml
services:
  btc-eth:
    image: ghcr.io/bearflower/trading-btc-eth:latest
    # 注意：不再有 build: 块，镜像由 CI 构建
```

postgres 用公共镜像 `postgres:15-alpine`，不动。

---

## 三、防幻觉机制（为什么 Actions 方案能杜绝幻觉）

| 原来的幻觉原因 | Actions 如何解决 |
|---------------|-----------------|
| 服务器 Docker 构建缓存旧代码 | **云端全新 Runner，零缓存污染** |
| SCP 上传不完整 | **直接 registry pull，HTTP 客户端有完整校验** |
| 服务器资源紧张导致构建失败没感知 | **Runner 2核/7G 内存，构建失败直接 exit code 非零** |
| 多容器混淆 | **compose up -d 只 recreate 镜像变了的容器** |
| 脚本提前退出 | **部署脚本 set -e + deploy job 失败即报红** |
| 提交信息含引号致 detect 失败、deploy 被整体跳过 | **提交信息走 env 传值，避免 shell 二次解析** |
| AI 手动部署掩盖问题 | **push → Actions → 自动部署，全链路可追溯** |

**核心改变：** 构建和部署解耦了。构建在云端（干净环境、充足资源），部署在服务器（只 pull + up，零构建）。服务器永远不会因为构建污染而出现"部署幻觉"。

---

## 四、验证流程（部署后确认）

### 4.1 Actions Run 全绿

push 后打开：
https://github.com/Bearflower/Binance_quantitative_trading_yi/actions

必须看到：
- ✅ detect job（绿色）
- ✅ build-all 里所有被标记为"应该构建"的 job（绿色）
- ⚪ build-all 里 skip 的 job（灰色，正常）
- ✅ deploy job（绿色）

### 4.2 服务器容器状态

```bash
ssh root@43.156.242.184 "docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'trading_system|ai-tuner|data-backend|dashboard-api'"
```

预期：所有 11 个容器 image 字段都是 `ghcr.io/bearflower/trading-xxx:latest`。

### 4.3 部署日志

```bash
ssh root@43.156.242.184 "cat /root/trading_system/deploy_logs/$(date '+%Y%m%d').log"
```

预期：当日日志有 `DEPLOY_SUCCESS` 行。

### 4.4 VERSION 文件验证

```bash
# 本地
cat VERSION
# 对比
ssh root@43.156.242.184 "docker exec trading_system-btc_eth cat /app/VERSION"
```

预期：DEPLOY_ID 和 GIT_COMMIT 一致。

---

## 五、常见故障排查

### 问题 1：Actions YAML syntax error

**症状**：Run 秒级失败，显示 "Invalid workflow file"

**原因**：YAML 语法问题（anchor 不支持、嵌套 `${{ }}` 非法、缩进错误）

**排查**：
1. 打开 https://github.com/Bearflower/Binance_quantitative_trading_yi/actions → 看报错行号
2. 本地 VS Code 安装 "GitHub Actions YAML" 插件，能实时提示语法错误
3. 用 `yamllint .github/workflows/deploy.yml` 本地校验

### 问题 2：build-all 的某个 job 失败

**症状**：矩阵里个别 job 红了，其他绿

**常见原因**：
- Dockerfile 里 COPY 了不存在的文件（检查新文件有没有被 .dockerignore 排除）
- pip install 超时（GitHub Runner 在美国，默认源能用但慢）
- apt-get 包名拼错

**排查**：点进红的 job → 展开 step → 看具体 error

### 问题 3：deploy job SSH 失败

**症状**：deploy 红了，显示 SSH exit code 100

**CI 已内置 SSH 加固**：deploy job 在 `~/.ssh/config` 中统一定义 `ConnectTimeout 15` / `ServerAliveInterval 15` / `ServerAliveCountMax 3`（Host/User/IdentityFile 也由 config 提供），并由 `/tmp/ci_retry.sh` 的 `retry_connect` **仅对 exit 255（连接层失败）重试 3 次**；非 255 立即返回，避免远程脚本重复部署与审计日志污染。因此瞬时网络不可达的现象从"静默挂起约 16 分钟"变为"约 45 秒内快速失败"（Run #18 曾挂起 953 秒后报 255）。

**常见原因**：
- `SERVER_SSH_KEY` Secret 没配或配错
- 服务器密钥没正确绑定到云平台实例
- 服务器防火墙没放行 GitHub Actions Runner 的 IP 段

**排查**：
1. 服务器手动 SSH 测试：`ssh -i only.pem root@43.156.242.184 "echo ok"`
2. GitHub Secrets → Actions → 检查 `SERVER_SSH_KEY` 值是否正确
3. 服务器 SSH 日志：`ssh root@43.156.242.184 "journalctl -u sshd --since '10 min ago'"`

### 问题 4：服务器 docker pull GHCR 失败

**症状**：deploy job 里 `docker pull ghcr.io/...` 报认证失败

**常见原因**：
- `GHCR_PULL_TOKEN` Secret 没配
- PAT token 权限不够（需要 `read:packages`）
- 服务器 Docker 没 login

**修复**：服务器手动 login
```bash
ssh root@43.156.242.184
echo "<GHCR_PULL_TOKEN>" | docker login ghcr.io -u bearflower --password-stdin
```

### 问题 5：Actions 全绿但服务器容器没更新

**症状**：Actions 全绿，`docker ps` 显示容器的 Image 不是 ghcr.io

**原因**：compose pull 是幂等的——如果容器正在跑的镜像 tag 正好是 `latest`，compose 可能不 pull。

**修复**：手动拉
```bash
ssh root@43.156.242.184
cd /root/trading_system
docker compose pull          # 强制拉最新
docker compose up -d         # 只 recreate 镜像变了的容器
```

### 问题 6：紧急回滚

**场景**：push 后发现新代码有 bug，容器运行异常

**步骤**：
```bash
# 1. 本地回滚代码
git revert HEAD              # 创建回滚 commit
git push origin main         # push 回滚

# 2. 等 Actions 自动部署完（3-5 min）

# 或者手动紧急回滚（不 push，直接服务器操作）
ssh root@43.156.242.184
cd /root/trading_system
# 找到上一个稳定镜像的 tag
docker images ghcr.io/bearflower/trading-btc-eth --format "table {{.Tag}}\t{{.CreatedSince}}"
# 强制 down + up
docker compose down
docker compose up -d
```

### 问题 7：服务器磁盘满了

**症状**：docker pull 报 "no space left on device"

**清理**：
```bash
ssh root@43.156.242.184
docker system prune -f          # 清理悬空镜像、停止的容器、构建缓存
docker builder prune -f         # 清理构建缓存（更激进）
docker images prune -f          # 清理未使用镜像
```

### 问题 8：只改了 deploy.yml，却触发全部 10 个镜像重建

**现象**：一次推送只改了 `.github/` 下的文件（如 `deploy.yml`），但 build-all 10 个 job 全部绿色构建。

**这是有意为之，不是故障**。判定逻辑中，若变更**全部落在 `.github/`**，无法判断历史镜像是否已构建（典型的"修 CI"提交若全部 skip，会导致真正的代码修复永远无法上线），因此改为全部重建，专门用于打破该死循环。

---

## 六、手动运维速查（仅 Actions 故障时用）

```bash
# 手动 SSH 服务器
ssh -i ~/.ssh/id_rsa root@43.156.242.184

# 手动登录 GHCR（首次或 token 过期）
echo "<TOKEN>" | docker login ghcr.io -u bearflower --password-stdin

# 手动拉取镜像 + 更新
cd /root/trading_system
docker compose pull
docker compose up -d

# 查看所有容器状态
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'

# 查看部署日志
cat deploy_logs/$(date '+%Y%m%d').log

# 查看某个容器日志
docker logs --tail 100 trading_system-btc_eth

# 清理磁盘
docker system prune -f
```

---

**最后更新：** 2026-09-24（v3.1 — deploy job SSH 加固：超时参数 + 仅连接失败重试 3 次；detect job 提交信息插值改为 env 传值，防止引号导致 detect 失败、deploy 被整体跳过）
