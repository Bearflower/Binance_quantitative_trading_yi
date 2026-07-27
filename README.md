# Binance_quantitative_trading

统一交易系统 - 多策略量化交易平台，整合趋势跟踪、网格交易、新币做空、混合反转等多种策略，支持 AI 自动调参、数据看板、飞书通知。

## 策略概览

| 策略 | 策略ID | 版本 | 状态 | 说明 |
|------|--------|:----:|:----:|------|
| **MTPCS 趋势策略** | btc_eth | v2.5.0 | 🟢 运行中 | BTC/ETH 永续合约趋势跟踪，动态ATR过滤+自适应仓位 |
| **网格交易策略** | grid | v2.4.0 | 🟢 运行中 | ETHUSDT 三层预警架构，解决 ADX 滞后问题 |
| **新币做空策略** | new_coin | v1.1.0 | 🟢 运行中 | 新上线币种 V4.1 信号质量优化版 |
| **HRS 混合反转策略** | hrs | v2.3.0 | 🟢 运行中 | 全市场动态阈值，双轨并行信号机制 |
| **StratTuneAI 调优系统** | — | v1.0 | 🟢 运行中 | AI 驱动多策略参数自动调优（覆盖 MTPCS + 新币做空） |
| **数据看板 Dashboard** | — | v1.0 | 🟢 运行中 | 交易数据可视化，策略分析、趋势图表 |

**部署状态**：✅ 已部署（4 个策略容器 + AI 调优容器 + 数据看板 + K线服务）
**服务器**：43.156.242.184

## 项目结构

```
├── strategies/           # 策略模块
│   ├── btc_eth/          # MTPCS 趋势策略（BTC/ETH）
│   ├── grid/             # 网格交易策略（ETHUSDT）
│   ├── new_coin/         # 新币做空策略
│   └── hrs/              # HRS 混合反转策略
├── shared/               # 共享核心模块
│   ├── base_strategy.py  # 策略基类
│   ├── binance_api.py    # Binance API 客户端
│   ├── condition_orders.py # 条件单管理
│   ├── database.py       # 数据库服务
│   ├── kline_service.py  # K线服务客户端
│   ├── notification.py   # 飞书通知服务
│   ├── trade_logger.py   # 统一交易记录器
│   ├── indicators.py     # 技术指标
│   ├── dynamic_atr_filter.py  # 动态ATR过滤器
│   └── dynamic_trailing.py    # 动态追踪止盈
├── ai_tuner/             # StratTuneAI 多策略AI调优系统
├── dashboard/            # 数据可视化看板
├── services/             # 独立服务
│   ├── kline_service/    # K线数据采集服务
│   └── kline_monitor/    # K线数据监控服务
├── docs/                 # 项目文档
├── tests/                # 测试代码
├── scripts/              # 运维脚本
├── tools/                # 工具脚本
└── database/             # 数据库初始化脚本
```

## 核心特性

- **统一交易记录器 (TradeLogger)**: 在 BinanceClient 层面自动记录所有策略的成交订单到 `trading.trade_records` 表，新增策略无需额外代码
- **多策略飞书通知**: 每个策略独立 Webhook，消息发送到专属群
- **动态ATR过滤器**: 根据历史波动率分布和ADX趋势强度动态调整最低ATR%阈值
- **AI 自动调参**: StratTuneAI 每周自动分析策略表现，生成调优建议，人工审批后生效
- **数据可视化看板**: 轻量级交易数据可视化，展示日报/周报、策略分析、趋势图表
- **条件单管理**: 统一的限价单、止盈止损单管理，孤儿订单自动检测与修复
- **Docker 容器化**: 每个策略独立容器运行，互不干扰

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env

# 运行策略（以 MTPCS 为例）
python strategies/btc_eth/main.py
```

## Dashboard 数据看板

### 功能特性

- **数据展示**: 总览数据、策略详情、币种明细、趋势图表
- **实时更新**: 支持日报/周报数据自动更新
- **可视化**: 使用 ECharts 进行金融级可视化
- **易部署**: 轻量级架构，易于部署和维护
- **安全可控**: IP 白名单控制，API 限流保护

### 访问地址

- **生产环境**: http://43.156.242.184/

## StratTuneAI 多策略AI调优系统

### 功能特性

StratTuneAI 是一个 AI 驱动的多策略参数自动调优系统，以独立 Docker 容器运行，不侵入现有策略容器。

- **自动化分析**：每周日 23:55 自动采集各策略的周度表现数据，生成标准化健康报告
- **AI 辅助决策**：利用 DeepSeek 大模型分析报告，结合历史调优记忆，生成参数调整建议
- **人工审批在环**：所有 AI 建议通过飞书卡片推送，必须经人工确认后方可生效
- **安全兜底**：自动回滚机制保护策略在极端情况下的安全（24h 连续亏损/累计亏损触发回滚）
- **知识沉淀**：每次调优过程结构化记录到 `trading.strategy_memory` 表，形成可追溯的策略进化日志

### 第一期覆盖策略

| 策略 | 策略ID | 状态 |
|------|--------|:----:|
| MTPCS 趋势策略 | btc_eth | 第一期 |
| 新币做空策略 | new_coin | 第一期 |

### 技术栈

| 模块 | 技术选型 |
|------|----------|
| 定时调度 | APScheduler 3.x（每周日 23:55） |
| AI 引擎 | DeepSeek (deepseek-chat) |
| 数据库 | PostgreSQL（trading schema） |
| 通知 | 飞书通知服务 |
| 容器化 | 独立 Docker 容器 |

## 文档

- [文档索引](docs/README.md) - 完整文档目录
- [系统架构设计](docs/architecture/系统架构设计.md)
- [数据库设计](docs/architecture/数据库设计.md)
- [StratTuneAI 架构设计](docs/architecture/StratTuneAI架构设计.md)
- [Dashboard 架构设计](docs/design/dashboard_architecture.md)
- [Dashboard UI 设计](docs/design/dashboard_ui_design.md)
- [部署指南](docs/deployment/统一交易系统部署指南.md)
- [迁移方案](docs/migration/README.md)
- [版本更新日志](CHANGELOG.md)