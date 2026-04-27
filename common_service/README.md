# 统一基础设施服务 (common_service)

> 为多个交易系统提供统一的 K 线数据采集、处理和通知服务

**版本**: v1.0  
**状态**: ✅ Phase 2 完成（通知服务就绪）  
**创建日期**: 2026-04-20  
**最后更新**: 2026-04-20

---

## 🚀 快速启动

### 本地开发（5 分钟）

```bash
# 1. 配置环境变量
cp .env.example .env
vim .env  # 编辑配置

# 2. 启动服务
docker-compose up -d

# 3. 验证部署
curl http://localhost:8765/api/v1/health  # K 线数据服务
curl http://localhost:8766/api/v1/health  # 通知服务
```

**详细指南**: [QUICKSTART.md](QUICKSTART.md)

### 服务器部署

```bash
# 1. 配置 SSH 免密登录
ssh-keygen -t ed25519 -C "your_email@example.com"
ssh-copy-id -i /Users/yl/vscode/inspection_automation/docs/only.pem.pub root@SERVER_IP

# 2. 配置部署文件
cp .deploy_config.example .deploy_config
vim .deploy_config

# 3. 一键部署
./one_click_deploy.sh
```

---

## 📖 项目简介

本项目是一个统一的基础设施服务平台，为以下 5 个交易/监控系统提供共享能力：

1. **BTC/ETH 交易系统** (`/Users/yl/vscode/bianace_btcethbnb_trade`)
2. **新币做空系统** (`/Users/yl/vscode/bianace_newtrade_trade`)
3. **网格交易系统** (`/Users/yl/vscode/Grid_Trading`)
4. **服务器巡检系统** (`/Users/yl/vscode/inspection_automation`)
5. **A 股筛选系统** (`/Users/yl/vscode/stockfilter`)

### 核心价值

- ✅ **降低 API 调用频率**：统一数据采集，避免重复请求币安 API
- ✅ **统一通知能力**：标准化消息发送接口，支持异步消息队列
- ✅ **数据一致性**：所有系统使用同一数据源
- ✅ **降低维护成本**：公共模块统一管理

---

## 🏗️ 服务组成

```
统一基础设施服务
├── K 线数据服务 (端口：8765)
│   ├── 定时采集币安 K 线数据
│   ├── 计算技术指标 (MA/EMA/RSI/MACD/ATR/布林带)
│   ├── PostgreSQL 存储（分区表）
│   └── REST API 查询接口
│
└── 统一通知服务 (端口：8766)
    ├── REST API 接收消息
    ├── Redis 消息队列（异步处理）
    ├── 飞书 Webhook 推送
    └── 错误重试 + 频率控制
```

---

## 🚀 快速开始

### 前置要求

- Docker 20+
- Docker Compose 2.0+
- Git

### 安装步骤

**1. 克隆项目**

```bash
git clone <repo_url>
cd binance_common_service
```

**2. 配置环境变量**

```bash
cp .env.example .env
vim .env  # 编辑配置
```

**.env 配置示例**:

```bash
# 数据库配置
DB_PASSWORD=your_secure_password

# 飞书 Webhook（5 个项目）
BTC_ETH_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx_btc_eth
NEW_COIN_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx_new_coin
GRID_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx_grid
INSPECTION_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx_inspection
STOCK_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx_stock

# 通知服务配置
WORKER_COUNT=3
RATE_LIMIT_PER_MINUTE=60

# K 线数据服务配置
SYMBOLS=BTCUSDT,ETHUSDT,BNBUSDT
COLLECT_INTERVALS=15m,1h,4h,1d
```

**3. 启动服务**

```bash
docker-compose up -d
```

**4. 验证部署**

```bash
# 查看日志
docker-compose logs -f

# 健康检查
curl http://localhost:8765/api/v1/health  # K 线数据服务
curl http://localhost:8766/api/v1/health  # 通知服务
```

**注意**: 项目名称已简化为 `common_service`，但服务功能保持不变。

---

## 📡 API 使用

### K 线数据服务 API

**查询 K 线数据**

```bash
curl "http://localhost:8765/api/v1/klines?symbol=BTCUSDT&interval=1h&limit=100"
```

**查询指标数据**

```bash
curl "http://localhost:8765/api/v1/indicators?symbol=BTCUSDT&interval=1h&indicator_type=RSI"
```

**健康检查**

```bash
curl http://localhost:8765/api/v1/health
```

### 通知服务 API

**发送消息**

```bash
curl -X POST http://localhost:8766/api/v1/send \
  -H "Content-Type: application/json" \
  -d '{
    "project": "btc_eth",
    "message": "测试消息",
    "type": "text",
    "level": "info"
  }'
```

**查询队列状态**

```bash
curl http://localhost:8766/api/v1/queue/status
```

**健康检查**

```bash
curl http://localhost:8766/api/v1/health
```

---

## 📚 文档导航

### 需求文档

- [需求分析](docs/requirements/统一基础设施服务需求分析.md)
- [功能清单](docs/requirements/统一基础设施服务需求分析.md#4-功能性需求)

### 设计文档

- [技术架构](docs/designs/技术架构设计.md)
- [数据库设计](docs/designs/技术架构设计.md#4-数据架构)
- [接口设计](docs/designs/技术架构设计.md#5-接口设计)

### 计划文档

- [实施计划](docs/plans/实施计划.md)
- [时间估算](docs/plans/实施计划.md#4-时间估算)

---

## 🛠️ 技术栈

| 组件 | 技术 | 版本 |
|------|------|------|
| 后端框架 | FastAPI | 0.104+ |
| 数据库 | PostgreSQL | 14+ |
| 消息队列 | Redis | 7+ |
| 定时任务 | APScheduler | 3.10+ |
| 容器化 | Docker | 20+ |
| 编排工具 | Docker Compose | 2.0+ |

---

## 📊 监控与运维

### 健康检查

```bash
# K 线数据服务
curl http://localhost:8765/api/v1/health

# 通知服务
curl http://localhost:8766/api/v1/health
```

### 日志查看

```bash
# 查看所有服务日志
docker-compose logs -f

# 查看单个服务日志
docker-compose logs -f kline_service
docker-compose logs -f notification_service
```

### 性能监控

- API 响应时间：< 500ms
- 队列处理速度：> 100 条/分钟
- 数据库连接池：20 个连接

---

## 🔧 开发指南

### 本地开发

```bash
# 安装依赖
pip install -r kline_data_service/requirements.txt
pip install -r notification_service/requirements.txt

# 运行服务（不使用 Docker）
python kline_data_service/src/main.py
python notification_service/src/main.py
```

### 运行测试

```bash
# 单元测试
pytest kline_data_service/tests/
pytest notification_service/tests/

# 集成测试
pytest tests/integration/
```

---

## 📝 变更日志

### v1.0.0 (2026-04-20)

- ✅ 初始版本
- ✅ K 线数据服务基础功能
- ✅ 通知服务基础功能
- ✅ Docker 部署支持

---

## 🤝 贡献指南

1. Fork 本仓库
2. 创建特性分支
3. 提交变更
4. 推送到分支
5. 创建 Pull Request

---

## 📄 许可证

MIT License

---

## 📞 联系方式

- 项目文档：`/docs/` 目录
- 问题反馈：GitHub Issues

---

**最后更新**: 2026-04-20
