#!/usr/bin/env bash
# Full remote deployment runtime. Workflow injects validated mode/action/identity.
set -e
cd /root/trading_system
export COMPOSE_FILE=docker-compose.yml:docker-compose.logging.yml
mkdir -p .deploy_tmp
exec 9>.deploy_tmp/compose_deploy.lock
flock -n 9 || { echo "❌ 另一个受控部署正在执行，停止本次部署"; exit 1; }

echo "========================================"
echo "CI/CD 自动部署"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
DEPLOY_MODE="__DEPLOY_MODE__"
FUNDING_DEPLOY_ACTION="__FUNDING_DEPLOY_ACTION__"
COMMIT_SHA="__COMMIT_SHA__"
DEPLOY_ID="__DEPLOY_ID__"
echo "模式: ${DEPLOY_MODE}"
echo "========================================"

# Scoped release exits before secrets, full cleanup, init SQL replay or broad up.
if [ "$DEPLOY_MODE" = "ledger-risk-remediation" ]; then
  [ "$FUNDING_DEPLOY_ACTION" = "keep" ] || { echo "funding rotation forbidden"; exit 2; }
  unset COMPOSE_FILE
  BUNDLE="/root/trading_system/.deploy_tmp/ledger-risk-${COMMIT_SHA}"
  python3 "$BUNDLE/scripts/scoped_ledger_risk_release.py" --bundle "$BUNDLE" --sha "$COMMIT_SHA"
  exit $?
fi
if [ "$DEPLOY_MODE" = "strategy-risk-successor" ]; then
  [ "$FUNDING_DEPLOY_ACTION" = "keep" ] || { echo "funding rotation forbidden"; exit 2; }
  unset COMPOSE_FILE
  BUNDLE="/root/trading_system/.deploy_tmp/strategy-successor-${COMMIT_SHA}"
  python3 "$BUNDLE/scripts/scoped_strategy_successor.py" --bundle "$BUNDLE" --sha "$COMMIT_SHA"
  exit $?
fi
if [ "$DEPLOY_MODE" = "strategy-risk-followup" ]; then
  [ "$FUNDING_DEPLOY_ACTION" = "keep" ] || { echo "funding rotation forbidden"; exit 2; }
  unset COMPOSE_FILE
  BUNDLE="/root/trading_system/.deploy_tmp/risk-followup-${COMMIT_SHA}"
  python3 "$BUNDLE/scripts/scoped_risk_followup.py" --bundle "$BUNDLE" --sha "$COMMIT_SHA"
  exit $?
fi
if [ "$DEPLOY_MODE" = "strategy-stop-followup" ]; then
  [ "$FUNDING_DEPLOY_ACTION" = "keep" ] || { echo "funding rotation forbidden"; exit 2; }
  unset COMPOSE_FILE
  BUNDLE="/root/trading_system/.deploy_tmp/stop-followup-${COMMIT_SHA}"
  python3 "$BUNDLE/scripts/scoped_stop_followup.py" --bundle "$BUNDLE" --sha "$COMMIT_SHA"
  exit $?
fi
if [ "$DEPLOY_MODE" = "income-attribution" ]; then
  [ "$FUNDING_DEPLOY_ACTION" = "keep" ] || { echo "funding rotation forbidden"; exit 2; }
  unset COMPOSE_FILE
  BUNDLE="/root/trading_system/.deploy_tmp/income-attribution-${COMMIT_SHA}"
  python3 "$BUNDLE/scripts/scoped_income_attribution_release.py" --bundle "$BUNDLE" --sha "$COMMIT_SHA"
  exit $?
fi
if [ -f .deploy_tmp/ledger-risk-active.json ]; then
  echo "Active ledger/risk image pins require reviewed successor; generic deploy blocked"
  exit 2
fi

# Compose parses every service's env_file even in target-only deployment modes.
# Prepare the isolated funding writer secret before the first compose command.
mkdir -p .secrets
chmod 700 .secrets
FUNDING_SECRET_FILE=.secrets/ai_tuner_funding_schedule.env
if grep -q '^AI_TUNER_FUNDING_DB_PASSWORD=' "$FUNDING_SECRET_FILE" 2>/dev/null; then
  AI_TUNER_FUNDING_DB_PASSWORD=$(sed -n 's/^AI_TUNER_FUNDING_DB_PASSWORD=//p' "$FUNDING_SECRET_FILE" | tail -n 1)
  if ! grep -qx 'AI_TUNER_FUNDING_DB_USER=ai_tuner_funding_writer' "$FUNDING_SECRET_FILE"; then
    echo "❌ 资金费采集角色配置不匹配，中止部署"
    exit 1
  fi
else
  AI_TUNER_FUNDING_DB_PASSWORD=$(openssl rand -hex 32)
  printf 'AI_TUNER_FUNDING_DB_USER=ai_tuner_funding_writer\nAI_TUNER_FUNDING_DB_PASSWORD=%s\n' \
    "$AI_TUNER_FUNDING_DB_PASSWORD" > "$FUNDING_SECRET_FILE"
fi
if ! [[ "$AI_TUNER_FUNDING_DB_PASSWORD" =~ ^[0-9a-f]{64}$ ]]; then
  echo "❌ 资金费采集数据库密钥格式无效，中止部署"
  exit 1
fi
# A pre-existing env_file must not smuggle trading credentials or override DB
# routing. The only allowed keys are the dedicated writer identity and secret.
if [ "$(grep -c '^AI_TUNER_FUNDING_DB_USER=' "$FUNDING_SECRET_FILE")" -ne 1 ] || \
   [ "$(grep -c '^AI_TUNER_FUNDING_DB_PASSWORD=' "$FUNDING_SECRET_FILE")" -ne 1 ] || \
   grep -qvE '^(AI_TUNER_FUNDING_DB_USER|AI_TUNER_FUNDING_DB_PASSWORD)=' "$FUNDING_SECRET_FILE"; then
  echo "❌ 资金费采集密钥文件存在额外或重复配置，中止部署"
  exit 1
fi
chmod 600 "$FUNDING_SECRET_FILE"

# Never activate the profiled funding collector before its migration and ACL gate.
export COMPOSE_PROFILES=

# calibrated-first-create begin
# 一次性 changed-files 信号只用于首次创建新服务。此分支不得触碰
# 共用 trading-btc-eth:latest 的生产策略或既有影子容器。
if [ "$DEPLOY_MODE" = "calibrated-first-create" ]; then
  TARGET_SERVICE="mtcps-xau-calibrated-shadow"
  TARGET_CONTAINER="trading_system-mtcps-xau-calibrated-shadow"
  echo "📥 仅拉取 ${TARGET_SERVICE}..."
  docker compose pull mtcps-xau-calibrated-shadow
  echo "🔄 仅创建 ${TARGET_SERVICE}（不启动/重建依赖）..."
  docker compose up -d --no-deps mtcps-xau-calibrated-shadow

  TARGET_HEALTHY=0
  for i in $(seq 1 60); do
    state=$(docker inspect -f '{{.State.Status}}' "$TARGET_CONTAINER" 2>/dev/null || echo missing)
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$TARGET_CONTAINER" 2>/dev/null || echo missing)
    if [ "$state" = "running" ] && [ "$health" = "healthy" ]; then
      TARGET_HEALTHY=1
      break
    fi
    sleep 3
  done
  if [ "$TARGET_HEALTHY" -ne 1 ]; then
    echo "❌ ${TARGET_SERVICE} 在 180s 内未通过运行态就绪探针"
    docker compose logs --tail=100 "$TARGET_SERVICE" || true
    docker compose stop mtcps-xau-calibrated-shadow || true
    exit 1
  fi

  mkdir -p deploy_logs
  DEPLOY_TIME=$(date "+%Y-%m-%d %H:%M:%S")
  echo "[${DEPLOY_TIME}] DEPLOY_SUCCESS commit=${COMMIT_SHA} deploy_id=${DEPLOY_ID} mode=${DEPLOY_MODE}" \
    >> "deploy_logs/$(date '+%Y%m%d').log"
  echo "✅ calibrated 服务首次创建完成；既有共享镜像消费者未重建"
  exit 0
fi
# calibrated-first-create end

# execution-facts-first-create begin
# 收入事件主键迁移必须先完成；只拉取采集镜像并创建独立容器，
# 不触发正在运行的策略服务或其本地热修复镜像。
if [ "$DEPLOY_MODE" = "execution-facts-first-create" ]; then
  echo "📥 仅拉取执行事实采集镜像..."
  docker compose pull hrs-strategy
  echo "🔄 仅创建执行事实采集器..."
  docker compose up -d --no-deps execution-facts-collector
  TARGET_CONTAINER="trading_system-execution-facts"
  RUNNING=0
  for i in $(seq 1 30); do
    state=$(docker inspect -f '{{.State.Status}}' "$TARGET_CONTAINER" 2>/dev/null || echo missing)
    if [ "$state" = "running" ]; then
      RUNNING=1
      break
    fi
    sleep 2
  done
  if [ "$RUNNING" -ne 1 ]; then
    docker compose logs --tail=100 execution-facts-collector || true
    exit 1
  fi
  mkdir -p deploy_logs
  DEPLOY_TIME=$(date "+%Y-%m-%d %H:%M:%S")
  echo "[${DEPLOY_TIME}] DEPLOY_SUCCESS commit=${COMMIT_SHA} deploy_id=${DEPLOY_ID} mode=${DEPLOY_MODE}" \
    >> "deploy_logs/$(date '+%Y%m%d').log"
  echo "✅ 执行事实采集器已创建；现有策略容器未更新"
  exit 0
fi
# execution-facts-first-create end

# allocation-reconcile begin
# Reconcile the live strategy and AI Tuner hotfixes without touching other services.
if [ "$DEPLOY_MODE" = "allocation-reconcile" ]; then
  OVERRIDE=docker-compose.override.yml
  if [ ! -f "$OVERRIDE" ]; then
    echo "❌ 生产热修复覆盖配置不存在，停止定向部署"
    exit 1
  fi
  python3 - <<'PY'
from pathlib import Path

expected = """services:
  hrs-strategy:
    image: allocation-fix:hrs-20261102
  btc-eth-strategy:
    image: allocation-fix:btc-20261102c
  btc-eth-aggressive-strategy:
    image: allocation-fix:aggr-20261102j
  new-coin-strategy:
    image: allocation-fix:nc-20261102
  ai-tuner:
    image: allocation-fix:tuner-health-bfc9ed4-20261102
"""
actual = Path("docker-compose.override.yml").read_text()
if actual.strip() != expected.strip():
    raise SystemExit("❌ 生产镜像覆盖配置与审核时不一致，停止定向部署")
PY
  OVERRIDE_SHA=$(sha256sum "$OVERRIDE" | awk '{print $1}')
  cp "$OVERRIDE" "${OVERRIDE}.pre_reconcile_${DEPLOY_ID}"
  bash scripts/archive_container_logs.sh "log_archive/pre_allocation_reconcile_${DEPLOY_ID}"
  SERVICES="hrs-strategy btc-eth-strategy btc-eth-aggressive-strategy new-coin-strategy grid-strategy ai-tuner"
  mkdir -p .deploy_tmp
  ROLLBACK_FILE=".deploy_tmp/allocation_rollback_${DEPLOY_ID}.yml"
  echo 'services:' > "$ROLLBACK_FILE"
  for spec in \
    "hrs-strategy:trading_system-hrs" \
    "btc-eth-strategy:trading_system-btc_eth" \
    "btc-eth-aggressive-strategy:trading_system-btc_eth_aggressive" \
    "new-coin-strategy:trading_system-new_coin" \
    "grid-strategy:trading_system-grid" \
    "ai-tuner:ai-tuner"; do
    service=${spec%%:*}
    container=${spec#*:}
    old_image=$(docker inspect -f '{{.Image}}' "$container")
    test -n "$old_image"
    rollback_tag="allocation-rollback:${service}-${DEPLOY_ID}"
    docker image tag "$old_image" "$rollback_tag"
    printf '  %s:\n    image: %s\n' "$service" "$rollback_tag" >> "$ROLLBACK_FILE"
  done
  docker compose pull $SERVICES
  ROLLBACK_REQUIRED=1
  allocation_rollback() {
    rc=$1
    trap - EXIT
    if [ "$ROLLBACK_REQUIRED" = "1" ]; then
      echo "⚠️ 定向部署未完成，恢复全部六个服务的部署前镜像"
      if docker compose -f docker-compose.yml -f docker-compose.logging.yml -f "$ROLLBACK_FILE" \
          up -d --no-deps $SERVICES; then
        for spec in \
          "hrs-strategy:trading_system-hrs" \
          "btc-eth-strategy:trading_system-btc_eth" \
          "btc-eth-aggressive-strategy:trading_system-btc_eth_aggressive" \
          "new-coin-strategy:trading_system-new_coin" \
          "grid-strategy:trading_system-grid" \
          "ai-tuner:ai-tuner"; do
          service=${spec%%:*}
          container=${spec#*:}
          restored=0
          for i in $(seq 1 30); do
            state=$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || true)
            health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || true)
            image=$(docker inspect -f '{{.Config.Image}}' "$container" 2>/dev/null || true)
            if [ "$state" = "running" ] && [ "$health" = "healthy" ] && \
               [ "$image" = "allocation-rollback:${service}-${DEPLOY_ID}" ]; then
              restored=1
              break
            fi
            sleep 3
          done
          if [ "$restored" -ne 1 ]; then
            echo "❌ ${container} 回滚后镜像或健康状态未恢复，需人工检查 ${ROLLBACK_FILE}"
          fi
        done
      else
        echo "❌ 自动回滚失败，需按 ${ROLLBACK_FILE} 手动恢复"
      fi
    fi
    exit "$rc"
  }
  trap 'allocation_rollback $?' EXIT
  docker compose up -d --no-deps $SERVICES
  for spec in \
    "trading_system-hrs:trading-hrs" \
    "trading_system-btc_eth:trading-btc-eth" \
    "trading_system-btc_eth_aggressive:trading-btc-eth-aggr" \
    "trading_system-new_coin:trading-new-coin" \
    "trading_system-grid:trading-grid" \
    "ai-tuner:trading-ai-tuner"; do
    container=${spec%%:*}
    image=${spec#*:}
    ready=0
    for i in $(seq 1 60); do
      state=$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || true)
      health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || true)
      actual_image=$(docker inspect -f '{{.Config.Image}}' "$container" 2>/dev/null || true)
      if [ "$state" = "running" ] && [ "$health" = "healthy" ] && \
         [ "$actual_image" = "ghcr.io/bearflower/${image}:latest" ]; then
        ready=1
        break
      fi
      sleep 3
    done
    if [ "$ready" -ne 1 ]; then
      echo "❌ ${container} 未以目标镜像进入健康状态"
      exit 1
    fi
    if [ "$container" != "ai-tuner" ]; then
      docker exec "$container" python -c \
        'import os; from shared.trade_logger import TradeLogger; expected=os.environ.get("TRADE_ACCOUNT_ID"); assert expected and TradeLogger(None, "deploy_probe").account_id == expected'
    fi
  done
  if [ "$(sha256sum "$OVERRIDE" | awk '{print $1}')" != "$OVERRIDE_SHA" ]; then
    echo "❌ 部署期间覆盖配置发生变化，停止修改"
    exit 1
  fi
  cat > "${OVERRIDE}.next_${DEPLOY_ID}" <<'EOF'
services: {}
EOF
  mv "${OVERRIDE}.next_${DEPLOY_ID}" "$OVERRIDE"
  ROLLBACK_REQUIRED=0
  trap - EXIT
  mkdir -p deploy_logs
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] DEPLOY_SUCCESS commit=${COMMIT_SHA} deploy_id=${DEPLOY_ID} mode=${DEPLOY_MODE}" \
    >> "deploy_logs/$(date '+%Y%m%d').log"
  echo "✅ 五个实盘策略及 AI Tuner 热修复已归并"
  exit 0
fi
# allocation-reconcile end

# Remaining local allocation-fix images must be reconciled before a full
# deployment can safely replace the production runtime.
if [ "$DEPLOY_MODE" = "full" ] && grep -q "allocation-fix:" docker-compose.override.yml 2>/dev/null; then
  echo "❌ 仍有未归并的生产热修复镜像，拒绝全量部署"
  exit 1
fi

# Dedicated persistent monitor alert state lives in this service's existing
# bind mount. Provision it for the image's non-root UID before any collector
# start; otherwise a restart could bypass the 15-minute alert cooldown.
mkdir -p logs/ai_tuner_funding_schedule
chown 1000:1000 logs/ai_tuner_funding_schedule
chmod 700 logs/ai_tuner_funding_schedule

stop_funding_collector_if_running() {
  # Exact container only; never stop sibling AI Tuner or trading services.
  if docker inspect ai-tuner-funding-schedule >/dev/null 2>&1; then
    docker stop ai-tuner-funding-schedule >/dev/null
    if [ "$(docker inspect -f '{{.State.Status}}' ai-tuner-funding-schedule)" = "running" ]; then
      echo "❌ 资金费采集器仍在运行，中止部署"
      return 1
    fi
  fi
}
funding_collector_identity() {
  docker inspect -f '{{.Id}}|{{.State.StartedAt}}|{{.Image}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}' ai-tuner-funding-schedule
}

FUNDING_IDENTITY_BEFORE=""
if [ "$FUNDING_DEPLOY_ACTION" = "keep" ]; then
  FUNDING_IDENTITY_BEFORE=$(funding_collector_identity) || {
    echo "❌ 资金费采集器不存在，不能保留；需安全轮换部署"
    exit 1
  }
  IFS='|' read -r FUNDING_ID FUNDING_STARTED FUNDING_IMAGE FUNDING_STATUS FUNDING_HEALTH FUNDING_PROJECT FUNDING_SERVICE <<< "$FUNDING_IDENTITY_BEFORE"
  if [ -z "$FUNDING_ID" ] || [ -z "$FUNDING_STARTED" ] || [ -z "$FUNDING_IMAGE" ] || \
     [ "$FUNDING_STATUS" != "running" ] || [ "$FUNDING_HEALTH" != "healthy" ] || \
     [ "$FUNDING_PROJECT" != "trading_system" ] || [ "$FUNDING_SERVICE" != "ai-tuner-funding-schedule" ]; then
    echo "❌ 资金费采集器身份或健康不符，拒绝保留路径"
    exit 1
  fi
  echo "✅ 保留资金费采集器 id=${FUNDING_ID} started=${FUNDING_STARTED} image=${FUNDING_IMAGE}"
elif [ "$FUNDING_DEPLOY_ACTION" = "rotate" ]; then
  # Sensitive changes: stop before migration/password/ACL gate. Missing slots
  # remain factual gaps and must never be backfilled.
  FUNDING_IDENTITY_BEFORE=$(funding_collector_identity 2>/dev/null || true)
  echo "🔄 安全轮换资金费采集器 before=${FUNDING_IDENTITY_BEFORE:-absent}"
  FUNDING_STOPPED_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  stop_funding_collector_if_running
else
  echo "❌ 非法资金费采集器部署动作"
  exit 1
fi

# 1.5 磁盘预检查（仅普通全量部署）。目标化首次创建不得执行全局 prune。
echo "💾 磁盘预检查..."
FREE_KB=$(df / | awk 'NR==2 {print $4}')
FREE_GB=$((FREE_KB / 1024 / 1024))
echo "   根分区剩余: ${FREE_GB}GB (${FREE_KB}KB)"
if [ "$FREE_GB" -lt 5 ]; then
  echo "⚠️ 磁盘不足 5GB，执行自动清理..."
  docker system prune -af --volumes || true
  docker builder prune -af || true
  FREE_KB=$(df / | awk 'NR==2 {print $4}')
  FREE_GB=$((FREE_KB / 1024 / 1024))
  echo "   清理后剩余: ${FREE_GB}GB"
fi
if [ "$FREE_GB" -lt 2 ]; then
  echo "❌ 磁盘严重不足（< 2GB），中止部署！请手动扩容或清理后重试。"
  exit 1
fi
echo "✅ 磁盘充足 (>= 2GB)"

# 任何 up/recreate 前归档 json-file 容器及当前 grid-realtime journald 日志。
# 即使本轮只更新一项，也保护并行部署后新产生的旧容器日志。
bash scripts/archive_container_logs.sh "log_archive/pre_up_$(date '+%Y%m%d_%H%M%S')_${DEPLOY_ID}"

# 2. 仅清理「不属于本 compose 项目」的残留容器（compose.yml 接管前的命名冲突防护）
# 场景：data-backend / dashboard-api 曾独立 docker run 部署，compose.yml 也定义了同名
#       → docker compose up 报 "container name already in use" exit 1
# 判定：容器 ID 是否出现在本项目管辖范围（docker compose ps -a -q）内
#       在  → 本项目容器，保留（交给 compose up -d 按镜像变化增量 recreate，避免全量停机）
#       不在 → 独立部署残留，移除
echo "🧹 清理非本 compose 项目的残留容器..."
PROJECT_IDS=$(docker compose ps -a -q 2>/dev/null || true)
RESIDUAL_CANDIDATES="trading_system-postgres trading_system-hrs trading_system-btc_eth \
  trading_system-btc_eth_aggressive trading_system-new_coin trading_system-grid \
  trading_system-grid-realtime trading_system-aggtrade-collector \
  trading_system-mtcps-xau-shadow trading_system-mtcps-xau-calibrated-shadow \
  ai-tuner trading_system-kline trading_system-kline-monitor data-backend dashboard-api"
if [ -z "$PROJECT_IDS" ]; then
  # 未取到本项目容器清单（compose 异常）时不删任何容器：
  # 先删后 up 失败会导致全部服务停机且无替代，宁可让 docker compose up 自己报冲突
  echo "⚠️ 未取到本 compose 项目容器清单，跳过残留清理"
else
  for name in $RESIDUAL_CANDIDATES; do
    cid=$(docker inspect -f '{{.Id}}' "$name" 2>/dev/null || true)
    if [ -z "$cid" ]; then
      continue
    fi
    if printf '%s\n' "$PROJECT_IDS" | grep -qx "$cid"; then
      echo "   - 保留（本 compose 项目）: $name"
      continue
    fi
    echo "   - 移除独立部署残留: $name"
    docker rm -f "$name" || true
  done
fi

# 3. 拉取有新版本的镜像（compose pull 自动对比 registry 标签）
# AI Tuner 快照写入使用独立最小权限角色。密钥只生成并保存在服务器 .env，
# 不进入 Git、Actions 日志或镜像。hex 字符集也避免后续 SQL/Compose 转义歧义。
mkdir -p .secrets
chmod 700 .secrets
SNAPSHOT_SECRET_FILE=.secrets/ai_tuner_snapshot.env
MIGRATION_ADMIN_SECRET_FILE=.secrets/trading_migration_admin_password
if grep -q '^AI_TUNER_SNAPSHOT_DB_PASSWORD=' "$SNAPSHOT_SECRET_FILE" 2>/dev/null; then
  AI_TUNER_SNAPSHOT_DB_PASSWORD=$(sed -n 's/^AI_TUNER_SNAPSHOT_DB_PASSWORD=//p' "$SNAPSHOT_SECRET_FILE" | tail -n 1)
else
  AI_TUNER_SNAPSHOT_DB_PASSWORD=$(openssl rand -hex 32)
  printf 'AI_TUNER_SNAPSHOT_DB_USER=ai_tuner_snapshot_writer\nAI_TUNER_SNAPSHOT_DB_PASSWORD=%s\n' \
    "$AI_TUNER_SNAPSHOT_DB_PASSWORD" > "$SNAPSHOT_SECRET_FILE"
fi
if [ -z "$AI_TUNER_SNAPSHOT_DB_PASSWORD" ]; then
  echo "❌ AI_TUNER_SNAPSHOT_DB_PASSWORD 为空，中止部署"
  exit 1
fi
chmod 600 "$SNAPSHOT_SECRET_FILE"

if [ -s "$MIGRATION_ADMIN_SECRET_FILE" ]; then
  TRADING_MIGRATION_ADMIN_PASSWORD=$(tr -d '\r\n' < "$MIGRATION_ADMIN_SECRET_FILE")
else
  TRADING_MIGRATION_ADMIN_PASSWORD=$(openssl rand -hex 32)
  printf '%s\n' "$TRADING_MIGRATION_ADMIN_PASSWORD" > "$MIGRATION_ADMIN_SECRET_FILE"
fi
if [ -z "$TRADING_MIGRATION_ADMIN_PASSWORD" ]; then
  echo "❌ TRADING_MIGRATION_ADMIN_PASSWORD 为空，中止部署"
  exit 1
fi
chmod 600 "$MIGRATION_ADMIN_SECRET_FILE"

echo "📥 docker compose pull..."
docker compose pull

# ========== step 3.5：grid_realtime 一次性 schema 迁移 ==========
# 为什么必须在 up -d 之前：
#   - grid-realtime 业务进程启动只 check_schema（不建表），缺表直接抛错 → crash-loop
#   - grid-strategy 出口的 _init_export 只在构造时校验一次 schema，失败即永久停用
#     （只 warning 不重试）→ 若此时表未建好，出口交接会一直不可用直到下次重启
# 迁移命令幂等：ensure_schema 全 CREATE ... IF NOT EXISTS + 版本更新
# 卷：与 grid-strategy / grid-realtime 共用 ./data/grid:/app/data
# --no-deps：不拉起 depends_on（迁移不依赖任何在跑服务）
echo "🗄️  grid_realtime schema 迁移（一次性）..."
docker compose run -T --rm --no-deps grid-realtime \
  python -m strategies.grid.realtime.reference_store \
  --db-path /app/data/grid_realtime.sqlite3

# 4. 只更新镜像变了的容器（compose up -d 自动检测）
# Docker Compose 重建失败可能留下形如「旧容器 ID_服务名」的 Created 容器。
# 仅在正式容器仍运行且健康、残留已超过 5 分钟时清理；否则停止部署。
python3 scripts/cleanup_compose_created.py
# 保留路径不得用 --remove-orphans 清理未启用 profile 的采集器。
echo "🔄 docker compose up -d..."
if [ "$FUNDING_DEPLOY_ACTION" = "keep" ]; then
  # Keep the database container stable too. A floating postgres:15-alpine
  # digest change could otherwise interrupt collection without changing the
  # collector's own ID/start time. Do not start dependencies implicitly.
  KEEP_SERVICE_LIST=$(docker compose config --services) || {
    echo "❌ Compose 服务清单不可用，拒绝保留路径"
    exit 1
  }
  KEEP_SERVICES=()
  while IFS= read -r service; do
    case "$service" in
      postgres|ai-tuner-funding-schedule) ;;
      *) KEEP_SERVICES+=("$service") ;;
    esac
  done <<< "$KEEP_SERVICE_LIST"
  if [ "${#KEEP_SERVICES[@]}" -eq 0 ]; then
    echo "❌ 无可安全更新的服务，拒绝保留路径"
    exit 1
  fi
  docker compose --parallel 1 up -d --no-deps "${KEEP_SERVICES[@]}"
else
  docker compose --parallel 1 up -d --remove-orphans
fi

# ========== step 4.5：执行数据库 init-scripts（幂等）==========
# 背景：postgres 官方镜像 entrypoint 仅在 PGDATA 首次为空时自动执行
#       /docker-entrypoint-initdb.d/ 脚本。生产 volume 已持久化，
#       entrypoint 不会再触发 → 新增 SQL 脚本无法自动生效。
#       解决：compose up -d 后显式 exec 跑一遍（脚本全 IF NOT EXISTS 幂等）。
# 前置：init-scripts 已由 deploy job 上一个 scp step 上传到
#       /root/trading_system/database/postgres/init-scripts/，
#       compose volume 自动挂载到容器内 /docker-entrypoint-initdb.d/
echo "🗄️  数据库 init-scripts 执行..."

# 4.5.1 等 postgres 就绪（最多 120s，超时让 set -e 炸掉整个部署）
echo "   等待 postgres 就绪..."
PG_READY=0
for i in $(seq 1 60); do
  if docker exec trading_system-postgres pg_isready \
        -U trading_user -d trading_platform -h localhost >/dev/null 2>&1; then
    PG_READY=1
    break
  fi
  sleep 2
done
if [ "$PG_READY" -ne 1 ]; then
  echo "❌ postgres 在 120s 内未就绪，中止部署！"
  exit 1
fi
echo "   ✅ postgres 就绪"

# 首次迁移时，旧 trading_user 仍是初始化超级用户，由它创建独立迁移管理员；
# 后续部署只使用 migration admin。密码仅保存在服务器 .env，不注入业务容器。
MIGRATION_ADMIN_EXISTS=$(docker exec trading_system-postgres \
  psql -U trading_user -d trading_platform -tAc \
    "SELECT 1 FROM pg_roles WHERE rolname='trading_migration_admin'" | tr -d '[:space:]')
if [ "$MIGRATION_ADMIN_EXISTS" != "1" ]; then
  docker exec trading_system-postgres \
    psql --set ON_ERROR_STOP=on -U trading_user -d trading_platform \
      -c "CREATE ROLE trading_migration_admin LOGIN SUPERUSER" >/dev/null
fi
printf '%s\n' \
  "ALTER ROLE trading_migration_admin LOGIN SUPERUSER PASSWORD :'migration_password';" \
  | docker exec -i trading_system-postgres \
      psql --set ON_ERROR_STOP=on \
        --set migration_password="$TRADING_MIGRATION_ADMIN_PASSWORD" \
        -U trading_migration_admin -d trading_platform \
      >/dev/null

ROLE_MIGRATION_REQUIRED=$(docker exec trading_system-postgres \
  psql -U trading_migration_admin -d trading_platform -tAc \
    "SELECT ((SELECT rolsuper FROM pg_roles WHERE rolname='trading_user') OR (SELECT pg_get_userbyid(datdba)='trading_user' FROM pg_database WHERE datname='trading_platform'))::int" \
  | tr -d '[:space:]')
if [ "$ROLE_MIGRATION_REQUIRED" = "1" ]; then
  # 首次权限迁移前保留数据库和全局角色双份备份，供人工回滚核验。
  ROLE_MIGRATION_BACKUP="$(date '+%Y%m%d_%H%M%S')"
  mkdir -p database/postgres/backups
  docker exec trading_system-postgres \
    pg_dump -U trading_migration_admin -d trading_platform -Fc \
    > "database/postgres/backups/pre_role_migration_${ROLE_MIGRATION_BACKUP}.dump"
  docker exec trading_system-postgres \
    pg_dumpall -U trading_migration_admin --globals-only \
    > "database/postgres/backups/pre_role_migration_globals_${ROLE_MIGRATION_BACKUP}.sql"
  test -s "database/postgres/backups/pre_role_migration_${ROLE_MIGRATION_BACKUP}.dump"
  test -s "database/postgres/backups/pre_role_migration_globals_${ROLE_MIGRATION_BACKUP}.sql"
fi

# 4.5.2 按文件名数字前缀顺序执行 /docker-entrypoint-initdb.d/*.sql
#       sort 保证 01→02→…→09 顺序，脚本内部 IF NOT EXISTS 全幂等
SQL_GLOB=$(docker exec trading_system-postgres \
  sh -c 'ls -1 /docker-entrypoint-initdb.d/*.sql 2>/dev/null | sort')
if [ -z "$SQL_GLOB" ]; then
  echo "   ⏭️  容器内无 SQL 脚本，跳过"
else
  echo "   发现 SQL 脚本："
  docker exec trading_system-postgres \
    sh -c 'ls -1 /docker-entrypoint-initdb.d/*.sql 2>/dev/null | sort | sed "s|^|     |"'
  echo "   开始执行..."
  while IFS= read -r sql_path; do
    [ -z "$sql_path" ] && continue
    sql_name=$(basename "$sql_path")
    echo "     ▶ 执行 $sql_name ..."
    # --set ON_ERROR_STOP=on：让 psql 遇到 SQL ERROR 级错误（如 CREATE INDEX 缺 IF NOT EXISTS 时的 duplicate）
    # 立即返回非零 exit code → set -e 中断远程脚本 → deploy job 报红，防止错误静默吞没
    # 仅吞 stdout（CREATE TABLE/CREATE INDEX 输出），stderr 透传到 Actions 日志便于报错时定位
    docker exec trading_system-postgres \
      psql --set ON_ERROR_STOP=on \
        -U trading_migration_admin -d trading_platform -f "$sql_path" \
      >/dev/null
    echo "     ✅ $sql_name 完成"
  done <<< "$SQL_GLOB"
fi
# init SQL 只创建 LOGIN 角色、不内嵌凭据；此处从服务器本地 secret 设置密码。
# 密码为 openssl 生成的 hex，使用 psql 变量 quote_literal，避免拼接 SQL。
printf '%s\n' \
  "ALTER ROLE ai_tuner_snapshot_writer LOGIN PASSWORD :'snapshot_password';" \
  | docker exec -i trading_system-postgres \
      psql --set ON_ERROR_STOP=on \
        --set snapshot_password="$AI_TUNER_SNAPSHOT_DB_PASSWORD" \
        -U trading_migration_admin -d trading_platform \
      >/dev/null
printf '%s\n' \
  "ALTER ROLE ai_tuner_funding_writer LOGIN PASSWORD :'funding_password';" \
  | docker exec -i trading_system-postgres \
      psql --set ON_ERROR_STOP=on \
        --set funding_password="$AI_TUNER_FUNDING_DB_PASSWORD" \
        -U trading_migration_admin -d trading_platform \
      >/dev/null
# 切断业务运行时的超级用户/数据库 owner 能力，使上面的 REVOKE 真正生效。
ROLE_MIGRATION_APPLIED=0
rollback_role_migration() {
  rc=$?
  trap - ERR
  set +e
  stop_funding_collector_if_running || echo "❌ 资金费采集器停止失败，需人工立即隔离"
  if [ "$ROLE_MIGRATION_REQUIRED" = "1" ] && [ "$ROLE_MIGRATION_APPLIED" = "1" ]; then
    echo "⚠️ 权限迁移后门禁失败，恢复 trading_user 原权限和数据库 owner"
    docker exec trading_system-postgres \
      psql -U trading_migration_admin -d trading_platform \
        -c "ALTER DATABASE trading_platform OWNER TO trading_user" \
        -c "ALTER ROLE trading_user SUPERUSER CREATEDB CREATEROLE REPLICATION" \
        >/dev/null
  fi
  docker compose ps
  exit "$rc"
}
trap rollback_role_migration ERR
if [ "$ROLE_MIGRATION_REQUIRED" = "1" ]; then
  ROLE_MIGRATION_APPLIED=1
  docker exec trading_system-postgres \
    psql --set ON_ERROR_STOP=on \
      -U trading_migration_admin -d trading_platform \
      -c "ALTER DATABASE trading_platform OWNER TO trading_migration_admin" \
      -c "ALTER ROLE trading_user NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION" \
      -c "GRANT CONNECT, TEMPORARY ON DATABASE trading_platform TO trading_user" \
      -c "GRANT USAGE, CREATE ON SCHEMA public TO trading_user" \
      >/dev/null
fi
PERMISSION_BOUNDARY_OK=$(docker exec trading_system-postgres \
  psql -U trading_migration_admin -d trading_platform -tAc \
    "SELECT (NOT (SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication FROM pg_roles WHERE rolname='trading_user') AND (SELECT pg_get_userbyid(datdba)='trading_migration_admin' FROM pg_database WHERE datname='trading_platform') AND has_table_privilege('trading_user','public.ai_tuner_market_snapshots','SELECT') AND has_table_privilege('trading_user','public.ai_tuner_universe_snapshots','SELECT') AND has_table_privilege('trading_user','public.ai_tuner_universe_scan_runs','SELECT') AND NOT has_table_privilege('trading_user','public.ai_tuner_market_snapshots','INSERT') AND NOT has_table_privilege('trading_user','public.ai_tuner_market_snapshots','UPDATE') AND NOT has_table_privilege('trading_user','public.ai_tuner_market_snapshots','DELETE') AND NOT has_table_privilege('trading_user','public.ai_tuner_market_snapshots','TRUNCATE') AND NOT has_table_privilege('trading_user','public.ai_tuner_universe_snapshots','INSERT') AND NOT has_table_privilege('trading_user','public.ai_tuner_universe_snapshots','UPDATE') AND NOT has_table_privilege('trading_user','public.ai_tuner_universe_snapshots','DELETE') AND NOT has_table_privilege('trading_user','public.ai_tuner_universe_snapshots','TRUNCATE') AND NOT has_table_privilege('trading_user','public.ai_tuner_universe_scan_runs','INSERT') AND NOT has_table_privilege('trading_user','public.ai_tuner_universe_scan_runs','UPDATE') AND NOT has_table_privilege('trading_user','public.ai_tuner_universe_scan_runs','DELETE') AND NOT has_table_privilege('trading_user','public.ai_tuner_universe_scan_runs','TRUNCATE') AND has_table_privilege('ai_tuner_snapshot_writer','public.ai_tuner_market_snapshots','SELECT') AND has_table_privilege('ai_tuner_snapshot_writer','public.ai_tuner_market_snapshots','INSERT') AND has_table_privilege('ai_tuner_snapshot_writer','public.ai_tuner_market_snapshots','UPDATE') AND NOT has_table_privilege('ai_tuner_snapshot_writer','public.ai_tuner_market_snapshots','DELETE') AND has_table_privilege('ai_tuner_snapshot_writer','public.ai_tuner_universe_snapshots','SELECT') AND has_table_privilege('ai_tuner_snapshot_writer','public.ai_tuner_universe_snapshots','INSERT') AND NOT has_table_privilege('ai_tuner_snapshot_writer','public.ai_tuner_universe_snapshots','DELETE') AND has_table_privilege('ai_tuner_snapshot_writer','public.ai_tuner_universe_scan_runs','SELECT') AND has_table_privilege('ai_tuner_snapshot_writer','public.ai_tuner_universe_scan_runs','INSERT') AND NOT has_table_privilege('ai_tuner_snapshot_writer','public.ai_tuner_universe_scan_runs','DELETE'))::int" \
  | tr -d '[:space:]')
if [ "$PERMISSION_BOUNDARY_OK" != "1" ]; then
  echo "❌ AI Tuner 快照权限边界验证失败，中止部署"
  false
fi
# A prospective public-data collector must have no trading or parameter-write
# surface, even through an inherited role or PUBLIC table grant.
FUNDING_PERMISSION_BOUNDARY_OK=$(docker exec trading_system-postgres \
  psql --set ON_ERROR_STOP=on -U trading_migration_admin -d trading_platform -tAc \
    "SELECT (
      EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ai_tuner_funding_writer'
              AND rolcanlogin AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication))
      AND NOT EXISTS (SELECT 1 FROM pg_auth_members
                      WHERE member='ai_tuner_funding_writer'::regrole)
      AND NOT EXISTS (
        SELECT 1 FROM pg_database d WHERE d.datname=current_database()
          AND (d.datdba='ai_tuner_funding_writer'::regrole
               OR has_database_privilege('ai_tuner_funding_writer',d.oid,'CREATE'))
      )
      AND has_schema_privilege('ai_tuner_funding_writer','public','USAGE')
      AND NOT has_schema_privilege('ai_tuner_funding_writer','public','CREATE')
      AND NOT EXISTS (
        SELECT 1 FROM pg_namespace n
        WHERE n.nspname NOT IN ('pg_catalog','information_schema','public')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND n.nspname NOT LIKE 'pg_temp%'
          AND has_schema_privilege('ai_tuner_funding_writer',n.oid,'USAGE,CREATE')
      )
      AND has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_funding_schedule_runs','SELECT')
      AND has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_funding_schedule_runs','INSERT')
      AND has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_funding_schedule_observations','SELECT')
      AND has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_funding_schedule_observations','INSERT')
      AND has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_universe_scan_runs','SELECT')
      AND has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_universe_snapshots','SELECT')
      AND NOT has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_funding_schedule_runs','UPDATE')
      AND NOT has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_funding_schedule_runs','DELETE')
      AND NOT has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_funding_schedule_runs','TRUNCATE')
      AND NOT has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_funding_schedule_runs','REFERENCES,TRIGGER')
      AND NOT has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_funding_schedule_observations','UPDATE')
      AND NOT has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_funding_schedule_observations','DELETE')
      AND NOT has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_funding_schedule_observations','TRUNCATE')
      AND NOT has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_funding_schedule_observations','REFERENCES,TRIGGER')
      AND NOT has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_universe_scan_runs','INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
      AND NOT has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_universe_snapshots','INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
      AND NOT EXISTS (
        SELECT 1 FROM pg_class c
        WHERE c.oid IN ('public.ai_tuner_funding_schedule_runs'::regclass,
                        'public.ai_tuner_funding_schedule_observations'::regclass)
          AND c.relowner='ai_tuner_funding_writer'::regrole
      )
      AND NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind='S'
          AND n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND n.nspname NOT LIKE 'pg_temp%'
          AND CASE WHEN c.relkind='S'
                   THEN has_sequence_privilege('ai_tuner_funding_writer',c.oid,'USAGE,SELECT,UPDATE')
                   ELSE FALSE END
      )
      AND NOT EXISTS (
        SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
          JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE a.attnum > 0 AND NOT a.attisdropped
          AND c.relkind IN ('r','p','v','m','f')
          AND n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND n.nspname NOT LIKE 'pg_temp%'
          AND (
            has_column_privilege('ai_tuner_funding_writer',c.oid,a.attnum,'UPDATE,REFERENCES')
            OR ((n.nspname <> 'public' OR c.relname NOT IN (
              'ai_tuner_funding_schedule_runs','ai_tuner_funding_schedule_observations'))
              AND has_column_privilege('ai_tuner_funding_writer',c.oid,a.attnum,'INSERT'))
            OR ((n.nspname <> 'public' OR c.relname NOT IN (
              'ai_tuner_funding_schedule_runs','ai_tuner_funding_schedule_observations',
              'ai_tuner_universe_scan_runs','ai_tuner_universe_snapshots'))
              AND has_column_privilege('ai_tuner_funding_writer',c.oid,a.attnum,'SELECT'))
          )
      )
      AND NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND n.nspname NOT LIKE 'pg_temp%'
          AND c.relkind IN ('r','p','v','m','f')
          AND (n.nspname <> 'public' OR c.relname NOT IN (
            'ai_tuner_funding_schedule_runs','ai_tuner_funding_schedule_observations',
            'ai_tuner_universe_scan_runs','ai_tuner_universe_snapshots'))
          AND has_table_privilege('ai_tuner_funding_writer',c.oid,
                                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
      )
    )::int" | tr -d '[:space:]')
if [ "$FUNDING_PERMISSION_BOUNDARY_OK" != "1" ]; then
  echo "❌ AI Tuner 资金费日程采集权限边界验证失败，中止部署"
  stop_funding_collector_if_running
  false
fi
docker compose restart ai-tuner-snapshot >/dev/null
# Only now may the profiled service start. --no-deps avoids touching postgres.
if [ "$FUNDING_DEPLOY_ACTION" = "rotate" ]; then
  docker compose --profile funding-schedule up -d --no-deps ai-tuner-funding-schedule
  echo "✅ 资金费采集器轮换窗口 ${FUNDING_STOPPED_AT}..$(date -u '+%Y-%m-%dT%H:%M:%SZ') after=$(funding_collector_identity)"
else
  FUNDING_IDENTITY_AFTER=$(funding_collector_identity) || {
    echo "❌ 资金费采集器在部署后消失"
    exit 1
  }
  if [ "$FUNDING_IDENTITY_AFTER" != "$FUNDING_IDENTITY_BEFORE" ]; then
    echo "❌ 资金费采集器身份或健康变化，拒绝宣称连续性"
    exit 1
  fi
  echo "✅ 资金费采集器身份与健康保持不变"
fi
echo "✅ init-scripts 全部执行完毕"
# ==========================================================

# 5. 等全部长期运行容器就绪；失败时先恢复旧运行时角色权限，避免扩大故障。
echo "⏳ 等待全部容器就绪（最多 180s）..."
ALL_CONTAINERS_HEALTHY=0
for i in $(seq 1 60); do
  ALL_CONTAINERS_HEALTHY=1
  for container in \
    trading_system-postgres trading_system-hrs trading_system-btc_eth \
    trading_system-btc_eth_aggressive trading_system-mtcps-xau-shadow \
    trading_system-mtcps-xau-calibrated-shadow \
    trading_system-new_coin trading_system-grid trading_system-grid-realtime \
    trading_system-aggtrade-collector ai-tuner ai-tuner-snapshot ai-tuner-funding-schedule \
    trading_system-kline trading_system-kline-monitor data-backend dashboard-api; do
    state=$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || echo missing)
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || echo missing)
    if [ "$state" != "running" ] || { [ "$health" != "healthy" ] && [ "$health" != "none" ]; }; then
      ALL_CONTAINERS_HEALTHY=0
      break
    fi
  done
  [ "$ALL_CONTAINERS_HEALTHY" -eq 1 ] && break
  sleep 3
done
if [ "$ALL_CONTAINERS_HEALTHY" -ne 1 ]; then
  echo "❌ 权限迁移后容器未全部就绪，恢复 trading_user 原权限并中止"
  false
fi
ROLE_MIGRATION_APPLIED=0
trap - ERR

# 6. 状态检查
echo ""
echo "✅ 当前容器状态："
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -E "trading_system|ai-tuner|data-backend|dashboard-api" || echo "⚠️ 未找到 trading 相关容器"

# 验证本批 journald 服务的实际驱动与可查询记录。
# 只打印服务名和容器 ID，不在 Actions 日志中输出业务日志内容。
JOURNAL_SERVICES=$(docker compose config --format json | python3 -c 'import json,sys; services=json.load(sys.stdin)["services"]; print(" ".join(name for name,config in services.items() if config.get("logging",{}).get("driver")=="journald"))')
for service in $JOURNAL_SERVICES; do
  cid=$(docker compose ps -q "$service")
  if [ -z "$cid" ] || [ "$(docker inspect -f '{{.HostConfig.LogConfig.Type}}' "$cid")" != "journald" ]; then
    echo "❌ $service 未使用 journald 日志驱动"
    exit 1
  fi
  # 没有标准输出的静默容器自然也没有 journal 记录。
  if [ -z "$(docker logs --tail 1 "$cid" 2>&1)" ]; then
    echo "⏳ journald $service container=$cid 尚无日志输出（驱动已核对）"
    continue
  fi
  found=0
  for i in $(seq 1 15); do
    if [ -n "$(journalctl -q --no-pager -n 1 -o cat CONTAINER_ID_FULL="$cid")" ]; then
      found=1
      break
    fi
    sleep 2
  done
  if [ "$found" -ne 1 ]; then
    echo "❌ $service 已有容器日志，但当前容器日志未进入 journald"
    exit 1
  fi
  echo "✅ journald $service container=$cid"
done

# 7. 写部署日志
mkdir -p deploy_logs
DEPLOY_TIME=$(date "+%Y-%m-%d %H:%M:%S")
LOG_FILE="deploy_logs/$(date '+%Y%m%d').log"
LOG_MSG="[${DEPLOY_TIME}] DEPLOY_SUCCESS commit=${COMMIT_SHA} deploy_id=${DEPLOY_ID}"
echo "${LOG_MSG}" >> "${LOG_FILE}"

echo ""
echo "========================================"
echo "🎉 CI/CD 部署完成！"
echo "========================================"
