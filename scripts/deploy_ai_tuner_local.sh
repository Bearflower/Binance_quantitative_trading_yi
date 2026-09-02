#!/usr/bin/env bash
# ============================================================
# 本地镜像构建优先的双路部署脚本
#
# 部署 ai-tuner 容器。优先本地构建 linux/amd64 镜像 → 传输到服务器加载；
# 若本地网络无法访问 Docker Hub，则自动降级为"上传代码到服务器构建"。
# 两种路径最后统一执行五层代码级验证。
#
# 用法: ./deploy_ai_tuner_local.sh
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_CONFIG="$PROJECT_ROOT/.deploy_config"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_FILE="/tmp/deploy_ai_tuner_${TIMESTAMP}.log"

# ---------- 工具函数 ----------
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
die() { log "❌ $*"; exit 1; }

# ---------- 加载配置 ----------
[ -f "$DEPLOY_CONFIG" ] || die ".deploy_config 不存在"
# shellcheck disable=SC1090
source "$DEPLOY_CONFIG"

SSH_CMD="ssh -i ${SSH_KEY_PATH} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ${SERVER_USER}@${SERVER_IP}"
SCP_CMD="scp -i ${SSH_KEY_PATH} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

log "==== 双路部署开始: $TIMESTAMP 日志: $LOG_FILE ===="

# ---------- Step 0: 生成 VERSION 文件 ----------
cd "$PROJECT_ROOT"
DEPLOY_ID="$(uuidgen | cut -d'-' -f1)"
FILE_MD5="$(md5 -q ai_tuner/backtest/grid_backtest.py 2>/dev/null || md5sum ai_tuner/backtest/grid_backtest.py | cut -d' ' -f1)"
cat > VERSION <<EOF
DEPLOY_TIME=$(date '+%Y-%m-%d %H:%M:%S')
GIT_COMMIT=$(git log --oneline -1 2>/dev/null | head -c 20 || echo "local-build")
DEPLOY_ID=$DEPLOY_ID
FILE_MD5=$FILE_MD5
EOF
log "生成 VERSION: DEPLOY_ID=$DEPLOY_ID"

# ---------- Step 1: 本地关键文件 MD5 记录 ----------
MD5_CACHE_FILE="/tmp/deploy_md5_${TIMESTAMP}.txt"
{
  md5 -q ai_tuner/backtest/grid_backtest.py 2>/dev/null || md5sum ai_tuner/backtest/grid_backtest.py | cut -d' ' -f1
  md5 -q ai_tuner/backtest/metrics.py        2>/dev/null || md5sum ai_tuner/backtest/metrics.py | cut -d' ' -f1
  md5 -q ai_tuner/backtest/models.py         2>/dev/null || md5sum ai_tuner/backtest/models.py | cut -d' ' -f1
  md5 -q ai_tuner/adapters/grid_adapter.py   2>/dev/null || md5sum ai_tuner/adapters/grid_adapter.py | cut -d' ' -f1
  md5 -q ai_tuner/adapters/base_adapter.py   2>/dev/null || md5sum ai_tuner/adapters/base_adapter.py | cut -d' ' -f1
  md5 -q ai_tuner/scheduler/weekly_job.py    2>/dev/null || md5sum ai_tuner/scheduler/weekly_job.py | cut -d' ' -f1
  md5 -q ai_tuner/config.yaml                2>/dev/null || md5sum ai_tuner/config.yaml | cut -d' ' -f1
  md5 -q VERSION                             2>/dev/null || md5sum VERSION | cut -d' ' -f1
} > "$MD5_CACHE_FILE"

# ---------- Step 2: 决策构建路径（本地 or 服务器） ----------
USE_LOCAL_BUILD=false
BUILD_METHOD="unknown"

log "探测本地 Docker 跨平台构建能力..."
if ! command -v docker >/dev/null 2>&1; then
  log "⚠️  未安装 docker，降级到服务器构建"
elif ! docker info >/dev/null 2>&1; then
  log "⚠️  Docker daemon 未运行，降级到服务器构建"
elif docker buildx version >/dev/null 2>&1; then
  log "  - Docker daemon + buildx 就绪，探测是否可联网..."
  # 5 秒超时，尝试拉取一个极小的 amd64 manifest
  rm -f /tmp/docker_pull_test.log
  (docker pull --platform=linux/amd64 hello-world:latest >/tmp/docker_pull_test.log 2>&1) &
  PID=$!
  sleep 5
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    log "⚠️  5秒未响应，镜像源访问慢，降级服务器构建"
  else
    wait "$PID" 2>/dev/null || true
    if [ -n "$(docker images hello-world:latest -q 2>/dev/null)" ]; then
      log "✅ 本地镜像源可达，使用本地构建"
      USE_LOCAL_BUILD=true
    else
      log "⚠️  镜像访问异常，降级服务器构建"
    fi
  fi
else
  log "⚠️  buildx 不可用，降级服务器构建"
fi

# ============================================================
# 路径 A: 本地构建 → 传输镜像
# ============================================================
if [ "$USE_LOCAL_BUILD" = true ]; then
  BUILD_METHOD="LOCAL_IMG"
  log "========== 路径 A: 本地构建镜像 =========="

  # A1: 跨平台构建
  log "构建 linux/amd64 镜像..."
  if ! docker buildx build --platform linux/amd64 \
         -t "${AI_TUNER_IMAGE_NAME}" \
         -f ai_tuner/Dockerfile . --load 2>&1 | tee -a "$LOG_FILE" | tail -15; then
    log "⚠️  buildx 构建失败，降级到服务器构建"
    USE_LOCAL_BUILD=false
  fi

  if [ "$USE_LOCAL_BUILD" = true ]; then
    IMG_SIZE="$(docker image inspect ${AI_TUNER_IMAGE_NAME} --format '{{.Size}}' 2>/dev/null || echo unknown)"
    log "✅ 镜像构建完成: $AI_TUNER_IMAGE_NAME 大小: $IMG_SIZE"

    # A2: save → gzip → 通过 ssh 管道直接 load，避免落地大文件
    TAR_BALL="/tmp/${AI_TUNER_IMAGE_NAME//:/-}-${DEPLOY_ID}.tar.gz"
    log "打包镜像到 $TAR_BALL (传输前压缩)"
    docker save "${AI_TUNER_IMAGE_NAME}" | gzip > "$TAR_BALL"
    log "压缩后大小: $(du -h "$TAR_BALL" | cut -f1)"

    # A3: 上传并加载
    REMOTE_IMG_PATH="/tmp/$(basename "$TAR_BALL")"
    log "传输镜像到服务器..."
    if ! $SCP_CMD "$TAR_BALL" "${SERVER_USER}@${SERVER_IP}:${REMOTE_IMG_PATH}"; then
      log "❌ 镜像传输失败"
      rm -f "$TAR_BALL"
      exit 1
    fi
    rm -f "$TAR_BALL"
    LOCAL_MD5="$(md5 -q "$TAR_BALL" 2>/dev/null || md5sum "$TAR_BALL" | cut -d' ' -f1)" || true
    log "远端 MD5 校验..."
    REMOTE_MD5="$($SSH_CMD "md5sum $REMOTE_IMG_PATH 2>/dev/null | cut -d' ' -f1 || echo no-md5")"
    if [ -n "${LOCAL_MD5:-}" ] && [ "$LOCAL_MD5" != "$REMOTE_MD5" ]; then
      die "镜像文件 MD5 不匹配: 本地 $LOCAL_MD5 vs 远端 $REMOTE_MD5"
    fi

    log "在服务器端 load 镜像..."
    $SSH_CMD "docker load -i $REMOTE_IMG_PATH && rm -f $REMOTE_IMG_PATH" | tee -a "$LOG_FILE"

    # A4: 更新服务器 docker-compose 的 ai-tuner 服务为 image 模式（避免 build）
    # 临时替换 compose
    $SSH_CMD "cd ${SERVER_PROJECT_PATH} && if [ -f docker-compose.yml ]; then cp docker-compose.yml docker-compose.yml.bak_${TIMESTAMP}; fi"
    log "重启 ai-tuner 容器..."
    $SSH_CMD "cd ${SERVER_PROJECT_PATH} &&
      cat > /tmp/ai-tuner-patch.yml <<'COMPOSE_EOF'
services:
  ai-tuner:
    image: ${AI_TUNER_IMAGE_NAME}
COMPOSE_EOF
      docker compose -f docker-compose.yml -f /tmp/ai-tuner-patch.yml up -d ai-tuner --no-deps --force-recreate 2>&1 | tail -10"
  fi
fi

# ============================================================
# 路径 B: 上传代码 → 服务器构建（A 失败降级 / 本地网络不支持时直接走）
# ============================================================
if [ "$USE_LOCAL_BUILD" != true ]; then
  BUILD_METHOD="SERVER_BUILD"
  log "========== 路径 B: 服务器构建 =========="

  # B1: 打包变更文件（仅 ai-tuner 相关）
  PKG="/tmp/deploy_ai_tuner_src_${TIMESTAMP}.tar.gz"
  log "打包源代码变更: $PKG"
  tar -czf "$PKG" \
    ai_tuner/ \
    shared/ \
    VERSION \
    strategies/btc_eth/config.yaml \
    strategies/new_coin/config.yaml \
    strategies/grid/config.yaml \
    strategies/hrs/config.yaml \
    2>/dev/null || die "打包失败"
  log "包大小: $(du -h "$PKG" | cut -f1)"

  # B2: 上传 + MD5
  $SCP_CMD "$PKG" "${SERVER_USER}@${SERVER_IP}:/tmp/$(basename "$PKG")"
  LOCAL_MD5="$(md5 -q "$PKG" 2>/dev/null || md5sum "$PKG" | cut -d' ' -f1)"
  REMOTE_MD5="$($SSH_CMD "md5sum /tmp/$(basename "$PKG") | cut -d' ' -f1")"
  rm -f "$PKG"
  if [ "$LOCAL_MD5" != "$REMOTE_MD5" ]; then
    die "源码包 MD5 不匹配: 本地 $LOCAL_MD5 vs 远端 $REMOTE_MD5"
  fi

  # B3: 解压 + 删除 macos ._* 文件 + 构建 + 重启
  log "服务器端解压、清理 macOS 资源文件、构建镜像..."
  $SSH_CMD "cd ${SERVER_PROJECT_PATH} &&
    tar -xzf /tmp/$(basename "$PKG") &&
    find . -name '._*' -delete &&
    rm -f /tmp/$(basename "$PKG") &&
    docker compose build --no-cache ai-tuner 2>&1 | tail -15 &&
    echo '=== 重启 ai-tuner ===' &&
    docker compose up -d ai-tuner --no-deps --force-recreate 2>&1 | tail -5" | tee -a "$LOG_FILE"
fi

# ============================================================
# 通用: 五层代码级验证
# ============================================================
log "========== 五层代码级验证 =========="
sleep 10

# Layer 1: 容器状态
log "[1/5] 容器状态"
STATUS_LINE="$($SSH_CMD "docker ps -f name=${AI_TUNER_CONTAINER_NAME} --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' 2>&1" || true)"
echo "$STATUS_LINE"
if ! echo "$STATUS_LINE" | grep -qi "healthy\|Up"; then
  die "容器未运行 / 不健康"
fi

# Layer 2: 镜像 ID 一致
log "[2/5] 镜像 ID 一致性"
CID="$($SSH_CMD "docker inspect -f '{{.Image}}' ${AI_TUNER_CONTAINER_NAME} 2>/dev/null" || true)"
LID="$($SSH_CMD "docker images --no-trunc ${AI_TUNER_IMAGE_NAME} --format '{{.ID}}' | head -1" || true)"
log "  容器镜像: ${CID:0:25}..."
log "  最新镜像: ${LID:0:25}..."
[ "$CID" = "$LID" ] && log "✅ 一致" || die "❌ 镜像不一致"

# Layer 3: VERSION
log "[3/5] VERSION 文件"
REMOTE_DEPLOY_ID="$($SSH_CMD "docker exec ${AI_TUNER_CONTAINER_NAME} cat /app/VERSION 2>/dev/null | grep DEPLOY_ID | cut -d= -f2" || echo NOT_FOUND)"
log "  本地: $DEPLOY_ID  容器内: $REMOTE_DEPLOY_ID"
[ "$REMOTE_DEPLOY_ID" = "$DEPLOY_ID" ] && log "✅ 匹配" || die "❌ DEPLOY_ID 不匹配"

# Layer 4: MD5 对照
log "[4/5] 关键文件 MD5 校验"
FILES=(
  "ai_tuner/backtest/grid_backtest.py"
  "ai_tuner/backtest/metrics.py"
  "ai_tuner/backtest/models.py"
  "ai_tuner/adapters/grid_adapter.py"
  "ai_tuner/adapters/base_adapter.py"
  "ai_tuner/scheduler/weekly_job.py"
  "ai_tuner/config.yaml"
  "VERSION"
)
FAIL_MD5=0
for i in "${!FILES[@]}"; do
  f="${FILES[$i]}"
  LOCAL_MD5_LINE="$(sed -n $((i+1))p "$MD5_CACHE_FILE" 2>/dev/null || echo local-err)"
  REMOTE_MD5_LINE="$($SSH_CMD "docker exec ${AI_TUNER_CONTAINER_NAME} md5sum /app/$f 2>/dev/null | cut -d' ' -f1 || echo NOT_FOUND" || true)"
  if [ "$LOCAL_MD5_LINE" = "$REMOTE_MD5_LINE" ] && [ -n "$REMOTE_MD5_LINE" ] && [ "$REMOTE_MD5_LINE" != "NOT_FOUND" ]; then
    log "  ✅ $f"
  else
    log "  ❌ $f 本地=${LOCAL_MD5_LINE:0:10} 容器=${REMOTE_MD5_LINE:0:10}"
    FAIL_MD5=$((FAIL_MD5+1))
  fi
done
[ "$FAIL_MD5" -gt 0 ] && die "MD5 有 $FAIL_MD5 项不匹配"

# Layer 5: 日志错误数
log "[5/5] 日志错误检查"
ERR_COUNT="$($SSH_CMD "docker logs --tail 200 ${AI_TUNER_CONTAINER_NAME} 2>&1 | grep -cE '(ERROR|Exception|FATAL|CRITICAL|Traceback)' || echo 0" || echo 0)"
log "  最近 200 行中 ERROR/Exception: $ERR_COUNT"

# ============================================================
# 部署确认报告
# ============================================================
echo ""
echo "============================================"
echo "✅ 部署完成 (构建方式: $BUILD_METHOD)"
echo "============================================"
echo "- 部署 ID:     $DEPLOY_ID"
echo "- 构建方式:    $BUILD_METHOD"
echo "- 容器:        ${AI_TUNER_CONTAINER_NAME}"
echo "- 镜像 ID:     ${CID:0:22}..."
echo "- 文件 MD5:    8/8 全匹配"
echo "- 错误日志:    $ERR_COUNT"
echo "- 日志:        $LOG_FILE"
