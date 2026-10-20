# XAUUSDT MTCPS 影子仿真与小资金灰度 — 实施架构方案

**日期**：2026-10-19
**状态**：架构方案（待实施核实项已显式标注，不臆造）
**上游 spec**：[2026-10-18-xau-mtcps-shadow-pilot-design.md](../specs/2026-10-18-xau-mtcps-shadow-pilot-design.md)（已获批）
**当前分支**：`xau-mtcps-shadow-pilot`

---

## 0. 方案概述

本方案在不替换现有两个 MTCPS 生产实例的前提下，新增一个**只读影子采集服务**（`mtcps_xau_shadow`），复用 `trading-btc-eth` 镜像与 MTCPS 分析代码，对 `XAUUSDT` 生成原版/激进版两个 profile 的**无状态决策事件**并落库到 `research` schema；观察期结束后在**本地**运行确定性回放器，产出对比报告与准入判定。最终只有在候选 profile 通过准入后，才独立设计 `mtcps_xau_pilot` 实盘服务（不在本方案落地，仅约定其与影子服务共用同一套实例参数化机制）。

三条强制边界贯穿全部设计：
1. **不复制第三套源码**：通过实例参数化 + `strategies/btc_eth/shadow/` 子模块复用，禁止 clone `strategies/btc_eth`。
2. **只读安全边界**：影子容器构造 `ReadOnlyBinanceClient`，任何写接口 fail-closed。
3. **影子数据与生产数据物理隔离**：只写 `research` schema，不写 `trading.*` 与看板聚合表，不改生产 `config.yaml`。

---

## 1. 实例参数化

### 1.1 现状硬编码点（已逐条核实）

| 属性 | 现状 | 位置 |
|------|------|------|
| `capital_strategy_id` | 硬编码 `strategy_id="btc_eth"` | [strategy.py:672](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L668-L673)（激进版在 `btc_eth_aggressive/strategy.py` 同位置为 `btc_eth_aggressive`） |
| `circuit_breaker_pool` | 硬编码 `pool="mtpcs"` | [strategy.py:683](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L680-L685) |
| 内部落库名 | 硬编码 `self.strategy_name = "btc_eth"` | [strategy.py:651](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L650-L651) |
| 频率控制状态表归属 | 硬编码 `strategy_name="MTPCS策略"` | [strategy.py:618](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L614-L619) |
| 状态/看板上报归属 | 硬编码 `"btc_eth"` | [main.py:222](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/main.py#L221-L226)、[main.py:351](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/main.py#L346-L352) |
| `record_name` | 已配置化（`strategy.record_name`） | config.yaml `strategy.record_name` |
| `instance_id` / `execution_mode` / `state_reporting_enabled` / `real_ordering_enabled` | 不存在 | 需新增 |

### 1.2 配置载体形态

**决策：配置新增顶层键 `instance`（在本策略 `config.yaml` 内），同时允许环境变量覆盖（二选一优先级：环境变量 > config.yaml）。**

理由：
- 生产 `btc_eth` / `btc_eth_aggressive` 的 `config.yaml` 被打进镜像、且已存在产量历史，直接加 `instance` 键并显式写全默认值即可锁定行为；改动文件少、可被 `compute_config_hash` 捕获。
- 环境变量覆盖用于影子容器**不改镜像内 config 文件**即可切换身份（也可用独立 bind-mount 的 XAU 专用 config 文件，见 §9）。二者互补，不只保留一种以防误配。

新增键清单（默认值保证生产行为**完全不变**）：

| 参数名 | 生产默认值 | 影子值 | 说明 |
|--------|-----------|--------|------|
| `instance.id`（即 `instance_id`） | `btc_eth` / `btc_eth_aggressive` | `mtcps_xau_shadow` | 实例唯一标识 |
| `instance.record_name` | 沿用 `strategy.record_name` | （影子不写 trade_records，置 `""` / 空） | 归属名 |
| `instance.config_path` | 当前 config 文件路径 | 独立 bind-mount 路径 | 用于 config_hash 与资金管理 |
| `instance.execution_mode` | `live` | `shadow_batch` | 第一道开关 |
| `instance.capital_strategy_id` | `btc_eth` / `btc_eth_aggressive` | `""`（影子不参与资金分配） | 注入 `CapitalManager.strategy_id` |
| `instance.circuit_breaker_pool` | `mtpcs` | `""`（影子不参与组合熔断） | 注入 `CircuitBreaker(pool=...)` |
| `instance.state_reporting_enabled` | `true` | `false` | 控制是否调 `save_strategy_state` / `sync_open_positions` |
| `instance.real_ordering_enabled` | `true` | `false` | 第二道开关（`execution_mode=shadow_batch` 时强制置 false） |

> 注意：`record_name` 语义要区分「归属名」与「内部落库名」。为最小侵入，仍以 `strategy.record_name` 作为归属名（`load_ownership_config` 输入），`instance.id` 作为实例标识；影子容器通过 `state_reporting_enabled=false` + 不调 `execute_signal` 保证不落 trade_records，而非把 `record_name` 置空。

### 1.3 注入点

- `BTCEthStrategy.__init__`（[strategy.py:582-710](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L582-L710)）：读取 `config['instance']`，覆盖 `self.strategy_name`、`self.capital_mgr`（传 `strategy_id`）、`self.circuit_breaker`（传 `pool`）、`self.instance_id`、`self.execution_mode`、`self.state_reporting_enabled`、`self.real_ordering_enabled`。
- `FrequencyController.__init__`（[strategy.py:151](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L144-L151)）：`strategy_name` 由 `instance.id` 派生（生产仍为 `"MTPCS策略"` 不变；影子不建此表）。
- `main.py`（[main.py:222](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/main.py#L221-L226)、[main.py:351](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/main.py#L346-L352)）：`"btc_eth"` 字面量替换为 `instance.id` 派生值，且以 `state_reporting_enabled` 守卫 `save_strategy_state` / `sync_open_positions` 调用。
- `CapitalManager`（[shared/capital_manager.py:61-88](file:///Users/yl/vscode/Binance_quantitative_trading/shared/capital_manager.py#L61-L88)）：`config_path` 与 `strategy_id` 已参数化，无需改，仅由 strategy 传入。

### 1.4 生产默认值不变 + 回归锁定的保证方式

- **保证方式**：`config['instance']` 缺省时走 `_default_instance_config()`（返回上表「生产默认值」），并唯一决定点放在一个共享函数（`strategies/btc_eth/shadow/` 或 `shared/` 内，供两个 strategy.py 复用，禁止重复实现）。
- **回归测试**（跑既有 + 新增，断言生产行为不变）：
  1. `tests/test_strategies/test_capital_allocation_enforcement.py`：断言未配置 `instance` 时 `capital_mgr.strategy_id` 仍为 `btc_eth` / `btc_eth_aggressive`，`get_effective_margin_limit` 行为不变。
  2. `tests/test_strategies/test_total_margin_control.py` + `test_position_size_allocated_ratio.py`：断言 `check_entry_within_limit` / `_calculate_position_size` 序号语义不变。
  3. `tests/test_frequency_controller.py`：断言 `strategy_name="MTPCS策略"` 缺省值不变，状态表读写路径不变。
  4. 新增 `tests/test_strategies/test_instance_parametrize.py`（离线）：逐键断言缺省=上表默认值；`execution_mode=shadow_batch` 时 `real_ordering_enabled` 被强制 false、`state_reporting_enabled` 被强制 false。
  5. `tests/integration/test_btc_eth_strategy.py`：构造一个未显式配置 `instance` 的 `BTCEthStrategy`，断言 `strategy_name`、`symbols`、`timeframes`、`capital_strategy_id` 与改动前一致。

---

## 2. 只读安全边界

### 2.1 落点与实现策略

- **新模块**：`shared/readonly_client.py`，定义 `class ReadOnlyBinanceClient(BinanceClient)`。
- **策略（双层 fail-closed）**：
  1. **底层收口**：覆写 `_request`（[binance_api.py:228-255](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L228-L255)）为「入口白名单」——仅放行只读端点；任何命中的写端点一律抛 `ReadOnlyViolationError`，**在发起 HTTP 之前**即拦截（不依赖上层是否记得覆写）。白名单按 `(method, endpoint)` 维护，写方法清单见 §2.2。
  2. **公开方法覆写**：对全部写方法（§2.2 清单）显式覆写为 `raise ReadOnlyViolationError(...)`（方法名 + symbol 写进日志），并调用 `notification_client.send_alert(level="error")`。这是让**误调用点在其调用处**即失败、错误栈直指业务代码，便于定位「谁试图下单」。

### 2.2 被拦截方法清单（已按 [binance_api.py 方法表](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py) 逐条核对）

仅以下 6 个公开方法触达交易所写接口：

| 方法 | 行号 |
|------|------|
| `place_order` | [L616](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L616) |
| `place_conditional_order` | [L753](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L753) |
| `cancel_order` | [L1051](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L1051) |
| `cancel_algo_order` | [L1191](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L1191) |
| `cancel_all_algo_orders` | [L1225](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L1225) |
| `set_leverage` | [L1300](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L1300) |

其余 `get_*` 方法（`get_klines` / `get_ticker_price` / `get_funding_rate` / `get_funding_rate_history` / `get_exchange_info` / `get_symbol_info` / `get_orderbook` / `get_position` / `get_open_orders` / `get_account_balance` 等）为只读，放行。

### 2.3 拦截后的行为契约

- **异常类型**：`ReadOnlyViolationError(ReadOnlyViolationError)`（新类，定义在 `shared/readonly_client.py`，继承 `Exception`）；异常消息含 `method` / `endpoint` / `symbol` / `instance_id`。
- **告警**：同步调用（尽力）`notification_client.send_alert(title="影子实例触达禁用的写接口", level="error")`；通知失败仅记日志不阻断。
- **是否写库**：**不写任何数据库**（含 `research`）——这不是「数据质量」问题而是「安全事件」，只走飞书高优先级告警 + 结构化日志。如需留痕，追加到独立 `shadow` 日志目录（非 DB）。
- **进程行为**：单次采集周期内，写接口拒绝导致该 profile 记 `analysis_error`（若发生在决策路径），但**不升级为进程退出**（影子服务保持运行以便继续采集，同时连续告警人工介入）。

### 2.4 「任何代码路径都不能触达写接口」的证明方式

1. **单点收口**：`_request` 是全部 HTTP 的唯一出口（[binance_api.py:327-388 `_request_once`](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L327-L388) 之上的唯一调度点），白名单在此统一拦截 → 未来新增写方法若未显式覆写，仍会被底层白名单拒绝。
2. **构造时不注入 `trade_logger`**：影子容器不调用 `set_trade_logger`，因此即使业务代码偶然走到 `place_order`，也不会有 `trading.trade_records` 落库路径。
3. **自动化测试（离线）**：`tests/test_shared/test_readonly_client.py` 用 `unittest.mock.patch.object(client, "session")`（或 patch `_request_once`）断言：调用 6 个写方法均抛 `ReadOnlyViolationError` 且**未发起任何 HTTP 请求**（`session.request` 不被调用）；对只读方法则正常放行。
4. **静态核对**：`ag`/grep 影子入口引用，确认 `dual` 未引用 `execute_signal` / `place_order` / `place_conditional_order` 等符号。

---

## 3. `analyze()` 拆分（无状态市场决策 vs 有状态执行资格检查）

### 3.1 现状边界（基于 [analyze L712-1086](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L712-L1086)）

`analyze()` 内交织两类逻辑，标注如下（**这是拆分的唯一事实来源**）：

| 段 | 内容 | 有状态/无状态 |
|----|------|--------------|
| L732-740 | `frequency_controller.can_trade`（连续亏损暂停/日损/周损） | 有状态 |
| L742-747 | 经济日历禁交易 | 有状态 |
| L749-761 | 已有持仓浮亏禁加仓 | 有状态（读 `self.positions`） |
| L763-779 | 多周期 K 线获取 + 数据完整性/闭合校验 | 无状态（输入有闭合校验，见 §5.3） |
| L781-782 | `_build_indicators` | 无状态 |
| L784-816 | 市场状态识别 | 无状态 |
| L818-869 | 市场状态特定频率/每日交易数/冷却期 | **有状态**（读 `frequency_controller` 计数器） |
| L871-880 | 震荡市 ADX 下限 | 无状态 |
| L882-902 | 入场检查（趋势/震荡）→ direction | 无状态 |
| L904-909 | `_check_prohibited_conditions`（含资金费率/spread 等只读行情） | 无状态（部分含 atr 历史缓存，见 §3.4） |
| L911-932 | 动态 ATR 过滤器 | 半有状态（`atr_filter.update_history` 维护 per-symbol 内存历史） |
| L934-978 | 评分 / 方向 / 成交量过滤 | 无状态 |
| L980-1022 | `_calculate_position_size`（依赖 `capital_mgr` + `self.positions`） | **有状态** |
| L1024-1062 | 初始止损/TP/限价 → 组装 signal | 无状态（quantity/leverage 字段除外） |

### 3.2 拆分函数边界（最小侵入）

新增两个纯函数式/半纯方法，原 `analyze()` 改为「编排」：

1. **`async def _analyze_stationary_signal(self, symbol, klines, indicators, market_state_name, market_behavior) -> Optional[Dict]`**（无状态）
   - 覆盖 L883-978 + L1024-1062（入场检查 → 禁止入场 → ATR → 评分 → 方向 → 成交量 → 止损/TP/entry_price → 组装 signal）。
   - 返回的 signal **不含** `position_size_usdt` / `quantity` / `leverage` / `position_ratio` 等资金字段（置 `None` 或直接不落），只含：`direction` / `grade` / `score` / `entry_price` / `initial_stop_loss` / `tp1_price` / `tp2_price` / `atr` / `market_state` / `feature_snapshot` 结构 + 规则来源（`stop_loss_rule` / `tp1_rule` / `tp2_rule` / `trailing_rule` / `time_stop_rule`）。
   - 不读取 `self.positions` / `capital_mgr` / `frequency_controller`，纯由 `klines` + `indicators` + `config` 决定。

2. **`async def _check_stateful_execution_eligibility(self, symbol, current_time, market_state_name, market_behavior) -> Tuple[bool, str]`**（有状态）
   - 覆盖 L732-761 + L818-869（频率控制、经济日历、持仓禁加仓、市场状态频率/冷却）。
   - 与 `_analyze_stationary_signal` 之间只通过 `market_state_name` / `market_behavior` 传递，不共享其他状态。

3. **`analyze()` 编排（生产语义不变）**：保持原顺序 —— 先 `_check_stateful_execution_eligibility`（原 L732-869 等价替换），再取 K 线 → 指标 → 市场状态，再 `_analyze_stationary_signal`（原 L883-978 + L1024-1062 等价替换），最后接回 `_calculate_position_size` + 精度 + min_notional + 组装 quantity/leverage 字段。**顺序与当前完全一致**，只是把「有无状态」拆开。

> 拆分关键：市场状态频率/冷却（L818-869）原本夹在「市场状态识别」之后。它依赖 market_state_name，故不能放进纯无状态函数前段；因此把「市场状态识别」保留在 `analyze()` 编排层（在调无状态函数之前产出 `market_state_name`），再把它作为参数传给无状态函数与有状态函数。

### 3.3 影子采集器如何复用无状态部分

影子采集器（§5）调用链：`K 线闭合校验 → _build_indicators → get_market_state → _analyze_stationary_signal → 落库`。
**不调用**：`_check_stateful_execution_eligibility`、`_calculate_position_size`、`execute_signal`、`update_positions`。
由此实现 spec §7「采集期不应用真实频率/资金/持仓门槛；有状态门槛在回放期按时间顺序施加」。

### 3.4 回归风险与对应测试

| 风险 | 测试 |
|------|------|
| 拆分改变 `analyze()` 顺序/字段 → 生产信号漂移 | `tests/integration/test_btc_eth_strategy.py`：以固定 klines fixture 断言 `analyze()` 返回 dict 的 `direction`/`grade`/`score`/`entry_price`/`tp1_price`/`tp2_price`/`temperature` 与拆分前**逐字段相等**；`tests/test_strategies/test_mtpcs_partial_tp_and_trailing.py` / `test_btc_eth_trailing_boundary.py` 全绿。 |
| 有状态门槛从无状态函数误删 | 新增 `tests/test_strategies/test_analyze_split.py`：同一输入分别断言「有状态路径被拒」与「无状态函数仍产出 candidate」，互不串状态。 |
| `atr_filter.update_history` / 其他半有状态副作用 | 拆分时 `_check_prohibited_conditions` 与 ATR 过滤仍保留在被调用侧，其内部内存历史副作用保持原样；用 `test_capital_allocation_enforcement.py` 之外新增 `test_dynamic_atr` 断言过滤阈值行为不变。 |

---

## 4. research schema

### 4.1 DDL 文件

**新增 `database/postgres/init-scripts/12-research-schema.sql`**（幂等、无 `BEGIN/COMMIT`、与 01-11 同风格，由 deploy job 的「同步 init-scripts + 幂等执行」自动在服务器生效，见 deploy.yml [L326-339](file:///Users/yl/vscode/Binance_quantitative_trading/.github/workflows/deploy.yml#L326-L339) 与 [L430-483](file:///Users/yl/vscode/Binance_quantitative_trading/.github/workflows/deploy.yml#L430-L483)）。

### 4.2 表结构设计（对应 spec §6.1–§6.3）

**`research.shadow_runs`**（每观察周期一行）
- `run_id TEXT PRIMARY KEY`
- `instance_id TEXT NOT NULL`（`mtcps_xau_shadow`）
- `symbol TEXT NOT NULL`（`XAUUSDT`）
- `started_at TIMESTAMPTZ` / `ended_at TIMESTAMPTZ`
- `status TEXT NOT NULL`（`active` / `closed` / `aborted`）
- `initial_virtual_equity_usdt NUMERIC(20,8) NOT NULL`（300）
- `git_commit TEXT` / `git_sha TEXT`（取自容器 `/app/VERSION`）
- `config_hashes JSONB`（`{"xau_original": "<sha256>", "xau_aggressive": "<sha256>"}`）
- `data_cutoff_at TIMESTAMPTZ`（`ended_at` 或观察截止；关闭 run 时固化）
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- 索引：`idx_shadow_runs_instance(symbol, started_at)`

**`research.shadow_decision_events`**（不可变决策事件）
- `id BIGSERIAL PRIMARY KEY`
- `run_id TEXT NOT NULL REFERENCES research.shadow_runs(run_id)`
- `profile_id TEXT NOT NULL`（`xau_original` / `xau_aggressive`）
- `symbol TEXT NOT NULL`
- `scheduled_at TIMESTAMPTZ NOT NULL`（调度时点）
- `evaluated_at TIMESTAMPTZ NOT NULL`
- `decision TEXT NOT NULL`（`candidate` / `no_signal` / `analysis_error`）
- `direction TEXT` / `grade TEXT` / `score NUMERIC(12,4)`
- `reference_price NUMERIC(24,8)`
- `intended_order_type TEXT` / `intended_entry_price NUMERIC(24,8)`
- `stop_loss_rule TEXT` / `tp1_rule TEXT` / `tp2_rule TEXT` / `trailing_rule TEXT` / `time_stop_rule TEXT`（规则亦可存 JSONB，见 §6.3）
- `feature_snapshot JSONB`（无状态特征快照）
- `reason_codes JSONB`（`no_signal`/`analysis_error`/数据质量原因的结构化码）
- `data_quality_reason TEXT`（K 线未闭合/缺失/断链时；片段级）
- `config_hash TEXT NOT NULL` / `git_commit TEXT`
- `source_kline_close_times JSONB`（`{"1h": ts, "4h": ts, "1d": ts}`，用于回放防未来数据）
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- **唯一键**：`UNIQUE (run_id, profile_id, symbol, scheduled_at)`（保证调度重试幂等）
- 索引：`idx_shadow_decision_run_profile(run_id, profile_id, scheduled_at)`、`idx_shadow_decision_decision(run_id, decision)`

**`research.shadow_replay_trades`**（本地回放器派生，经导入归档；见 §6.4 落库策略）
- `id BIGSERIAL PRIMARY KEY`
- `replay_run_id TEXT NOT NULL`（引擎版本 + 输入快照 hash 派生，非随机）
- `run_id TEXT NOT NULL`（关联影子 run）
- `profile_id TEXT NOT NULL`
- `decision_event_id BIGINT`（来源事件，可选）
- `symbol TEXT`
- `direction TEXT`
- `entry_time TIMESTAMPTZ` / `entry_price NUMERIC(24,8)` / `filled BOOLEAN` / `unfilled_reason TEXT`
- `quantity NUMERIC(30,8)` / `notional NUMERIC(24,8)` / `margin NUMERIC(24,8)` / `leverage NUMERIC(8,2)`
- `stop_loss / tp1 / tp2 / trailing / time_stop 事件` 以 JSONB `exit_events` 承载（结构化数组）
- `realized_pnl NUMERIC(24,8)` / `fee NUMERIC(24,8)` / `funding_fee NUMERIC(24,8)` / `slippage NUMERIC(24,8)` / `net_pnl NUMERIC(24,8)`
- `mae NUMERIC(24,8)` / `mfe NUMERIC(24,8)` / `holding_duration_seconds INTEGER`
- `exit_reason TEXT`
- `engine_version TEXT` / `input_cutoff_at TIMESTAMPTZ` / `manifest_sha256 TEXT`
- 索引：`idx_shadow_replay_prof(replay_run_id, profile_id)`

### 4.3 授权

- `GRANT USAGE ON SCHEMA research TO trading_user;`
- `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA research TO trading_user;`
- `ALTER DEFAULT PRIVILEGES IN SCHEMA research GRANT ... TO trading_user;`
- 影子容器连库账号保持 `trading_user`（复用 `DATABASE_URL`，仅 `?schema=research` 前端约定 + 代码内显式 `research.` 前缀，不依赖 `search_path`）；**不**授予 `trading.*` 的新增权限，杜绝越写（既有 `trading_user` 对 `trading.*` 可能已有权限，靠 §4.4 代码层守卫）。

### 4.4 research 写入模块落点与幂等 upsert

- **新模块**：`strategies/btc_eth/shadow/repository.py`，封装 `ShadowRepository(db_manager)`。
- **复用 `DatabaseManager` 能力**（[shared/database.py](file:///Users/yl/vscode/Binance_quantitative_trading/shared/database.py)）：`execute`（带 `_validate_sql`，禁 DROP/GRANT 等，天然防越权跨 schema）、`fetch_one`/`fetch_all`、`fetch_one_advisory_lock`（并发 safe 的 upsert 判定）。DDL 一律走 `execute_ddl` 仅限启动建表（或交 init-scripts，不重复建）。
- **幂等策略**：`INSERT ... ON CONFLICT (run_id, profile_id, symbol, scheduled_at) DO NOTHING`（或 `DO UPDATE` 只更新 `evaluated_at`/`created_at`，不覆盖决策字段），使调度重试不产生重复事件；`shadow_runs` 用 `INSERT ... ON CONFLICT (run_id) DO NOTHING`。
- **写失败处置**：捕获异常 → 仅记日志（写入失败**不得回退为本地下单**，也不得改变已算出的决策），该周期标记告警；不 throw 回主循环。
- **schema 白名单守卫**：`repository.py` 内所有 SQL 硬性 `research.` 前缀，并提供 `tests` 断言「不出现 `trading.` 的 INSERT/UPDATE」，作为不越界写生产表的自动化证据。

---

## 5. 影子采集器

### 5.1 代码落点

```
strategies/btc_eth/shadow/
├── __init__.py
├── main.py            # 影子入口（python -m strategies.btc_eth.shadow.main）
├── collector.py       # 调度编排 + K 线闭合校验 + 双 profile 并行分析 + 落库
├── repository.py      # research schema 读写（§4.4）
├── instance.py        # 实例配置解析（复用 §1 的默认实例函数）
├── export.py          # 决策事件 + run manifest 导出 CLI（服务器侧）
└── config/            # XAU 两个 profile 的冻结配置（审计；生产 bind-mount 覆盖用副本）
    ├── xau_original.yaml
    └── xau_aggressive.yaml
```

该目录已被 [btc_eth Dockerfile:20](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/Dockerfile#L20) 的 `COPY strategies/btc_eth/` 覆盖，进镜像无需改 Dockerfile。

### 5.2 调度与 K 线闭合校验

- 调度 cron 复用生产错峰思想：影子服务不锚定整点（与生产 `5 分`/`10 分`错开，避免并发抢 kline-service 读），建议 `15 * * * *`（config 可配）。
- 每次触发：拉 `XAUUSDT` 的 `1h/4h/1d`（复用 `KLineService.get_multi_timeframe_data`），**校验最后一根 K 线 `close_time` 已闭合**（`close_time <= now` 且时间戳连续、无缺口）。未闭合/缺失/断链 → 该周期两 profile 均记 `decision=analysis_error` 或 `no_signal` + `data_quality_reason`，不产 candidate（spec §12.1）。

### 5.3 双 profile 并行分析

- 每个 profile 独立构造一个**只读** `BTCEthStrategy` 子类实例（或一个轻量 wrapper，仅注入 `kline_service + ReadOnlyBinanceClient + config(该 profile) + db_manager(不绑定贸易)`），复用 `_build_indicators` → `get_market_state` → `_analyze_stationary_signal`。
- 两个 profile 用 `asyncio.gather(..., return_exceptions=True)` 并行；任一 profile 抛异常只影响自己（记 `analysis_error`），另一 profile 继续（spec §12.1）。**profile 之间不共享任何可变状态**：各自独立 `self.positions = {}`、独立 `frequency_controller`（若构造则需要独立实例）、独立指标缓存。

### 5.4 全量事件落库

- `decision=candidate`：落全特征快照 + 规则来源 + `source_kline_close_times`。
- `decision=no_signal`：落 `reason_codes`（含 `grade < min_score`、`trend_filter`、`prohibition` 等成因码）。
- `decision=analysis_error`：落异常摘要 + `data_quality_reason`。

### 5.5 run_id / config_hash / git_commit 冻结

- `run_id`：确定性生成（如 `xau-shadow-YYYYMMDDHHMMSS` 或 `run_id = sha256(git_commit + profile_config_hashes 排序拼接)[:16]`），**非随机 uuid**，保证可复现。
- `config_hash`：复用 [compute_config_hash](file:///Users/yl/vscode/Binance_quantitative_trading/shared/config_loader.py#L179-L200)（已剔除敏感键、排序键、SHA-256）。
- `git_commit`/`git_sha`：从容器 `/app/VERSION`（CI 在构建期写入的 `GIT_SHA`/`GIT_COMMIT`）读取。
- 中途改配置 → `config_hash` 变化，采集器检测到当前 active run 的 `config_hashes` 与新值不一致时**开启新 `run_id`**（不与旧样本混算，spec §5.2）。

### 5.6 错误隔离与异常路径

- `analysis_error` / 写库失败：不调用 `execute_signal`，不建立虚拟在线持仓；写库失败只告警不回退下单（§4.4）。
- 只读客户端拒绝（§2）触发高优告警；本周期该 profile 记 `analysis_error` 但不退出进程。

---

## 6. 本地回放器

### 6.1 模块落点（本地独立运行、不依赖容器路径、不连生产库）

```
backtest/btc_eth/replay/          # 已被 .dockerignore 排除 → 不进镜像、不部署
├── __init__.py
├── main.py            # CLI 入口（显式指定导出文件 + manifest）
├── snapshot.py        # Binance 公共接口拉 1m/资金费/exchangeInfo → 不可变快照 + manifest + SHA-256
├── engine.py          # 事件驱动重放状态机
├── matching.py        # 撮合（限价穿越 / 市价保守滑点 / tick/step/minNotional 舍入）
├── fees.py            # 手续费/资金费/滑点口径
├── state.py           # 有状态门槛状态机（per-profile 虚拟权益/持仓/冷却/日交易数/连续亏损）
├── invariants.py      # 回放不变量校验（无保护状态/重复平仓/数量越界）
├── report.py          # 指标计算 + 准入判定 + 报告产物
├── counterfactual.py  # 反事实停牌标记（不截断）
└── config.yaml        # 回放口与费用/滑点/pilot 资金约束配置（禁硬编码）
```

回放器默认只读「导出事件文件 + 1m 快照 manifest」，**不在运行时连生产 PostgreSQL**（`DATABASE_URL` 缺省即异常，而非回退连生产）。

### 6.2 输入契约

- **决策事件导出文件**：JSON Lines（每行一条 `shadow_decision_events`），由服务器侧 `strategies/btc_eth/shadow/export.py` 按 `run_id` 导出，含全部 `candidate`/`no_signal`/`analysis_error` 与 `source_kline_close_times`。
- **1m 快照 manifest**：`manifest.json` 记录 `symbol / interval=1m / start/end / 来源端点 / 拉取时间 / 行数 / 文件 SHA-256 / 缺口检查结果 / 历史资金费率文件 hash / (exchangeInfo) tickSize-stepSize-minNotional hash`。回放引用 `manifest_sha256`，**同一 `replay_run_id` 内不允许网络覆盖旧快照**（spec §4.4）。

### 6.3 撮合与退出规则实现载体（复用/取舍）

- **不复用 `backtest_engine_complete.py` 的 `BacktestEngine`**：它是「CSV 全量驱动、内部重算 score/direction 的**重演式**回测」（[backtest_engine_complete.py](file:///Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/scripts/backtest_engine_complete.py) 的 `load_klines_from_csv` → `_calculate_score` → `_determine_direction`），与影子回放「事件驱动、信号已由采集器冻结」的口径冲突，强行复用会破坏「信号不能重算」的约束。
- **复用/对齐**：
  - `Position` 结构、`BacktestEngine._check_and_close_position` / `_close_position` 的「同根 K 线多价位触发顺序」保守口径可**借鉴**其思路（不照抄代码）。
  - 盈亏计算复用 [TradeLogger.calculate_pnl](file:///Users/yl/vscode/Binance_quantitative_trading/shared/trade_logger.py#L689-L713)（direction×entry×exit×qty 单一实现，禁止重复）。
- **退出规则值来源**：从对应 profile 的**冻结 config**读取（`stop_loss_atr_multiplier` / `tp1/tp2` / `dynamic_trailing` / `time_stop`），回放器不自带第二份常量。

### 6.4 费用/资金费/滑点/保守顺序口径

- **手续费**：主口径每边 **5 bps taker**（spec §8.4）；可选「实际费率口径」仅在能取到账户费档时作为附加列，不改准入口径。
- **资金费**：用**历史 funding rate**（快照内），按持仓跨越的结算时点累加；复用币安只读口径（参考 [BinanceClient.get_funding_rate_history](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L1735) 的端点，本地直接 HTTP get，不依赖容器客户端）。
- **滑点**：基准每边 **2 bps**；额外输出 1/2/4 bps 三档压力测试；限价入场成交不额外加入场滑点，限价退出按压力档。
- **保守顺序**：同根 1m K 线同时穿越止损与止盈、无法判序 → **止损先发生**。
- 以上数值（5bps / 2bps 三档 / 300 秒限价有效期）全部入 `replay/config.yaml`，**不硬编码**。

### 6.5 量化舍入

- 用 `tickSize` / `stepSize` / `minNotional` 快照做价格/数量舍入；pilot 外层资金约束（最大单笔保证金 30 USDT、最大并发持仓 1、最大杠杆 3x）由回放器在「有状态门槛状态机」施加（spec §5.2），**采集期不落这组约束**。

### 6.6 有状态门槛状态机（per-profile 独立）

维护：虚拟权益/可用资金、当前持仓、每日交易计数、单品种冷却、连续亏损暂停。任一门槛拒绝的 candidate 仍**永久保留**并写结构化 `rejection_reason`（spec §7）。

### 6.7 反事实停牌标记（不截断）

- 影子主口径**不施加** 15%/25% 停牌，持续推进到数据截止。
- 虚拟权益（相对 300 USDT）触及 **−45 / −75 / −90 USDT**（15%/25%/30%）时，记录首次触发时点与当时持仓，但**不改变**后续回放路径（`counterfactual.py` 独立计算）。

### 6.8 确定性重放的保证手段

- 全程 `Decimal` 运算，禁止 float 累计；结果仅在最终展示时转 float。
- 稳定排序：决策事件按 `(scheduled_at, profile_id, symbol)` 排序；同 `scheduled_at` 用 `profile_id` 字典序破平，避免 dict 顺序抖动。
- **无随机来源**：`run_id` / `replay_run_id` 由外部显式传入或由输入 hash 确定性派生，禁 `uuid`/`random`。
- 重放结果引用 `manifest_sha256 + engine_version + input_cutoff_at`；引擎版本变更 → 新 `replay_run_id`，不覆盖旧结果（spec §12.2）。
- 不变量校验（`invariants.py`）：无保护状态、重复平仓、数量越界，作为 `shadow_replay_trades` 落库前拦截项。

---

## 7. 报告与准入

### 7.1 指标计算模块与报告产物

- `report.py` 输出 per-profile：调度次数/分析成功率/候选数；虚拟订单/成交率/未成交原因；门槛拒绝分布；总盈亏/手续费/资金费/滑点/净盈亏；胜率/盈亏比/Profit Factor/单笔期望；最大回撤/最大连续亏损/最差单笔；MAE/MFE/均持仓时长；退出占比（SL/TP1/TP2/trailing/time_stop）；LONG/SHORT 分组；活跃时段/非活跃时段分组（TradFi 非活跃时段按 §8 定义）；滑点三档压力；15/25/30% 反事实触发时点；原版 vs 激进版同点决策差异。
- **产物形态**：两文件 —— `report_<run_id>.md`（人读）+ `report_<run_id>.json`（机读，含 `replay_run_id`、`engine_version`、`manifest_sha256`、逐项指标 + `admission` 结论），本地落盘，不依赖 DB。

### 7.2 准入判定

- 实现 `report.admission.py`（或 `report.py` 内函数）落地 spec §10.2 七项，返回 `admission ∈ {original, aggressive, none}`：
  1. 有效已结虚拟交易 ≥ 12；
  2. 净盈利（扣除手续费/资金费/基准滑点）> 0；
  3. Profit Factor ≥ 1.20；
  4. 最大回撤 ≤ 15%；
  5. 回放不变量无违反；
  6. 扣除最佳单笔后无结构性亏损（组合不为明显负）；
  7. 2× 滑点压力下不触发 30% 红线。
- 净盈利/PF/最大回撤均以「不施加停牌」的完整路径为准（§8.3）。
- 两 profile 都通过时，选风险调整后期望更高、回撤更小、更稳定者；都未通过 → 结论 `none`。

### 7.3 原版/激进版差异对比

`报告` 中「同点决策差异」章节：按 `scheduled_at` 对齐两 profile 的 `candidate/no_signal` 差异 + 各自指标并排表 + 用 `config_hash` 标明对比基线。

---

## 8. 行情侧

### 8.1 注册 XAUUSDT 并采集 1h/4h/1d 的改动点

- **改动点**：`SymbolRegistry`（[core/registry.py](file:///Users/yl/vscode/Binance_quantitative_trading/services/kline_service/core/registry.py)）为 **DB 驱动**（`registered_symbols` 表 + `/register` 端点），注册**不改 `collector.py` 默认符号/周期列表**（[collector.py:42-47](file:///Users/yl/vscode/Binance_quantitative_trading/services/kline_service/core/collector.py#L42-L47) 的 `symbols`/`intervals` 只是 `collect_all` 兜底）。注册动作走 `KLineService.register_symbol(symbol="XAUUSDT", intervals=["1h","4h","1d"])`（[kline_service.py:174](file:///Users/yl/vscode/Binance_quantitative_trading/shared/kline_service.py#L174-L178)）或直接 HTTP `POST /register`。
- **确认不改**：`core/collector.py`（默认列表）、`api/routes.py` 的 `/klines/latest`（`limit ≤ 100` 已满足策略 `_get_limit` 的 100，无需改）。
- **1m 常驻采集**：`XAUUSDT` 只注册 `1h/4h/1d`，**不注册 1m**；1m 数据由本地回放器从 Binance 公共接口按需拉取（spec §4.4）。

### 8.2 TradFi 合约注意点

- **非活跃时段 K 线缺口**：XAUUSD 传统市场休市/流动性稀薄期可能 1m 成交稀疏或长时间无成交，需在快照生成时做「缺口检查」（`snapshot.py`），缺失 1m 段标记数据缺口而非插值；缺口导致该时段撮合不可判定时保守按不可成交处理，并在报告标记为不完整（不得产出「通过准入」）。
- **标记价/指数价差异**：分析输入用 kline-service 的 K 线成交价（`close/open/high/low`），与 TradFi 的标记价/指数价口径不同。影子决策只用 K 线价格，回放撮合只用成交价，**不引入标记价**（避免与现货黄金指数价格混淆）；若后续要评估强平/保证金比例，需另行接 `markPrice` 只读端点（本期不做，标注待实施核实）。
- **上线时间样本不足**：XAUUSDT 2026-01 上线，历史 <1 年；基线回测仅作「发现缺陷/校验频率/量级」，不触发准入（spec §9）。

---

## 9. 部署契约

### 9.1 compose 新增 shadow service（复用镜像，独立 command/挂载/日志/schema）

在 [docker-compose.yml](file:///Users/yl/vscode/Binance_quantitative_trading/docker-compose.yml) 新增 `mtcps-xau-shadow` 服务（参考既有 `grid-realtime` shadow 形态 [L212-246](file:///Users/yl/vscode/Binance_quantitative_trading/docker-compose.yml#L212-L246)）：

- `image: ghcr.io/bearflower/trading-btc-eth:latest`（复用镜像，**不新增镜像，不改 Actions 构建矩阵**）。
- `command: ["python", "-m", "strategies.btc_eth.shadow.main"]`。
- `container_name: trading_system-mtcps-xau-shadow`。
- `environment`：`STRATEGY_NAME=btc_eth`、`EXECUTION_MODE=shadow_batch`、`DATABASE_URL=...?schema=research`、`STATE_REPORTING_ENABLED=false`、独立日志目录、只读客户端标志。
- `volumes`：`./logs/mtcps_xau_shadow:/app/logs`、`./data/mtcps_xau_shadow:/app/data`、`./strategies/btc_eth/shadow/config:/app/strategies/btc_eth/shadow/config:ro`（独立 bind-mount 配置，物理隔离生产 `config.yaml`）。
- `depends_on`：`postgres(healthy)` + `kline-service(healthy)`；不依赖 `btc-eth-strategy`。
- 健康检查同现有 `grid-realtime` 方式；失败不阻塞生产策略（`restart: unless-stopped` 仅限本服务）。

### 9.2 确认不改 Actions 构建矩阵

[deploy.yml 构建矩阵 L100-112](file:///Users/yl/vscode/Binance_quantitative_trading/.github/workflows/deploy.yml#L100-L112) **不改**：影子代码落在 `strategies/btc_eth/` 下，触发 `btc-eth` 镜像重建即包含影子入口；新增 `12-research-schema.sql` 走既有「同步 init-scripts + 幂等执行」自动生效。新增 bind-mount 配置不进镜像，不触发重建。

### 9.3 隔离与可回退

- 影子与生产隔离：独立容器、独立 schema（`research`）、独立日志/数据目录、只读客户端、不写 `trading.*`、不参与资金分配/熔断/看板。
- **回退**：`docker compose stop mtcps-xau-shadow`（或删服务定义后 `up -d`），无需改生产交易对池、无需清理真实持仓、无需回滚镜像。
- 残留容器清理口径（deploy.yml [L378-407](file:///Users/yl/vscode/Binance_quantitative_trading/.github/workflows/deploy.yml#L378-L407)）需在 `RESIDUAL_CANDIDATES` 增加 `trading_system-mtcps-xau-shadow`（否则独立 `docker run` 残留与 compose 同名会冲突）——此项属**修改 deploy.yml 的清理候选清单**，不新增矩阵条目，需明确（见文件改动清单）。

### 9.4 初次上线前需人工执行的步骤清单

1. **注册 XAUUSDT**：调 kline-service `register XAUUSDT ["1h","4h","1d"]`（一次性，之后 kline-service 持久化在 `registered_symbols`）。
2. **确认行情就绪**：等 kline-service 回填/采集到可用 XAU `1h/4h/1d`；确认 `/klines/latest?symbol=XAUUSDT&interval=1h` 返回非空（含至少 1 根已闭合 K 线）。
3. **run 初始化**：影子采集器首次启动自动创建 `research.shadow_runs` active run（或人工执行一次 `shadow/export.py --init-run`）；确认 `config_hashes` 与 `git_commit` 写入正确。
4. **本地基线回测 + 安全验收**（spec 实施顺序 8）：先在本地跑历史数据基线回测与安全验收，通过后才开影子。
5. **确认 init-scripts 生效**：push 后观察 deploy job 的 init-scripts 步骤执行到 `12-research-schema.sql`（无报错），`research` schema 三表 + GRANT 就位。
6. **观察 Actions 全绿**：`btc-eth` 镜像因 `strategies/btc_eth/shadow/` 变更而重建，矩阵其余 skip 属正常。

---

## 10. 测试策略映射（spec §13.1–§13.4）

| spec 测试项 | 目标测试文件（新增/既有） | 关键断言 | 是否须离线 |
|---|---|---|---|
| §13.1 安全：`shadow_batch` 下任何写调用失败 | 新增 `tests/test_shared/test_readonly_client.py` | mock `session`，6 写方法抛 `ReadOnlyViolationError` 且 0 次 HTTP | 是（不连币安/生产库） |
| §13.1 影子不写 trade_records/持仓/看板 | 新增 `tests/test_strategies/test_shadow_no_trading_write.py` | mock `db.execute`，断言无 `trading.` INSERT/UPDATE；`repository.py` 全部 SQL 前缀 `research.` | 是 |
| §13.1 生产两实例未配置新参数行为不变 | `test_capital_allocation_enforcement.py`、`test_total_margin_control.py`、`test_frequency_controller.py`、`integration/test_btc_eth_strategy.py` + `test_instance_parametrize.py` | 缺省 `instance` 时逐项 `strategy_id`/`pool`/`strategy_name`/`symbols` 不变 | 是 |
| §13.2 采集：profile 不串状态/重试幂等/字段契约/config_hash 检出 | 新增 `tests/test_strategies/test_shadow_collector.py` | 两 profile 独立 `positions`；`ON CONFLICT DO NOTHING` 重试不重复；`candidate/no_signal/analysis_error` 字段稳定；改 config 后 `config_hash` 变化触发新 run | 是（注入 mock kline） |
| §13.2 未闭合 K 线不参与决策 | `test_shadow_collector.py` | 末根 `close_time > now` 时该周期不产 candidate | 是 |
| §13.3 回放：限价/止损/分批/移动止损/时间止损/冷却/手续费资金费滑点/tick-step/反事实不截断/确定性 | 新增 `tests/test_shadow_replay/`（`test_matching.py`、`test_fees.py`、`test_state.py`、`test_counterfactual.py`、`test_replay_determinism.py`、`test_invariants.py`） | 见 §6 | 是（全离线，读固定 fixture K 线） |
| §13.4 报告：明细与汇总守恒/无交易不误判通过/最佳单笔与滑点压力可复现/活跃-非活跃分组和=全量 | 新增 `tests/test_shadow_replay/test_report.py` | `sum(trade.net_pnl) == 汇总净盈亏`；`n_trades<12` 或全无交易时 `admission=none`；`active+inactive == all` | 是 |

---

## 11. 文件级改动清单

| 文件 | 动作 | 责任 |
|------|------|------|
| `strategies/btc_eth/strategy.py` | 修改 | 实例参数化注入 + `_analyze_stationary_signal` / `_check_stateful_execution_eligibility` 拆分 + `analyze()` 编排等效改写 |
| `strategies/btc_eth_aggressive/strategy.py` | 修改 | 同步实例参数化（激进版为复制文件，需与原版一致）；`analyze()` 拆分 |
| `strategies/btc_eth/main.py` | 修改 | `"btc_eth"` 上报字面量参数化；`state_reporting_enabled` 守卫 `save_strategy_state`/`sync_open_positions` |
| `strategies/btc_eth/config.yaml` | 修改 | 新增 `instance` 键（含生产默认值），**其余键不动** |
| `strategies/btc_eth_aggressive/config.yaml` | 修改 | 新增 `instance` 键，默认值 `btc_eth_aggressive` |
| `shared/readonly_client.py` | 新增 | `ReadOnlyBinanceClient` + `ReadOnlyViolationError` + 写方法白名单 + 底层 `_request` 收口 |
| `strategies/btc_eth/shadow/__init__.py` | 新增 | 包标记 |
| `strategies/btc_eth/shadow/main.py` | 新增 | 影子入口（只读装配 + 调度） |
| `strategies/btc_eth/shadow/collector.py` | 新增 | K 线闭合校验/双 profile 并行/全量事件落库 |
| `strategies/btc_eth/shadow/repository.py` | 新增 | research 读写 + 幂等 upsert + `research.` 白名单 |
| `strategies/btc_eth/shadow/instance.py` | 新增 | 实例配置解析（供两 strategy.py 复用，避免重复） |
| `strategies/btc_eth/shadow/export.py` | 新增 | 决策事件 + run manifest 导出 CLI（服务器侧） |
| `strategies/btc_eth/shadow/config/xau_original.yaml` | 新增 | 冻结 profile 配置（首轮基线，spec §5.2 表） |
| `strategies/btc_eth/shadow/config/xau_aggressive.yaml` | 新增 | 冻结 profile 配置 |
| `backtest/btc_eth/replay/*` | 新增（不部署） | 本地回放器（snapshot/engine/matching/fees/state/report/counterfactual/invariants/main/config.yaml） |
| `database/postgres/init-scripts/12-research-schema.sql` | 新增 | `research` schema 三表 + 索引 + GRANT（幂等） |
| `docker-compose.yml` | 修改 | 新增 `mtcps-xau-shadow` service；`RESIDUAL_CANDIDATES` 清理项在 deploy.yml |
| `.github/workflows/deploy.yml` | 修改（仅清理候选清单） | `RESIDUAL_CANDIDATES` 追加 `trading_system-mtcps-xau-shadow`；**不新增矩阵、不改构建判定/init-scripts 步骤** |
| `tests/test_shared/test_readonly_client.py` | 新增 | 只读安全边界离线测试 |
| `tests/test_strategies/test_instance_parametrize.py` | 新增 | 实例参数化缺省回归 |
| `tests/test_strategies/test_analyze_split.py` | 新增 | analyze 拆分等价回归 |
| `tests/test_strategies/test_shadow_collector.py` | 新增 | 采集/幂等/config_hash/未闭合 K 线 |
| `tests/test_strategies/test_shadow_no_trading_write.py` | 新增 | 不越界写库守卫 |
| `tests/test_shadow_replay/*` | 新增 | 回放/费用/状态/反事实/确定性/报告/不变量 |
| `shared/binance_api.py` | 不动 | 只读客户端以子类隔离，不改基类 |
| `shared/capital_manager.py` / `shared/position_baseline.py` / `shared/database.py` / `shared/trade_logger.py` / `shared/notification.py` / `shared/kline_service.py` | 不动 | 全部复用 |
| `services/kline_service/core/collector.py` / `core/registry.py` / `api/routes.py` | 不动（注册走 DB，无需改默认值） | 仅人工执行注册动作 |

---

## 12. 风险与回退

| 风险 | 影响 | 缓解/回退 |
|------|------|-----------|
| analyze 拆分引入语义漂移 | 生产信号改变 | 等价回归（§3.4）+ 灰度前跑 `test_analyze_split.py`；回退 = revert 该次 commit，两生产镜像回滚 |
| 只读边界被未来写方法绕过 | 影子误下单 | 底层 `_request` 白名单收口 + 测试锁定；上线前安全验收 |
| XAU TradFi 非活跃时段 1m 缺口 | 回放不完整 | 快照缺口检查，不足标记不完整、不得下级准入结论 |
| 观察期信号不足（<12 笔） | 结论「样本不足，暂不准入」 | 依 spec，不放宽参数、不缩短观察期 |
| 影子容器故障 | 不阻塞生产 | 独立 service，`restart: unless-stopped` 仅限本服务；回退仅 `stop` |
| 残留容器与 compose 同名冲突 | deploy 报错 | deploy.yml `RESIDUAL_CANDIDATES` 追加新容器名 |
| 写库失败被误当决策失败 | 采集空洞 | 写库失败只告警不回退下单，继续下一周期 |

---

## 13. 实施顺序（与 spec §16 对齐）

| spec §16 | 本方案步骤 |
|----------|-----------|
| 1 行情侧前置 | kline-service 注册 XAUUSDT `1h/4h/1d`，确认输入可用（§8） |
| 2 参数化 + 回归锁定 | §1 实例参数化 + §3.4 等价回归 |
| 3 只读安全边界 | §2 + `test_readonly_client.py` |
| 4 拆分无状态/有状态 | §3 analyze 拆分 |
| 5 research schema + 幂等写入 | §4 + `12-research-schema.sql` + `repository.py` |
| 6 两冻结 profile + XAU 配置 | §5.5 冻结 config（`xau_original/xau_aggressive`） |
| 7 本地回放器 + 报告 | §6 + §7 |
| 8 本地快照/manifest + 基线回测 + 安全验收 | §6.1 `snapshot.py` + 本地基线 + §13.1 安全测试 |
| 9 部署影子实例 | §9 compose 改动 + push 观察 Actions |
| 10 准入报告 | 观察达标后 §7 准入判定 |
| 11 独立设计 `mtcps_xau_pilot` | 仅准入后单独设计（本方案不落地） |

---

## 14. 待实施核实项 / 与 spec 不一致处

### 14.1 待实施核实（不得臆造）

1. **最大单笔保证金 30 USDT 上限是否需新增配置键**：现有仅有 `trading.total_position_margin_limit: 210`（总额约束，非单笔上限）；是否新增 `instance` 或 `pilot` 下的 `max_single_position_margin_usdt: 30` 待实施核实。此值用于**回放器的有状态门槛**，不进入影子采集（采集不落 quantity）。
2. **激进版 strategy.py 的 analyze 拆分线性同步量**：`btc_eth_aggressive/strategy.py` 是从原版复制的独立文件（含自身 `_build_indicators` 等），拆分需两处同步；是否可在本分支先行只对原版拆分、激进版复用同一基类待实施核实（本项目目前两版是复制关系而非继承关系）。
3. **XAU markPrice/indexPrice**：本期分析与回放均只用成交价（kline），不接标记价；若准入阶段要求评估强平/保证金比例，需另行接只读 `markPrice` 端点，本期不做。
4. **`shadow_replay_trades` 落库通道**：本地回放器默认不连生产库；该表是否由「本地产出导入文件 → 服务器 `shadow/import.py` 写回」或「显式只读凭据可选写回」，需在实施阶段与用户确认；表 DDL 已在 §4 建好，不影响先落本地报告。

### 14.2 与 spec 不一致/需补充

1. spec §6.3 提到 `research.shadow_replay_trades` 写生产 schema，而 spec §14.1 要求回放不连生产库——两者存在张力，故本方案将其落库通道外置为「可选导入」（§14.1.4），默认本地 JSON/SQLite，避免自相矛盾。
2. spec 未明确回放器代码物理位置；本方案落 `backtest/btc_eth/replay/`（已被 `.dockerignore` 排除、天然不部署、遵循「禁止服务器回测」）。若团队希望改放 `tools/shadow_replay/` 亦可，仅移动路径，不影响接口契约。
3. spec 未提及 deploy.yml 的 `RESIDUAL_CANDIDATES` 需追加新 shadow 容器名；本方案补上这处必要修改（否则独立 `docker run` 残留会与 compose 同名冲突），属对 spec 的必要补全而非冲突。

---

**需要同步的既有文档**：`docs/README.md`（新建 plans 索引）、`docs/plans/项目需求迭代文档.md`（XAU 影子里程碑）、项目 `CLAUDE.md`（容器清单新增 `mtcps-xau-shadow`）。上述文档更新属 code-document-curator 环节，不在本方案文件改动清单内，实施时由流程触发。