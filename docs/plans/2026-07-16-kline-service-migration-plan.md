# K 线服务迁移计划

> **目标：** 将 `common_service/kline_data_service` 迁移到 `Binance_quantitative_trading` 项目中，实现币安项目一站式维护

**架构：** 将 K 线服务代码从 `common_service` 完整复制到 `Binance_quantitative_trading/services/kline_service/`，保留 `common_service_postgres` 作为共享数据库，通过 Docker 网络通信，迁移后 `common_service` 移除 `kline_service` 容器。

**前提条件：**
- `common_service` 中的 PostgreSQL 和 Redis 保持不变
- 新 K 线容器通过 Docker 网络连接到 `common_service` 的数据库
- 部署采用并行运行方式，先建新容器，确认稳定后移除旧容器

---

## 一、迁移范围

### 要迁移的内容

| 来源 | 目标 |
|------|------|
| `common_service/kline_data_service/` | `Binance_quantitative_trading/services/kline_service/` |
| `common_service/src/shared/` (数据库、日志、配置模块) | `Binance_quantitative_trading/services/kline_service/shared/` |
| `common_service/docker-compose.yml` 中 `kline_service` 配置 | `Binance_quantitative_trading/docker-compose.yml` 新增 `kline_service` 服务 |
| `.env` 中数据库、Redis 配置 | `Binance_quantitative_trading/.env` 新增配置项 |

### 保持不变的内容

- `common_service_postgres` 容器（数据库）
- `common_service_redis` 容器（缓存）
- `common_service_notification` 容器（通知服务）

### 迁移后移除的内容

- `common_service/docker-compose.yml` 中的 `kline_service` 服务
- `common_service/nginx` 中 `kline_service` 的反向代理配置

---

## 二、目录结构

迁移后的目录结构如下：

```
Binance_quantitative_trading/
├── services/
│   └── kline_service/                    # K 线数据服务
│       ├── Dockerfile                    # 构建配置
│       ├── requirements.txt              # Python 依赖
│       ├── src/
│       │   └── main.py                   # 应用入口
│       ├── core/
│       │   ├── binance_client.py         # 币安 API 客户端
│       │   ├── collector.py              # K 线采集器
│       │   ├── indicator.py              # 技术指标计算
│       │   ├── registry.py               # 标的注册管理
│       │   └── scheduler.py              # 定时任务调度器
│       ├── api/
│       │   ├── routes.py                 # 数据查询 API
│       │   └── registry_routes.py        # 注册管理 API
│       ├── models/
│       │   ├── kline.py                  # K 线数据模型
│       │   └── registered_symbol.py      # 注册标的模型
│       ├── db/
│       │   └── migrations/
│       │       └── create_registered_symbols_table.py
│       ├── scripts/
│       │   ├── fetch_history_bulk.py
│       │   └── fetch_history_data.py
│       └── shared/                       # 共享模块（从 common_service 复制）
│           ├── core/
│           │   ├── config.py
│           │   └── database.py
│           └── utils/
│               ├── helpers.py
│               └── logger.py
├── docker-compose.yml                    # 新增 kline_service 服务
├── .deploy_config                        # 新增 kline_service 部署配置
├── auto_package.sh                       # 更新打包脚本，包含 kline_service
├── docs/
│   └── plans/
│       └── 2026-07-16-kline-service-migration-plan.md  # 本计划
```

---

## 三、任务分解

### Task 1: 创建目录结构并复制代码

**操作：**
1. 在 `Binance_quantitative_trading/` 下创建 `services/kline_service/` 目录
2. 复制 `common_service/kline_data_service/` 中的代码
3. 复制 `common_service/src/shared/` 模块

**命令：**
```bash
cd /Users/yl/vscode/Binance_quantitative_trading

# 创建目录
mkdir -p services/kline_service/{core,api,models,db/migrations,scripts,src}

# 复制 K 线服务代码
cp -r /Users/yl/vscode/common_service/kline_data_service/core/* ./services/kline_service/core/
cp -r /Users/yl/vscode/common_service/kline_data_service/api/* ./services/kline_service/api/
cp -r /Users/yl/vscode/common_service/kline_data_service/models/* ./services/kline_service/models/
cp -r /Users/yl/vscode/common_service/kline_data_service/db/* ./services/kline_service/db/
cp -r /Users/yl/vscode/common_service/kline_data_service/scripts/* ./services/kline_service/scripts/
cp /Users/yl/vscode/common_service/kline_data_service/src/main.py ./services/kline_service/src/
cp /Users/yl/vscode/common_service/kline_data_service/Dockerfile ./services/kline_service/
cp /Users/yl/vscode/common_service/kline_data_service/requirements.txt ./services/kline_service/

# 复制共享模块
cp -r /Users/yl/vscode/common_service/src/shared ./services/kline_service/shared/
```

---

### Task 2: 调整 Dockerfile

**变更：** 适配新的目录结构

```dockerfile
# content of: services/kline_service/Dockerfile
FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt databases

# 复制共享模块
COPY shared/ ./shared/

# 复制 K 线数据服务源代码
COPY core/ ./core/
COPY api/ ./api/
COPY models/ ./models/
COPY db/ ./db/
COPY scripts/ ./scripts/
COPY src/ ./src/

# 设置 PYTHONPATH
ENV PYTHONPATH=/app:/app/src

# 创建日志目录
RUN mkdir -p /app/logs

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8000/api/v1/health', timeout=5)" || exit 1

# 启动命令
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

### Task 3: 调整 main.py 中的 import 路径

**变更：** 由于目录结构变化，`main.py` 中的 import 路径需要调整

```python
# 修改前（在 common_service 中）
from kline_data_service.core.binance_client import BinanceClient
from kline_data_service.core.collector import KlineCollector
from kline_data_service.core.scheduler import TaskScheduler
from kline_data_service.core.registry import registry
from kline_data_service.api import routes
from kline_data_service.api import registry_routes

# 修改后（在 services/kline_service 中）
from core.binance_client import BinanceClient
from core.collector import KlineCollector
from core.scheduler import TaskScheduler
from core.registry import registry
from api import routes
from api import registry_routes
```

需要调整的文件：
- `services/kline_service/src/main.py`
- `services/kline_service/core/` 中所有引用 `kline_data_service.xxx` 的 import
- `services/kline_service/api/` 中所有引用 `..core` 的 import

---

### Task 4: 更新 docker-compose.yml

**变更：** 在 `Binance_quantitative_trading/docker-compose.yml` 中新增 `kline_service` 服务

在 `docker-compose.yml` 的 `services:` 末尾添加：

```yaml
  # ============================================
  # K 线数据服务（从 common_service 迁移）
  # ============================================
  kline_service:
    build:
      context: ./services/kline_service
      dockerfile: Dockerfile
    container_name: trading_system-kline
    restart: unless-stopped
    environment:
      DATABASE_URL: postgresql://binance:${DB_PASSWORD:-secure_password_here}@common_service_postgres:5432/binance_data
      REDIS_URL: redis://common_service_redis:6379
      BINANCE_API_URL: https://fapi.binance.com
      SYMBOLS: ${SYMBOLS:-BTCUSDT,ETHUSDT,BNBUSDT}
      COLLECT_INTERVALS: ${COLLECT_INTERVALS:-15m,1h,4h,1d}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
    ports:
      - "8765:8000"
    networks:
      - trading-network
      - common_network  # 需要连接到 common_service 的网络
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 512M
        reservations:
          memory: 256M
```

**关键点：** `DATABASE_URL` 中的 host 需要改为 `common_service_postgres`（容器名），因为数据库在另一个 Docker Compose 项目中。

---

### Task 5: 配置跨 Docker 网络通信

**问题：** `Binance_quantitative_trading` 的容器默认在 `trading-network` 上，`common_service` 的容器在 `common_network` 上。K 线服务需要访问 `common_service_postgres` 和 `common_service_redis`。

**方案：** 使用 Docker 的 `external` 网络连接

```bash
# 在服务器上执行，确保 K 线服务容器能访问 common_service 的数据库
# 方案 A：使用 external 网络
docker network connect common_network trading_system-kline

# 方案 B（推荐）：在 docker-compose 中声明 external 网络
```

在 `Binance_quantitative_trading/docker-compose.yml` 的 `networks:` 部分添加：

```yaml
networks:
  trading-network:
    driver: bridge
    name: trading-network-v2
  common_network:
    external: true
    name: common_service_common_network  # 或 docker network ls 查看实际名称
```

---

### Task 6: 更新 .deploy_config 部署配置

**变更：** 在 `.deploy_config` 中新增 K 线服务的部署配置

```bash
# K 线数据服务
KLINE_CONTAINER_NAME="trading_system-kline"
KLINE_IMAGE_NAME="trading_system-kline:latest"
DEPLOY_KLINE=true
```

---

### Task 7: 更新 auto_package.sh 打包脚本

**变更：** 确保 K 线服务代码被包含在打包中

在 `rsync` 命令的排除列表后，添加包含路径：

```bash
# 在 auto_package.sh 中，rsync 之后添加 K 线服务目录
# 确保 services/kline_service/ 被包含在打包中
```

---

### Task 8: 更新 common_service 的 docker-compose.yml

**变更：** 移除 `kline_service` 服务

在确认新容器稳定运行后，从 `common_service/docker-compose.yml` 中：
1. 删除 `kline_service:` 服务定义（第 54-83 行）
2. 删除 `nginx` 中 `depends_on` 的 `kline_service` 引用

---

### Task 9: 更新文档和调用方

**调用方列表：**

| 调用方 | 修改内容 |
|--------|---------|
| `Binance_quantitative_trading` 内部 | 引用 `kline_service` 时使用容器名 `trading_system-kline` |
| `bianace_btcethbnb_trade` | 引用 `8765` 端口，IP 地址不变，无需修改 |
| `Grid_Trading` | 同上 |
| `bianace_newtrade_trade` | 同上 |

**注意：** 只要端口 `8765` 映射不变，外部调用方（通过服务器 IP 访问的）无需任何修改。

---

### Task 10: 部署到服务器

**部署步骤：**

1. 在本地打包项目
```bash
cd /Users/yl/vscode/Binance_quantitative_trading
bash auto_package.sh
```

2. 上传到服务器
```bash
scp -i /Users/yl/vscode/inspection_automation/docs/only.pem deployment_package.tar.gz root@43.156.242.184:/root/
```

3. 在服务器上部署
```bash
ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem root@43.156.242.184

# 解压到交易系统目录
cd /root/trading_system
tar -xzf /root/deployment_package.tar.gz

# 构建并启动 K 线服务
docker-compose up -d --build --force-recreate kline_service

# 等待服务启动
sleep 10
```

4. 验证服务
```bash
curl -s http://localhost:8765/api/v1/health | python3 -m json.tool
curl -s "http://localhost:8765/api/v1/klines/latest?symbol=BTCUSDT&interval=1h&limit=5" | python3 -m json.tool
```

---

## 四、验证清单

### 部署后验证

| 验证项 | 方法 | 预期 |
|--------|------|------|
| 容器运行状态 | `docker ps -f name=trading_system-kline` | Running |
| 数据库连接 | 查看日志无 `ERROR database` | 正常 |
| 币安 API 连接 | 查看日志无 `ERROR binance` | 正常 |
| K 线数据查询 | `curl localhost:8765/api/v1/klines/latest?symbol=BTCUSDT` | 返回数据 |
| 技术指标 | `curl localhost:8765/api/v1/indicators?symbol=BTCUSDT` | 返回指标 |
| 注册管理 | `curl localhost:8765/api/v1/symbols` | 返回列表 |
| 定时任务 | 等待 1 分钟后查看日志 | 采集正常 |
| 旧容器停用 | 移除旧容器后，确认服务正常 | 正常 |

### 回滚方案

如果新容器存在问题，立即恢复：

```bash
# 1. 停止新容器
docker-compose stop kline_service

# 2. 重启旧容器
cd /root/common_service
docker-compose up -d kline_service

# 3. 验证
curl -s http://localhost:8765/api/v1/health
```

---

## 五、迁移时间线

| 步骤 | 预估时间 | 风险 |
|------|---------|------|
| Task 1: 创建目录 & 复制代码 | 5 分钟 | 低 |
| Task 2: 调整 Dockerfile | 5 分钟 | 低 |
| Task 3: 调整 import 路径 | 10 分钟 | 中（需逐个文件检查） |
| Task 4: 更新 docker-compose | 5 分钟 | 低 |
| Task 5: 网络配置 | 10 分钟 | 中（需服务器操作） |
| Task 6-7: 部署配置 | 5 分钟 | 低 |
| Task 8: 本地打包 | 2 分钟 | 低 |
| Task 9: 部署到服务器 | 10 分钟 | 中（并行运行阶段） |
| Task 10: 验证 | 15 分钟 | 中 |
| **总计** | **约 67 分钟** | |

---

## 六、风险与注意事项

### 网络风险
- **关键风险：** 新 K 线容器需要跨网络访问 `common_service_postgres`，需要确保 Docker 网络配置正确
- **缓解措施：** 先保留旧容器，并行运行验证通过后再移除

### 数据库风险
- **关键风险：** `DATABASE_URL` 中的 host 名称需要正确指向 `common_service_postgres`
- **缓解措施：** 使用 Docker 容器名（`common_service_postgres`）而非 IP 地址

### 调用方风险
- **关键风险：** 如果外部调用方硬编码了容器名或 IP，需要更新
- **缓解措施：** 端口映射 `8765:8000` 保持不变，IP 不变，外部调用方无需修改

### 配置同步风险
- **关键风险：** 后续更新 K 线服务代码时，需要更新 `Binance_quantitative_trading` 中的代码
- **缓解措施：** 迁移完成后，`common_service` 中的 K 线代码不再维护，只维护 `Binance_quantitative_trading` 中的代码