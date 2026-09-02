# Docker容器编排文档

**文档版本**: v2.0
**最后更新**: 2026-09-02
**作者**: 开发团队

---

## 文档修订历史

| 版本 | 日期 | 修改人 | 修改内容 |
|------|------|--------|----------|
| v1.0 | 2026-05-05 | 需求文档专家 | 初始版本创建 |
| v2.0 | 2026-09-02 | 开发团队 | 更新容器架构图、容器规划表、docker-compose 配置，反映所有服务（HRS、日报/周报、AI调优、K线监控等）

---

## 1. Docker架构设计

### 1.1 容器架构图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Docker 容器集群                                  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │                    策略容器层 (Strategy Containers)               │  │
│  │                                                                  │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │  │
│  │  │ BTC/ETH  │  │   Grid   │  │ New Coin │  │   HRS    │       │  │
│  │  │  容器    │  │   容器   │  │   容器   │  │   容器   │       │  │
│  │  │          │  │          │  │          │  │          │       │  │
│  │  │ Python   │  │ Python   │  │ Python   │  │ Python   │       │  │
│  │  │ 3.10     │  │ 3.10     │  │ 3.10     │  │ 3.10     │       │  │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘       │  │
│  │                                                                  │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐                      │  │
│  │  │ 日报服务  │  │ 周报服务  │  │AI调优系统│                      │  │
│  │  │   容器    │  │   容器    │  │   容器   │                      │  │
│  │  │          │  │          │  │          │                      │  │
│  │  │ Python   │  │ Python   │  │ Python   │                      │  │
│  │  │ 3.10     │  │ 3.10     │  │ 3.10     │                      │  │
│  │  └──────────┘  └──────────┘  └──────────┘                      │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │                  基础设施容器层 (Infrastructure)                  │  │
│  │                                                                  │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │  │
│  │  │PostgreSQL│  │  K线服务 │  │K线监控   │  │  Dashboard│       │  │
│  │  │   容器   │  │   容器   │  │   容器   │  │   容器   │       │  │
│  │  │          │  │          │  │          │  │          │       │  │
│  │  │PostgreSQL│  │  Python  │  │  Python  │  │  Python  │       │  │
│  │  │ 15-alpine│  │  3.10    │  │  3.10    │  │  3.10    │       │  │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘       │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │              Docker网络 (trading-network-v2)                     │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 1.2 容器规划

| 容器名称 | 镜像 | 用途 | 资源配置 | 端口映射 |
|---------|------|------|----------|----------|
| btc-eth-strategy | trading_system-btc_eth:latest | 主流币种趋势回调确认策略(MTPCS) | 1核/1GB | 无 |
| grid-strategy | trading_system-grid:latest | 网格策略（ETHUSDT） | 1核/512MB | 无 |
| new-coin-strategy | trading_system-new_coin:latest | 新币做空策略 | 1核/512MB | 无 |
| hrs-strategy | trading_system-hrs:latest | HRS 混合反转策略 | 1核/512MB | 无 |
| postgres-db | postgres:15-alpine | PostgreSQL 数据库 | 2核/4GB | 5433:5432 |
| kline-service | trading_system-kline:latest | K线数据采集服务 | 1核/1GB | 8765:8000 |
| kline-monitor | trading_system-kline-monitor:latest | K线服务健康监控 | 1核/256MB | 无 |
| ai-tuner | ai-tuner:latest | StratTuneAI 多策略AI调优系统 | 0.5核/512MB | 8777:8777 |
| daily-report | trading_system-daily_report:latest | 日报服务 | 0.5核/256MB | 无 |
| weekly-report | trading_system-weekly_report:latest | 周报服务 | 0.5核/256MB | 无 |

### 1.3 网络设计

使用Docker网络隔离容器:

```yaml
networks:
  trading-network:
    driver: bridge
    ipam:
      config:
        - subnet: 172.20.0.0/16
```

> **注意：** 实际部署使用 `trading-network-v2` 网络（见 docker-compose.yml），与旧版 `trading-network` 隔离，避免容器名冲突。

**网络特点**:
- 所有容器在同一网络中,可以通过容器名互相访问
- 外部无法直接访问策略容器
- 只有基础设施容器暴露端口（postgres 5433, kline 8765, ai-tuner 8777）

---

## 2. Dockerfile配置

### 2.1 主流币种趋势回调确认策略(MTPCS) Dockerfile

创建 `strategies/btc_eth/Dockerfile`:

```dockerfile
# ============================================
# 主流币种趋势回调确认策略(MTPCS) Dockerfile
# ============================================

# 基础镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制共享模块
COPY shared/ /app/shared/

# 复制策略代码
COPY strategies/btc_eth/ /app/strategy/

# 设置工作目录
WORKDIR /app/strategy

# 创建日志目录
RUN mkdir -p /app/logs

# 设置环境变量
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV STRATEGY_NAME=btc_eth

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

# 启动命令
CMD ["python", "main.py"]
```

### 2.2 Grid策略Dockerfile

创建 `strategies/grid/Dockerfile`:

```dockerfile
# ============================================
# Grid 策略 Dockerfile
# ============================================

# 基础镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制共享模块
COPY shared/ /app/shared/

# 复制策略代码
COPY strategies/grid/ /app/strategy/

# 设置工作目录
WORKDIR /app/strategy

# 创建日志目录
RUN mkdir -p /app/logs

# 设置环境变量
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV STRATEGY_NAME=grid

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

# 启动命令
CMD ["python", "main.py"]
```

### 2.3 New Coin策略Dockerfile

创建 `strategies/new_coin/Dockerfile`:

```dockerfile
# ============================================
# New Coin 策略 Dockerfile
# ============================================

# 基础镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制共享模块
COPY shared/ /app/shared/

# 复制策略代码
COPY strategies/new_coin/ /app/strategy/

# 设置工作目录
WORKDIR /app/strategy

# 创建日志目录
RUN mkdir -p /app/logs

# 设置环境变量
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV STRATEGY_NAME=new_coin

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

# 启动命令
CMD ["python", "main.py"]
```

### 2.4 Dockerfile最佳实践

#### 2.4.1 多阶段构建(可选)

如果需要优化镜像大小,可以使用多阶段构建:

```dockerfile
# ============================================
# 多阶段构建示例
# ============================================

# 构建阶段
FROM python:3.10-slim as builder

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# 运行阶段
FROM python:3.10-slim

WORKDIR /app

# 复制依赖
COPY --from=builder /install /usr/local

# 复制代码
COPY shared/ /app/shared/
COPY strategies/btc_eth/ /app/strategy/

WORKDIR /app/strategy

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
```

#### 2.4.2 镜像优化建议

- ✅ 使用`.dockerignore`排除不必要的文件
- ✅ 合并RUN指令减少镜像层数
- ✅ 清理apt缓存减少镜像大小
- ✅ 使用alpine镜像(可选,但可能存在兼容性问题)
- ✅ 设置合理的健康检查

创建 `.dockerignore` 文件:

```
.git
.gitignore
*.pyc
__pycache__
*.pyo
*.pyd
.Python
*.so
*.egg
*.egg-info
dist
build
.trae
logs
data
reports
*.log
*.tar.gz
.env.local
.pytest_cache
.mypy_cache
.coverage
htmlcov
tmp
node_modules
```

---

## 3. docker-compose.yml 配置

### 3.1 配置文件位置

`docker-compose.yml` 位于项目根目录 `/Users/yl/vscode/Binance_quantitative_trading/docker-compose.yml`，是实际运行中的配置文件。

### 3.2 服务架构

当前 docker-compose.yml 包含以下服务：

| 服务名 | 容器名 | 说明 |
|--------|--------|------|
| `postgres` | `trading_system-postgres` | PostgreSQL 15-alpine 数据库，端口 5433:5432 |
| `hrs-strategy` | `trading_system-hrs` | HRS 混合反转策略，依赖 postgres + kline-service |
| `btc-eth-strategy` | `trading_system-btc_eth` | BTC/ETH 趋势策略，依赖 postgres + kline-service |
| `new-coin-strategy` | `trading_system-new_coin` | 新币做空策略，依赖 postgres + kline-service |
| `grid-strategy` | `trading_system-grid` | 网格交易策略，只依赖 postgres（不依赖 kline-service，避免重启连锁) |
| `daily-report` | `trading_system-daily_report` | 日报服务，依赖 postgres |
| `weekly-report` | `trading_system-weekly_report` | 周报服务，依赖 postgres |
| `ai-tuner` | `ai-tuner` | StratTuneAI 调优系统，依赖 postgres，端口 8777:8777 |
| `kline-service` | `trading_system-kline` | K线数据服务，依赖 postgres，端口 8765:8000 |
| `kline-monitor` | `trading_system-kline-monitor` | K线监控告警服务，依赖 postgres + kline-service |

### 3.3 关键配置说明

#### 服务依赖

策略容器依赖 `postgres` 和 `kline-service` 健康检查通过后才启动：

```yaml
depends_on:
  postgres:
    condition: service_healthy
  kline-service:
    condition: service_healthy
```

> **注意：** `grid-strategy` 不依赖 `kline-service`，避免 K 线服务重启时网格策略跟随重启。网格策略通过 `shared/kline_service.py` 客户端按需访问 K 线服务。

#### 资源限制

```yaml
deploy:
  resources:
    limits:
      cpus: '0.5'
      memory: 512M
    reservations:
      cpus: '0.1'
      memory: 256M
```

> **注意：** 资源限制仅对 `ai-tuner` 和 `postgres` 以外的服务通过 docker-compose 配置生效。deploy 配置在非 swarm 模式下仅作为限制建议，实际运行时通过 docker run 参数或 docker-compose 的 `mem_limit` / `cpus` 字段控制。

#### 数据卷挂载

```yaml
volumes:
  - ./logs/btc_eth:/app/logs       # 日志持久化
  - ./data/btc_eth:/app/data       # 数据持久化
  - ./strategies/hrs/config.yaml:/app/strategies/hrs/config.yaml:rw  # 配置文件读写挂载
```

> **注意：** AI 调优覆盖层（tuning_overrides）通过 `ai-tuner` 容器的 `./strategies:/app/strategies:rw` 读写挂载，实现参数写入。

#### 网络配置

```yaml
networks:
  trading-network:
    driver: bridge
    name: trading-network-v2
```

> 使用 `trading-network-v2` 网络名，与旧版 `trading-network` 隔离。

#### 数据卷

```yaml
volumes:
  postgres-data:
    driver: local
  kline-monitor-data:
    driver: local
```

---

## 4. 容器管理命令

### 4.1 基本操作

#### 4.1.1 启动服务

```bash
# 启动所有服务
docker-compose up -d

# 启动指定服务
docker-compose up -d btc-eth-strategy

# 构建并启动
docker-compose up -d --build

# 不使用缓存构建
docker-compose build --no-cache
docker-compose up -d
```

#### 4.1.2 停止服务

```bash
# 停止所有服务
docker-compose down

# 停止指定服务
docker-compose stop btc-eth-strategy

# 停止并删除容器
docker-compose down --volumes

# 停止并删除镜像
docker-compose down --rmi all
```

#### 4.1.3 重启服务

```bash
# 重启所有服务
docker-compose restart

# 重启指定服务
docker-compose restart btc-eth-strategy

# 重新构建并重启
docker-compose up -d --build --force-recreate btc-eth-strategy
```

#### 4.1.4 查看状态

```bash
# 查看所有容器状态
docker-compose ps

# 查看指定容器状态
docker-compose ps btc-eth-strategy

# 查看容器详细信息
docker inspect btc-eth-strategy

# 查看容器资源使用
docker stats btc-eth-strategy
```

### 4.2 日志管理

#### 4.2.1 查看日志

```bash
# 查看所有容器日志
docker-compose logs

# 查看指定容器日志
docker-compose logs btc-eth-strategy

# 实时查看日志
docker-compose logs -f btc-eth-strategy

# 查看最近100行日志
docker-compose logs --tail 100 btc-eth-strategy

# 查看带时间戳的日志
docker-compose logs -f --timestamps btc-eth-strategy
```

#### 4.2.2 日志过滤

```bash
# 过滤错误日志
docker-compose logs btc-eth-strategy | grep -i error

# 过滤特定时间段日志
docker-compose logs --since 2026-05-05T10:00:00 btc-eth-strategy

# 过滤最近30分钟的日志
docker-compose logs --since 30m btc-eth-strategy
```

### 4.3 容器操作

#### 4.3.1 进入容器

```bash
# 进入容器终端
docker-compose exec btc-eth-strategy /bin/bash

# 执行单个命令
docker-compose exec btc-eth-strategy python -c "print('hello')"

# 以root用户进入
docker-compose exec -u root btc-eth-strategy /bin/bash
```

#### 4.3.2 文件操作

```bash
# 复制文件到容器
docker cp local_file.txt btc-eth-strategy:/app/

# 从容器复制文件
docker cp btc-eth-strategy:/app/logs/strategy.log ./local_logs/

# 查看容器内文件
docker-compose exec btc-eth-strategy ls -la /app/
```

#### 4.3.3 容器清理

```bash
# 清理已停止的容器
docker container prune

# 清理悬空镜像
docker image prune

# 清理所有未使用的资源
docker system prune

# 清理所有资源(包括镜像)
docker system prune -a
```

### 4.4 镜像管理

#### 4.4.1 镜像操作

```bash
# 查看镜像列表
docker images

# 查看镜像详细信息
docker inspect trading-btc-eth:latest

# 删除镜像
docker rmi trading-btc-eth:latest

# 强制删除镜像
docker rmi -f trading-btc-eth:latest

# 镜像标签
docker tag trading-btc-eth:latest trading-btc-eth:v1.0
```

#### 4.4.2 镜像导出导入

```bash
# 导出镜像
docker save -o trading-btc-eth.tar trading-btc-eth:latest

# 导入镜像
docker load -i trading-btc-eth.tar

# 压缩导出
docker save trading-btc-eth:latest | gzip > trading-btc-eth.tar.gz
```

---

## 5. 容器监控

### 5.1 资源监控

#### 5.1.1 实时监控

```bash
# 实时监控所有容器资源使用
docker stats

# 监控指定容器
docker stats btc-eth-strategy grid-strategy

# 不刷新显示
docker stats --no-stream
```

#### 5.1.2 资源使用报告

```bash
# 查看容器资源使用情况
docker container stats --no-stream

# 查看容器进程
docker top btc-eth-strategy

# 查看容器端口映射
docker port btc-eth-strategy
```

### 5.2 健康检查监控

#### 5.2.1 查看健康状态

```bash
# 查看容器健康状态
docker inspect --format='{{.State.Health.Status}}' btc-eth-strategy

# 查看健康检查历史
docker inspect --format='{{json .State.Health}}' btc-eth-strategy | jq

# 查看所有容器的健康状态
docker ps --format "table {{.Names}}\t{{.Status}}"
```

#### 5.2.2 健康检查脚本

创建 `monitor_health.sh`:

```bash
#!/bin/bash

# ============================================
# 容器健康检查监控脚本
# ============================================

CONTAINERS=("trading_system-btc_eth" "trading_system-grid" "trading_system-new_coin" "trading_system-hrs" "trading_system-kline" "trading_system-kline-monitor" "ai-tuner" "trading_system-daily_report" "trading_system-weekly_report")

echo "============================================="
echo "容器健康状态监控"
echo "时间：$(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================="

for container in "${CONTAINERS[@]}"; do
    status=$(docker inspect --format='{{.State.Health.Status}}' $container 2>/dev/null)

    if [ "$status" = "healthy" ]; then
        echo "✅ $container: $status"
    elif [ "$status" = "unhealthy" ]; then
        echo "❌ $container: $status"
        # 发送告警通知
        # curl -X POST http://notification-service:8766/api/v1/send ...
    else
        echo "⚠️  $container: $status"
    fi
done

echo "============================================="
```

### 5.3 日志监控

#### 5.3.1 日志聚合

创建 `monitor_logs.sh`:

```bash
#!/bin/bash

# ============================================
# 容器日志监控脚本
# ============================================

CONTAINER="trading_system-btc_eth"
ERROR_KEYWORDS=("error" "exception" "fatal" "failed")

echo "============================================="
echo "容器日志监控"
echo "容器：$CONTAINER"
echo "时间：$(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================="

# 获取最近100行日志
logs=$(docker logs --tail 100 $CONTAINER 2>&1)

# 统计错误数量
for keyword in "${ERROR_KEYWORDS[@]}"; do
    count=$(echo "$logs" | grep -i "$keyword" | wc -l)
    if [ $count -gt 0 ]; then
        echo "⚠️  发现 $count 个 $keyword 日志"
        echo "$logs" | grep -i "$keyword" | tail -5
    fi
done

echo "============================================="
```

#### 5.3.2 日志告警

创建 `alert_on_error.sh`:

```bash
#!/bin/bash

# ============================================
# 日志错误告警脚本
# ============================================

CONTAINER="trading_system-btc_eth"
WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/your_webhook_id"

# 获取最近10分钟日志
logs=$(docker logs --since 10m $CONTAINER 2>&1)

# 检查是否有错误
error_count=$(echo "$logs" | grep -cE "(ERROR|Exception|FATAL|CRITICAL|Traceback)")

if [ $error_count -gt 0 ]; then
    echo "发现 $error_count 个错误，发送告警..."

    # 发送告警
    curl -X POST $WEBHOOK_URL \
        -H "Content-Type: application/json" \
        -d "{
            \"project\": \"trading_system\",
            \"message\": \"容器 $CONTAINER 发现 $error_count 个错误\",
            \"type\": \"text\",
            \"level\": \"error\"
        }"
fi
```

### 5.4 性能监控

#### 5.4.1 性能指标收集

创建 `collect_metrics.sh`:

```bash
#!/bin/bash

# ============================================
# 容器性能指标收集脚本
# ============================================

CONTAINERS=("trading_system-btc_eth" "trading_system-grid" "trading_system-new_coin" "trading_system-hrs" "trading_system-kline" "trading_system-kline-monitor" "ai-tuner" "trading_system-daily_report" "trading_system-weekly_report")
OUTPUT_FILE="/tmp/container_metrics_$(date +%Y%m%d_%H%M%S).csv"

for container in "${CONTAINERS[@]}"; do
    # 获取性能数据
    stats=$(docker stats --no-stream --format \
        "{{.Timestamp}},{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}},{{.NetIO}}" \
        $container)

    # 解析并写入文件
    echo "$stats" >> $OUTPUT_FILE
done

echo "性能指标已保存到：$OUTPUT_FILE"
```

#### 5.4.2 性能报告

创建 `generate_report.sh`:

```bash
#!/bin/bash

# ============================================
# 容器性能报告生成脚本
# ============================================

echo "============================================="
echo "容器性能报告"
echo "时间：$(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================="

# CPU使用率
echo ""
echo "📊 CPU使用率TOP 3:"
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}" \
    | sort -k 2 -hr \
    | head -4

# 内存使用率
echo ""
echo "📊 内存使用率TOP 3:"
docker stats --no-stream --format "table {{.Name}}\t{{.MemPerc}}" \
    | sort -k 2 -hr \
    | head -4

# 网络IO
echo ""
echo "📊 网络IO:"
docker stats --no-stream --format "table {{.Name}}\t{{.NetIO}}"

echo "============================================="
```

---

## 6. 故障处理

### 6.1 容器无法启动

**症状**: 容器启动后立即退出

**排查步骤**:

```bash
# 1. 查看容器日志
docker-compose logs btc-eth-strategy

# 2. 查看容器退出码
docker inspect trading_system-btc_eth --format='{{.State.ExitCode}}'

# 3. 查看容器状态
docker inspect trading_system-btc_eth

# 4. 手动启动容器调试
docker run -it --rm \
    -e BINANCE_API_KEY=${BINANCE_API_KEY} \
    -e BINANCE_SECRET_KEY=${BINANCE_SECRET_KEY} \
    trading_system-btc_eth:latest \
    /bin/bash
```

### 6.2 容器资源不足

**症状**: 容器因内存不足被OOM Kill

**排查步骤**:

```bash
# 1. 查看容器资源使用
docker stats trading_system-btc_eth

# 2. 查看容器内存限制
docker inspect trading_system-btc_eth --format='{{.HostConfig.Memory}}'

# 3. 查看OOM事件
docker events --filter 'container=trading_system-btc_eth' --filter 'event=oom'

# 4. 增加内存限制
# 修改docker-compose.yml中的memory配置
# deploy:
#   resources:
#     limits:
#       memory: 2G
```

### 6.3 容器网络问题

**症状**: 容器无法访问其他服务

**排查步骤**:

```bash
# 1. 检查网络连接
docker network ls
docker network inspect trading-network-v2

# 2. 进入容器测试网络
docker-compose exec btc-eth-strategy ping postgres
docker-compose exec btc-eth-strategy curl http://kline-service:8000/api/v1/health

# 3. 检查DNS解析
docker-compose exec btc-eth-strategy nslookup postgres

# 4. 重建网络
docker-compose down
docker network rm trading-network-v2
docker-compose up -d
```

### 6.4 容器存储问题

**症状**: 磁盘空间不足

**排查步骤**:

```bash
# 1. 查看磁盘使用
df -h

# 2. 查看Docker磁盘使用
docker system df

# 3. 清理悬空镜像
docker image prune -a

# 4. 清理未使用的容器
docker container prune

# 5. 清理未使用的卷
docker volume prune

# 6. 清理所有未使用资源
docker system prune -a --volumes
```

---

## 7. 最佳实践

### 7.1 镜像构建

- ✅ 使用多阶段构建减小镜像大小
- ✅ 使用`.dockerignore`排除不必要的文件
- ✅ 合并RUN指令减少镜像层数
- ✅ 清理apt缓存减少镜像大小
- ✅ 使用特定版本的镜像标签,避免使用`latest`

### 7.2 容器运行

- ✅ 设置合理的资源限制
- ✅ 配置健康检查
- ✅ 使用日志轮转避免磁盘占满
- ✅ 使用环境变量管理配置
- ✅ 使用命名卷持久化数据

### 7.3 网络安全

- ✅ 使用Docker网络隔离容器
- ✅ 不暴露不必要的端口
- ✅ 使用环境变量管理敏感信息
- ✅ 定期更新基础镜像

### 7.4 监控告警

- ✅ 配置健康检查
- ✅ 监控资源使用
- ✅ 监控日志错误
- ✅ 设置告警阈值
- ✅ 定期备份容器配置

---

## 8. 附录

### 8.1 常用命令速查

```bash
# 启动服务
docker-compose up -d

# 停止服务
docker-compose down

# 重启服务
docker-compose restart

# 查看日志
docker-compose logs -f

# 进入容器
docker-compose exec btc-eth-strategy /bin/bash

# 查看状态
docker-compose ps

# 查看资源
docker stats

# 清理资源
docker system prune -a
```

### 8.2 故障排查流程

1. 查看容器状态: `docker-compose ps`
2. 查看容器日志: `docker-compose logs`
3. 查看容器详情: `docker inspect`
4. 进入容器调试: `docker-compose exec`
5. 检查网络连接: `docker network inspect`
6. 检查资源使用: `docker stats`
7. 重启容器: `docker-compose restart`
8. 重建容器: `docker-compose up -d --build --force-recreate`

---

**文档结束**
