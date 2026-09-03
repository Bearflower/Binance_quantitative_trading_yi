# 部署规则

## 部署触发条件

- 用户输入 `/deploy`，触发部署流程
- 用户明确要求部署到服务器，触发部署流程
- 用户输入 `/git`，完成提交后询问是否需要部署

---

## 一、"部署幻觉"根因分析（强制阅读）⭐⭐⭐

"部署幻觉"是指：**部署流程显示成功，但线上运行的仍是旧代码。**

| 根因 | 具体表现 | 概率 |
|------|---------|------|
| Docker 构建缓存 | `docker-compose build` 使用缓存层，旧代码被打包进镜像 | 高 |
| 旧镜像未删除 | 未执行 `docker rmi`，`up -d` 发现镜像已存在就不重建 | 高 |
| 构建失败未感知 | build 报错但脚本未用 `set -e`，继续执行 `up -d` 启动旧容器 | 中 |
| 文件上传不完整 | scp/rsync 中途中断，服务器上解压时文件损坏 | 中 |
| 容器启动失败 | 新容器启动后立即退出，docker-compose 未报错 | 中 |
| 多个容器混淆 | 服务器上有多个同名容器，更新的不是运行中的那个 | 低 |
| 卷挂载覆盖 | 代码通过 volume 挂载，但宿主机文件未更新 | 低 |
| 脚本提前退出 | 流水线中某一步失败但未阻断后续步骤 | 低 |

**核心问题：** 传统验证只检查"容器是否在运行"和"镜像创建时间"，但**不验证"容器内的代码是否与本地一致"**。

---

## 二、防幻觉机制总览

```
变更范围分析 → 部署前快照 → 版本标记 → 上传验证 → 按需构建验证 → 容器验证 → 代码验证 → 部署确认报告
```

每个阶段都有明确的验证点，任一阶段失败必须阻断后续流程。

---

## 三、部署前检查（强制）⭐⭐⭐

### 3.1 记录版本快照

记录当前本地和线上版本，作为后续对比基线：

```bash
# 本地版本快照
echo "=== 本地版本快照 ===" > /tmp/deploy_verify_$(date +%Y%m%d_%H%M%S).txt
git log --oneline -1 >> /tmp/deploy_verify_*.txt
echo "关键文件 MD5:" >> /tmp/deploy_verify_*.txt
md5sum strategies/btc_eth/main.py >> /tmp/deploy_verify_*.txt
md5sum strategies/btc_eth/config.yaml >> /tmp/deploy_verify_*.txt
md5sum shared/*.py >> /tmp/deploy_verify_*.txt

# 线上版本快照（部署前）
ssh root@SERVER_IP "echo '=== 线上容器启动时间 ===' && docker inspect -f '{{.Created}}' CONTAINER_NAME"
ssh root@SERVER_IP "echo '=== 线上容器内文件时间 ===' && docker exec CONTAINER_NAME stat /app/main.py 2>/dev/null || echo '容器未运行'"
```

### 3.2 代码同步检查

```bash
cd /Users/yl/vscode/Binance_quantitative_trading
bash scripts/check_code_sync.sh
```

**检查项：** 回测代码 vs 生产代码版本、策略参数一致性、共享模块完整性、代码修改时间差异。

### 3.3 环境区分

| 环境 | 代码位置 | 部署方式 |
|------|---------|---------|
| 回测环境 | `backtest/btc_eth/scripts/` | 本地执行，不部署到服务器 |
| 生产环境 | `strategies/btc_eth/` | 部署到服务器 |

### 3.4 配置文件检查

- `.env` — 环境变量
- `strategies/btc_eth/config.yaml` — 策略配置
- `.deploy_config` — 部署配置

### 3.5 生成版本标记文件

打包前自动生成 VERSION 文件，后续用它验证容器内代码是否为当前版本：

```bash
cat > VERSION << EOF
DEPLOY_TIME=$(date '+%Y-%m-%d %H:%M:%S')
GIT_COMMIT=$(git log --oneline -1 2>/dev/null || echo "no-git")
DEPLOY_ID=$(uuidgen | cut -d- -f1)
FILE_MD5=$(md5sum strategies/btc_eth/main.py | cut -d' ' -f1)
EOF
```

---

## 四、变更范围分析（强制）⭐⭐⭐

**部署前必须分析本次代码变更影响哪些容器，仅对受影响的目标容器进行重建，不触碰无关容器。**

### 4.1 变更与容器映射表

根据本项目的 Dockerfile 配置，代码变更与受影响容器的映射关系如下：

| 代码修改路径 | 受影响容器 | 影响性质 | 是否需要重建 |
|--------------|-----------|---------|------------|
| `shared/*.py` | btc-eth-strategy, new-coin-strategy, grid-strategy, hrs-strategy, ai-tuner | 直接 COPY 共享代码，所有容器共享同一份业务逻辑 | 必重建 |
| `strategies/btc_eth/*` | **btc-eth-strategy** | 策略专属代码，仅影响该策略容器 | 必重建 |
| `strategies/new_coin/*` | **new-coin-strategy** | 策略专属代码，仅影响该策略容器 | 必重建 |
| `strategies/grid/*` | **grid-strategy** | 策略专属代码，仅影响该策略容器 | 必重建 |
| `strategies/hrs/*` | **hrs-strategy**（⚠️ `hrs` 的 Dockerfile 使用 `COPY . /app/` 复制整个项目） | 策略专属代码，但构建时会复制整个项目目录 | 必重建 |
| `ai_tuner/*` | **ai-tuner** | AI 调优器代码 | 必重建 |
| `services/kline_service/*` | **kline-service** | K 线数据服务 | 必重建 |
| `services/kline_monitor/*` | **kline-monitor** | K 线健康监控服务 | 必重建 |
| `strategies/*/config.yaml` | ai-tuner + 对应策略容器 | 配置文件通过 volume 挂载，但构建时也会 COPY | 按需重建 |
| `requirements.txt` | btc-eth-strategy, new-coin-strategy, grid-strategy | 根目录依赖变更 | 必重建 |
| `.env` | **所有容器** | 环境变量通过 `env_file` 注入，修改后重启容器即可生效，**无需重建镜像** | 重启即可 |
| `database/postgres/init-scripts/*` | **postgres** | 初始化脚本通过 volume 挂载，重启即可 | 重启即可 |
| 任何文件 | **hrs-strategy**（`COPY . /app/`） | 该项目目录下任何文件变更都会影响 hrs 容器构建 | 必重建 |

### 4.2 确定受影响容器

部署前，根据 `git diff` 的结果，对照上表确定受影响容器清单：

```bash
# 1. 查看本次修改了哪些文件
CHANGED_FILES=$(git diff --name-only HEAD~1 HEAD)

# 2. 根据修改文件确定受影响容器
AFFECTED_SERVICES=()

if echo "$CHANGED_FILES" | grep -q "^shared/"; then
    AFFECTED_SERVICES+=(btc-eth-strategy new-coin-strategy grid-strategy hrs-strategy ai-tuner)
fi
if echo "$CHANGED_FILES" | grep -q "^strategies/btc_eth/"; then
    AFFECTED_SERVICES+=(btc-eth-strategy)
fi
if echo "$CHANGED_FILES" | grep -q "^strategies/new_coin/"; then
    AFFECTED_SERVICES+=(new-coin-strategy)
fi
if echo "$CHANGED_FILES" | grep -q "^strategies/grid/"; then
    AFFECTED_SERVICES+=(grid-strategy)
fi
if echo "$CHANGED_FILES" | grep -q "^strategies/hrs/"; then
    AFFECTED_SERVICES+=(hrs-strategy)
fi
if echo "$CHANGED_FILES" | grep -q "^ai_tuner/"; then
    AFFECTED_SERVICES+=(ai-tuner)
fi
if echo "$CHANGED_FILES" | grep -q "^services/kline_service/"; then
    AFFECTED_SERVICES+=(kline-service)
fi
if echo "$CHANGED_FILES" | grep -q "^services/kline_monitor/"; then
    AFFECTED_SERVICES+=(kline-monitor)
fi
# hrs 的特殊性：任何非 shared/ 目录变更都会影响 hrs
if echo "$CHANGED_FILES" | grep -qv "^shared/\|^backtest/\|^docs/\|^.trae/\|^.git"; then
    AFFECTED_SERVICES+=(hrs-strategy)
fi

# 3. 去重并输出受影响容器列表
echo "受影响容器（需重建）:"
printf '%s\n' "${AFFECTED_SERVICES[@]}" | sort -u
```

### 4.3 部署策略选择

根据受影响容器范围，选择对应的部署策略：

| 容器范围 | 部署策略 | 执行方式 |
|----------|---------|---------|
| **仅 1-2 个策略容器** | 按需重建 | `docker-compose build --no-cache <service>` → `docker-compose up -d <service>` |
| **共享模块变更（shared/）** | 重建所有依赖 shared 的容器 | 逐个 `build --no-cache` 并 `up -d` |
| **hrs 策略变更** | 因其 `COPY . /app/`，需重建 hrs 容器 | 单独重建 hrs |
| **仅 postgres 相关** | 无需构建镜像，重启即可 | `docker-compose restart postgres` |
| **仅 .env 变更** | 无需构建，重启容器即可 | `docker-compose restart <service>` |
| **首次部署 / 全量部署** | 全量重建 | `docker-compose down --remove-orphans` → 全量 `build --no-cache` → `docker-compose up -d` |

**核心原则：** 能按需重建就不全量重建，能重启就不重建。任一项验证失败只需修复对应的容器，不影响其他运行中的服务。

---

## 五、部署中防幻觉机制（强制）⭐⭐⭐

### 5.1 上传完整性验证

```bash
LOCAL_MD5=$(md5sum deployment_package.tar.gz | cut -d' ' -f1)
SERVER_MD5=$(ssh root@SERVER_IP "md5sum /root/deployment_package.tar.gz | cut -d' ' -f1")

if [ "$LOCAL_MD5" != "$SERVER_MD5" ]; then
    echo "❌ 文件上传不完整！本地MD5=$LOCAL_MD5 服务器MD5=$SERVER_MD5"
    exit 1
fi
echo "✅ 文件上传完整性验证通过"
```

### 5.2 按需重建容器（替代全量重建）

**根据第四节的变更范围分析结果，仅对受影响容器执行重建，不触碰无关容器。**

#### 5.2.1 按需重建（推荐，仅重建受影响容器）

```bash
ssh root@SERVER_IP << 'EOF'
set -e

cd /root/PROJECT_NAME

# 解压新代码包
tar -xzf /root/deployment_package.tar.gz -C /root/PROJECT_NAME

# 验证 VERSION 文件已正确解压
if [ ! -f "VERSION" ]; then
    echo "❌ VERSION 文件不存在，部署包可能损坏"
    exit 1
fi
cat VERSION

# 根据变更范围分析结果，逐个重建受影响容器
# 请替换为实际受影响的容器列表
AFFECTED_SERVICES=("btc-eth-strategy")

for SERVICE in "${AFFECTED_SERVICES[@]}"; do
    echo "=== 开始重建 $SERVICE ==="
    
    # 1. 删除该服务的旧镜像
    docker images | grep "$SERVICE" | awk '{print $3}' | xargs -r docker rmi --force
    
    # 2. 构建（不使用缓存）
    docker-compose build --no-cache "$SERVICE"
    
    # 3. 重新启动该服务（不影响其他运行中的容器）
    docker-compose up -d "$SERVICE"
    
    # 4. 等待容器就绪
    sleep 5
    
    # 5. 确认该容器正在运行
    if ! docker ps -q -f name="$SERVICE" | grep -q .; then
        echo "❌ $SERVICE 启动失败"
        docker logs --tail 50 "$SERVICE"
        exit 1
    fi
    echo "✅ $SERVICE 重建完成"
done

# 清理构建缓存（可选，节省磁盘空间）
docker builder prune -f
EOF
```

#### 5.2.2 全量重建（仅首次部署或清理积压旧镜像时使用）

```bash
ssh root@SERVER_IP << 'EOF'
set -e

cd /root/PROJECT_NAME

# 1. 停止并删除所有容器
docker-compose down --remove-orphans

# 2. 删除所有相关镜像（强制）
docker images | grep PROJECT_NAME | awk '{print $3}' | xargs -r docker rmi --force

# 3. 清理构建缓存
docker builder prune -f -a

# 4. 解压新代码包
tar -xzf /root/deployment_package.tar.gz -C /root/PROJECT_NAME

# 5. 验证 VERSION 文件
if [ ! -f "VERSION" ]; then
    echo "❌ VERSION 文件不存在，部署包可能损坏"
    exit 1
fi
cat VERSION

# 6. 全量构建
docker-compose build --no-cache

# 7. 全量启动
docker-compose up -d

# 8. 等待所有容器就绪
sleep 10

# 9. 确认所有容器都在运行
docker ps --format 'table {{.Names}}\t{{.Status}}'
EOF
```

#### 5.2.3 仅重启容器（无需重建的场景）

对于 `.env`、配置文件（volume 挂载）等变更，无需重建镜像，重启容器即可：

```bash
# 重启单个容器
docker-compose restart btc-eth-strategy

# 重启多个容器
docker-compose restart btc-eth-strategy kline-service

# 确认重启后容器状态
docker ps -f name=btc-eth-strategy --format '{{.Names}} {{.Status}}'
```

#### 5.2.4 策略选择指南

| 场景 | 使用方式 | 原因 |
|------|---------|------|
| 代码变更（shared/、strategies/、services/ 等） | 按需重建 5.2.1 | 仅重建受影响容器，其他服务不受影响 |
| 首次部署或清理积压旧镜像 | 全量重建 5.2.2 | 全新环境，需要完整初始化 |
| 仅 .env 或配置文件变更 | 仅重启 5.2.3 | 无需构建镜像，最快生效 |
| 仅 postgres 初始化脚本变更 | 仅重启 5.2.3 | postgres 使用预构建镜像，无需重建 |

### 5.3 构建日志错误检测

```bash
# 对每个受影响服务的构建日志进行检测
for SERVICE in "${AFFECTED_SERVICES[@]}"; do
    BUILD_LOG=$(ssh root@SERVER_IP "cd /root/PROJECT_NAME && docker-compose build --no-cache --progress=plain $SERVICE 2>&1")
    if echo "$BUILD_LOG" | grep -qi "error\|exception\|failed\|exit code"; then
        echo "❌ $SERVICE 构建过程中发现错误！"
        echo "$BUILD_LOG" | grep -i "error\|exception\|failed\|exit code"
        exit 1
    fi
    echo "✅ $SERVICE 构建通过"
done
```

---

## 六、部署后代码级验证（强制）⭐⭐⭐

**这是验证新版本代码是否真正在线上运行的核心步骤。**

### 第一层：容器状态验证

```bash
echo "=== 1. 容器运行状态 ==="
ssh root@SERVER_IP "docker ps -f name=CONTAINER_NAME --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
```

### 第二层：镜像 ID 验证

```bash
echo "=== 2. 镜像版本验证 ==="
CONTAINER_IMAGE_ID=$(ssh root@SERVER_IP "docker inspect -f '{{.Image}}' CONTAINER_NAME")
LATEST_IMAGE_ID=$(ssh root@SERVER_IP "docker images --no-trunc --format '{{.ID}}' | head -1")

if [ "$CONTAINER_IMAGE_ID" != "$LATEST_IMAGE_ID" ]; then
    echo "❌ 部署幻觉检测失败：容器使用的镜像不是最新构建的镜像！"
    exit 1
fi
echo "✅ 镜像版本一致"
```

### 第三层：VERSION 文件验证（关键）⭐⭐⭐

```bash
echo "=== 3. VERSION 文件验证 ==="
LOCAL_DEPLOY_ID=$(grep DEPLOY_ID VERSION | cut -d= -f2)
CONTAINER_DEPLOY_ID=$(ssh root@SERVER_IP "docker exec CONTAINER_NAME cat /app/VERSION 2>/dev/null | grep DEPLOY_ID | cut -d= -f2" || echo "NOT_FOUND")

if [ "$CONTAINER_DEPLOY_ID" = "NOT_FOUND" ]; then
    echo "❌ 部署幻觉检测失败：容器内不存在 VERSION 文件！说明容器内运行的代码不是本次部署的代码"
    exit 1
fi

if [ "$LOCAL_DEPLOY_ID" != "$CONTAINER_DEPLOY_ID" ]; then
    echo "❌ 部署幻觉检测失败：VERSION 文件不匹配！"
    echo "   本地 DEPLOY_ID: $LOCAL_DEPLOY_ID"
    echo "   容器内 DEPLOY_ID: $CONTAINER_DEPLOY_ID"
    exit 1
fi
echo "✅ VERSION 文件匹配，确认容器内为本次部署代码"
```

### 第四层：代码 MD5 校验（终极验证）⭐⭐⭐

```bash
echo "=== 4. 代码 MD5 校验 ==="
KEY_FILES=(
    "strategies/btc_eth/main.py"
    "strategies/btc_eth/config.yaml"
    "shared/constants.py"
)

for FILE in "${KEY_FILES[@]}"; do
    LOCAL_MD5=$(md5sum "$FILE" | cut -d' ' -f1)
    CONTAINER_MD5=$(ssh root@SERVER_IP "docker exec CONTAINER_NAME md5sum /app/$FILE 2>/dev/null | cut -d' ' -f1" || echo "NOT_FOUND")
    
    if [ "$CONTAINER_MD5" = "NOT_FOUND" ]; then
        echo "⚠️  容器内 $FILE 不存在，检查路径是否正确"
        continue
    fi
    if [ "$LOCAL_MD5" != "$CONTAINER_MD5" ]; then
        echo "❌ 部署幻觉检测失败：$FILE MD5 不匹配！"
        echo "   本地: $LOCAL_MD5"
        echo "   容器: $CONTAINER_MD5"
        exit 1
    fi
    echo "✅ $FILE MD5 匹配"
done
```

### 第五层：功能验证

```bash
echo "=== 5. 功能验证 ==="
ERROR_COUNT=$(ssh root@SERVER_IP "docker logs --tail 200 CONTAINER_NAME 2>&1 | grep -cE "(ERROR|Exception|FATAL|CRITICAL|Traceback)"")
if [ "$ERROR_COUNT" -gt 0 ]; then
    echo "⚠️  发现 $ERROR_COUNT 个错误日志，请检查："
    ssh root@SERVER_IP "docker logs --tail 50 CONTAINER_NAME 2>&1 | grep -E '(ERROR|Exception|FATAL|CRITICAL|Traceback)'"
fi
```

---

## 七、部署确认报告（强制）⭐⭐⭐

所有验证通过后，必须生成部署确认报告：

```bash
#!/bin/bash
REPORT_FILE="/tmp/deploy_report_$(date +%Y%m%d_%H%M%S).md"

cat > "$REPORT_FILE" << REPORT_HEADER
# 部署确认报告

## 基本信息
- 部署时间: $(date '+%Y-%m-%d %H:%M:%S')
- 部署人员: $(whoami)
- 目标服务器: $SERVER_IP
- 项目名称: $PROJECT_NAME
- 容器名称: $CONTAINER_NAME

## 版本信息
- Git Commit: $(git log --oneline -1 2>/dev/null || echo "N/A")
- 部署 ID: $(grep DEPLOY_ID VERSION | cut -d= -f2)
- 部署时间戳: $(grep DEPLOY_TIME VERSION | cut -d= -f2)

## 验证结果
REPORT_HEADER

# 容器状态
CONTAINER_STATUS=$(ssh root@SERVER_IP "docker ps -f name=$CONTAINER_NAME --format '{{.Status}}'")
echo "- 容器运行状态: ✅ $CONTAINER_STATUS" >> "$REPORT_FILE"

# 镜像一致性
CONTAINER_IMAGE_ID=$(ssh root@SERVER_IP "docker inspect -f '{{.Image}}' $CONTAINER_NAME")
LATEST_IMAGE_ID=$(ssh root@SERVER_IP "docker images --no-trunc --format '{{.ID}}' | head -1")
if [ "$CONTAINER_IMAGE_ID" = "$LATEST_IMAGE_ID" ]; then
    echo "- 镜像版本一致性: ✅ 一致" >> "$REPORT_FILE"
else
    echo "- 镜像版本一致性: ❌ 不一致" >> "$REPORT_FILE"
fi

# VERSION 文件
CONTAINER_VERSION=$(ssh root@SERVER_IP "docker exec $CONTAINER_NAME cat /app/VERSION 2>/dev/null | grep DEPLOY_ID | cut -d= -f2" || echo "NOT_FOUND")
LOCAL_VERSION=$(grep DEPLOY_ID VERSION | cut -d= -f2)
if [ "$CONTAINER_VERSION" = "$LOCAL_VERSION" ]; then
    echo "- 容器内代码版本: ✅ 匹配 (部署ID: $CONTAINER_VERSION)" >> "$REPORT_FILE"
else
    echo "- 容器内代码版本: ❌ 不匹配" >> "$REPORT_FILE"
fi

# 关键文件 MD5
MD5_PASS=true
for FILE in "strategies/btc_eth/main.py" "strategies/btc_eth/config.yaml"; do
    LOCAL_MD5=$(md5sum "$FILE" 2>/dev/null | cut -d' ' -f1)
    CONTAINER_MD5=$(ssh root@SERVER_IP "docker exec $CONTAINER_NAME md5sum /app/$FILE 2>/dev/null" | cut -d' ' -f1)
    if [ "$LOCAL_MD5" = "$CONTAINER_MD5" ]; then
        echo "- 文件 $FILE: ✅ MD5 匹配" >> "$REPORT_FILE"
    else
        echo "- 文件 $FILE: ❌ MD5 不匹配" >> "$REPORT_FILE"
        MD5_PASS=false
    fi
done

# 最终结论
echo "" >> "$REPORT_FILE"
echo "## 最终结论" >> "$REPORT_FILE"
if [ "$CONTAINER_IMAGE_ID" = "$LATEST_IMAGE_ID" ] && [ "$CONTAINER_VERSION" = "$LOCAL_VERSION" ] && [ "$MD5_PASS" = true ]; then
    echo "✅ **部署成功！新版本代码已确认在生产环境中运行。**" >> "$REPORT_FILE"
    echo "   - 镜像版本: 一致" >> "$REPORT_FILE"
    echo "   - 代码版本: 匹配" >> "$REPORT_FILE"
    echo "   - 文件校验: 通过" >> "$REPORT_FILE"
else
    echo "❌ **部署失败！存在部署幻觉风险。**" >> "$REPORT_FILE"
    echo "   请检查上述验证失败项并重新部署。" >> "$REPORT_FILE"
fi

cat "$REPORT_FILE"
echo ""
echo "报告已保存到: $REPORT_FILE"
```

---

## 八、反幻觉检查清单

部署完成后，必须逐项检查：

```
□ 1. 变更范围分析: 仅重建了受影响容器，无关容器未受影响
□ 2. 容器状态确认: docker ps 显示所有目标容器运行中
□ 3. 镜像 ID 对比: 容器镜像 ID == 最新构建镜像 ID
□ 4. VERSION 文件: 容器内 DEPLOY_ID == 本地 DEPLOY_ID
□ 5. 关键文件 MD5: 容器内文件 MD5 == 本地文件 MD5
□ 6. 日志无错误: 容器日志中无 error/exception/fatal
□ 7. 部署确认报告: 已生成并保存
□ 8. 无遗漏服务: 未重建的容器仍正常运行（docker ps 确认）

**任一检查项失败，视为部署失败，必须修复后重新部署。**

---

## 九、代码同步规则

### 回测代码 vs 生产代码

- 修改了 `backtest/` 目录下的代码，必须检查 `strategies/` 是否需要同步
- 修改了共享逻辑，必须同步到 `shared/` 目录
- 部署前必须确认生产环境代码已更新

### 同步流程

1. 在回测环境验证策略改动
2. 将验证通过的逻辑同步到生产环境代码
3. 更新配置文件中的参数
4. 执行代码同步检查
5. 部署到生产环境

---

## 十、常见问题处理

### 问题1：VERSION 文件不匹配

**症状：** 容器内 VERSION 文件的 DEPLOY_ID 与本地不同

**解决方案：**
1. 确认本地代码是最新的：`git status`
2. 确认部署包是最新的：重新打包
3. 执行强制重新部署：

```bash
ssh root@SERVER_IP << 'EOF'
cd /root/PROJECT_NAME
docker-compose down
docker images | grep PROJECT_NAME | awk '{print $3}' | xargs -r docker rmi --force
docker builder prune -f -a
EOF
./one_click_deploy.sh
```

### 问题2：容器内文件 MD5 不匹配

**可能原因：** 部署包中文件损坏、构建时使用了缓存层、Dockerfile 中 COPY 路径有误

**解决方案：**
1. 检查 Dockerfile 中的 COPY 指令是否正确
2. 检查 .dockerignore 是否过滤了必要文件
3. 重新执行部署（带 --no-cache）

### 问题3：容器未更新（按需重建场景）

**症状：** 按需重建后，某容器运行的仍是旧代码

**解决方案：**
```bash
# 1. 先确认该容器是否在受影响列表内
# 2. 如果不在，说明变更范围分析有误，补充容器名
# 3. 单独重建该容器
ssh root@SERVER_IP << 'EOF'
cd /root/PROJECT_NAME
docker-compose down SERVICE_NAME
docker images | grep SERVICE_NAME | awk '{print $3}' | xargs -r docker rmi --force
docker-compose build --no-cache SERVICE_NAME
docker-compose up -d SERVICE_NAME
EOF
```

### 问题4：文件遗漏

**解决方案：**
1. 检查打包脚本的排除规则
2. 手动上传遗漏的文件
3. 重启容器

### 问题5：【Binance quantitative trading】PostgreSQL 容器命名冲突导致部署中断

**根因：** `.deploy_config` 中的 `POSTGRES_CONTAINER_NAME` 与 docker-compose.yml 中实际的容器名不一致（如 `postgres-db` vs `trading_system-postgres`），导致部署脚本每次都认为 postgres 未运行，尝试 `docker-compose up -d postgres` 时触发命名冲突。

**另一点：** docker-compose 项目名变更时，会产生带前缀的残留容器（如 `b62539d01e8b_trading_system-postgres`），与现有容器名冲突。

**影响：** 部署脚本使用 `set -e`，postgres 启动失败后整个 SSH 脚本退出，**后续所有服务（btc_eth、kline-monitor 等）都不会被启动**，但这些服务的状态不会被报告为"部署失败"。

**解决方案：**
1. 确保 `.deploy_config` 中的 `POSTGRES_CONTAINER_NAME` 与 docker-compose.yml 一致
2. 启动 postgres 前清理所有残留的旧 pg 容器：`docker rm -f $(docker ps -aq -f name=postgres)`
3. 使用 `||` 降级方案：`docker-compose up -d postgres || docker run -d ...` 直接创建

### 问题6：【Binance quantitative trading】部署后缺少容器（如 kline-monitor）

**症状：** 部署完成后，部分容器（如 `trading_system-kline-monitor`）未运行，甚至不存在。

**根因：** 部署脚本中 postgres 启动失败（见问题5），导致 `set -e` 退出 SSH 脚本，排在 postgres 后面的服务全部被跳过。

**注意：** 即使 postgres 启动成功，`docker-compose up -d <service>` 也可能因为 `depends_on` 条件不满足而跳过。例如 `kline_monitor` 依赖 `postgres`，如果 postgres 健康检查未通过，`kline_monitor` 不会被启动。

**解决方案：**
1. 部署完成后，必须执行 `docker ps | grep kline-monitor` 确认所有服务都在运行
2. 如果缺少某个服务，单独启动：`docker-compose up -d kline-monitor`
3. 长期方案：将部署脚本中的 `set -e` 改为对非关键服务不阻断，或使用 `|| true` 降级

### 问题7：【Binance quantitative trading】按需重建后依赖服务未启动

**症状：** 按需重建某个策略容器（如 `btc-eth-strategy`）后，该容器正常启动，但依赖它的其他服务（如 `ai-tuner` 读取策略数据）功能异常。

**根因：** 按需重建时，`docker-compose up -d <service>` 会启动该服务及其 `depends_on` 依赖，但**不会启动依赖该服务的其他服务**。如果被重建的服务有版本兼容性变化，依赖它的上游服务可能因为 API 不兼容而出错。

**发生场景：** 策略代码变更（如数据库 schema 变更、API 接口签名变更），但依赖该策略的其他服务未同步重建。

**解决方案：**
1. 变更范围分析时，不仅要考虑哪个容器直接受代码变更影响，还要考虑**间接依赖关系**
2. 如果变更涉及接口/协议/数据库 schema 变化，应同时重建所有依赖该服务的上游容器
3. 部署完成后，不仅要检查被重建的容器，还要检查依赖它的上游容器日志无错误
4. 不确定时，优先使用全量重建（5.2.2）

---

## 十一、部署命令速查

```bash
# 代码同步检查
bash scripts/check_code_sync.sh

# 变更范围分析（确定受影响容器）
git diff --name-only HEAD~1 HEAD | grep -oP '^[^/]+/[^/]+' | sort -u

# 生成版本标记
cat > VERSION << EOF
DEPLOY_TIME=$(date '+%Y-%m-%d %H:%M:%S')
GIT_COMMIT=$(git log --oneline -1 2>/dev/null || echo "no-git")
DEPLOY_ID=$(uuidgen | cut -d- -f1)
FILE_MD5=$(md5sum strategies/btc_eth/main.py | cut -d' ' -f1)
EOF

# 按需部署（仅重建受影响容器）
ssh root@SERVER_IP "cd /root/PROJECT_NAME && docker-compose build --no-cache btc-eth-strategy && docker-compose up -d btc-eth-strategy"

# 全量部署
./one_click_deploy.sh

# 验证部署（五层验证）
./verify_deployment.sh

# 生成部署确认报告
./generate_deploy_report.sh

# 查看所有容器状态（确认无遗漏）
ssh root@SERVER_IP "docker ps --format 'table {{.Names}}\t{{.Status}}'"

# 单独检查某个服务状态
ssh root@SERVER_IP "docker ps -f name=btc-eth-strategy --format '{{.Names}} {{.Status}}' || echo '❌ 未运行'"

# 仅重启容器（无需重建）
ssh root@SERVER_IP "cd /root/PROJECT_NAME && docker-compose restart btc-eth-strategy"

# 查看容器日志
ssh root@SERVER_IP "docker logs --tail 50 CONTAINER_NAME"

# 容器内代码校验（按需重建后验证）
ssh root@SERVER_IP "docker exec CONTAINER_NAME cat /app/VERSION"
ssh root@SERVER_IP "docker exec CONTAINER_NAME md5sum /app/main.py"
```

---

## 相关技能

- **服务器自动化部署** — 详细的部署流程和脚本
- **通用模块调用指南** — K线服务、通知服务等通用模块的使用

---

**最后更新：** 2026-09-03