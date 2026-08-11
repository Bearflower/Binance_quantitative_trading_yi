# Docker容器编排文档

**文档版本**: v1.0
**最后更新**: 2026-05-05
**作者**: 需求文档专家
**审核人**: 待定

---

## 文档修订历史

| 版本 | 日期 | 修改人 | 修改内容 | 审核人 |
|------|------|--------|----------|--------|
| v1.0 | 2026-05-05 | 需求文档专家 | 初始版本创建 | 待定 |

---

## 1. Docker架构设计

### 1.1 容器架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                     Docker 容器集群                              │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              策略容器层 (Strategy Containers)             │  │
│  │                                                            │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐               │  │
│  │  │ BTC/ETH  │  │   Grid   │  │ New Coin │               │  │
│  │  │  容器    │  │   容器   │  │   容器   │               │  │
│  │  │          │  │          │  │          │               │  │
│  │  │ Python   │  │ Python   │  │ Python   │               │  │
│  │  │ 3.10     │  │ 3.10     │  │ 3.10     │               │  │
│  │  └──────────┘  └──────────┘  └──────────┘               │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              基础设施容器层 (Infrastructure)              │  │
│  │                                                            │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐               │  │
│  │  │PostgreSQL│  │  K线服务 │  │ 通知服务 │               │  │
│  │  │  容器    │  │   容器   │  │   容器   │               │  │
│  │  │          │  │          │  │          │               │  │
│  │  │PostgreSQL│  │  Python  │  │  Python  │               │  │
│  │  │   14     │  │  3.10    │  │  3.10    │               │  │
│  │  └──────────┘  └──────────┘  └──────────┘               │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              Docker网络 (trading-network)                │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 容器规划

| 容器名称 | 镜像 | 用途 | 资源配置 | 端口映射 |
|---------|------|------|----------|----------|
| btc-eth-strategy | trading-btc-eth:latest | 主流币种趋势回调确认策略(MTPCS) | 1核/1GB | 无 |
| grid-strategy | trading-grid:latest | 网格策略 | 1核/512MB | 无 |
| new-coin-strategy | trading-new-coin:latest | 新币策略 | 1核/512MB | 无 |
| postgres-db | postgres:14 | 数据库 | 2核/4GB | 5432 |
| kline-service | kline-service:latest | K线服务 | 1核/1GB | 8765 |
| notification-service | notification-service:latest | 通知服务 | 1核/512MB | 8766 |

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

**网络特点**:
- 所有容器在同一网络中,可以通过容器名互相访问
- 外部无法直接访问策略容器
- 只有基础设施容器暴露端口

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

## 3. docker-compose.yml配置

### 3.1 完整配置文件

创建项目根目录下的 `docker-compose.yml`:

```yaml
# ============================================
# 统一交易系统 Docker Compose 配置
# ============================================

version: '3.8'

# 服务配置
services:
  # ==================== 策略容器 ====================

  # BTC/ETH 策略
  btc-eth-strategy:
    build:
      context: .
      dockerfile: strategies/btc_eth/Dockerfile
    image: trading-btc-eth:latest
    container_name: btc-eth-strategy
    restart: unless-stopped
    environment:
      - STRATEGY_NAME=btc_eth
      - BINANCE_API_KEY=${BINANCE_API_KEY}
      - BINANCE_SECRET_KEY=${BINANCE_SECRET_KEY}
      - DATABASE_URL=postgresql://trading_user:your_password@postgres-db:5432/trading_platform
      - KLINE_SERVICE_URL=http://kline-service:8765/api/v1
      - NOTIFICATION_SERVICE_URL=http://notification-service:8766/api/v1
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
    volumes:
      - ./logs/btc_eth:/app/logs
      - ./data/btc_eth:/app/data
    depends_on:
      postgres-db:
        condition: service_healthy
      kline-service:
        condition: service_healthy
      notification-service:
        condition: service_healthy
    networks:
      - trading-network
    deploy:
      resources:
        limits:
          cpus: '1'
          memory: 1G
        reservations:
          cpus: '0.5'
          memory: 512M
    healthcheck:
      test: ["CMD", "python", "-c", "import sys; sys.exit(0)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

  # Grid 策略
  grid-strategy:
    build:
      context: .
      dockerfile: strategies/grid/Dockerfile
    image: trading-grid:latest
    container_name: grid-strategy
    restart: unless-stopped
    environment:
      - STRATEGY_NAME=grid
      - BINANCE_API_KEY=${BINANCE_API_KEY}
      - BINANCE_SECRET_KEY=${BINANCE_SECRET_KEY}
      - DATABASE_URL=postgresql://trading_user:your_password@postgres-db:5432/trading_platform
      - KLINE_SERVICE_URL=http://kline-service:8765/api/v1
      - NOTIFICATION_SERVICE_URL=http://notification-service:8766/api/v1
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
    volumes:
      - ./logs/grid:/app/logs
      - ./data/grid:/app/data
    depends_on:
      postgres-db:
        condition: service_healthy
      kline-service:
        condition: service_healthy
      notification-service:
        condition: service_healthy
    networks:
      - trading-network
    deploy:
      resources:
        limits:
          cpus: '1'
          memory: 512M
        reservations:
          cpus: '0.25'
          memory: 256M
    healthcheck:
      test: ["CMD", "python", "-c", "import sys; sys.exit(0)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

  # New Coin 策略
  new-coin-strategy:
    build:
      context: .
      dockerfile: strategies/new_coin/Dockerfile
    image: trading-new-coin:latest
    container_name: new-coin-strategy
    restart: unless-stopped
    environment:
      - STRATEGY_NAME=new_coin
      - BINANCE_API_KEY=${BINANCE_API_KEY}
      - BINANCE_SECRET_KEY=${BINANCE_SECRET_KEY}
      - DATABASE_URL=postgresql://trading_user:your_password@postgres-db:5432/trading_platform
      - KLINE_SERVICE_URL=http://kline-service:8765/api/v1
      - NOTIFICATION_SERVICE_URL=http://notification-service:8766/api/v1
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
    volumes:
      - ./logs/new_coin:/app/logs
      - ./data/new_coin:/app/data
    depends_on:
      postgres-db:
        condition: service_healthy
      kline-service:
        condition: service_healthy
      notification-service:
        condition: service_healthy
    networks:
      - trading-network
    deploy:
      resources:
        limits:
          cpus: '1'
          memory: 512M
        reservations:
          cpus: '0.25'
          memory: 256M
    healthcheck:
      test: ["CMD", "python", "-c", "import sys; sys.exit(0)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

  # ==================== 基础设施容器 ====================

  # PostgreSQL 数据库(已有服务,这里仅作参考)
  # postgres-db:
  #   image: postgres:14
  #   container_name: postgres-db
  #   restart: unless-stopped
  #   environment:
  #     - POSTGRES_USER=trading_user
  #     - POSTGRES_PASSWORD=your_password
  #     - POSTGRES_DB=trading_platform
  #   volumes:
  #     - postgres-data:/var/lib/postgresql/data
  #     - ./init-scripts:/docker-entrypoint-initdb.d
  #   ports:
  #     - "5432:5432"
  #   networks:
  #     - trading-network
  #   healthcheck:
  #     test: ["CMD-SHELL", "pg_isready -U trading_user -d trading_platform"]
  #     interval: 10s
  #     timeout: 5s
  #     retries: 5

  # K线服务(已有服务,这里仅作参考)
  # kline-service:
  #   image: kline-service:latest
  #   container_name: kline-service
  #   restart: unless-stopped
  #   environment:
  #     - DATABASE_URL=postgresql://trading_user:your_password@postgres-db:5432/trading_platform
  #   ports:
  #     - "8765:8765"
  #   networks:
  #     - trading-network
  #   healthcheck:
  #     test: ["CMD", "curl", "-f", "http://localhost:8765/api/v1/health"]
  #     interval: 30s
  #     timeout: 10s
  #     retries: 3

  # 通知服务(已有服务,这里仅作参考)
  # notification-service:
  #   image: notification-service:latest
  #   container_name: notification-service
  #   restart: unless-stopped
  #   environment:
  #     - DATABASE_URL=postgresql://trading_user:your_password@postgres-db:5432/trading_platform
  #   ports:
  #     - "8766:8766"
  #   networks:
  #     - trading-network
  #   healthcheck:
  #     test: ["CMD", "curl", "-f", "http://localhost:8766/api/v1/health"]
  #     interval: 30s
  #     timeout: 10s
  #     retries: 3

# 网络配置
networks:
  trading-network:
    driver: bridge
    name: trading-network
    ipam:
      config:
        - subnet: 172.20.0.0/16

# 数据卷配置
volumes:
  postgres-data:
    driver: local
```

### 3.2 配置说明

#### 3.2.1 服务依赖

使用 `depends_on` 和 `condition` 确保服务启动顺序:

```yaml
depends_on:
  postgres-db:
    condition: service_healthy  # 等待数据库健康检查通过
  kline-service:
    condition: service_healthy  # 等待K线服务健康检查通过
  notification-service:
    condition: service_healthy  # 等待通知服务健康检查通过
```

#### 3.2.2 资源限制

使用 `deploy.resources` 限制容器资源使用:

```yaml
deploy:
  resources:
    limits:
      cpus: '1'        # 最多使用1个CPU核心
      memory: 1G       # 最多使用1GB内存
    reservations:
      cpus: '0.5'      # 保留0.5个CPU核心
      memory: 512M     # 保留512MB内存
```

#### 3.2.3 健康检查

使用 `healthcheck` 监控容器健康状态:

```yaml
healthcheck:
  test: ["CMD", "python", "-c", "import sys; sys.exit(0)"]
  interval: 30s        # 每30秒检查一次
  timeout: 10s         # 超时时间10秒
  retries: 3           # 连续失败3次才认为不健康
  start_period: 60s    # 容器启动后60秒才开始健康检查
```

#### 3.2.4 日志配置

使用 `logging` 配置日志轮转:

```yaml
logging:
  driver: "json-file"
  options:
    max-size: "10m"    # 单个日志文件最大10MB
    max-file: "3"      # 最多保留3个日志文件
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

CONTAINERS=("btc-eth-strategy" "grid-strategy" "new-coin-strategy")

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

CONTAINER="btc-eth-strategy"
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

CONTAINER="btc-eth-strategy"
WEBHOOK_URL="http://notification-service:8766/api/v1/send"

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

CONTAINERS=("btc-eth-strategy" "grid-strategy" "new-coin-strategy")
OUTPUT_FILE="/tmp/container_metrics_$(date +%Y%m%d_%H%M%S).csv"

echo "timestamp,container,cpu_percent,mem_usage,mem_limit,mem_percent,net_rx,net_tx" > $OUTPUT_FILE

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
docker inspect btc-eth-strategy --format='{{.State.ExitCode}}'

# 3. 查看容器状态
docker inspect btc-eth-strategy

# 4. 手动启动容器调试
docker run -it --rm \
    -e BINANCE_API_KEY=${BINANCE_API_KEY} \
    -e BINANCE_SECRET_KEY=${BINANCE_SECRET_KEY} \
    trading-btc-eth:latest \
    /bin/bash
```

### 6.2 容器资源不足

**症状**: 容器因内存不足被OOM Kill

**排查步骤**:

```bash
# 1. 查看容器资源使用
docker stats btc-eth-strategy

# 2. 查看容器内存限制
docker inspect btc-eth-strategy --format='{{.HostConfig.Memory}}'

# 3. 查看OOM事件
docker events --filter 'container=btc-eth-strategy' --filter 'event=oom'

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
docker network inspect trading-network

# 2. 进入容器测试网络
docker-compose exec btc-eth-strategy ping postgres-db
docker-compose exec btc-eth-strategy curl http://kline-service:8765/api/v1/health

# 3. 检查DNS解析
docker-compose exec btc-eth-strategy nslookup postgres-db

# 4. 重建网络
docker-compose down
docker network rm trading-network
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
