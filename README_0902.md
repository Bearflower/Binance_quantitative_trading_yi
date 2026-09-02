# Binance 量化交易系统 - 0902 版本快照

> 本文档为 2026-09-02 项目状态快照，记录各模块版本、近期变更和新增功能。

## 策略版本概览

| 策略 | 策略ID | 版本 | 状态 | 说明 |
|------|--------|:----:|:----:|------|
| **MTPCS 趋势策略** | btc_eth | v2.6.0 | 🟢 运行中 | BTC/ETH/BNB/SOL/XRP 趋势跟踪，v6.21 BNB参数放宽+差异化冷却+移除TRX |
| **网格交易策略** | grid | v2.4.0 | 🟢 运行中 | ETHUSDT 三层预警架构，解决 ADX 滞后问题 |
| **新币做空策略** | new_coin | v1.1.0 | 🟢 运行中 | V4.1 信号质量优化版，限价单替代市价单 |
| **HRS 混合反转策略** | hrs | v2.5.0 | 🟢 运行中 | 候选池扩容 + LV-RM 独立化，V2.6 趋势过滤增强 |
| **StratTuneAI 调优系统** | — | v1.0 | 🟢 运行中 | AI 多策略参数调优，覆盖 MTPCS + 新币做空 + 网格 |
| **数据看板 Dashboard** | — | v1.0.0 | 🟢 运行中 | 交易数据可视化，策略分析、趋势图表 |

**部署状态**：✅ 已部署
**服务器**：43.156.242.184

## 近期主要变更（0819 → 0902）

### 1. AI 调优系统增强

- **LLM JSON 解析修复**：增大 max_tokens + 精简提示词，修复 new_coin 策略 LLM JSON 解析失败
- **思考模式兼容**：增加回退搜索 + 提示词优化，修复 LLM 思考模式下 JSON 解析失败
- **语义重试机制**：AI 响应解析失败时增加语义重试，提高调优成功率
- **红线参数防护**：btc_eth user prompt 显式列出红线参数禁止调整，消除提示词误导
- **tuning_overrides 权限修复**：修复 ai-tuner 写入 tuning_overrides 目录权限不足问题
- **网格策略 AI 调优**：网格策略接入 AI 调优系统，新增 grid_adapter

### 2. MTPCS 策略修复

- **日线指标补传**：补传日线指标修复强趋势市永远无法识别的问题
- **LLM 红线参数**：策略 LLM 建议红线参数，消除提示词误导

### 3. HRS 策略增强

- **V2.6 标准模式趋势过滤**：防止逆势回调陷阱，标准模式增加趋势过滤

### 4. 新增模块

- **ai_tuner/backtest/**：网格回测引擎（market_segment、metrics、models）
- **ai_tuner/reconciler/**：收益对账模块（income_reconciler），用于核对 Binance 收入数据
- **MTPCS 震荡反转三风机制**：v6.26 震荡反转风控三机制技术方案
- **MTPCS 时间平仓复核制**：v6.27 时间平仓复核制技术方案
- **HRS 保护单管理修复**：PRD + 技术设计方案

### 5. Dashboard 看板优化

- 后端 data_service_docker 优化
- 前端样式和图表展示优化
- Docker 管理路由增强

## 项目结构

```
├── strategies/               # 策略模块
│   ├── btc_eth/              # MTPCS 趋势策略（BTC/ETH/BNB/SOL/XRP）
│   │   └── tests/            # 单元测试（信号风险、震荡三风机制、时间平仓复核）
│   ├── grid/                 # 网格交易策略（ETHUSDT）
│   ├── new_coin/             # 新币做空策略
│   ├── hrs/                  # HRS 混合反转策略
│   │   └── tests/            # 单元测试（评分引擎、趋势过滤、动态阈值等）
│   ├── daily_report/         # 日报模块
│   └── weekly_report/        # 周报模块
├── shared/                    # 共享核心模块
│   ├── base_strategy.py     # 策略基类
│   ├── binance_api.py       # Binance API 客户端
│   ├── capital_manager.py   # 资金管理模块
│   ├── condition_orders.py  # 条件单管理
│   ├── config_loader.py     # 统一配置加载器（含 AI 覆盖层合并）
│   ├── database.py          # 数据库服务
│   ├── dynamic_atr_filter.py # 动态ATR过滤器
│   ├── dynamic_trailing.py # 动态追踪止盈
│   ├── indicators.py        # 技术指标
│   ├── kline_service.py     # K线服务客户端
│   ├── market_cap.py        # 市值数据
│   ├── notification.py      # 飞书通知服务
│   ├── strategy_state.py    # 策略状态管理
│   ├── trade_logger.py      # 统一交易记录器
│   └── utils.py             # 工具函数
├── ai_tuner/                  # StratTuneAI 多策略AI调优系统
│   ├── adapters/            # 策略数据适配器（MTPCS、新币做空、网格）
│   ├── allocation/          # 资金分配
│   ├── backtest/            # 网格回测引擎（新增）
│   ├── cleanup/             # 清理模块
│   ├── deploy/              # 配置管理与版本管理
│   ├── engine/              # DeepSeek AI 决策引擎
│   ├── feedback/            # 反馈闭环系统
│   ├── memory/              # 记忆管理与上下文构建
│   ├── monitor/             # 监控模块
│   ├── notifier/            # 飞书审批通知
│   ├── prompts/             # AI Prompt 模板
│   ├── reconciler/          # 收益对账模块（新增）
│   ├── scheduler/           # 定时调度（每周日 23:55）
│   └── tests/               # 测试
├── dashboard/                 # 数据可视化看板
│   ├── backend/             # 后端 API（FastAPI）
│   └── frontend/            # 前端（ECharts 可视化）
├── services/                  # 独立服务
│   ├── kline_service/       # K线数据采集服务（FastAPI）
│   └── kline_monitor/       # K线数据监控服务
├── docs/                      # 项目文档
│   ├── architecture/        # 架构设计
│   ├── design/              # 设计文档（含 v6.26/v6.27 技术方案）
│   ├── deployment/          # 部署文档
│   ├── plans/               # 计划文档
│   ├── reports/             # 报告
│   └── requirements/        # 需求文档
├── scripts/                   # 运维脚本
├── tests/                     # 通用测试
├── database/                  # 数据库初始化脚本
├── docker-compose.yml         # 容器编排
├── one_click_deploy.sh        # 一键部署脚本
├── auto_package.sh            # 自动打包脚本
└── verify_deployment.sh       # 部署验证脚本
```

## 核心特性

- **统一交易记录器 (TradeLogger)**: 自动记录所有策略成交订单到 `trading.trade_records` 表
- **AI 自动调参 (StratTuneAI)**: 每周自动分析策略表现，覆盖层机制生效，人工审批后应用
- **覆盖层配置系统**: `config_loader.py` 自动合并 `tuning_overrides/.active` 覆盖层，不污染原始配置
- **安全兜底机制**: 红线参数防护、每日健康检查、24h 连续亏损/累计亏损自动回滚
- **收益对账 (Income Reconciler)**: 自动核对 Binance 收入数据，确保交易记录准确性
- **网格回测引擎**: 支持网格策略历史回测验证
- **条件单管理**: 统一限价单、止盈止损单管理，孤儿订单自动检测与修复
- **动态ATR过滤器**: 基于历史波动率分布和 ADX 趋势强度动态调整最低 ATR% 阈值
- **Docker 容器化**: 每个策略独立容器运行，互不干扰

## 部署信息

- **生产服务器**：43.156.242.184
- **数据库**：PostgreSQL（trading schema）
- **数据看板**：http://43.156.242.184/
- **容器清单**：trading_system-btc_eth、trading_system-grid、trading_system-new_coin、trading_system-hrs、trading_system-ai-tuner、trading_system-dashboard、trading_system-kline、trading_system-kline-monitor

## 文档索引

- [文档总索引](docs/README.md)
- [系统架构设计](docs/architecture/系统架构设计.md)
- [StratTuneAI 架构设计](docs/architecture/StratTuneAI架构设计.md)
- [网格回测引擎架构设计](docs/architecture/网格回测引擎-架构设计.md)
- [v6.26 MTPCS震荡反转风控三机制](docs/design/v6.26_MTPCS震荡反转风控三机制技术方案.md)
- [v6.27 时间平仓复核制](docs/design/v6.27_时间平仓复核制技术方案.md)
- [HRS保护单管理修复](docs/plans/PRD-HRS保护单管理修复.md)
- [部署指南](docs/deployment/统一交易系统部署指南.md)
- [版本更新日志](CHANGELOG.md)

---

**快照时间**：2026-09-02
**合并目标**：main 主干
