# MTPCS 激进版（BTC/ETH/BNB 激进策略）

> MTPCS（主流币种趋势回调确认策略）激进参数版：**交易代码与原版一致，仅调整策略参数为更激进的取值**，独立部署、独立数据库 schema，并**独立参与** ai-tuner 月度资金分配。

**当前版本**: v2.6.0-aggressive-1（复制自原版 v6.21 配置基线，参数激进化）

---

## 📖 文档导航

| 文档 | 说明 |
|------|------|
| [激进版需求与实现说明](./激进版需求与实现说明.md) | 激进版完整需求、参数差异、隔离机制、部署说明 |

---

## 🎯 激进版定位

在保留原版 MTPCS 策略（strategies/btc_eth/）稳定运行的前提下，复制一套独立的激进参数版（strategies/btc_eth_aggressive/），用于：

1. **更频繁地触发交易信号**：放宽入场门槛（min_score 70、s_min_score 降低、ADX 阈值降低、RSI 极值放宽）
2. **更大的风险敞口**：提高震荡市仓位乘数、放大单笔仓位比例、放宽单币种/全局每日交易次数
3. **更快的止盈节奏**：A 级 tp1 提前至 4.0×ATR、rsi_neutral 追单点位更激进的切换
4. **与生产并行验证**：通过独立 schema 和独立容器，激进版与生产原版互不干扰，可在真实行情下对比两套参数的表现
5. **恢复 v6.25 宽松开仓**（重要）：激进版关闭了开仓侧的 v6.26「震荡反转风控三机制」总开关（`ranging_strategy.entry_filters.enabled: false`），震荡市开仓不再被方向对齐/过热禁开/量价确认/波动熔断四机制短路拦截，仅凭三条件投票放行，避免因参数收紧导致几乎无法开仓
6. **保留 v6.28 总持仓保证金控制**：激进版仍执行 `position_sizing.total.account_ratio_cap`（全部持仓+拟开仓 ≤ 账户权益×比例上限），防止激进开仓导致过度风险敞口
7. **保留时间平仓复核制**：激进版开仓侧三机制关闭后，`_should_keep_position` 时间平仓复核**不受影响**，仍复用三机制判断持仓是否继续持有（防持仓失控）

## 🔒 隔离与共享机制

| 维度 | 激进版实现 |
|------|-----------|
| **订单隔离** | 独立 schema `btc_eth_aggressive`；统一交易记录写入共享表 `trading.trade_records`，以 `strategy` 字段="MTPCS激进策略"区分，**绝不改写原版订单** |
| **持仓归属隔离** | **双策略共享同一币安账户**：补保护单/开仓/对账按**开仓订单归属**（`trade_records` 中未平开仓单最新者）判定，非按币种。边界A（latest 归属）+ 边界B（持仓期互斥，他策略已持有该币未平开仓单→跳过开仓）。收敛于 `shared/position_ownership.py` |
| **资金分配** | 激进版**独立参与**月度资金分配（`capital_allocation.participating_strategies` 含 `btc_eth_aggressive`），按自身月度表现独立排名、独立获得分配额度；已移除 `_SHARED_FUND_MIRROR` 资金镜像 |
| **配置独立** | 独立 `strategies/btc_eth_aggressive/config.yaml` + 独立 `tuning_overrides/` 覆盖层（`.active` 指针管理版本） |
| **AI 调优独立** | 独立 adapter `MTPCSAggressiveAdapter` 独立采集激进版周度表现数据；激进版独立进入月度资金分配池 |
| **部署独立** | docker-compose 独立 service `btc-eth-aggressive-strategy`，容器 `trading_system-btc_eth_aggressive` |

## 🚀 快速部署

```bash
# 打包（含激进版，VERSION 记录激进版 main.py MD5）
./auto_package.sh

# 一键部署（会构建并启动激进版容器）
./one_click_deploy.sh

# 仅远程部署 btc_eth + 激进版
./remote_deploy_btc_eth.sh

# 🎯 MTPCS激进版专属部署（推荐，只部署激进版，不影响原版及其他服务）
./deploy_aggressive.sh
```

> **部署说明**：激进版与原版部署完全解耦。上线激进版**不会停止原版**——激进版使用独立容器（`trading_system-btc_eth_aggressive`）、独立 schema、独立调度，`docker-compose up -d btc-eth-aggressive-strategy` 只操作激进版容器，原版（`btc-eth-strategy`）及 kline-service/postgres 等共享服务不受影响。推荐使用 `./deploy_aggressive.sh` 进行激进版定向部署。

## 📊 调度与通知

| 项 | 激进版 | 原版 |
|----|--------|------|
| 分析调度 | 每天每小时 **15 分**（错峰） | 每天每小时 5 分 |
| 飞书 webhook | 与原版**完全相同**（独立 env `FEISHU_WEBHOOK_BTC_ETH_AGGRESSIVE`，默认指向原版地址） | 独立地址 |

激进版与原版**错峰运行**（原版 xx:05、激进版 xx:15），避免同一时刻并发分析竞争 K 线资源。

## 📁 代码目录

```
strategies/btc_eth_aggressive/
├── main.py              # 策略主入口（身份标识 btc_eth_aggressive）
├── strategy.py          # 策略核心逻辑（与原版一致）
├── market_state.py      # 市场状态识别（与原版一致）
├── config.yaml          # 激进参数配置（v2.6.0-aggressive-1）
├── Dockerfile           # 激进版镜像构建
├── __init__.py
└── tuning_overrides/    # AI 调优覆盖层（.active → V20260910.yaml）
```

相关集成点：
- `ai_tuner/adapters/mtpcs_aggressive_adapter.py` — 激进版数据适配器
- `ai_tuner/config.yaml` — 注册激进版策略条目（strategy_id=btc_eth_aggressive）+ 月度资金分配 `participating_strategies` 含激进版
- `database/postgres/init-scripts/` — `btc_eth_aggressive` schema 与表
- `docker-compose.yml` — `btc-eth-aggressive-strategy` service

## ⚠️ 风险提示

1. 激进版参数放宽了入场门槛与风控尺度，**单笔与整体风险敞口高于原版**
2. 激进版与原版共用同一个币安账户的资金，但**月度资金分配独立计算**：激进版按自身月度表现（已实现 PnL）独立排名、独立获得分配额度，分配结果不再由原版镜像决定
3. 建议激进版先以较低的资金优先级运行，观察一段周期后再评估是否加大投入
4. 共享资金：原版24h内3连亏或2%亏损触发自动回滚时，应同步关注激进版是否受影响
5. **分批止盈调整（2026-09-16）**：激进版与原版 v6.30 一致，分批止盈调整为 TP1 止盈 30% + TP2 止盈 40% + 剩余 30% 尾仓交移动止损，硬止损全仓单保留；对 S/A/B/C 四个信号等级统一生效
6. **ReduceOnly 平仓缺陷修复（2026-09-20）**：激进版与原版 v6.31 一致，修复 `[-4118] ReduceOnly Order Failed`（64 个 XRPUSDT TRAILING_STOP 平仓失败）：下单平仓前先取消交易所全部条件单（新增 `_cancel_symbol_conditional_orders`，-4046 无挂单静默），except 块合并处理 `-2022`/`-4118` 三分支重试，closed 分支止损打标后直接返回，消除 `order_result` 空引用误报
7. **条件单平仓盈亏回写（2026-09-20）**：激进版与原版 v6.32 一致，v6.31 closed 分支仅打标不回写盈亏，本轮新增 `_write_close_pnl` helper（条件单已平仓、`order_result` 为 None 场景）：用 `current_price` 估算 `exit_price`、按 `close_side` 反推方向、数量取 `close_quantity` 参数、`order_id=''` 走模式二降级匹配；止盈类无记录可 UPDATE 时 `insert_pnl_summary` 兜底（止损类由 `log_stop_loss` 兜底）；入口 closed 分支与 except closed 分支均回写盈亏后再止损打标

---

**完整说明**：[激进版需求与实现说明](./激进版需求与实现说明.md)