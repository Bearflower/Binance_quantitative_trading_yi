# XAUUSDT MTCPS 影子回放引擎阻断缺陷修复设计

- 文档类型：架构修复设计（design，不写实现代码）
- 涉及模块：`backtest/btc_eth/replay/*`（本地离线回放器，不部署、不连生产 DB）
- 关联计划：[2026-10-19-xau-mtcps-shadow-implementation-plan.md](file:///Users/yl/vscode/Binance_quantitative_trading/docs/superpowers/plans/2026-10-19-xau-mtcps-shadow-implementation-plan.md)
- 关联设计：[2026-10-18-xau-mtcps-shadow-pilot-design.md](file:///Users/yl/vscode/Binance_quantitative_trading/docs/superpowers/specs/2026-10-18-xau-mtcps-shadow-pilot-design.md)
- 负责缺陷：阻断项 #3、#4、#5，耦合项 #4b、#4c
- 边界：#1 / #2 / #6a 由另一批工程师并发修复，不在本文档范围。
- 增量回写：§13（第三轮复审打回后修复轮，2026-10-20）——连续亏损按整笔 `position_net_pnl` 判定、顶层快照哈希 shared helper + fail-closed 重算、虚拟订单统计口径。

---

## 0. 结论速览（供汇报与工程师快速对齐）

1. **#3 trailing 分档判定**：生产 `profit_ceiling` 是对「历史最高浮盈（峰值浮盈）」分档，不是对「当前浮盈」分档（证据 [strategy.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L4543-L4626)）。回放引擎必须维护`highest_price/lowest_price`（历史峰值），以峰值浮盈 `unrealized_pnl_pct` 选 `retrace_ratio`，再以峰值价回推 `stop_price`。
2. **#4 未实现盈亏与盯市**：`marked_equity = equity + unrealized_pnl`；`unrealized_pnl = (mark − entry) × qty`（LONG），`mark` 取当前 1m K 线的 `close`（成交价口径，与 §8.2「不引入标记价、撮合只用成交价」一致）。逐持仓分钟盯市，最大回撤改用盯市曲线。
3. **#5 聚合 trade 净 pnl**：一个仓位生命周期 = 一笔 trade；`net_pnl(pos) = Σ_i (realized_pnl_i + fee_i + funding_i + slippage_i)`，各批次已在 [_execute_exit](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L730-L734) 算好，聚合只求和。PF / 胜率 / 连续亏损 / 扣除最佳单笔全部改净 pnl + 仓位级。

（fact 查证结论详见 §1，公式详见 §2–§4。）

---

## 1. 事实查证（文件路径 + 行号 + 结论）

### 1.1 生产 `dynamic_trailing` 真实语义

生产策略文件 [strategies/btc_eth/strategy.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py)：

| 项 | 位置 | 结论 |
|----|------|------|
| 触发入口 | `_check_dynamic_trailing` L4463-4503 | 每 tick 调 `_calculate_dynamic_trailing_stop` 得 `trailing_stop`，`current_price <= trailing_stop`（LONG）或 `>=`（SHORT）→ `_handle_trailing_trigger` 平仓 |
| 参数读取 | L4530-4539 | 从 `risk.signal_levels.{grade}.dynamic_trailing` 读 `enabled` / `activation` / `regression_tiers`（**不是**扁平键） |
| 峰值浮盈 | L4541-4569 | `reference_price = position.highest_price`（LONG，>entry 时才用，否则 current_price）或 `position.lowest_price`（SHORT）；`unrealized_pnl_pct = (reference − entry) / entry × 100`，负值 clamp 到 0。**这是「历史最高浮盈」** |
| 激活判定 | L4571-4581 | `profit_activated = (unrealized_pnl_pct >= activation.min_profit_pct)`；`tp1_activated = activation.also_on_tp1 && position.tp1_hit`；一旦 `trailing_activated=True` 保持，浮盈回落不退出激活 |
| 分档（核心） | L4592-4626 | `profit_ceiling` **对峰值浮盈分档**（非当前浮盈）：`unrealized_pnl_pct < tier.profit_ceiling` 选该档 `retrace_ratio`。保本模式：`unrealized_pnl_pct < tiers[0].profit_ceiling` 且未 `tp1_hit` → `stop_price = entry_price` |
| 允许回撤 | L4618-4626 | `allowed_retrace = (reference − entry) × retrace_ratio × vol_adj`（LONG）；`stop_price = reference − allowed_retrace`（LONG）/ `+`（SHORT） |
| 硬止损兜底 | L4628-4639 | `hard_stop = entry ∓ atr × stop_loss_atr_multiplier`；`final_stop = max(stop, hard_stop)`（LONG）/ min（SHORT） |
| 单向移动保护 | L4641-4648 | `trailing_stop_price` 只能朝有利方向移动（LONG 只升、SHORT 只降） |
| 触发平仓量 | `_handle_trailing_trigger` L4419-4458 | 平仓量 = `initial_quantity × partial_take_profit.remaining_ratio`（尾仓），非全平；随后 `_rebuild_remaining_protection` 补挂硬止损 + TP2 |
| 波动率调节 | `_get_volatility_adjustment` L4652-4750 | `vol_adj = current_atr_pct / base_atr_pct_median`，clamp [0.5, 2.0]；`base_atr_pct` 需要 `atr_lookback_days(30) + atr_period(14)` 约 44 天**日线** ATR% 中位数；数据不足 → fallback `1.0`（L4694-4695 / L4724） |
| 峰值维护 | L3561-3565 | `highest_price` / `lowest_price` 由每 tick `current_price` 更新 |

**结论：`profit_ceiling` 对「历史最高浮盈」分档（非当前浮盈）。**

**可确定性回放性判定**：
- 峰值浮盈 → **可确定性近似**：用 1m K 线累积极值（LONG 用 `k_high`、SHORT 用 `k_low`）替代实盘 tick 的 `highest_price/lowest_price`。属保守近似（K 线 highest 是区间上界，可能高于逐 tick 观察值，使 trailing_stop 略高、触发略早）。
- 触发判定（`current_price <= stop`）→ 回放单根 1m 内无线序，只能用 `k_low`（LONG）/ `k_high`（SHORT）判定（保守）。
- `vol_adj` → **无法确定性重算**：需要约 44 天日线 ATR，回放快照仅 30 天 1m（见 §2.6 降级口径）。

### 1.2 生产 `trade_records` 分批止盈口径（决定 #5 是否与生产一致）

生产 [shared/trade_logger.py](file:///Users/yl/vscode/Binance_quantitative_trading/shared/trade_logger.py)：

- `calculate_pnl` L689-713：`LONG → (exit−entry)×qty`，`SHORT → (entry−exit)×qty`。
- `log_order` L200-303：开仓 INSERT 一条（order 级）。
- `update_realized_pnl` L315-459：平仓动作回写 `realized_pnl`（每个平仓 order 各回一次）。
- `insert_pnl_summary` L461-552：条件单全平时 INSERT `PNL_SUMMARY` 汇总行；`since=entry_time` 幂等去重。
- `log_stop_loss` L554+：止损打标。

策略层 [strategy.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L4093-L4127) 与 [_write_close_pnl](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L4194-L4244)：

**结论：生产一个仓位分批止盈（TP1 / TP2 / 尾仓）记多条 trade_records**——每次 `_close_position`（TP1 一部分、TP2 一部分、trailing 尾仓、止损/时间止损）各自 `update_realized_pnl` 或 `insert_pnl_summary` 一条。即生产 DB 是「平仓动作级」记账流水。

**对 #5 的含义**：报告评估口径（胜率 / PF / 单笔期望 / 最大连续亏损 / 扣除最佳单笔 / `min_closed_trades≥12`）**是仓位生命周期维度**，与生产 DB 的「动作级流水」是两层不同语义。设计采用：底层保留逐条退出明细（对生产 DB 记账形态），报告层聚合为「仓位级 trade」后再算指标。详见 §4。

### 1.3 `scripts/check_code_sync.sh` 是否约束 replay

[check_code_sync.sh](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/check_code_sync.sh#L22-L23) 的 `BACKTEST_DIR="$PROJECT_ROOT/backtest/btc_eth/scripts"`，比较对象是 `backtest_v*.py` 版本号与 `strategies/btc_eth` 参数。**它不覆盖 `backtest/btc_eth/replay/`**。结论：replay 引擎不受 `check_code_sync.sh` 强制同步约束；`CLAUDE.md` 的「backtest 验证后同步 strategies」针对旧 `backtest/btc_eth/scripts` 重演式回测，不针对影子事件回放器。本设计不改 `strategies/`，不触发生产同步要求。

### 1.4 `replay/config.yaml` 结构与事实

[config.yaml](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/config.yaml)：
- `entry.limit_order_expiry_seconds: 300`（L7）
- `fees.taker_bps: 5`（L14）（**2026-10-19 修订**：原列出的 `fees.maker_bps` 为死配置键——回放主口径为 taker，无任何读者；已从 `config.yaml` 删除，不再描述）
- `slippage.baseline_bps: 2`（L20），`slippage.tiers_bps: [1, 2, 4]`（L21），`slippage.limit_entry_no_extra_slippage: true`（L23）
- `tradfi_active_hours_utc: [7, 20]`（L34）（**2026-10-19 修订**：原列出的顶层 `pilot.*` 为死配置键——pilot 资金约束实际取自 profile 冻结配置，回放器不读顶层 `pilot`；已从 `config.yaml` 删除，不再描述。`entry.market_order_type` 同属死配置键，本篇未列出、亦不再描述）

- `admission.min_closed_trades: 12`、`min_profit_factor: 1.20`、`max_drawdown_pct: 15`、`capital_preservation_pct: 30`、`slippage_pressure_multiplier: 2`（L37-42）

### 1.5 engine 关键行事实（#3/#4/#5 落点）

| 位置 | 事实 |
|------|------|
| [_check_and_close_on_bar](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L501-L574) | 退出顺序：止损(1) L524-532 → TP1(2) L535 → TP2(3) L538 → trailing(4) L542-558 → time_stop(5) L560-572 |
| trailing 现状 L543-558 | 读扁平键 `trailing_cfg.get("activation_min_profit_pct","0")` / `get("retrace_ratio","0.5")`，恒默认；只比较单根 `k_high/k_close`（L548-552），无历史峰值 |
| [_attempt_entry](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L824) | `trailing_cfg = level_cfg.get("dynamic_trailing", {})` 已是嵌套结构（含 `activation/regression_tiers/volatility_adjustment`），经 `_fill_entry` → `on_entry_approved` 注入 `pos["trailing"]` |
| [_execute_exit](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L691-L778) | L731 `pnl=calculate_pnl`；L718 出场费、L719-720 入场费按 `entry_fee_per_qty` 分摊；L728 funding；L734 `net_pnl = pnl + fee + funding + slip` |
| [_partial_close](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L639-L689) | TP1 后置 `pos["tp1"]=None`（L684-685）防重复；**未置 `tp1_hit` 标记** |
| [_run_position_exit_up_to](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L472-L499) | 逐根推进到 `up_to`，每根调 `_check_and_close_on_bar` |
| parse symbol L127 | `parse_symbol_filters(ei["symbols"][0])` 直接取第一个（#4b） |
| [state.py equity L28 / snapshot L55-60 / on_position_closed L184-215](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/state.py) | `equity` 仅平仓/部分平仓时更新，未逐分钟盯市 |
| [report.py closed L43 / PF L61-68 / worst L80](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/report.py) | `closed_trades` 每条退出算一笔；PF 用毛 `realized_pnl` |
| [counterfactual.py scan_triggers L41-66](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/counterfactual.py) | `open_position` 恒 `None`（L44-45、L58-62） |

---

## 2. 缺陷 #3：移动止损（trailing）与冻结配置对齐 + 历史最高浮盈

### 2.1 目标

让回放引擎忠实复刻生产 `dynamic_trailing` 的「峰值浮盈 + 分档回撤 + 单向移动 + 硬止损兜底」语义，并保持确定性。

### 2.2 位置状态扩展（`position` 字典）

在 [state.on_entry_approved](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/state.py#L140-L182) 写入的 `open_position` 字典新增字段（默认值保证既有测试不破）：

```
highest_price          # LONG 历史最高，初始 = fill_price
lowest_price           # SHORT 历史最低，初始 = fill_price
trailing_activated     # bool，初始 False
trailing_stop_price    # Optional[Decimal]，初始 None
pending_profit_pct     # Optional[Decimal]，上次峰值浮盈 %
tp1_hit                # bool，初始 False；TP1 部分平仓后置 True（用于 also_on_tp1）
```

约束：所有新字段由 `_fill_entry` / `_attempt_entry` 计算后经 `on_entry_approved` 传入（沿用「缺陷 1 修复」的注入模式），`state.py` 不自行读配置。

### 2.3 峰值浮盈与分档公式（精确）

设 `entry` = 入场价，`ref` = 历史峰值价、`qty` 已不重要（百分比口径）。每根 1m K 线先刷新峰值：

- LONG：`highest_price = max(highest_price, k_high)`
- SHORT：`lowest_price = min(lowest_price, k_low)`

峰值浮盈 `pct`（与 [strategy.py L4543-4567](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L4543-L4567) 一致）：

```
LONG:  ref = highest_price if highest_price > entry else current_ref
       pct = (ref - entry) / entry * 100
SHORT: ref = lowest_price  if (lowest_price > 0 and lowest_price < entry) else current_ref
       pct = (entry - ref) / entry * 100
pct = max(pct, 0)
```

激活（含单向保持）：

```
profit_activated = pct >= activation.min_profit_pct
tp1_activated    = activation.also_on_tp1 and tp1_hit
if (profit_activated or tp1_activated): trailing_activated = True
if not trailing_activated: return（未激活 → 不产生 trailing_stop）
```

分档（**对峰值浮盈 `pct` 分档**）：

```
tiers = regression_tiers（升序按 profit_ceiling）
if pct < tiers[0].profit_ceiling and not tp1_hit:
    stop_price = entry_price   # 保本模式，tier_index = 0
else:
    tier_index = first i where pct < tiers[i].profit_ceiling（无则 len(tiers)-1）
    retrace_ratio = tiers[tier_index].retrace_ratio
    LONG:  profit_per_unit = ref - entry
           allowed_retrace = profit_per_unit * retrace_ratio * vol_adj
           stop_price = ref - allowed_retrace
    SHORT: profit_per_unit = entry - ref
           allowed_retrace = profit_per_unit * retrace_ratio * vol_adj
           stop_price = ref + allowed_retrace
```

硬止损兜底 + 单向移动 + 触发（与 [strategy.py L4628-4646 / L4485-4489](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L4628-L4646) 一致）：

```
hard_stop  = entry - atr * stop_loss_atr_multiplier   (LONG)
           = entry + atr * stop_loss_atr_multiplier   (SHORT)
final_stop = max(stop_price, hard_stop)   (LONG)   /   min(...)  (SHORT)
# 单向移动：
final_stop = max(final_stop, trailing_stop_price) if LONG and trailing_stop_price 已存在
           = min(final_stop, trailing_stop_price) if SHORT ...
trailing_stop_price = final_stop

# 触发（回放保守口径，用 K 线极值，无逐 tick 线序）：
LONG:  if k_low <= final_stop  → TRAILING_STOP
SHORT: if k_high >= final_stop → TRAILING_STOP
```

`atr` 复用在 `_attempt_entry` 已提取的 `atr`（缺陷 #2 路径），写入 `pos["atr"] = atr`，供 trailing 兜底与硬止损计算。`stop_loss_atr_multiplier` 从 `level_cfg` 读（现有 [L818](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L818)）。

### 2.4 触发平仓量：全平 vs 平尾仓（本次必须确认的语义差异）

- 生产：`_handle_trailing_trigger` 平 `initial_quantity × remaining_ratio`（尾仓），剩余仓位继续受硬止损 + TP2 保护。
- 现状 engine：[L557](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L557) 是 `_close_position` **全平**。

**设计建议（推荐）**：改为 `_partial_close(profile_id, at, k_close, "TRAILING_STOP", close_qty=剩余量×remaining_ratio)`，置 `pos["trailing_stop_price"]=None`（避免重复触发），剩余仓位继续跑硬止损 / TP2 / 时间止损。`remaining_ratio` 从 `level_cfg.partial_take_profit.remaining_ratio` 读（冻结 profile S 档 [xau_original.yaml L118](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/shadow/config/xau_original.yaml#L118)）。

> 理由：全平会系统性低估 trailing 后的剩余利润、系统性高估回撤/亏损，污染准入；且与 #5 聚合口径（一个仓位多批次退出）天然一致。此改动**不触碰 `strategies/` 生产代码**，只改回放器行为。

**备选（最小修复）**：本期保持全平，但在报告元数据注明 `trailing_flat_close=true`（已知简化偏差）。若采纳此条，需在 §8 待确认记录。

### 2.5 vol_adj 降级（保守优先，可确定性）

快照仅 30 天 1m，无法提供生产所需的约 44 天日线 ATR 中位数 → **无法确定性重算 `vol_adj`**。

降级口径：

```
vol_adj = Decimal("1.0")   # 与生产数据不足 fallback（strategy.py L4694-4695 / L4724）一致，无偏
```

- 论证：`vol_adj` 越大允许回撤越多、止损越松（越激进）。生产无数据时取 `1.0`（中性），回放取 `1.0` 是唯一「无偏、可确定性、与生产 fallback 一致」的取值；取 `0.5` 会造成系统性过早平仓（偏离生产），取 `>1.0` 会系统性过松（非保守）。故取 `1.0`，并把「vol_adj 未重算」写入报告元数据与报告字段。
- 配置化（禁止硬编码）：新增 `replay/config.yaml` 键：
  ```
  trailing:
    volatility_adjustment_override: 1.0   # 1.0=未重算的中性降级；仅审计/未来扩展用
  ```
  引擎读 `cfg["trailing"]["volatility_adjustment_override"]`，未来若扩展快照到 ≥44 天日线可改为动态计算，**本期固定中性，不改生产**。

### 2.6 读取路径

`regression_tiers` / `activation` / `volatility_adjustment` 全部从 `pos["trailing"]`（= `level_cfg.dynamic_trailing`）读，与冻结 profile 嵌套结构一致（`signal_levels.{grade}.dynamic_trailing.*`）。**不再**读扁平键 `activation_min_profit_pct` / `retrace_ratio`。

> 附带发现（不改生产）：生产 [_get_volatility_adjustment L4673](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L4673) 读的 `self.risk_config['dynamic_trailing']['volatility_adjustment']` 与冻结 profile 实际嵌在 `signal_levels.{grade}.dynamic_trailing.volatility_adjustment` 的路径不一致，可能恒走 fallback `1.0`。这进一步佐证「vol_adj 取 1.0」与生产实际运行行为对齐。属生产潜在一致性课题，记录于 §8，不在本次改。

---

## 3. 缺陷 #4：逐分钟盯市 + 最大回撤 + 反事实真实 open_position

### 3.1 未实现盈亏公式与标记价

```
LONG:  unrealized_pnl = (mark - entry) × qty
SHORT: unrealized_pnl = (entry - mark) × qty
```

`mark` = **当前 1m K 线的 `close`**。依据：§8.2 明确「撮合只用成交价，不引入标记价（markPrice）」，未实现盈亏盯市口径与撮合统一取成交价 `close`（收盘价作为该根的无偏盯市价，保守且确定）。

`marked_equity = equity + unrealized_pnl`，其中 `equity` 为「已实现权益」（含已平批次净 pnl 与费用/资金费/滑点）。

### 3.2 盯市快照频率与数据量控制

- **主口径：仅持仓期间逐根盯市**，空仓期沿用既有快照时机（平仓 / 门槛拒绝 / 期终 `snapshot_equity`）。空仓时 `unrealized=0`，`marked_equity == equity` 恒定，逐根无用。
- 载体：`VirtualState.equity_history` 元素从二元组升级为**三元组 `(datetime, marked_equity, has_open_position)`**。新增方法 `snapshot_marked_equity(at, marked_equity, has_open_position)`（内部去重逻辑沿用 [snapshot_equity L55-60](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/state.py#L55-L60)）。
- 引擎接入点：在 [_run_position_exit_up_to](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L472-L499) 的 `for` 循环内，对每根 K 线：**先**按优先级判定退出（§5.1），若未平仓则 `mark = k["close"]` 计算 `marked_equity` 并 `snapshot_marked_equity(k_open_time, marked_equity, True)`。入场/平仓/拒绝/期终等既有 snapshot 点，`has_open_position` 按当刻真实值填。
- 数据量评估：
  - 全量上限 = 30 天 × 1440 根 × 2 profile = 86 400 条三元组。
  - 实际（仅持仓期）= 持仓分钟数 + 事件数，通常远小于上限；单条约 60 字节，即使全量也约 5–8 MB，可控。
  - 不设默认截断；可选配置键 `mark_to_market.max_history_rows`（默认 0=不截断）留作超长窗口护栏（不启用时无影响）。

### 3.3 最大回撤算法

复用现有 [_compute_max_drawdown](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/report.py#L162-L177) 的逐点 `peak-drawdown` 算法（公式正确，无需改），**仅把输入从旧 `equity_history`（已实现）换为盯市曲线**（三元组取第二字段 `marked_equity`）。这使「持仓期一度亏 30% 后回本」能被记录，`max_drawdown_pct` 与 15% 准入门槛、counterfactual 15/25/30 触发时点均基于同一盯市曲线。

### 3.4 counterfactual 三元组真实 open_position

[counterfactual.scan_triggers](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/counterfactual.py#L41-L66) 签名改为：

```
scan_triggers(equity_history: List[Tuple[datetime, Decimal, bool]], initial_equity) -> CounterfactualTriggers
```

- 三元组第三字段 = 该 snapshot 时点是否有持仓。
- `trigger_* = (at, eq, has_open_position)`；`as_dict()` 的 `open_position: bool(pos)` 自动呈现真实值。
- engine [run() L309](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L309) 调用传入三元组序列。

---

## 4. 缺陷 #5：仓位生命周期聚合 trade + 净 pnl 报告口径

### 4.1 数据结构（最小侵入：明细保留 + 报告层聚合）

- **引擎侧**：每个持仓分配确定性 `position_id`。建议 `position_id = f"{profile_id}@{entry_open_time_iso}@{direction}@{open_seq}"`（`open_seq` 为该 profile 第 N 次开仓，确定性、稳定）。写入 `pos["position_id"]`；`_execute_exit` / `_fill_entry` / `_record_unfilled` / 门槛拒绝记录统一携带 `position_id`（未成交/拒绝置 `position_id=None`）。
- `replay_trades` 保持「逐条明细」不变（对生产 DB 动作级记账形态），新增每条的 `position_id` 字段。
- **报告侧聚合**：`report.py` 新增纯函数 `_aggregate_positions(trades) -> List[Dict]`，按 `position_id` 把「filled 且 exit_reason 非空」的明细聚合成仓位级列表 `closed_trades`；`position_id is None` 的 unfilled/rejected 明细不进入聚合（作为 `n_unfilled` 与去重背景保留）。

### 4.2 聚合 trade 字段与净 pnl 公式

```
aggregated_trade = {
  position_id, symbol, direction,
  entry_time, entry_price,
  exit_time,                 # 最后一个退出批次的 at
  exit_reason,               # 终结原因 = 最后一次 exit 的 reason
  exit_path,                 # 有序退出原因列表，如 ["TP1","TP2","TRAILING_STOP"]
  total_quantity,            # 入场数量
  margin, leverage,
  realized_pnl = Σ_i realized_pnl_i,
  fee          = Σ_i fee_i,            # 负值，含入场分摊 + 各退出
  funding_fee  = Σ_i funding_i,
  slippage     = Σ_i slip_i,
  net_pnl      = Σ_i net_pnl_i,
  max_mfe, max_mae,          # 生命周期内极值（配合盯市逐根记录）
  holding_seconds,           # entry → 最后 exit
  exits = [ ...每批次明细... ],
}
```

**聚合净 pnl 公式（精确，各子项均 = 批内计算值，勿重算）**：

```
net_pnl(pos) = Σ over exits (realized_pnl_i + fee_i + funding_i + slippage_i)
             = Σ over exits net_pnl_i          # engine _execute_exit L734 已算
```

其中 `fee_i`、`slippage_i` 为负值，`funding_i` 正负皆可（值与 [engine L718-L728](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L718-L728) 一致）。

### 4.3 report.py 指标改造（全部净 pnl + 仓位级）

| 现状（report.py） | 改为 |
|---|---|
| `closed_trades = [...]` L43 | 聚合后的仓位级列表（`_aggregate_positions`） |
| `n_closed_trades` L44 | 聚合仓位数 |
| `wins/losses/total_win/total_loss` L61-64 用 `realized_pnl` | 全部改用聚合 trade 的 `net_pnl` |
| `profit_factor` L68 | `total_win / total_loss`（净 pnl 口径） |
| `avg_win/avg_loss/expectancy` L66-70 | 净 pnl 口径 |
| `_compute_max_consecutive_loss` L180-191 用 `realized_pnl` | 聚合 net_pnl（**状态机层同口径详见 §13.1**：连亏判定亦改按整笔 `position_net_pnl`，不再按单批 `realized_pnl`） |
| `worst_loss` L80 用 `net_pnl` | 聚合 net_pnl（语义不变，粒度变） |
| `exit_breakdown` L92-96 按每条 exit | 按聚合 `exit_reason`；另增 `partial_exit_breakdown`（TP1/TP2 分别，来自 `exit_path`） |
| `long/short/active/inactive` L99-115 | 基于聚合 trade 的 `exit_time` |
| `_admission_checks` ⑥ L247-260 | 在聚合 net_pnl 上「扣除最佳单笔」 |
| ① `min_closed_trades` L216-219 | `n = len(聚合仓位)` |

其中「净盈亏 > 0」「② net_profit_positive」现有 `net_pnl = realized_pnl - fee_paid - slippage_cost + funding_fee`（L53）是 state 级汇总，保持不动。

MAE / MFE：改为聚合 trade 的生命周期极值（引擎在盯市/退出路径内维护 `pos["max_mfe"/"max_mae"]`，聚合时取之），替代「取 average」的现有 L86-90 近似。

---

## 5. 保守时序原则（统一约束）

同一根 1m K 线内，退出判定优先级**保持现有顺序不变**（[engine L524-L572](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L524-L572)）：

```
1. STOP_LOSS（止损）
2. TP1
3. TP2
4. TRAILING_STOP（本次接入真实止损价后在此档位）
5. TIME_STOP
```

- 高/低价无法判序（一根 K 线同时触及止损与止盈）→ **沿用「止损优先」**（保守：优先记亏损路径）。
- trailing 触发判定用 `k_low`（LONG）/ `k_high`（SHORT）：保守（区间内若触及即视为触发，不推断 tick 顺序）。
- 盯市在「退出判定之后、未平仓之时」发生，保证盯市曲线与退出路径严格一致；平仓当刻先更新 `equity` 再以平仓后 `marked_equity(=equity)` 快照。

---

## 6. 耦合项 #4b：snapshot 符号未校验

- 位置：[snapshot.py L99-101](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/snapshot.py#L99-L101) `fetch_exchange_info` 请求 `/fapi/v1/exchangeInfo?symbol=...`；[engine.py L127](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L127) 直接 `parse_symbol_filters(ei["symbols"][0])`。
- 设计：
  1. engine 新增纯函数 `_find_symbol_entry(exchange_info, manifest_symbol)`：遍历 `exchange_info["symbols"]`，命中 `s["symbol"] == manifest_symbol` 返回该 `s`；未命中 → 抛 `ValueError`（**fail-closed**，engine 构造失败、main 返回非零，不得静默用别的合约 filter）。
  2. 命中后校验 `tickSize/stepSize/minNotional` 存在且 `> 0`；否则 fail-closed 报错。
  3. `parse_symbol_filters`（[matching.py L97](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/matching.py#L97)）签名**不变**（仍接收单个 symbol 条目）。
  4. `self.tick_size / self.step_size / self.min_notional` 赋值不变（仍来自 `symbol_filters`），仅来源由 `symbols[0]` 改为「按 manifest symbol 命中项」。
- `manifest["symbol"]` 即 [snapshot.py L164](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/snapshot.py#L164) 写入的 symbol，两端对齐。

---

## 7. 耦合项 #4c：报告缺计划要求指标

新增字段及数据来源（全部落在 `report.py compute_metrics` 与 `write_reports`，不另造报告体系）：

### 7.1 采集期统计（来源：决策事件文件 / run manifest / engine 事件序列）

engine `run()` 返回值 `profile_result` 增加 `event_stats`（引擎在 `load_events`/`_process_one_event` 时按 `profile_id` 统计）：

```
event_stats = {
  "n_scheduled":          事件总数（每调度周期每 profile ≥1 条）,
  "n_candidates":         decision=="candidate" 计数,
  "n_no_signal":          decision=="no_signal" 计数,
  "n_analysis_error":     decision=="analysis_error" 计数,
  "no_signal_reason_breakdown": {reason_code: count},   # 来自 event.reason_codes
}
```

report 派生 `analysis_success_rate = (n_candidates + n_no_signal) / n_scheduled`（或 1 − n_analysis_error/n_scheduled 口径，按 spec §7.1「分析成功率」取 `1 - analysis_error/总`，在 §8 待确认精确分母）。

### 7.2 回放期门槛拒绝分布（来源：引擎有状态门槛）

引擎在 `_attempt_entry` gate 拒绝分支（[L873-889](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L873-L889)）已写 `unfilled_reason="rejected:[...]"`。设计：除保留该明细外，额外累计结构化计数：

```
gate_rejection_breakdown = {
  "position_limit", "exceeds_max_single_margin", "insufficient_margin",
  "daily_trade_limit", "symbol_cooldown", "consecutive_loss_pause",
}.counts
```

来源：`gate['rejection_reasons']`（[state.check_entry_eligibility L132-136](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/state.py#L132-L136)）。写入 `profile_result["gate_rejection_breakdown"]`，report 透传。

### 7.3 三档滑点压力（来源：report 层 + config）

```
slippage_pressure = {
  tier_bps=1: pressure_test_final_equity(final_equity, total_notional, 1 - baseline_bps),
  tier_bps=2: ...,
  tier_bps=4: ...,
}
```

- 复用 [counterfactual.pressure_test_final_equity](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/counterfactual.py#L69-L79)，档位读 `cfg["slippage"]["tiers_bps"]`（[config.yaml L21](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/config.yaml#L21)）。
- 计量口径：每档「新增 bps」= `tier_bps − baseline_bps`，正数时额外扣减、负数/零档位按 0 处理（与 §7.2 ⑦ 的 `added_bps` 口径一致）。确切公式在 §8 待确认（三档是「叠加 baseline 之上」还是「独立档位」——config L21 注释已标注此悬而未决点）。

### 7.4 原版/激进版同点决策差异（来源：事件决策序列）

engine 结果顶层新增 `decision_divergence`（报告层对齐 `scheduled_at` 逐点统计两 profile 的 `decision`）：

```
decision_divergence = {
  "aligned_points": n,
  "divergent_points": n,
  "pairs": {"candidate|candidate": n, "candidate|no_signal": n, ...},
}
```

来源：`event_stats` 中按 `scheduled_at` 对齐的 `decision` 序列。采用 `profile_id` 字典序（`xau_original` 在前）构造 pair key。写入 `write_reports` 的 JSON 顶层（跨 profile 对比，spec §7.3）。

### 7.5 data_completeness 与 admission 契约（已知契约，本设计对齐）

- `write_reports` 需获得缺口信息。设计：engine `engine_metadata` 新增 `snapshot_gaps: int`（来自 `self.snapshot_manifest["gaps"]` 长度，[snapshot.py L161](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/snapshot.py#L161)）；`write_reports` 读 `engine_metadata["snapshot_gaps"]` 产出顶层：
  ```
  data_completeness: {"gaps": int, "complete": bool}   # complete = (gaps == 0)
  ```
- `decide_admission` / `write_reports`：`complete == false` 时，`admission.admission` 必须为字符串 `"insufficient_data"`（不得为 `"original"/"aggressive"/"none"`）。此为已知契约，另一批工程师负责的主口径不变，本设计仅指定 engine 侧 `snapshot_gaps` 的上游来源与字段名。

---

## 8. 会变化的 report JSON 字段与受影响测试

### 8.1 report JSON 字段变化清单

顶层新增：

```
data_completeness: {"gaps": int, "complete": bool}
decision_divergence: {...}
```

`admission.admission` 新增可取值 `"insufficient_data"`（缺口时）。

per-profile（`xau_original` / `xau_aggressive`）新增：

```
analysis_success_rate, n_scheduled(n 进 event_stats), gate_rejection_breakdown,
slippage_pressure, partial_exit_breakdown
（event_stats 整体新增）
```

per-profile 语义变更（键保留、口径变）：

```
n_closed_trades          # 仓位级
win_rate / avg_win / avg_loss / profit_factor / expectancy   # 净 pnl + 仓位级
max_drawdown_pct         # 盯市曲线
max_consecutive_loss / worst_loss                            # 净 pnl + 仓位级
exit_breakdown           # 仓位级终结原因（另增 partial_exit_breakdown 保留 TP1/TP2）
long_trades / short_trades / active_trades / inactive_trades # 仓位级
counterfactual_15pct/25pct/30pct.open_position               # 真实 bool（不再恒 False）
avg_mae / avg_mfe        # 由「均值」→「生命周期极值」逐项呈现（可保留 avg 键，值由极值计算）
```

引擎结果（`engine_result`）新增：`profile_result["event_stats"]`、`profile_result["gate_rejection_breakdown"]`、`engine_metadata["snapshot_gaps"]`。

### 8.2 受影响既有测试断言（`tests/test_shadow_replay/`）

| 文件 | 受影响断言 | 原因 |
|------|-----------|------|
| [test_report.py](file:///Users/yl/vscode/Binance_quantitative_trading/tests/test_shadow_replay/test_report.py) | `test_sum_net_pnl_equals_total`、`test_active_plus_inactive_equals_all`、`test_less_than_12_trades_admission_none` 等 | 输入 trade 结构需带 `position_id`；`closed_trades` 改为聚合口径后，`n_closed_trades`/`active+inactive` 语义变 |
| 同上 | `test_admission_profit_factor_threshold`、`test_admission_no_structural_loss_after_best_removed` | PF/⑥ 改用 `net_pnl`（原测试用 `realized_pnl=net_pnl*1.1` 的近似断言将失效） |
| 同上 | `test_write_reports_smoke` | 报告 JSON 结构新增 `data_completeness`/`snapshot_gaps`；`admission` 新增 `insufficient_data` 分支 |
| 同上 | `_make_profile_result`/`_make_engine_meta` fixture | engine_meta 需补 `snapshot_gaps` |
| [test_counterfactual.py](file:///Users/yl/vscode/Binance_quantitative_trading/tests/test_shadow_replay/test_counterfactual.py) | `test_as_dict_format`（`open_position is False`）、`_mk` 构造二元组 | scan_triggers 改为三元组；open_position 变真实布尔 |
| [test_state.py](file:///Users/yl/vscode/Binance_quantitative_trading/tests/test_shadow_replay/test_state.py) | `snapshot_equity`/`equity_history` 相关断言 | equity_history 元素三元组化；新增 `snapshot_marked_equity` |
| [test_engine_exit_rules.py](file:///Users/yl/vscode/Binance_quantitative_trading/tests/test_shadow_replay/test_engine_exit_rules.py) | trailing 相关用例（若有用扁平键/假配置构造） | #3 改嵌套解析 + 峰值浮盈 + 平尾仓（若采纳） |
| [test_replay_determinism.py](file:///Users/yl/vscode/Binance_quantitative_trading/tests/test_shadow_replay/test_replay_determinism.py) | 引擎结果结构断言 | engine_result 新增 `event_stats`/`gate_rejection_breakdown`/`snapshot_gaps` |

> 说明：这些测试此前「全绿」是因为用假数据/假配置（例如手工构造扁平 trailing 键、忽略盯市、每 exit 记一笔）。修复后必须同步更新 fixture 与断言，并**新增**真 profile 配置（`xau_original.yaml`/`xau_aggressive.yaml`）+ 真 1m K 线驱动的回归用例，覆盖本文档 §2–§4 的分支。

---

## 9. 不变量校验口径（与实现对齐：分散显式检查，停用 `run_all_invariants`）

[invariants.run_all_invariants](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/invariants.py#L57-L88) 签名 `(state_after, pos_before, pos_after, quantity=None, step_size=None, price=None, min_notional=None, label="") → List[str]`。

**实现结论（2026-10-19 修复轮，与代码一致）**：引擎**不**统一调用 `run_all_invariants`，改为在特定调用点**分散显式**调用单项检查，把非空返回追加进 `self.invariant_violations`：

- **入场**（`_fill_entry`，[engine.py 约 L1634-1650](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L1634-L1650)）：调 `check_quantity_in_range` + `check_notional_above_min`；`position_already_open` 由约 L1285 附近的手工守卫覆盖。
- **整仓平仓**（`_execute_exit` 剩余 ≤ 0 分支，[约 L1269-1287](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L1269-L1287)）：调 `check_no_double_close`（此时 `pos_after` 应为 None）+ `check_quantity_in_range`；跳过 `check_notional_above_min`。
- **部分平仓**（`_partial_close`，[约 L1149-1161](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/engine.py#L1149-L1161)）：只调 `check_quantity_in_range`。

**停用 `run_all_invariants` 的原因**：其内部包含 `check_no_double_close`，只要 `pos_after` 非 None 就报「平仓后持仓仍存在」伪违规——但**部分平仓后持仓仍在是正常的**，入场成功时同样会误报，使准入门槛 ⑤（`no_invariant_violations`）永远失败（每次入场/部分平仓都记一条）。故改为按调用点语义只跑真正适用的单项检查；`run_all_invariants` 仅保留在 `invariants.py` 供单元测试直接调用，不再被引擎引用。

- `report.py` 的 `no_invariant_violations` 门槛（[L240-245](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/report.py#L240-L245)）仍以 `len(invariant_violations) == 0` 判定，**无需改**（分散检查的结果同样汇入该列表）。

---

## 10. 影响面分析

| 面 | 涉及 |
|----|------|
| 文件（改） | `backtest/btc_eth/replay/engine.py`、`state.py`、`report.py`、`counterfactual.py`、`config.yaml`（新增 `trailing.*` / `mark_to_market.*`），`snapshot.py` 不改（符号校验落在 engine 侧） |
| 文件（只读引用） | `strategies/btc_eth/shadow/config/xau_original.yaml`、`xau_aggressive.yaml`（退出参数来源，不改） |
| 不改 | `strategies/`（生产策略）、`shared/trade_logger.py`（仅复用 `calculate_pnl`）、`scripts/check_code_sync.sh` |
| 不部署 | replay 已在 `.dockerignore`/`.trae` 之外，不进镜像；`main.py` 已离线守卫 `DATABASE_URL`（[main.py L52-57](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/replay/main.py#L52-L57)） |
| 文档同步 | 本设计文档；后续实现需更新 `docs/superpowers/plans/2026-10-19-xau-mtcps-shadow-implementation-plan.md` 的 §6.4 / §6.7 / §7.1 口径说明（vol_adj 降级、盯市曲线、仓位级聚合） |

---

## 11. 已决策事项（2026-10-19 架构师 + 用户联合裁定，覆盖 §11 原待确认项 #1–#4）

1. **[已决策] trailing 触发平仓量：全平（close_ratio = 1.0）**
   - 退出原因统一为 `TRAILING_STOP`
   - 平仓价取触发时的停止价（保守，不用 K 线收盘价）
   - 设计文档 §2.4 原推荐"平尾仓"被否决，采用全平口径
   - 依据：回放场景下 trailing 触发平尾仓需重构部分平仓后的剩余仓位保护（硬止损 + TP2 续挂），复杂度显著升高；而全平简化实现且保持确定性。生产侧平尾仓行为在本期回放中以"简化模型"标注，不追求与生产逐笔一致
   - 标记：报告元数据 `trailing_flat_close: true`

2. **[已决策] 滑点三档 `tiers_bps: [1, 2, 4]` 为独立档位（不叠加 baseline）**
   - spec §8.4 原文："滑点基准固定为每边 2 bps；额外输出每边 1 bps、2 bps、4 bps 三档压力测试"
   - 三档各自独立取值（1/2/4 bps/边），不在 baseline 2 bps 之上累加
   - 报告逐档输出三档压力测试结果
   - 准入第 7 项维持现状不变：`"2× 基准滑点压力"`（= 每边 4 bps），键名 `no_capital_preservation_breach_at_2x_slippage` 不变，JSON 契约不变（已有主口径，不新增独立测试路径）

3. **[已决策] `analysis_success_rate = (n_scheduled - n_analysis_error) / n_scheduled`**
   - 分母 = 该 profile 的调度次数（决策事件条数 `n_scheduled`）
   - 分子 = `decision != "analysis_error"` 的条数（即 `n_candidates + n_no_signal`）
   - 候选信号数 = `decision == "candidate"` 条数（单独输出为 `n_candidates`）
   - 三项（`analysis_success_rate`、`n_scheduled`、`n_candidates`）一并进入报告 per-profile 字段

4. **[已决策] `vol_adj` 本期降级取 1.0（从 config 读取，禁止硬编码）**
   - 快照仅 30 天 1m，无法确定性重算生产所需约 44 天日线 ATR 中位数
   - 在 `backtest/btc_eth/replay/config.yaml` 新增 `trailing: {volatility_adjustment_override: 1.0}`
   - 引擎从此配置读取，禁止硬编码 1.0
   - 报告需标注该降级（让读者知道 trailing 未做波动率调整），字段：`vol_adj_fallback_used: true, vol_adj_value: 1.0`

### 仍待确认（不在本期范围）

- **生产 `_get_volatility_adjustment` 路径不一致**（§2.6 附带发现）：生产 L4673 读 `self.risk_config['dynamic_trailing']['volatility_adjustment']` 与冻结 profile 嵌在 `signal_levels.{grade}.dynamic_trailing.volatility_adjustment` 的路径不一致，可能恒走 fallback 1.0。**本次不改生产代码**，仅在此保留待确认记录，后续另立生产一致性课题。

---

## 12. 精确公式汇总（对齐汇报）

```
# trailing 峰值浮盈与分档（§2.3）
pct = max(((ref−entry)/entry×100), 0)          # ref = highest_price(LONG)/lowest_price(SHORT)
activated = (pct >= activation.min_profit_pct) or (activation.also_on_tp1 and tp1_hit)
tier = first i where pct < tiers[i].profit_ceiling   # 保本: pct < tiers[0].ceiling 且未 tp1_hit → stop=entry
allowed_retrace = (ref−entry) × tiers[tier].retrace_ratio × vol_adj(1.0)
stop = ref − allowed_retrace   (LONG) ; = ref + allowed_retrace (SHORT)
final = max(stop, hard_stop) 单向抬高/降低后  (LONG) / min(...) (SHORT)
触发: LONG k_low <= final ; SHORT k_high >= final

# 未实现盈亏与盯市（§3.1）
unrealized = (mark−entry)×qty (LONG) ; (entry−mark)×qty (SHORT) ; mark = 当前1m K线 close
marked_equity = equity + unrealized

# 聚合 trade 净 pnl（§4.2）
net_pnl(pos) = Σ_i net_pnl_i = Σ_i (realized_pnl_i + fee_i + funding_i + slippage_i)
```

---

## 13. 第三轮复审打回后修复口径补充（2026-10-20）

> 本节回写第三轮复审打回后修复轮的口径与落点，供实现与测试对齐；不改变 §1–§12 已定方案。

### 13.1 连续亏损按整笔 `position_net_pnl` 判定（P1#1）

**问题**：§4.3 已定「连续亏损改聚合 `net_pnl` + 仓位级」，但实现层曾按「单批 `realized_pnl`」判定。分批平仓（TP1/TP2/尾仓/止损）场景下，`realized_pnl` 只是**最后一批**的毛盈亏，据此判定连亏会只看最后一腿，遗漏前序批次已实现的亏损/盈利，使 `consecutive_loss_pause` 误判。

**口径（与 §4.2 `net_pnl(pos)=Σ_i net_pnl_i` 同源，落在状态机层）**：

- `state.on_entry_approved` 建仓时写入 `pos["position_net_pnl"] = Decimal("0")`（整笔仓位级累计净盈亏，含全部腿的 fee/funding/slippage）。
- `engine._execute_exit` **每腿累加**：`pos["position_net_pnl"] += net_pnl`（`net_pnl = pnl + fee + funding + slip`，与 L1223 同）；部分平仓/全平均累加。
- 全平分支（`remaining <= 0`）调用 `state.on_position_closed(..., position_net_pnl=pos["position_net_pnl"])`。
- `state.on_position_closed` 的 `position_net_pnl` 为**必填关键字参数**；连亏判定由 `realized_pnl < 0` 改为 `position_net_pnl < 0`（`>=` 阈值时置 `consecutive_loss_pause_until`，否则清零）。
- **不变**：`on_position_closed` 只处理最后一批的权益/费用累计口径（部分腿已在 `_execute_exit` 部分平仓分支入账，严禁双重累计）。

> 与报告层 `report._aggregate_positions` 的仓位级 `net_pnl` 一致：状态机层的 `pos["position_net_pnl"]` 即该仓位的 `Σ net_pnl`，报告聚合值应与之相等（回归测试断言 `Σ net_pnl == pos.position_net_pnl`）。

### 13.2 顶层快照哈希 `shared helper` + fail-closed 重算（P1#2）

**问题**：`verify_snapshot` 只逐文件比对 `files.*.sha256`，未重算顶层组合哈希 → 若攻击者**同步篡改**三个文件的 sha256 字段并把顶层 `manifest["sha256"]` 留旧值（或反之），逐文件校验可能通过而顶层不一致仍被放过；`build_snapshot` 与 `verify_snapshot` 若各自实现组合口径，易发生口径漂移。

**口径**：

- 新增共享纯函数 `snapshot._compute_manifest_sha256(files)`，**canonical** 组合：
  ```
  json.dumps({"klines_1m":..,"funding_rates":..,"exchange_info":..},
             sort_keys=True, separators=(",", ":"))   # utf-8 后取 SHA-256
  ```
  （`files` 为 `manifest["files"]` 三键字典；`build_snapshot` 与 `verify_snapshot` **共用同一 helper**，杜绝口径漂移。）
- `build_snapshot` 写 `manifest["sha256"] = _compute_manifest_sha256(files)`（基于三文件内容哈希组合）。
- `verify_snapshot` 流程：① 逐文件比对 `files.*.sha256`（缺失 → `files_ok=False` + `{"file":key,"error":"missing"}`；不一致 → `files_ok=False` + mismatch）；② 再 `actual_top = _compute_manifest_sha256(manifest["files"])` 与 `expected_top = manifest["sha256"]` 比对，不一致 → `files_ok=False` 且 mismatches **追加 `{"file":"__manifest_sha256__", "expected":.., "actual":..}`**（fail-closed，不得静默放行）。

### 13.3 虚拟订单统计口径（P2）

**问题**：截止时**仍未到期**（`cutoff_at < expiry_at`）的 pending 限价挂单既未被撮合、也未被 `_settle_pending_limit_orders` 按过期记为 `expired_unfilled`，于是静默脱离结果与报告，导致成交率样本缺失（分母漏项）。

**口径**：

- `engine.run()` 尾部（L536-554）：`_settle_pending_limit_orders` 处理完过期挂单后，对每个 profile 剩余 pending 挂单中 `cutoff_at < expiry_at` 者记 `_record_unfilled(pid, order, reason="pending_at_cutoff")`，随后清空 `pending_limit_orders`（本次 run 无后续撮合）。
- `engine_metadata` 新增 `pending_at_cutoff_count`。
- `report.compute_metrics` 新增字段：
  ```
  n_filled_orders        = n_closed（聚合后已成交仓位数）
  n_pending_at_cutoff    = trades 中 !filled 且 unfilled_reason=="pending_at_cutoff" 的条数
  n_total_virtual_orders = n_filled_orders + n_unfilled        # 分母；n_unfilled 含 pending_at_cutoff
  fill_rate              = n_filled_orders / n_total_virtual_orders   # 分母含 pending_at_cutoff
  fill_rate_computable   = (n_total_virtual_orders > 0)
  # 分母为 0 → fill_rate=None 且 fill_rate_computable=False（渲染「不可计算」）
  ```
- `report._build_markdown` 渲染「虚拟订单总数 / 成交率 / 截止未到期挂单数」；`fill_rate=None`（不可计算）时渲染「不可计算」，不得用 `.1%` 格式化（否则对 `None` 抛 `TypeError`）。

> 口径要点：成交率分子是**已成交仓位数**（聚合后），分母是「已成交 + 全部未成交（含 pending_at_cutoff）」；`pending_at_cutoff` 必须计入分母，否则截止未到期挂单会系统性抬高成交率（幸存者偏差）。