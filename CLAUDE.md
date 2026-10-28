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
| MTCPS XAU 影子采集 | — | `mtcps-xau-shadow` | XAUUSDT 影子仿真 + 本地回放 + 小资金 pilot 通道；复用 btc-eth 镜像，只读不交易，仅写 `research` schema |
| MTPCS 黄金校准版（XAU） | — | `mtcps-xau-calibrated-shadow` | 基于 MTCPS 原版、仅校准 ATR；300 USDT 独立虚拟权益，每小时第 20 分运行，只读不交易 |

**生产服务器**：`43.156.242.184`（root，SSH 密钥 `/Users/yl/vscode/inspection_automation/docs/only.pem`）

## 项目结构

```
├── strategies/           # 策略模块（btc_eth / grid / new_coin / hrs）
│   └── btc_eth/shadow/   # XAUUSDT 影子采集器（只读决策落库 research schema）
├── shared/               # 共享核心模块（所有容器共用，改动必重建全部策略容器）
│   ├── base_strategy.py  # 策略基类
│   ├── binance_api.py    # Binance API 客户端
│   ├── condition_orders.py # 条件单管理
│   ├── database.py       # 数据库服务（PostgreSQL）
│   ├── kline_service.py  # K线服务客户端
│   ├── notification.py   # 飞书通知
│   ├── instance_config.py    # 实例参数化（INSTANCE_ID / EXECUTION_MODE / CAPITAL_STRATEGY_ID 等）
│   ├── readonly_client.py    # 影子只读安全边界（写接口 fail-closed）
│   └── trade_logger.py   # 统一交易记录器（自动记录到 trading.trade_records）
├── ai_tuner/             # StratTuneAI 多策略 AI 调优系统
├── dashboard/            # 数据可视化看板
├── services/             # kline_service / kline_monitor
├── backtest/             # 回测代码（仅本地执行，不部署）
│   └── btc_eth/replay/   # XAU 影子确定性回放引擎（本地运行，产出对比报告）
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
2. **防部署幻觉五层验证**：容器状态 → 镜像 ID → VERSION 文件 DEPLOY_ID（CI 构建期用 Run 元数据写入，`DEPLOY_ID=printf '%08X' ((run_number<<8)|run_attempt)` 确定性可反解；第三层**仅对本次实际构建的容器**可用，被 skip 的容器回退下一层）→ 关键文件 MD5（本地 `md5 -q` vs 容器内 `md5sum`）→ 日志无错误。任一失败 = 部署失败
3. **已知坑**：PostgreSQL 容器命名冲突会让 `set -e` 中断部署脚本，导致后面的服务（如 kline-monitor）不会被启动——部署后必须 `docker ps` 确认**所有**服务在运行
4. 一键部署入口：**push main → GitHub Actions 自动构建 + GHCR + SSH 部署**（见 `.trae/rules/deployment.md`），不再手动打包
5. **残留清理口径**：服务器侧只移除不属于本 compose 项目的独立 `docker run` 残留，本项目容器交由 `docker compose up -d` 增量 recreate（避免每次部署全量停机）
6. **影子容器（`mtcps-xau-shadow`）**：复用 `ghcr.io/bearflower/trading-btc-eth:latest` 镜像、仅换 `command` 为 shadow 入口，**不新增镜像、不参与生产交易、不参与资金分配/熔断、不进看板、不写 `trading.*` 与交易记录**，仅写 `research` schema；`docker compose stop mtcps-xau-shadow` 即回退，不影响生产实例。**复用前提（2026-10-20 补记）**：该 btc-eth 镜像现须含影子入口依赖的兄弟包 `strategies/btc_eth_aggressive/`（`strategies/btc_eth/shadow/collector.py` 会 import 其 `BTCEthStrategy`）——`strategies/btc_eth/Dockerfile` 曾漏 COPY 该目录致 2026-10-19 部署后容器 `ModuleNotFoundError` crash-loop，本次已补齐 `COPY strategies/btc_eth_aggressive/` 并加静态守护测试；因 CI 按各 job 自身目录 diff 判定，**仅改 `strategies/btc_eth_aggressive/` 时该镜像不重建（遗留隐患，见 `docs/knowledge/INDEX.md` 待复查清单）**。**profile 配置来源（2026-10-20 补记）**：影子 profile `xau_original.yaml` / `xau_aggressive.yaml` **随镜像烘焙**（`COPY strategies/btc_eth/` 带入 `strategies/btc_eth/shadow/config/`，git 跟踪、运行期只读不写回），**不使用宿主 bind mount**——宿主目录缺失时 Docker 会创建空目录并**遮蔽**镜像内配置，曾致 2026-10-20 「profile 配置文件不存在」crash-loop，故已移除该挂载并加守护测试；回退仍为 `docker compose stop mtcps-xau-shadow`
7. **黄金校准影子容器（`mtcps-xau-calibrated-shadow`）**：展示名固定为“MTPCS 黄金校准版（XAU）”，技术标识保持英文（instance=`mtcps_xau_calibrated_shadow`、profile=`xau_calibrated`、run prefix=`xau-calibrated-shadow`）。它基于 MTCPS 原版且只校准 ATR，用 300 USDT 独立虚拟权益，每小时第 20 分运行；以首个数据质量合格的干净事件为起点独立观察八周。原版/激进版观察期不重置，评估报告按同窗口比较。回退命令为 `docker compose stop mtcps-xau-calibrated-shadow`；当前仅完成部署前代码与冻结证据，**不表示已部署或八周观察已开始**。

## 数据库

- 统一 PostgreSQL（容器独立部署于 `/root/database/postgres`），各应用以 Schema 隔离
- 统一部署方案见 `.trae/documents/database_unified_deployment_plan.md`（历史方案文档）

## 上下文交接

完成关键里程碑、上下文偏高或用户要求时，调用 `context-handoff` skill 写交接文件到 `docs/handoffs/`（已 gitignore）。
