# Binance_quantitative_trading

统一交易系统 - 整合多个交易策略的模块化平台

## 当前版本

**策略版本**：v6.16.10
**部署状态**：✅ 已部署
**部署时间**：2026-06-23 (UTC+8)
**服务器**：43.156.242.184
**容器状态**：healthy

### v6.16.10 核心改进

#### 1. 强制利润提取（account ATH 追踪）
- 每次巡检检测账户权益，创新高时通过飞书推送 50% 盈利提取建议
- 每周最多推送一次、API 调用频率控制（60 分钟间隔）
- ATH 余额持久化到 `strategy_global_state` 数据库表，重启后恢复

#### 2. 单周亏损 >15% 暂停 3 天
- 从 `daily_pnl` 按 ISO 周号聚合，超阈值时自动暂停 3 天
- 配置：`weekly_loss_max_ratio: 0.15`、`weekly_loss_pause_days: 3`
- 状态持久化到 `frequency_control_state` 表，重启后恢复

#### 3. 经济日历（重大消息前后 1 小时禁止交易）
- 覆盖 2025-2026 年 CPI/FOMC/NFP 共 47 个事件
- 事件前后各 60 分钟禁止交易，避免极端波动
- 配置：`config.yaml` 中 `economic_calendar` 节

#### 4. 代码审查修复
- 修正 `send_error_notification` → `send()` 参数匹配错误
- 添加 API 调用频率控制
- 修复 6 处硬编码

### v6.16.7 回测结果

| 指标 | v6.16.6 | v6.16.7 | 变化 |
|------|:---:|:---:|:---:|
| 总收益率 | +1.30% | **+6.63%** | ✅ +5.33% |
| 总交易次数 | 131 | 99 | -32笔 |
| 胜率 | 67.18% | **67.68%** | ✅ +0.5% |
| 最大回撤 | 2.23% | 3.29% | +1.06% |
| 夏普比率 | +0.10 | **+1.55** | ✅ +1.45 |
| 动态ATR过滤 | 0 | **287** | ✅ 生效 |

### 核心参数配置

- 动态ATR过滤器：启用（35%分位数，绝对下限0.6%）
- ATR%上限：8.5%
- 成交量倍数(S级)：1.4
- 成交量倍数(A级)：1.3
- 冷却期：4小时
- S级额外过滤：启用（ADX>25 或 MACD柱连续放大）
- 资金费率上限：0.08%
- 24h涨跌幅限制：+25%/-20%

## 项目结构

- `shared/` - 共享核心模块（API客户端、数据库服务、K线服务、通知服务、交易记录器、技术指标、动态ATR过滤器等）
- `strategies/` - 策略模块（BTC/ETH、新币做空、网格交易）
- `ai_tuner/` - StratTuneAI 多策略AI调优系统（独立 Docker 容器）
- `dashboard/` - 数据可视化看板（交易数据展示、策略分析、趋势图表）
- `docs/` - 项目文档
- `tests/` - 测试代码
- `database/` - 数据库初始化脚本和备份脚本

## 快速开始

1. 安装依赖：`pip install -r requirements.txt`
2. 配置环境变量：`cp .env.example .env`
3. 运行策略：`python strategies/btc_eth/main.py`

## 核心特性

- **统一交易记录器 (TradeLogger)**: 在 BinanceClient 层面自动记录所有策略的成交订单到 `trading.trade_records` 表，新增策略无需额外代码
- **多项目飞书通知**: 每个策略独立 Webhook，消息发送到专属群
- **动态ATR过滤器**: 根据历史波动率分布和ADX趋势强度动态调整最低ATR%阈值
- **数据可视化看板 (Dashboard)**: 轻量级交易数据可视化看板，展示日报/周报数据、策略分析、趋势图表

## Dashboard 数据看板

### 功能特性

- **数据展示**: 总览数据、策略详情、币种明细、趋势图表
- **实时更新**: 支持日报/周报数据自动更新
- **可视化**: 使用 ECharts 进行金融级可视化
- **易部署**: 载量级架构，易于部署和维护
- **安全可控**: IP 白名单控制，API 限流保护

### 快速启动

```bash
# 启动后端服务
cd dashboard/backend
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# 访问应用
# API 文档: http://localhost:8000/api/docs
# 前端页面: 打开 dashboard/frontend/index.html
```

### 访问地址

- **本地开发**: http://localhost:8000/api/docs
- **生产环境**: http://your-server-ip/ (需配置 Nginx)

详细文档请参考 [dashboard/README.md](dashboard/README.md)

## StratTuneAI 多策略AI调优系统

### 功能特性

StratTuneAI 是一个 AI 驱动的多策略参数自动调优系统，以独立 Docker 容器运行，不侵入现有策略容器。

- **自动化分析**：每周日 23:55 自动采集各策略的周度表现数据，生成标准化健康报告
- **AI 辅助决策**：利用 DeepSeek-v4-pro 大模型分析报告，结合历史调优记忆，生成参数调整建议
- **人工审批在环**：所有 AI 建议通过飞书卡片推送，必须经人工确认后方可生效
- **安全兜底**：自动回滚机制保护策略在极端情况下的安全（24h 连续亏损/累计亏损触发回滚）
- **知识沉淀**：每次调优过程结构化记录到 `trading.strategy_memory` 表，形成可追溯的策略进化日志

### 第一期覆盖策略

| 策略 | 策略ID | 状态 |
|------|--------|------|
| MTPCS趋势策略 | btc_eth | 第一期 |
| 新币做空策略 | new_coin | 第一期 |

### 技术栈

| 模块 | 技术选型 |
|------|----------|
| 定时调度 | APScheduler 3.x（每周日 23:55） |
| AI 引擎 | DeepSeek-v4-pro (deepseek-chat) |
| 数据库 | 复用现有 PostgreSQL（trading schema） |
| 通知 | 复用飞书通知服务 |
| 容器化 | 独立 Docker 容器 |

### 快速启动

```bash
# 构建并启动 StratTuneAI 容器
cd ai_tuner
docker build -t strattune-ai .
docker run -d --name strattune-ai \
  --env-file .env \
  -v $(pwd)/../strategies:/app/strategies:ro \
  strattune-ai

# 手动触发调优（调试用）
docker exec strattune-ai python main.py --trigger
```

详细文档请参考 [StratTuneAI 文档索引](docs/README.md#strattuneai-调优系统)

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