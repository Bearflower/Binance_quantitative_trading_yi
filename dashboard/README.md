# Dashboard - 交易数据可视化看板

> **版本**: v1.1
> **更新日期**: 2026-06-03
> **作者**: Python 工程师

---

## 目录

- [项目简介](#项目简介)
- [技术架构](#技术架构)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [API 接口](#api-接口)
- [部署指南](#部署指南)
- [开发指南](#开发指南)

---

## 项目简介

Dashboard 是一个轻量级的交易数据可视化看板，用于展示 Binance 量化交易系统的日报和周报数据。

### 核心特性

- **数据展示**: 总览数据、策略详情、币种明细、趋势图表
- **实时更新**: 日报/周报/月报数据实时更新（日=今天00:00~现在，周=本周一00:00~现在，月=本月1日00:00~现在）
- **可视化**: 使用 ECharts 进行金融级可视化
- **易部署**: 轻量级架构，易于部署和维护
- **安全可控**: IP 白名单控制，API 限流保护，无硬编码敏感信息

---

## 技术架构

| 层次 | 技术选型 | 说明 |
|------|---------|------|
| **前端** | HTML + ECharts + 原生JS | 轻量级，无需打包工具 |
| **后端** | FastAPI + Uvicorn | 高性能异步框架 |
| **数据源** | 复用现有采集器 | 避免重复开发 |
| **缓存** | 内存缓存（TTL） | 轻量级，无需额外依赖 |
| **部署** | Nginx + systemd / Docker | 支持本地部署和容器化部署 |

---

## 目录结构

```
dashboard/
├── frontend/                    # 前端静态文件
│   ├── index.html              # 首页（总览仪表板）
│   ├── detail.html             # 详情页（单个策略）
│   ├── css/
│   │   └── style.css           # 主样式文件
│   └── js/
│       ├── api.js              # API 调用封装
│       ├── charts.js           # 图表配置
│       ├── config.js           # 前端配置（API地址、图表颜色等）
│       ├── main.js             # 主逻辑
│       └── vendor/
│           └── echarts.min.js  # ECharts本地库文件（优先加载）
├── backend/                     # 后端 API 服务
│   ├── main.py                 # FastAPI 主程序（非Docker环境）
│   ├── main_docker.py          # FastAPI 主程序（Docker环境入口）
│   ├── Dockerfile              # Docker镜像构建文件
│   ├── docker-compose.yml      # Docker Compose编排文件
│   ├── config.yaml             # 配置文件
│   ├── requirements.txt        # Python 依赖
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py           # API 路由（非Docker环境）
│   │   └── routes_docker.py    # API 路由（Docker环境）
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py           # 配置管理
│   │   └── cache.py            # 缓存管理
│   ├── models/
│   │   ├── __init__.py
│   │   └── schemas.py          # 数据模型
│   └── services/
│       ├── __init__.py
│       ├── data_service.py     # 数据服务（非Docker环境）
│       └── data_service_docker.py  # 数据服务（Docker环境）
└── nginx/
    └── dashboard.conf           # Nginx 配置
```

---

## 快速开始

### 环境要求

- Python 3.10+
- PostgreSQL 12+
- Nginx

### 本地开发

1. **安装依赖**

```bash
cd dashboard/backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

2. **配置环境变量**

创建 `.env` 文件：

```bash
# 数据库配置
DB_HOST=localhost
DB_PORT=5432
DB_NAME=trading
DB_USER=postgres
DB_PASSWORD=your_password

# Binance API 配置
BINANCE_API_KEY=your_api_key
BINANCE_API_SECRET=your_api_secret

# API 配置
API_HOST=0.0.0.0
API_PORT=8000
API_DEBUG=true

# 缓存配置
CACHE_TTL_DAILY=60
CACHE_TTL_WEEKLY=180
CACHE_TTL_MONTHLY=300

# income API 缓存配置
INCOME_CACHE_TTL=30
INCOME_CACHE_MAX=100

# API 并发配置
API_CONCURRENCY=5
```

3. **启动后端服务**

```bash
cd dashboard/backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

4. **访问应用**

- API 文档: http://localhost:8000/api/docs
- 前端页面: 使用任意 HTTP 服务器托管 `frontend/` 目录

---

## 配置说明

### 后端配置 (config.yaml)

```yaml
# 应用配置
app:
  version: "1.0.0"           # 应用版本号
  timezone_offset: 8         # 时区偏移量（北京时间 UTC+8）

# API 服务配置
api:
  host: "0.0.0.0"
  port: 8000
  debug: false

# 缓存配置
cache:
  enabled: true
  ttl_daily: 60         # 日报缓存 60 秒
  ttl_weekly: 180       # 周报缓存 180 秒
  ttl_monthly: 300      # 月报缓存 300 秒
  ttl_metadata: 86400   # 元数据缓存 24 小时

# income API 缓存配置
income_cache:
  ttl: 30               # income 缓存 30 秒
  max_size: 100         # 最大缓存条目数

# API 并发配置
api_concurrency:
  max_concurrent: 5     # allOrders 并发上限（Semaphore 限流）

# 数据库配置
database:
  host: "${DB_HOST:localhost}"
  port: "${DB_PORT:5432}"
  database: "${DB_NAME:trading}"
  user: "${DB_USER:postgres}"
  password: "${DB_PASSWORD}"
  min_pool_size: 5       # 最小连接池大小
  max_pool_size: 20      # 最大连接池大小

# 策略配置
strategies:
  btc_eth:
    name: "MTPCS策略"
    emoji: "📈"
    symbols: ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "TRXUSDT"]
```

### 前端配置 (js/config.js)

```javascript
const DashboardConfig = {
    // API 配置
    api: {
        baseUrl: '/api',  // API 基础地址
        timeout: 30000    // 请求超时时间（毫秒）
    },
    
    // 趋势图配置
    trend: {
        defaultDays: 7,   // 默认显示天数
        maxDays: 30       // 最大显示天数
    },
    
    // 图表主题颜色
    chartColors: [
        '#F59E0B', // Primary 金色
        '#8B5CF6', // Accent 紫色
        '#10B981', // Success 绿色
        // ... 更多颜色
    ],
    
    // CDN 配置（ECharts降级方案：本地 vendor/echarts.min.js 优先加载）
    cdn: {
        echarts_primary: 'https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js',
        echarts_fallback: 'https://cdn.bootcdn.net/ajax/libs/echarts/5.5.0/echarts.min.js'
    }
};
```

### Nginx 配置

修改 `nginx/dashboard.conf` 中的路径和域名：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        root /path/to/dashboard/frontend;
        # ...
    }

    location /api/ {
        proxy_pass http://localhost:8000;
        # ...
    }
}
```

---

## API 接口

### 接口列表

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/health` | GET | 健康检查 |
| `/api/metadata` | GET | 元数据（策略映射、时间范围） |
| `/api/overview` | GET | 总览数据 |
| `/api/strategies` | GET | 策略列表 |
| `/api/strategies/{id}` | GET | 策略详情 |
| `/api/strategies/{id}/symbols` | GET | 币种明细 |
| `/api/trend` | GET | 趋势数据 |

### 示例请求

```bash
# 获取日报总览
curl http://localhost:8000/api/overview?type=daily

# 获取策略详情
curl http://localhost:8000/api/strategies/btc_eth?type=weekly
```

---

## 部署指南

### 生产环境部署

1. **安装系统依赖**

```bash
sudo apt update
sudo apt install python3-pip nginx
```

2. **部署后端**

```bash
# 复制代码
sudo mkdir -p /opt/dashboard
sudo cp -r dashboard/backend /opt/dashboard/

# 安装依赖
cd /opt/dashboard/backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 创建 systemd 服务
sudo cat > /etc/systemd/system/dashboard-api.service << EOF
[Unit]
Description=Dashboard API Service
After=network.target postgresql.service

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/dashboard/backend
Environment="PATH=/opt/dashboard/backend/venv/bin"
ExecStart=/opt/dashboard/backend/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# 启动服务
sudo systemctl daemon-reload
sudo systemctl enable dashboard-api
sudo systemctl start dashboard-api
```

3. **部署前端**

```bash
# 复制前端文件
sudo cp -r dashboard/frontend /opt/dashboard/

# 配置 Nginx
sudo cp dashboard/nginx/dashboard.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/dashboard.conf /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

4. **验证部署**

```bash
# 检查服务状态
sudo systemctl status dashboard-api

# 检查 API
curl http://localhost:8000/api/health

# 检查前端
curl http://localhost/
```

### Docker 部署

适用于生产环境容器化部署，与现有 Docker 基础设施集成。

**前提条件**：
- Docker 已安装
- 外部网络 `trading-network-v2` 已创建（用于连接数据库）

**部署步骤**：

```bash
# 1. 进入 Docker Compose 目录
cd dashboard/backend

# 2. 构建并启动（首次部署或代码变更后）
docker-compose build --no-cache && docker-compose up -d

# 3. 仅重启容器（配置变更后）
docker-compose restart

# 4. 查看容器状态
docker ps -f name=dashboard-api

# 5. 查看日志
docker logs -f dashboard-api

# 6. 验证 API
curl http://localhost:8767/api/health
```

**Docker 部署关键参数**：

| 参数 | 值 | 说明 |
|------|------|------|
| 服务端口 | 8767 | Dashboard API 端口 |
| 容器名称 | dashboard-api | Docker 容器名 |
| Docker 网络 | trading-network-v2 | 连接数据库的网络 |
| 健康检查 | /api/health | 健康检查接口 |

**环境区分**：

| 文件 | 环境 | 说明 |
|------|------|------|
| `main.py` + `routes.py` + `data_service.py` | 本地开发 | 连接本地 PostgreSQL |
| `main_docker.py` + `routes_docker.py` + `data_service_docker.py` | Docker 生产 | 通过 Docker 网络连接数据库 |

---

## 开发指南

### 添加新策略

1. 在 `backend/config.yaml` 中添加策略配置：

```yaml
strategies:
  new_strategy:
    name: "新策略"
    emoji: "🎯"
    symbols: ["BTCUSDT"]
```

2. 重启服务即可，无需修改代码。

### 自定义图表

修改 `frontend/js/charts.js` 中的 ECharts 配置：

```javascript
const fintechDarkTheme = {
    // 自定义主题配置
    color: ['#F59E0B', '#8B5CF6', ...]
};
```

### 扩展 API

在 `backend/api/routes.py` 中添加新的路由：

```python
@router.get("/custom-endpoint")
async def custom_endpoint():
    # 实现逻辑
    return {"data": "..."}
```

---

## 故障排查

### 常见问题

**Q: API 返回 503 错误**

A: 检查数据库连接和 Binance API 配置是否正确。

**Q: 前端无法访问 API**

A: 检查 Nginx 配置和 CORS 设置。

**Q: 数据不更新**

A: 检查缓存配置和采集器是否正常运行。

---

## 许可证

内部项目，仅供团队使用。

---

**最后更新**: 2026-06-03
