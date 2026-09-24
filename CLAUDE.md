# Binance_quantitative_trading（项目级 CLAUDE.md）

统一交易系统 —— 多策略量化交易平台。在本目录下工作时，除工作区根 `CLAUDE.md` 的规则外，还必须遵守本文件。

## 项目概览

| 策略/系统 | 策略ID | 容器 | 说明 |
|-----------|--------|------|------|
| MTPCS 趋势策略 | btc_eth | `btc-eth-strategy` | BTC/ETH/BNB/SOL/XRP 永续合约趋势+震荡双模式 |
| 网格交易策略 | grid | `grid-strategy` | ETHUSDT 三层预警架构 |
| 新币做空策略 | new_coin | `new-coin-strategy` | 新上线币种做空，按评分分档开仓 |
| HRS 混合反转策略 | hrs | `hrs-strategy` | 三轨并行信号机制（标准+EMM+LV-RM） |
| StratTuneAI 调优 | — | `ai-tuner` | AI 驱动参数调优，人工审批在环 |
| 数据看板 | — | dashboard | 交易数据可视化，http://43.156.242.184/ |
| K线服务 | — | `kline-service` | 独立部署，为所有策略提供 K 线数据 |
| K线监控 | — | `kline-monitor` | K 线数据健康监控（部署后容易遗漏，必须确认） |

**生产服务器**：`43.156.242.184`（root，SSH 密钥 `/Users/yl/vscode/inspection_automation/docs/only.pem`）

## 项目结构

```
├── strategies/           # 策略模块（btc_eth / grid / new_coin / hrs）
├── shared/               # 共享核心模块（所有容器共用，改动必重建全部策略容器）
│   ├── base_strategy.py  # 策略基类
│   ├── binance_api.py    # Binance API 客户端
│   ├── condition_orders.py # 条件单管理
│   ├── database.py       # 数据库服务（PostgreSQL）
│   ├── kline_service.py  # K线服务客户端
│   ├── notification.py   # 飞书通知
│   └── trade_logger.py   # 统一交易记录器（自动记录到 trading.trade_records）
├── ai_tuner/             # StratTuneAI 多策略 AI 调优系统
├── dashboard/            # 数据可视化看板
├── services/             # kline_service / kline_monitor
├── backtest/             # 回测代码（仅本地执行，不部署）
├── database/             # 数据库初始化脚本
├── docs/                 # 项目文档（plans/architecture/deployment/reports/handoffs/...）
├── scripts/              # 运维脚本（含 check_code_sync.sh）
└── tests/                # 测试代码
```

## 环境区分（重要）

| 环境 | 代码位置 | 说明 |
|------|---------|------|
| 回测环境 | `backtest/` | **仅本地执行**，严禁在服务器上跑回测 |
| 生产环境 | `strategies/` | 部署到服务器，真实资金交易 |

修改了 `backtest/` 下的策略逻辑并验证通过后，必须同步到 `strategies/` 生产代码，并执行 `bash scripts/check_code_sync.sh` 确认一致。

## 部署（强制规范）⭐⭐⭐

**完整部署规则见 @.claude/rules/deployment.md，每次部署前必读。** 核心要点：

1. **变更范围分析**：按 `git diff` 对照变更-容器映射表，按需重建（`shared/` 变更 → 重建所有策略容器 + ai-tuner；`.env` 变更 → 仅重启；hrs 的 Dockerfile 是 `COPY . /app/`，任何项目文件变更都要重建 hrs）
2. **防部署幻觉五层验证**：容器状态 → 镜像 ID → VERSION 文件 DEPLOY_ID → 关键文件 MD5（本地 `md5 -q` vs 容器内 `md5sum`）→ 日志无错误。任一失败 = 部署失败
3. **已知坑**：PostgreSQL 容器命名冲突会让 `set -e` 中断部署脚本，导致后面的服务（如 kline-monitor）不会被启动——部署后必须 `docker ps` 确认**所有**服务在运行
4. 一键部署入口：**push main → GitHub Actions 自动构建 + GHCR + SSH 部署**（见 `.trae/rules/deployment.md`），不再手动打包

## 数据库

- 统一 PostgreSQL（容器独立部署于 `/root/database/postgres`），各应用以 Schema 隔离
- 统一部署方案见 `.trae/documents/database_unified_deployment_plan.md`（历史方案文档）

## 上下文交接

完成关键里程碑、上下文偏高或用户要求时，调用 `context-handoff` skill 写交接文件到 `docs/handoffs/`（已 gitignore）。
