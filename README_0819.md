# Binance 量化交易系统 - 0819 版本快照

> 本文档为 2026-08-19 项目状态快照，用于记录当前各模块版本及近期变更，便于分支提交与回溯。

## 策略版本概览

| 策略 | 策略ID | 版本 | 状态 | 说明 |
|------|--------|:----:|:----:|------|
| **MTPCS 趋势策略** | btc_eth | v2.6.0 | 🟢 运行中 | BTC/ETH/BNB/SOL/XRP 永续合约，v6.21 BNB参数放宽+差异化冷却+移除TRX |
| **网格交易策略** | grid | v2.4.0 | 🟢 运行中 | ETHUSDT 三层预警架构，解决 ADX 滞后问题 |
| **新币做空策略** | new_coin | v1.1.0 | 🟢 运行中 | V4.1 信号质量优化版，限价单替代市价单 |
| **HRS 混合反转策略** | hrs | v2.5.0 | 🟢 运行中 | 候选池扩容（OR-2 + 50分位）+ LV-RM 独立化 |
| **StratTuneAI 调优系统** | — | v1.0 | 🟢 运行中 | AI 驱动多策略参数自动调优，覆盖 MTPCS + 新币做空 |
| **数据看板 Dashboard** | — | v1.0.0 | 🟢 运行中 | 交易数据可视化，策略分析、趋势图表 |

**部署状态**：✅ 已部署（4 个策略容器 + AI 调优容器 + 数据看板 + K线服务 + K线监控）
**服务器**：43.156.242.184

## 近期主要变更（相对上一版快照）

### 1. StratTuneAI 调优覆盖层机制（6 次提交闭环）

- **覆盖层机制**：实现 `tuning_overrides/.active` 配置覆盖层，AI 调优建议通过覆盖层生效，不修改原始 config.yaml
- **反馈闭环系统**：新增 `ai_tuner/feedback/` 模块，自动采集调优后的策略表现反馈
- **P0 安全网加固**：红线参数防护、统一参数校验、每日健康检查（连续亏损/累计亏损触发回滚）
- **P1 自动审批流程**：实现 auto-apply 闭环，关键词确认后自动应用调优建议
- **策略接入**：HRS 和 new_coin 策略接入 AI 调优覆盖层

### 2. 孤儿条件单累积问题修复

- 补单前取消旧单 + 批量取消二次验证，避免孤儿单累积导致意外成交

### 3. 每日健康检查修复

- 修复连续亏损查询中 missing `executed_at` 列的问题

## 项目结构

```
├── strategies/               # 策略模块
│   ├── btc_eth/              # MTPCS 趋势策略（BTC/ETH/BNB/SOL/XRP）
│   ├── grid/                 # 网格交易策略（ETHUSDT）
│   ├── new_coin/             # 新币做空策略
│   ├── hrs/                  # HRS 混合反转策略
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
│   ├── adapters/            # 策略数据适配器
│   ├── allocation/          # 资金分配
│   ├── cleanup/             # 清理模块
│   ├── deploy/              # 配置管理与版本管理
│   ├── engine/              # DeepSeek AI 决策引擎
│   ├── feedback/            # 反馈闭环系统
│   ├── memory/              # 记忆管理与上下文构建
│   ├── monitor/             # 监控模块
│   ├── notifier/            # 飞书审批通知
│   ├── prompts/             # AI Prompt 模板
│   ├── scheduler/           # 定时调度（每周日 23:55）
│   └── tests/               # 测试
├── dashboard/                 # 数据可视化看板
├── services/                  # 独立服务
│   ├── kline_service/       # K线数据采集服务（FastAPI）
│   └── kline_monitor/       # K线数据监控服务
├── docs/                      # 项目文档
│   ├── architecture/        # 架构设计
│   ├── design/              # 设计文档
│   ├── deployment/          # 部署文档
│   ├── plans/               # 计划文档
│   └── requirements/        # 需求文档（btc_eth/grid/HRS/new_coin/StratTuneAI）
├── scripts/                   # 运维脚本
├── tools/                     # 工具脚本
├── database/                  # 数据库初始化脚本
├── docker-compose.yml         # 容器编排
├── one_click_deploy.sh        # 一键部署脚本
└── verify_deployment.sh       # 部署验证脚本
```

## 核心特性

- **统一交易记录器 (TradeLogger)**: 自动记录所有策略成交订单到 `trading.trade_records` 表
- **AI 自动调参 (StratTuneAI)**: 每周自动分析策略表现，覆盖层机制生效，人工审批后应用
- **覆盖层配置系统**: `config_loader.py` 自动合并 `tuning_overrides/.active` 覆盖层，不污染原始配置
- **安全兜底机制**: 红线参数防护、每日健康检查、24h 连续亏损/累计亏损自动回滚
- **条件单管理**: 统一限价单、止盈止损单管理，孤儿订单自动检测与修复
- **动态ATR过滤器**: 基于历史波动率分布和 ADX 趋势强度动态调整最低 ATR% 阈值
- **Docker 容器化**: 每个策略独立容器运行，互不干扰

## 本次分支提交内容

**分支**：`feature/0819-snapshot`
**基线**：main 分支 `46505c1` 之后
**新增/修改文件**：
- `README_0819.md` — 本文档（新增）
- `one_click_deploy.sh` — 部署脚本调整
- `shared/dynamic_atr_filter.py` — ATR 过滤器优化
- `strategies/btc_eth/strategy.py` — MTPCS 策略调整
- `strategies/btc_eth/tests/` — MTPCS 单元测试（新增）

## 部署信息

- **生产服务器**：43.156.242.184
- **容器清单**：trading_system-btc_eth、trading_system-grid、trading_system-new_coin、trading_system-hrs、trading_system-ai-tuner、trading_system-dashboard、trading_system-kline、trading_system-kline-monitor
- **数据库**：PostgreSQL（trading schema）
- **数据看板**：http://43.156.242.184/

## 文档索引

- [文档总索引](docs/README.md)
- [系统架构设计](docs/architecture/系统架构设计.md)
- [数据库设计](docs/architecture/数据库设计.md)
- [StratTuneAI 架构设计](docs/architecture/StratTuneAI架构设计.md)
- [限价单与孤儿单修复方案](docs/requirements/限价单与孤儿单修复方案.md)
- [部署指南](docs/deployment/统一交易系统部署指南.md)
- [版本更新日志](CHANGELOG.md)

---

**快照时间**：2026-08-19
**对应分支**：`feature/0819-snapshot`
