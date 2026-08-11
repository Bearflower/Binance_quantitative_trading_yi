# 币安自动化交易系统

> 基于 traderule.txt 规则引擎的币安合约自动化交易系统，支持 PM 账户（投资组合保证金账户）

**最新版本**: v6.23 (2026-07-27) - 孤儿条件单修复：条件单取消重试机制、启动时孤儿条件单检测、并发安全

---

## 📖 文档导航

本项目采用规范化文档管理，所有文档存储在 `docs/` 目录：

| 文档类型 | 路径 | 说明 |
|---------|------|------|
| 📘 **项目需求迭代文档** | [`docs/proposals/项目需求迭代文档.md`](docs/proposals/项目需求迭代文档.md) | 完整功能说明、版本迭代、使用指南 |
| 🏗️ **技术架构文档** | [`docs/design/技术架构文档.md`](docs/design/技术架构文档.md) | 系统架构、模块设计、数据流、部署架构 |
| 📋 **快速开始** | [`docs/proposals/QUICKSTART.md`](docs/proposals/QUICKSTART.md) | 新手快速上手指南 |
| 📊 **回测指南** | [`docs/proposals/回测模块使用指南.md`](docs/proposals/回测模块使用指南.md) | 回测系统使用说明 |
| 🔧 **通用模块使用** | [`docs/通用模块使用说明.md`](docs/通用模块使用说明.md) | 飞书通知、K 线数据服务使用说明 |

## 🎯 核心特性

- ✅ **通用 K 线服务** (v6.14.0) - 集成统一 K 线数据服务，数据持久化
- ✅ **量化评分系统** (v6.12) - 多维度信号评分（趋势、形态、动量）
- ✅ **动态仓位调整** (v6.13) - 根据可用保证金自动缩放仓位
- ✅ **优化止盈止损** (v6.13.1) - 降低止盈目标 + 时间止损
- ✅ **限价单优化** (v6.13.2) - 市价单改限价单，节省 60% 手续费
- ✅ **胜率统计** (v6.14.0) - 自动检测平仓并更新胜率统计
- ✅ **市场状态识别** (v6.19) - 5 条件强趋势市判定，策略模式动态切换
- ✅ **震荡市策略** (v6.20) - 反转信号入场，快进快出，独立风控
- ✅ **全量限价单** (v6.20.3) - 止损/止盈/平仓全部改为限价单，节省 60% 手续费
- ✅ **per-symbol 震荡市差异化配置** (v6.21) - BNBUSDT 独立参数（ranging_adx_min、ranging_max_daily_trades、ranging_cooldown_hours）
- ✅ **震荡市正式实现** (v6.22) - 投票制入场、评分分流、止损参数分支
- ✅ **孤儿条件单修复** (v6.23) - 条件单取消重试机制、启动时孤儿条件单检测、并发安全
- ✅ **频率控制** - 每日交易限制 + 冷却期管理
- ✅ **多时间框架分析** - 日线 +4 小时 +1 小时全量数据
- ✅ **自动调度** - 每小时执行分析（可配置执行时间）
- ✅ **飞书通知** - 重要事件实时推送
- ✅ **Docker 部署** - 一键部署到服务器
- ✅ **配置化管理** - 通过配置文件灵活调整执行时间

## 🚀 快速部署

```bash
# 部署 v6.14.0 到服务器
bash deploy_v6140.sh

# 查看运行状态
ssh root@43.156.242.184 "docker ps -f name=binance-trade-analyzer"
```

## 📁 项目结构

```
bianace_btcethbnb_trade/
├── core/                      # 核心模块
│   ├── signal/               # 信号模块（拆分为3个子模块）
│   │   ├── detector.py       # 信号检测核心
│   │   ├── filter.py         # 过滤器（ADX、成交量、ATR）
│   │   └── validator.py      # 验证器（一票否决项）
│   ├── scoring/              # 评分模块（工厂模式）
│   │   ├── base.py           # 评分引擎基类
│   │   ├── v612.py           # v6.12版本实现
│   │   └── factory.py        # 工厂模式
│   ├── data/                 # 数据模块（拆分为3个子模块）
│   │   ├── fetcher.py        # 数据获取（支持并发）
│   │   ├── indicators.py     # 指标计算
│   │   └── cache.py          # 缓存管理（TTL + LRU）
│   ├── position_calculator.py # 仓位计算
│   ├── risk_manager.py       # 风险管理
│   ├── order_generator.py    # 订单生成
│   └── emergency_handler.py  # 应急处理
├── scheduler/                # 调度器模块（拆分为5个子模块）
│   ├── scheduler.py          # 调度器核心
│   ├── analyzer.py           # 分析流程
│   ├── trade_executor.py     # 交易执行
│   ├── statistics.py         # 统计功能
│   └── notifier.py           # 通知功能
├── services/                 # 服务模块
│   ├── base.py               # 🆕 服务基类（v1.0.0）
│   ├── frequency_controller.py # 频率控制
│   ├── rule_executor.py      # 规则执行器
│   └── trade_executor.py     # 交易执行器
├── models/                   # 数据模型
│   ├── repository.py         # 🆕 数据仓库基类（v1.0.0）
│   ├── entities.py           # 🆕 具体数据仓库实现（v1.0.0）
│   └── database.py           # 数据库连接管理
├── utils/                    # 工具模块
│   ├── exceptions.py         # 🆕 自定义异常类（v1.0.0）
│   ├── error_handler.py      # 🆕 统一错误处理器（v1.0.0）
│   ├── logger.py             # 🆕 统一日志配置（v1.0.0）
│   ├── kline_service.py      # 通用 K 线服务客户端
│   ├── technical_indicators.py # 技术指标计算
│   └── lark_notifier_v2.py   # 飞书通知服务
├── config/                   # 配置文件
│   ├── config.yaml           # 🆕 统一配置文件（v1.0.0）
│   ├── config_manager.py     # 🆕 统一配置管理器（v1.0.0）
│   └── scheduler_config.yaml # 调度器配置
├── tests/                    # 测试模块
│   ├── test_new_infrastructure.py # 🆕 新基础设施测试（v1.0.0）
│   ├── test_core_modules.py  # 核心模块测试
│   ├── test_service_base.py  # 服务基类测试
│   └── test_repository.py    # 数据仓库测试
├── scripts/                  # 脚本工具
│   ├── set_schedule_time.sh  # 快速修改执行时间脚本
│   └── fix_win_rate_issue.py # 胜率问题诊断和修复脚本 (v1.0.1)
├── docs/                     # 项目文档
│   ├── design/               # 设计文档（技术架构、模块设计）
│   ├── proposals/            # 方案文档（使用指南、需求迭代）
│   ├── reports/              # 报告文档（版本报告、回测报告）
│   └── deployment/           # 部署文档
├── scheduler_new.py          # 主调度器（已拆分为scheduler/模块）
└── Dockerfile                # Docker 配置
```

**v1.0.0 重构说明** (2026-04-27):

**阶段一：配置统一与基础设施重构**
- ✅ 新增统一配置管理器 `config/config_manager.py`
- ✅ 新增统一配置文件 `config/config.yaml`
- ✅ 新增自定义异常类 `utils/exceptions.py`
- ✅ 新增统一错误处理器 `utils/error_handler.py`
- ✅ 新增统一日志配置 `utils/logger.py`
- ✅ 新增测试脚本 `tests/test_new_infrastructure.py`

**阶段二：核心模块重构**
- ✅ 调度器模块拆分：`scheduler_new.py` (904行) → 5个子模块
- ✅ 信号模块拆分：`signal_detector.py` (502行) → 3个子模块
- ✅ 评分模块重构：工厂模式，支持版本管理
- ✅ 数据模块拆分：`data_fetcher.py` (563行) → 3个子模块
- ✅ 缓存增强：TTL + LRU策略
- ✅ 并发支持：多币种并发数据获取

**阶段三：服务层重构**
- ✅ 服务基类：创建统一的BaseService
- ✅ 数据仓库：引入Repository Pattern
- ✅ 服务重构：frequency_controller、rule_executor、trade_executor
- ✅ 测试结果：104个测试通过，代码质量评分97.7/100

**详细报告**：
- [`docs/reports/重构第一阶段实施报告.md`](docs/reports/重构第一阶段实施报告.md)
- [`docs/design/技术架构文档.md`](docs/design/技术架构文档.md)

## ⚙️ 配置说明

### 修改执行时间

系统默认每小时执行一次，可以通过以下命令快速修改执行时间：

```bash
# 修改为每小时的 05 分执行
ssh root@43.156.242.184 "cd /root/bianace_btcethbnb_trade && ./set_schedule_time.sh 05"

# 修改为每小时的 15 分执行
ssh root@43.156.242.184 "cd /root/bianace_btcethbnb_trade && ./set_schedule_time.sh 15"

# 修改为每小时的 30 分执行
ssh root@43.156.242.184 "cd /root/bianace_btcethbnb_trade && ./set_schedule_time.sh 30"
```

**详细说明**: 查看 [`docs/如何修改执行时间.md`](docs/如何修改执行时间.md)

### 配置文件

调度器配置文件：`/root/bianace_btcethbnb_trade/config/scheduler_config.yaml`

```yaml
hourly_analysis:
  minute: 05  # 每小时 05 分执行（00:05, 01:05, ..., 23:05）

daily_report:
  hour: 9     # 每天 09:00 发送日报
  minute: 0
```

├── backtesting/               # 回测模块
├── scripts/                   # 脚本模块（数据获取、回测执行）
├── config/                    # 配置文件（策略参数、评分参数）
├── utils/                     # 工具模块（K 线服务、技术指标、通知服务）
│   ├── kline_service.py      # 🆕 通用 K 线服务客户端
│   ├── technical_indicators.py # 技术指标计算（使用通用 K 线服务）
│   └── lark_notifier_v2.py   # 飞书通知服务
├── docs/                      # 📚 所有文档
│   ├── design/                # 设计文档（技术架构、模块设计）
│   ├── proposals/             # 方案文档（使用指南、需求迭代）
│   ├── reports/               # 报告文档（版本报告、回测报告）
│   └── deployment/            # 部署文档
├── scheduler_new.py           # 主调度器
└── Dockerfile                 # Docker 配置
```

## 📊 最新版本 v6.14.0

**核心优化** (2026-04-21):

### 1. 通用 K 线服务集成 ⭐

**服务地址**: `http://43.156.242.184:8765/api/v1`

**优势**:
- ✅ 数据持久化到 PostgreSQL 数据库
- ✅ 多个项目共享 K 线数据
- ✅ 统一管理，减少重复 API 调用
- ✅ 支持多种时间间隔（15m, 1h, 4h, 1d）

**调用示例**:
```python
# utils/technical_indicators.py
KLINE_SERVICE_URL = "http://43.156.242.184:8765/api/v1"

def get_binance_klines(symbol="BTCUSDT", interval="1h", limit=100):
    url = f"{KLINE_SERVICE_URL}/klines/latest?symbol={symbol}&interval={interval}&limit={limit}"
    response = requests.get(url, timeout=10)
    return response.json()['data']
```

**服务状态**:
- ✅ 容器运行：`common_service_kline - Up (healthy)`
- ✅ API 可访问：`http://43.156.242.184:8765`
- ✅ 数据采集：定时任务每 15 分钟采集一次

### 2. 胜率统计修复 ⭐

**问题**: 胜率统计方法从未调用，导致交易日报胜率始终为 0%

**修复**:
- ✅ 新增 `_check_closed_positions_and_update_stats()` 方法
- ✅ 每小时检查已平仓订单
- ✅ 自动计算盈亏并更新胜率统计
- ✅ 交易日报正常显示胜率

**调用时机**: 每小时整点分析时自动检查

**验证方法**:
```bash
# 查看胜率统计日志
ssh root@43.156.242.184 "docker logs binance-trade-analyzer 2>&1 | grep -E '检查已平仓 | 平仓统计完成'"

# 查询数据库统计
ssh root@43.156.242.184 "docker exec binance-trade-analyzer psql -h localhost -U postgres -d trading_system -c 'SELECT stat_date, win_count, loss_count FROM daily_execution_stats ORDER BY stat_date DESC LIMIT 7;'"
```

### 3. 飞书通知服务 ⭐

**服务地址**: `http://43.156.242.184:8766/api/v1`

**调用示例**:
```python
from utils.lark_notifier_v2 import LarkNotifier

notifier = LarkNotifier()
notifier.send_notification(
    message="BTC 突破 70000 美元！",
    level="warning"
)
```

**服务状态**:
- ✅ 容器运行：`common_service_notification - Up (healthy)`
- ✅ API 可访问：`http://43.156.242.184:8766`
- ✅ 消息队列：异步发送，支持限流

## 📈 历史版本

### v6.13.2 (2026-04-13) - 限价单优化

**核心优化**:
- 开仓订单：市价单 → 限价单
- 做多：按买一价下单，做空：按卖一价下单
- 手续费：taker 0.05% → maker 0.02%（节省 60%）

**手续费节省**（每天 4 笔交易）:
- 市价单月手续费：约 12U
- 限价单月手续费：约 4.8U
- **每月节省：约 7.2U**（年度节省 86.4U）

详细数据请查看：[`docs/reports/v6132 部署验证报告.md`](docs/reports/v6132 部署验证报告.md)

### v6.13.1 (2026-04-12) - 止盈止损优化

**核心优化**:
- 降低止盈目标：TP1 从 4.0×ATR 降至 2.5×ATR
- 优化吊灯止损：启动从 2.5×ATR 降至 1.8×ATR
- 新增时间止损：72 小时未达 TP1 自动平仓 50%

**回测结果** (6 个月，109 笔交易):
- 胜率：100%（所有交易都盈利）
- 总盈亏：+639U
- 最大回撤：0.0%
- 夏普比率：22.56

详细数据请查看：[`docs/reports/v613_vs_v6131 对比报告.md`](docs/reports/v613_vs_v6131 对比报告.md)

## 🔧 通用服务配置

### 环境变量配置

在项目根目录创建 `.env` 文件：

```bash
# 通用服务配置
NOTIFICATION_SERVICE_URL=http://43.156.242.184:8766/api/v1
KLINE_SERVICE_URL=http://43.156.242.184:8765/api/v1
NOTIFICATION_PROJECT=btc_eth

# 数据库配置
DATABASE_URL=postgresql://postgres:password@localhost:5432/trading_system

# 币安 API 配置（备用）
BINANCE_API_KEY=your_api_key
BINANCE_API_SECRET=your_api_secret
```

### 服务依赖

**通用服务**（运行在 43.156.242.184）:
- ✅ K 线数据服务：端口 8765
- ✅ 飞书通知服务：端口 8766
- ✅ PostgreSQL 数据库：端口 5432
- ✅ Redis 缓存：端口 6379

**本地项目**:
- ✅ Docker 容器：binance-trade-analyzer
- ✅ 调度器：每小时整点执行
- ✅ 数据库：schema_bianace

## 📊 系统架构

```
┌─────────────────────────────────────────────────┐
│           通用服务层 (43.156.242.184)            │
├─────────────────────────────────────────────────┤
│  K 线数据服务  │  飞书通知服务  │  PostgreSQL   │
│   (8765)       │   (8766)       │   (5432)     │
└─────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────┐
│              项目应用层 (Docker)                 │
├─────────────────────────────────────────────────┤
│  scheduler_new.py (主调度器)                     │
│  ├─ 每小时行情分析 (整点执行)                   │
│  ├─ 信号检测 (core/signal_detector.py)          │
│  ├─ 交易执行 (core/trading_executor.py)         │
│  └─ 胜率统计更新                                 │
├─────────────────────────────────────────────────┤
│  core/data_fetcher.py (数据获取)                │
│  └─ 从通用 K 线服务获取数据 ⭐                   │
├─────────────────────────────────────────────────┤
│  utils/technical_indicators.py (技术指标)       │
│  └─ 从通用 K 线服务获取 K 线 ⭐                  │
└─────────────────────────────────────────────────┘
```

## 🚀 部署流程

### 一键部署脚本

项目根目录包含完整的部署脚本：

```bash
# 1. 自动打包
./auto_package.sh

# 2. 上传到服务器
./upload_to_server.sh

# 3. 一键部署（包含打包 + 上传 + 部署）
./one_click_deploy.sh
```

### 部署配置

创建 `.deploy_config` 文件：

```bash
# 服务器配置
SERVER_IP="43.156.242.184"
SERVER_USER="root"
SERVER_PROJECT_PATH="/root/bianace_btcethbnb_trade"

# Docker 配置
DOCKER_CONTAINER_NAME="binance-trade-analyzer"
DOCKER_IMAGE_NAME="bianace_btcethbnb_trade:latest"

# 项目配置
PROJECT_NAME="bianace_btcethbnb_trade"
DEPLOY_PACKAGE_NAME="deployment_package.tar.gz"
```

## 📈 监控和日志

### 查看容器状态

```bash
ssh root@43.156.242.184 "docker ps -f name=binance-trade-analyzer"
```

### 查看实时日志

```bash
ssh root@43.156.242.184 "docker logs -f binance-trade-analyzer"
```

### 查看特定日志

```bash
# 查看 K 线服务调用
ssh root@43.156.242.184 "docker logs binance-trade-analyzer 2>&1 | grep '从通用 K 线服务'"

# 查看信号检测
ssh root@43.156.242.184 "docker logs binance-trade-analyzer 2>&1 | grep '检测到.*信号'"

# 查看胜率统计
ssh root@43.156.242.184 "docker logs binance-trade-analyzer 2>&1 | grep -E '检查已平仓 | 平仓统计完成'"
```

### 数据库查询

```bash
# 查询交易记录
ssh root@43.156.242.184 "docker exec binance-trade-analyzer psql -h localhost -U postgres -d trading_system -c 'SELECT symbol, direction, pnl, status, created_at FROM trade_records ORDER BY created_at DESC LIMIT 10;'"

# 查询胜率统计
ssh root@43.156.242.184 "docker exec binance-trade-analyzer psql -h localhost -U postgres -d trading_system -c 'SELECT stat_date, executed_count, win_count, loss_count, (win_count::float / NULLIF(executed_count, 0) * 100) as win_rate FROM daily_execution_stats ORDER BY stat_date DESC LIMIT 7;'"
```

## ⚠️ 风险提示

1. 加密货币市场波动大，存在亏损风险
2. 回测结果不代表未来表现
3. 建议使用小资金测试，确认稳定后再增加投入
4. 通用服务依赖网络，建议配置本地降级策略

---

## 📝 最近报告

- [`K 线服务重新对接完成报告`](docs/reports/K 线服务重新对接完成报告.md) - 2026-04-21
- [`胜率统计修复完成报告`](docs/reports/胜率统计修复完成报告.md) - 2026-04-21
- [`回退部署完成报告`](docs/reports/回退部署完成报告.md) - 2026-04-21
- [`K 线服务修复进展报告`](docs/reports/K 线服务修复进展报告.md) - 2026-04-21

---

**完整文档**请查看 [`docs/`](docs/) 目录

**技术架构**详见 [`docs/design/技术架构文档.md`](docs/design/技术架构文档.md)

**需求迭代**详见 [`docs/proposals/项目需求迭代文档.md`](docs/proposals/项目需求迭代文档.md)

**通用服务**详见 [`docs/通用模块使用说明.md`](docs/通用模块使用说明.md)
