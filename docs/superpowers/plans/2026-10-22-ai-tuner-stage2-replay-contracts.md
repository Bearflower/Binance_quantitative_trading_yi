# AI Tuner 阶段二：双策略确定性回放与资格考试 契约设计

> **状态**：契约设计（只读设计文档，不包含实现代码）
> **上游需求**：`docs/superpowers/plans/2026-10-17-ai-tuner-mvp-replay-qualification.md`（Task 0–10 双策略口径，D1–D4 决策）
> **并行背景**：`docs/superpowers/plans/2026-10-21-ai-tuner-stage2-parallel-readiness.md`
> **定位**：本文定义模块职责、数据模型、as_of 契约、Broker 撮合、报告 schema、适配器协议、等价提取、配置项、测试矩阵与风险。实现阶段（Task 1–10）必须逐条对照本文，不得在代码中重写契约口径。

---

## 0. 全局约束（实现必须遵守）

| 编号 | 约束 | 落点 |
|---|---|---|
| C1 | 本地离线优先：回放/对账/考试只在开发机执行，运行期**不连 Postgres**、不在容器内回放 | `data_portal` 只读本地快照目录；`engine` 无 DB 依赖 |
| C2 | 快照 = 本地目录 + manifest（SHA-256），由只读导出脚本生成到 `var/replay/`（git 忽略） | `scripts/export_replay_snapshot.py` 仅 SELECT / COPY TO STDOUT |
| C3 | 金额/价格/数量全用 `decimal.Decimal`；禁止 float 参与撮合与盈亏 | `models.py` / `broker.py` / `metrics` |
| C4 | 运行期 Python 3.9（容器 3.10），禁用 3.10+ 语法（`match`、`X \| Y` 联合类型、注解中的 `list[int]` 运行时求值） | 所有新增模块 |
| C5 | 相同输入重复执行，六个报告文件逐字节一致；canonical JSON = `sort_keys=True`、`separators=(",",":")`、Decimal→字符串、UTF-8、末尾单个换行 | `artifacts.py` |
| C6 | 阶段二只输出 `PASS`/`FAIL`/`BLOCKED_DATA`，不写覆盖层、不改 `auto_apply`、不降 readiness 阈值、不触发自动生效 | `qualification/runner.py` |
| C7 | MTPCS 等价纯函数提取范围仅限评分/方向/入场过滤/止损止盈退出；流程 = characterization 冻结 → 移动 → 双端共用 → 逐字段一致 | `strategies/btc_eth_aggressive/core/` |
| C8 | HRS 未获提取授权，适配器只调现有 `strategies/hrs/*`；三表零行 → `BLOCKED_DATA` | `adapters/hrs.py` |

---

## 1. 模块职责表

> 路径基准为仓库根。新增文件分为：回放内核、适配器、市场状态、资格考试、金样对账、等价提取、导出脚本七组。

### 1.1 回放内核 `ai_tuner/replay/`

| 文件 | 单一职责 | 禁止职责 |
|---|---|---|
| `__init__.py` | 声明 `ENGINE_VERSION` / `COST_MODEL_VERSION` 两个常量（进入 run_manifest） | 不含业务逻辑 |
| `models.py` | 定义全部冻结 Decimal 数据模型（dataclass frozen），见 §2 | 不做 IO；不 import 策略代码；不 import `backtest.*`；不用 float 存波动字段 |
| `clock.py` | 确定性事件排序：输入为「事件流」，输出按 `(event_time, priority, seq)` 全序；事件时间来自快照内容，不读系统时钟 | 不读 `datetime.now()`；不用 `random`；不引入集合/dict 遍历顺序导致的非确定性 |
| `data_portal.py` | `as_of` 强制 + `FutureDataAccess` 异常 + 覆盖度检查；唯一数据读取面（bars/universe/external_snapshot/funding_rates/trade_signals/config_version），见 §3 | 不回填；不 fallback 到「当前值」；不越过 as_of 返回数据 |
| `broker.py` | 统一虚拟 Broker：市价/限价/止损/止盈/reduce-only/部分平仓、费用/滑点/资金费/精度/保证金撮合，见 §4 | 不读策略 config；不 import 策略代码；不用 float 计算；不产生随机成交价 |
| `engine.py` | 共享事件循环：推进时钟 → 依据 adapter `intents` → 调用 broker → 写 audit trail → 记录盯市权益 | 不直接调用策略的下单方法；不 import `backtest/btc_eth/replay/engine`；不重算 score（score 由 adapter 提供） |
| `artifacts.py` | 六个不可变报告的 canonical 序列化与原子写（`.tmp` + `os.replace`），见 §5 | 不用 `indent=2` 美化；不写生产表；不依赖 dict 插入顺序 |

### 1.2 适配器 `ai_tuner/replay/adapters/`

| 文件 | 单一职责 | 禁止职责 |
|---|---|---|
| `base.py` | 定义 `ReplayAdapter` 抽象协议 + `QualificationBlocked`/`DataBlocked` 异常类型，见 §6 | 不含任何策略计算逻辑 |
| `mtpcs_aggressive.py` | 把实盘 `_build_indicators` + `strategies/btc_eth_aggressive/core/*` 纯函数串成「K线→指标→方向→过滤→评分→等级→意图」管线，输出 `OrderIntent` | 不 import `strategies/btc_eth_aggressive/strategy.py`（须只 import core）；不 import `backtest.*`；不在回放目录手抄策略逻辑 |
| `hrs.py` | 仅调 `strategies/hrs/scoring_engine.py` / `strategy.py` 现有模块 + 消费三张历史表快照；缺数据即 `BLOCKED_DATA` | 不复刻 HRS 评分逻辑；不实时补值；不假造 universe/外部快照 |

### 1.3 市场状态 `ai_tuner/regime/`

| 文件 | 单一职责 | 禁止职责 |
|---|---|---|
| `__init__.py` | 导出公开 API | — |
| `features.py` | 从**已收盘** K 线计算 regime 特征（区间涨跌幅、已实现波动率/ATR 分位） | 不读未来 K 线；不联网 |
| `classifier.py` | 把特征映射为 6 类（涨/跌/震荡 × 正常/高波动）+ `STRESS`，阈值全走配置 | 不把 regime 变成交易条件；不输出到 broker 之外的决策 |

### 1.4 资格考试 `ai_tuner/qualification/`

| 文件 | 单一职责 | 禁止职责 |
|---|---|---|
| `__init__.py` | 导出公开 API | — |
| `metrics.py` | 扣费后净收益、最大回撤、Sortino、Calmar、单笔期望、胜率、盈亏比、有效信号数、已平仓交易数、收益集中度、分币种/分 regime 表现、1.5×/2× 成本压力、相邻扰动、淘汰原因 | 指标公式不得复制 `shared.trade_logger.TradeLogger.calculate_pnl`（盈亏统一委托它） |
| `gates.py` | 10 项硬门禁判定（阈值全走 `config.yaml` 的 `qualification.gates`），见 §5.4 | 不在代码写死阈值；样本不足不得判 `FAIL`（归 `BLOCKED_DATA`） |
| `runner.py` | 编排「同场同数据」多候选比较（当前覆盖层 + 1 个 LLM 候选 + ≤3 个历史覆盖层），只出 `PASS`/`FAIL`/`BLOCKED_DATA` | 不跨策略比较；不选「冠军」；不写覆盖层/`.active`/`auto_apply` |

### 1.5 金样对账 `ai_tuner/parity/`

| 文件 | 单一职责 | 禁止职责 |
|---|---|---|
| `__init__.py` | 导出公开 API | — |
| `golden.py` | 冻结实盘信号/入场/退出/费用/盈亏为 golden 基线，与回放结果逐字段比对，产出 `parity_report.json` | 不在代码内放宽容差；样本不足记 `partial_parity`+`BLOCKED_DATA` 而非 `FAIL` |

### 1.6 等价提取 `strategies/btc_eth_aggressive/core/`

| 文件 | 单一职责 | 禁止职责 |
|---|---|---|
| `__init__.py` | 导出公开 API | — |
| `scoring.py` | 趋势强度、形态质量、动量背离、综合评分（配置作为参数传入） | 读 `self.xxx_config`；logger；DB；下单 |
| `direction.py` | 方向投票（MA/MACD 多时间框架） | 读实例属性；任何副作用 |
| `filters.py` | 趋势过滤器、禁止入场条件中**无 IO 部分**、突破回踩判定 | IO（资金费率/24h 涨跌幅这类 `self.binance` 调用**不得**进 core，见 §7.1） |
| `exits.py` | 止盈/止损/移动止损/时间退出价格计算公式 | 读交易所精度（精度由 broker 层负责）；改公式/默认值/异常语义 |

> 说明：`core/` 目录已随 `strategies/btc_eth_aggressive/Dockerfile:20` 的 `COPY strategies/btc_eth_aggressive/` 整目录打包，实盘镜像自动包含，无需改 Dockerfile。

### 1.7 导出脚本

| 文件 | 单一职责 | 禁止职责 |
|---|---|---|
| `scripts/export_replay_snapshot.py` | 只读导出到 `var/replay/snapshots/<strategy>/<as_of>/`，仅 SELECT / COPY TO STDOUT，写 manifest（≥10 项） | 不写生产表；不 UPDATE/DELETE；不 import `backtest.*` |

---

## 2. 数据模型字段表

> 全部为 `@dataclass(frozen=True)`；Decimal 字段在 JSON 中一律带引号存字符串。下表「单位」列对 Decimal 明确精度要求。

### 2.1 Bar（一根已收盘 K 线）

| 字段 | 类型 | 单位/格式 | 不变式 |
|---|---|---|---|
| `symbol` | `str` | 如 `BTCUSDT` | 非空，与 UniverseSlice 一致 |
| `interval` | `str` | `1h`/`4h`/`1d` | 属于该策略声明的 intervals |
| `open_time` | `datetime`(aware UTC) | ISO 8601 | `close_time == open_time + interval` 且区间闭合 |
| `close_time` | `datetime`(aware UTC) | ISO 8601 | `close_time <= as_of`（越界由 data_portal 拦截） |
| `open` / `high` / `low` / `close` | `Decimal` | USDT 价格 | `low <= open,close <= high`；`high >= low` |
| `volume` | `Decimal` | 基础币数量 | `>= 0` |
| `quote_volume` | `Decimal` | USDT | `>= 0` |
| `trades` | `int` | 笔数 | `>= 0` |

### 2.2 UniverseSlice（某段时间内的可交易集合）

| 字段 | 类型 | 单位/格式 | 不变式 |
|---|---|---|---|
| `strategy_id` | `str` | — | 非空 |
| `valid_from` / `valid_to` | `datetime`(UTC) | — | `valid_from < valid_to`；`valid_to <= as_of`（历史切片） |
| `symbols` | `tuple[str, ...]` | — | 有序、去重（顺序决定遍历顺序，写入快照即冻结） |
| `intervals` | `tuple[str, ...]` | — | 非空、去重 |
| `source` | `str` | `strategy_config`/`historical_snapshot` | 枚举见 readme universe 段 |
| `config_hash` | `str` | SHA-256 hex | 长度 64 |

### 2.3 ExternalSnapshot（HRS 外部时点快照）

> 对齐 `public.ai_tuner_market_snapshots` 表结构（见并行计划 §4.3）。

| 字段 | 类型 | 单位/格式 | 不变式 |
|---|---|---|---|
| `strategy_id` / `symbol` | `str` | — | — |
| `observed_at` / `bucket_start` | `datetime`(UTC) | — | `bucket_start` 为整小时桶；`source_observed_at <= bucket_end` |
| `open_interest_usd` | `Decimal` | USDT | 完整快照要求 `> 0` |
| `funding_rate` | `Decimal` | 每 8h 结算率 | 完整要求非空 |
| `market_cap_usd` | `Decimal` | USDT | 完整要求 `> 0` 且来源 `coingecko`/`cache_hit` |
| `oi_market_cap_ratio` | `Decimal` | 比例 | 可由同点输入重算 |
| `sentiment_score` | `Decimal` | — | 公式版本 `funding_annual_pct_v1` 固定 |
| `oi_source` / `funding_source` / `market_cap_source` | `str` | — | 枚举；`oi_proxy`/`missing` 不得计为完整 |
| `payload_hash` | `str` | SHA-256 hex | 64 |
| `is_complete` | `bool` | — | 仅当上述完整条件全满足 |

### 2.4 OrderIntent（适配器产出的「想做什么」）

| 字段 | 类型 | 单位/格式 | 不变式 |
|---|---|---|---|
| `strategy_id` / `symbol` | `str` | — | — |
| `seq` | `int` | — | 同策略内单调递增（来自事件序列，非系统时钟） |
| `side` | `str` | `LONG`/`SHORT` | 枚举 |
| `order_type` | `str` | `MARKET`/`LIMIT`/`STOP`/`TAKE_PROFIT` | 枚举 |
| `trigger_price` | `Decimal \| None` | USDT | `STOP`/`TAKE_PROFIT`/`LIMIT` 必填；`MARKET` 为 None |
| `limit_price` | `Decimal \| None` | USDT | `LIMIT` 必填（触发价 + offset 后的挂单价） |
| `quantity` | `Decimal` | 基础币 | `> 0` 且为 `stepSize` 整数倍（撮合后保证） |
| `reduce_only` | `bool` | — | `True` 时不允许增大净持仓；与 `close_ratio` 语义配合 |
| `close_ratio` | `Decimal` | 比例 `(0,1]` | `1` = 全平；`<1` = 部分平仓 |
| `grade` | `str \| None` | `S`/`A`/`B`/`C` | 仅 MTPCS 有，用于退出参数 |
| `reason` | `str` | — | 非空（进入 audit） |

### 2.5 Order（Broker 内部，Intent + 生命周期）

| 字段 | 类型 | 单位/格式 | 不变式 |
|---|---|---|---|
| `order_id` | `str` | 稳定 ID（`<strategy>.<seq>`） | 全局唯一（同一回放内） |
| `intent` | `OrderIntent` | — | — |
| `status` | `str` | `OPEN`/`FILLED`/`CANCELLED`/`EXPIRED` | 单次状态迁移 |
| `created_at` | `datetime`(UTC) | — | 来自事件时钟 |

### 2.6 Fill（一笔成交）

| 字段 | 类型 | 单位/格式 | 不变式 |
|---|---|---|---|
| `order_id` | `str` | — | 引用 Order |
| `fill_price` | `Decimal` | USDT | 为 `tickSize` 整数倍 |
| `quantity` | `Decimal` | 基础币 | `> 0` 且为 `stepSize` 整数倍 |
| `fee` | `Decimal` | USDT | `< 0`（支出）；`= quantity*price*taker_bps/10000` |
| `slippage_cost` | `Decimal` | USDT | `< 0`（支出），见 §4.4 |
| `ts` | `datetime`(UTC) | — | 来自事件时钟 |

### 2.7 Position（当前持仓）

| 字段 | 类型 | 单位/格式 | 不变式 |
|---|---|---|---|
| `position_id` | `str` | — | 稳定唯一 |
| `symbol` / `direction` | `str` | — | — |
| `entry_price` | `Decimal` | USDT | 加权成本价；`> 0` |
| `entry_quantity` | `Decimal` | 基础币 | 初始开仓数量 |
| `current_quantity` | `Decimal` | 基础币 | `0 <= current_quantity <= entry_quantity`；`=0` 即全平 |
| `atr` | `Decimal` | USDT | 入场时冻结，用于 stop/tp |
| `grade` | `str` | `S`/`A`/`B`/`C` | — |
| `stop_loss_price` / `tp1_price` / `tp2_price` | `Decimal \| None` | USDT | 由 `core/exits.py` 计算写入 |
| `trailing_stop_price` | `Decimal \| None` | USDT | 激活后存在 |
| `trailing_activated` / `tp1_hit` / `tp2_hit` | `bool` | — | 单向置位 |

### 2.8 ClosedTrade（已平仓（部分平仓）批次）

| 字段 | 类型 | 单位/格式 | 不变式 |
|---|---|---|---|
| `trade_id` | `str` | — | 稳定唯一 |
| `position_id` / `symbol` / `direction` | `str` | — | — |
| `entry_price` / `exit_price` | `Decimal` | USDT | — |
| `quantity` | `Decimal` | 基础币 | `> 0` |
| `realized_pnl` | `Decimal` | USDT | `= TradeLogger.calculate_pnl(direction, entry, exit, qty)`（禁重复实现） |
| `fee_paid` / `slippage_cost` | `Decimal` | USDT | `< 0` |
| `funding_fee` | `Decimal` | USDT | 持仓跨越结算点的合计（正=收） |
| `net_pnl` | `Decimal` | USDT | `= realized_pnl + fee_paid + slippage_cost + funding_fee` |
| `exit_reason` | `str` | `STOP_LOSS`/`TP1`/`TP2`/`TRAILING_STOP`/`TIME_STOP`/`REDUCE_ONLY` | 用于同根 K 线保守顺序判定 |
| `entry_time` / `exit_time` | `datetime`(UTC) | — | `entry_time <= exit_time <= as_of` |
| `mae` / `mfe` | `Decimal` | USDT | 非负 |

### 2.9 EquityPoint（盯市曲线点）

| 字段 | 类型 | 单位/格式 | 不变式 |
|---|---|---|---|
| `ts` | `datetime`(UTC) | — | 单调不降 |
| `realized_equity` | `Decimal` | USDT | 已实现权益 |
| `marked_equity` | `Decimal` | USDT | `realized_equity + 未实现盈亏`（最大回撤以此为准） |
| `has_open_position` | `bool` | — | — |

> 资金费结算点：每次 crossing 8h funding 时间戳，追加一条 `EquityPoint`（不做交易、只计提 funding_fee），保证资金费进入盯市曲线与净盈亏。

### 2.10 ReplayResult

| 字段 | 类型 | 说明 |
|---|---|---|
| `manifest_ref` | `str` | 快照 manifest SHA-256 |
| `equity_curve` | `tuple[EquityPoint, ...]` | 有序 |
| `closed_trades` | `tuple[ClosedTrade, ...]` | 有序 |
| `final_position` | `Position \| None` | 截止未平仓 |
| `audit_events` | `tuple[dict, ...]` | 依 `(ts, seq)` 有序 |
| `invariant_violations` | `tuple[str, ...]` | 空 = 通过 |

---

## 3. as_of 契约表

> 所有读取接口显式携带 `as_of`；越界抛 `FutureDataAccess`（自定义异常，继承 `RuntimeError`）；缺数据抛结构化覆盖度不足 → 归 `BLOCKED_DATA`。`FutureDataAccess` 一旦抛出**不捕获、不降级**，沿调用栈传播到 `runner`，整场资格考试失败并写 `audit_events.jsonl` 证据（exit code 由 FAIL 语义承载，见 §5.5）。

| 接口 | 输入文件/表（快照内） | 过滤条件 | 越界行为 | 缺失行为 |
|---|---|---|---|---|
| `bars(strategy, symbol, interval, as_of)` | `<strategy>/<as_of>/klines/<symbol>_<interval>.jsonl`（本地） | `close_time <= as_of` | `FutureDataAccess` | 覆盖度不足 → `BLOCKED_DATA`，**禁止**用当前 API 价填补 |
| `universe(strategy, as_of)` | `universe_slices.json`（本地）或历史 universe 快照 | `valid_to <= as_of`；历史切片 `observed_at <= as_of` | `FutureDataAccess` | HRS 无历史 → `historical_universe_missing`，禁回退 `public.strategy_states` 当前值 |
| `external_snapshot(strategy, symbol, as_of)` | `market_snapshots.jsonl`（本地） | `bucket_end <= as_of`；`source_observed_at <= bucket_end` | `FutureDataAccess` | 该 symbol-hour 无完整快照 → 不计入 95% 覆盖率；`oi_proxy`/`missing` 不算 |
| `funding_rates(strategy, symbol, as_of)` | `funding_rates.jsonl`（本地） | `fundingTime <= as_of` 且属于窗口内 | `FutureDataAccess` | 缺口 → `BLOCKED_DATA`（资金费影响净盈亏，不可近似为 0） |
| `trade_signals(strategy, as_of)` | `trade_signals.jsonl`（本地，由导出脚本按有效状态过滤后冻结） | `signal_time <= as_of` | `FutureDataAccess` | 有效信号 `< min_effective_signals` → 样本不足 `BLOCKED_DATA`（不判 `FAIL`） |
| `config_version(strategy, as_of)` | `<strategy>/<as_of>/config_hashes.json` | 记录 `base_config_hash` / `params_manifest_hash` / `overlay_hash` | 版本列越界 → `BLOCKED_DATA`（版本兼容门禁失败） | 任一哈希缺失 → `BLOCKED_DATA` |

**FutureDataAccess 传播方式**：`data_portal` 抛 → `engine` 不捕获 → `runner` 顶层捕获后固定写入 `audit_events`（类型 `future_data_access`，含 `interface` / `as_of` / `offending_field` / `offending_value`）→ 报告 `qualification_decision.status=FAIL`（数据访问违规属框架级失败，强于普通门禁失败），`data_quality.future_data_violations += 1`。

---

## 4. Broker 撮合规则

### 4.1 成交价规则

| 订单类型 | 成交价公式 | 备注 |
|---|---|---|
| `MARKET` | 入场：下一根 K 线 `open` 按方向保守滑点：`LONG: open*(1+s/10000)`，`SHORT: open*(1-s/10000)` | 复用 `market_fill_price` 口径（`matching.py:76`） |
| `LIMIT` | 一根 K 线内 `low <= limit（LONG）` 或 `high >= limit（SHORT）` → 以 `limit_price` 成交 | 穿越判据复用 `price_crosses_limit`（`matching.py:56`） |
| `STOP` | 触发价穿越后，成交价 = 触发价向不利方向偏移 `stop_limit_offset_pct`（LONG 向下、SHORT 向上），复用 `_apply_limit_offset` 口径 | 保证触发后必成交 |
| `TAKE_PROFIT` | 触发价穿越后，成交价 = 触发价向有利方向偏移 `tp_limit_offset_pct`（LONG 向上、SHORT 向下） | 同 `_apply_limit_offset` 反向 |

- 精度：成交价再经 `round_price`（ROUND_HALF_UP 到 `tickSize`）；数量经 `round_quantity`（ROUND_DOWN 到 `stepSize`）；`minNotional` 不满足则拒绝（`clamp_notional` + 撮合前校验）。三组纯函数搬迁复用 `backtest/btc_eth/replay/matching.py`，**搬迁实现、不 `import backtest`**。

### 4.2 reduce-only 语义

- `reduce_only=True` 时：撮合后 `current_quantity` 不得低于 0 或高于 `entry_quantity`；超出的数量被裁剪为该持仓剩余量（裁剪记 audit）。
- `close_ratio` 决定部分平仓数量：`close_qty = round_quantity(current_quantity * close_ratio, step_size)`；`close_qty == 0`（精度舍到 0）→ 不成交并记 `REDUCE_TO_ZERO` 警告。

### 4.3 同根 K 线止盈止损的确定性保守顺序（写死，不许随机）

一根 K 线内同时穿越多个退出条件时，按**固定优先级**逐条判定，命中即停：

| 优先级 | 退出条件 | 成交价 | 理由 |
|---|---|---|---|
| 1 | `STOP_LOSS`（硬止损） | 止损触发价偏移 | 不利方向优先，最保守 |
| 2 | `TP1`（部分止盈） | 止盈触发价偏移 | 部分平仓后剩仓继续判定 |
| 3 | `TP2` | 止盈触发价偏移 | — |
| 4 | `TRAILING_STOP`（移动止损） | 触发时 `trailing_stop_price` | — |
| 5 | `TIME_STOP`（时间退出） | 该根 `close` | — |

> 依据：现 XAU 引擎 `_check_and_close_on_bar`（`engine.py:833`）已固定「止损先于止盈」，本设计沿袭并扩展为 5 级；「无法判序 → 取更差结果（止损先成交）」是保守口径。TP1 触发的部分平仓不影响同根 K 线剩余持仓继续按 3/4/5 级判定。

### 4.4 费用 / 滑点 / 资金费公式

| 项 | 公式 | 来源 |
|---|---|---|
| taker 手续费（每边） | `fee = -quantity * fill_price * taker_bps / 10000` | `fees.py:17` |
| 滑点（每边） | `slippage = -quantity * fill_price * slippage_bps / 10000` | `fees.py:35` |
| 资金费（每 8h 结算点） | `contribution = -quantity * entry_price * funding_rate`（LONG）；`+`（SHORT） | `fees.py:64`，mark_price 近似取 `entry_price` |
| 单笔净盈亏 | `TradeLogger.calculate_pnl(...) + fee + slippage + funding` | 委托 `shared.trade_logger`，禁重复实现 |

### 4.5 成本档 1.5× / 2× 计算公式

- 定义**每边有效成本 bps**：`cost_bps(m) = round((taker_bps + slippage_baseline_bps) * m)`，其中 `m ∈ {1.0, 1.5, 2.0}`。
- 三档独立重放同一输入：`m=1.0`（基线）、`m=1.5`、`m=2.0`；各档有效成本分别作用于入出场手续费与滑点（费用、滑点同乘 `m`，资金费不变）。
- 分摊：`fee_bps(m) = round(taker_bps * m)`，`slippage_bps(m) = round(slippage_baseline_bps * m)`，均配置注入，禁止在 broker 硬编码 `1.5/2.0`。

### 4.6 成本单调性不变量的判定方式

对同一 `(strategy, snapshot, params)` 三档重放：

```text
final_equity(m=1.0) >= final_equity(m=1.5) >= final_equity(m=2.0)
```

- 违反任一不等式 → 报告 `scorecard.cost_monotonicity_violation = true`，并作为 `1.5×/2×` 成本压力门禁的失败项。
- 比较在 `Decimal` 上执行；`m=1.0` 档的 `final_equity` 用 `marked_equity` 最后一个点（对齐最大回撤口径）。

---

## 5. 报告 JSON schema

> 六个文件均由 `artifacts.py` 用 canonical 口径写出。顶层键、字段名、枚举值如下；`Decimal` 一律字符串。

### 5.1 `run_manifest.json`

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `engine_version` | `str` | ✓ | `ENGINE_VERSION` |
| `cost_model_version` | `str` | ✓ | `COST_MODEL_VERSION` |
| `strategy_id` | `str` | ✓ | — |
| `as_of` | `str` | ✓ | UTC ISO Z |
| `snapshot_manifest_sha256` | `str` | ✓ | 快照顶层 SHA-256 |
| `code_commit_sha` | `str` | ✓ | 回放代码提交 SHA |
| `config_hashes` | `object` | ✓ | `base_config_hash`/`params_manifest_hash`/`overlay_hash` |
| `window` | `object` | ✓ | `diagnostic_weeks`/`blind_weeks`/`start`/`end` |
| `initial_equity_usdt` | `str` | ✓ | Decimal |
| `candidate_refs` | `array[str]` | ✓ | `qualify` 时列出所有候选覆盖层引用 |
| `status` | `str` | ✓ | `PASS`/`FAIL`/`BLOCKED_DATA` |

### 5.2 `data_quality.json`

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `data_complete` | `bool` | ✓ | 缺任一必需数据 → false |
| `future_data_violations` | `int` | ✓ | `>0` → 状态 `FAIL`（框架级） |
| `kline_coverage` / `external_snapshot_coverage` | `str` | ✓ | 百分数字符串（Decimal） |
| `missing_symbol_intervals` | `array` | ✓ | 逐 symbol/interval 缺口 |
| `universe_gaps` | `array[str]` | ✓ | 如 `historical_universe_missing_before` |
| `funding_gaps` | `array` | ✓ | 影响净盈亏，不可近似 0 |
| `proxy_ignored` | `int` | ✓ | `oi_proxy`/`missing` 未计入覆盖的条数 |
| `status` | `str` | ✓ | 数据级 `OK`/`BLOCKED_DATA` |

### 5.3 `parity_report.json`

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parity_status` | `str` | ✓ | `match`/`mismatch`/`partial_parity`/`no_baseline` |
| `golden_snapshot_ref` | `str` | ✓ | 冻结实盘基线 ref |
| `compares` | `array` | ✓ | 每项 `{field, golden, replay, tolerance, pass}` |
| `field_schema` | `array[str]` | ✓ | 比对字段：方向/时间/价格/退出原因/费用/盈亏/权益 |
| `sample_count` | `int` | ✓ | 真实信号/交易数 |
| `tolerance` | `object` | ✓ | 价格/时间/盈亏容差（来自 config，代码不写死） |
| `status` | `str` | ✓ | `PASS`/`FAIL`/`BLOCKED_DATA` |

**状态传播**：`partial_parity`（样本不足）→ 状态 `BLOCKED_DATA`，**不得**记 `FAIL`；`mismatch`（金样含量不匹配，非样本不足）→ `FAIL`。

### 5.4 `scorecard.json`

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `net_pnl_after_costs` | `str` | ✓ | Decimal USDT |
| `max_drawdown_pct` | `str` | ✓ | Decimal |
| `sortino` / `calmar` | `str` | ✓ | Decimal |
| `expectancy` | `str` | ✓ | Decimal USDT |
| `win_rate` / `profit_factor` | `str` | ✓ | Decimal |
| `effective_signals` | `int` | ✓ | 有效信号数（来自 trade_signals） |
| `closed_trades` | `int` | ✓ | 已平仓交易数 |
| `concentration` | `str` | ✓ | 收益集中度（单一最佳单占比等） |
| `per_symbol` / `per_regime` | `object` | ✓ | 分币种/分 regime 表现 |
| `cost_tiers` | `object` | ✓ | `{baseline, 1.5x, 2.0x}` 各档终值权益 |
| `adjacent_perturbation` | `object` | ✓ | 相邻参数扰动稳定性 |
| `elimination_reasons` | `array[str]` | ✓ | 全部淘汰原因（空=全部通过） |
| `cost_monotonicity_violation` | `bool` | ✓ | §4.6 判定结果 |
| `gates` | `object` | ✓ | 10 项门禁 `{name: {threshold, actual, pass}}` |

### 5.5 `qualification_decision.json`

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `strategy_id` | `str` | ✓ | — |
| `candidate_ref` | `str` | ✓ | 本次候选覆盖层引用 |
| `status` | `str` | ✓ | `PASS`/`FAIL`/`BLOCKED_DATA` |
| `gates_passed` | `int` | ✓ | — |
| `gates_total` | `int` | ✓ | `10` |
| `blocked_reason` | `str \| null` | — | `BLOCKED_DATA` 时非空 |
| `diagnostic_only` | `bool` | ✓ | readiness 未达标时跑框架自测 = true，带 `NOT_QUALIFICATION_EVIDENCE` |
| `blind_test_excluded` | `bool` | ✓ | 盲测区结果不进入 LLM（固定 true） |

**退出码**：`0`=PASS、`2`=FAIL、`3`=BLOCKED_DATA、`1`=运行时错误。

**状态传播规则（贯穿六文件）**：

1. `BLOCKED_DATA` 优先于 `FAIL`——数据缺失/样本不足一律 `BLOCKED_DATA`，不写「策略 FAIL」。
2. 样本不足 ≠ `FAIL`（D4 强制）。
3. `FutureDataAccess` → `FAIL`（框架级违规，唯一例外于规则 1）。
4. 六文件顶层 `status` 必须一致，否则视为序列化 bug（由 `artifacts.py` 断言）。

### 5.6 `audit_events.jsonl`

- 每行一个 JSON 对象，行序按 `(timestamp, seq)` 固定；字段：`ts`、`seq`、`type`、`payload`。
- `type` 枚举：`order_intent` / `fill` / `reject` / `funding_settlement` / `equity_mark` / `invariant_violation` / `future_data_access` / `data_blocked` / `gate_fail`。
- 末尾单个换行；无事件时文件至少含一行 `{"type":"run_start",...}`（保证非空、可逐字节比对）。

---

## 6. 适配器协议

### 6.1 `ReplayAdapter` 抽象方法签名

```python
class ReplayAdapter(Protocol):
    def prepare(self, data: DataPortal) -> PreparedContext: ...
        # 一次性加载 as_of 前的 universe/config 指纹；校验必需数据，缺失抛 DataBlocked

    def on_bar(self, bar: Bar, ctx: PreparedContext) -> None: ...
        # 消费一根已收盘 K 线，更新内部指标状态（不得返回未来信息）

    def on_snapshot(self, snap: ExternalSnapshot, ctx: PreparedContext) -> None: ...
        # HRS 专用：消费一个外部时点快照（MTPCS 恒空实现，抛 NotImplemented 视为缺数据）

    def intents(self, ctx: PreparedContext) -> tuple[OrderIntent, ...]: ...
        # 从当前指标状态产出意图（入场/减仓/离场），保持确定性序
```

- `DataBlocked`（自定义异常）携带结构化 `gaps`，被 `engine` 捕获后转 `BLOCKED_DATA`。
- 适配器**不得**持有 Broker 引用、不得直接撮合、不得读系统时间。

### 6.2 数据依赖矩阵

| 输入 | MTPCS 激进版 | HRS |
|---|---|---|
| `bars`（1h/4h/1d） | 必需；缺任一 interval → `BLOCKED_DATA` | 必需 |
| `universe` | 静态 `strategy_config`（`strategy.symbols`）；缺失 → `BLOCKED_DATA` | 历史 `historical_snapshot`；无历史 → `BLOCKED_DATA` |
| `external_snapshot` | 不依赖（`not_required_for_current_manifest`） | **必需**；覆盖率 `< 0.95` → `BLOCKED_DATA` |
| `funding_rates` | 必需（影响资金费净盈亏） | 必需 |
| `trade_signals` | 必需（有效信号 `< 100` → `BLOCKED_DATA`） | 必需 |
| `config_version` | 必需（版本列） | 必需 |

> 矩阵判定顺序即缺数据优先级：先 `universe` → `bars` → `funding_rates` → `external_snapshot`（HRS）→ `trade_signals` → `config_version`，任一缺即 `BLOCKED_DATA`，不继续计算。

---

## 7. 等价提取设计（MTPCS 激进版）

### 7.1 提取边界（硬性）

- **可提取**：纯计算、读入参数 = 输入参数 + 从 config 显式透传的只读配置，输出为值。无 `self` 实例属性、无 logger、无 DB、无网络。
- **不可提取（留在 strategy.py）**：任何 `await self.binance.*` / `self.kline_service.*` / DB 读写 / 下单 / 取消单 / 精度获取缓存（`_get_symbol_precision`，因为精度归 broker 层从 exchangeInfo 提供，回放路径不复用该 async 方法）。

### 7.2 核心模块与方法清单

| core 模块 | 待提取方法 | 源行号 | 入参 | 出参 | DB/IO 耦合 |
|---|---|---|---|---|---|
| `scoring.py` | `_calculate_score` | [strategy.py:1374](file:///Users/yl/.codex/worktrees/ai-tuner-stage2-readiness-plan/Binance_quantitative_trading/strategies/btc_eth_aggressive/strategy.py#L1374) | `indicators, klines, market_state, scoring_cfg, risk_cfg, symbol_config` | `float`(0-100) | 有（`self.risk_config`/`self.scoring_config`/`self.symbol_config`+logger） |
| `scoring.py` | `_calculate_trend_strength_score` | :1436 | `indicators, scoring_cfg, timeframes` | `float` | 有（配置+timeframes） |
| `scoring.py` | `_calculate_pattern_quality_score` | :1497 | `indicators, klines, scoring_cfg` | `float` | 有 |
| `scoring.py` | `_calculate_momentum_divergence_score` | :1704 | `indicators, klines, scoring_cfg` | `float` | 有 |
| `scoring.py` | `_detect_divergence` | :1659 | `indicators, klines` | `bool` | 无 |
| `scoring.py` | `_detect_bullish_engulfing` / `_detect_bearish_engulfing` | 待核实 | `df` | `bool` | 无 |
| `direction.py` | `_determine_direction` | :2267 | `indicators, timeframes` | `str(LONG/SHORT)` | 有（`self.timeframes`） |
| `direction.py` | `_check_trend_filter` | :1753 | `symbol, indicators, klines, risk_cfg` | `(bool, str)` | 有（配置）；**IO 部分已在此文件但无 IO 调用**（本方法纯计算） |
| `direction.py` | `_bb_touch_votes`（静态） | :1854 | `df_4h, indicators, entry_conditions` | `(int,int,list,float,float)` | 无 |
| `filters.py` | `_detect_breakout_pullback` | :1619 | `df, indicators, scoring_cfg` | `bool` | 有（配置） |
| `filters.py` | `_check_prohibited_conditions`（仅**无 IO** 的「单根 K 线涨跌幅」子检查） | :2310 | 部分提取 | `(bool,str)` | **有 IO**：第 2/3 项 `await self.binance.*` 必须剥离，只提取第 1 项 |
| `exits.py` | `_calculate_tp_price` | :2597 | `entry_price, atr, direction, tp_level, grade, risk_cfg` | `Decimal` | 有（`_get_grade_risk`） |
| `exits.py` | `_calculate_stop_price` | :3271 | `position(direction,entry_price,atr,grade), risk_cfg` | `Decimal` | 有（`_get_grade_risk`）；改为显式传 `direction/entry/atr/grade` 而非 PositionState |
| `exits.py` | `_apply_limit_offset`（静态） | :3288 | `price, offset_pct, direction` | `Decimal` | 无 |
| `exits.py` | `_build_indicators` | :5129 | `klines, risk_cfg` | `indicators` | 依赖 `shared.indicators.TechnicalIndicators.calculate_all`（`shared/indicators.py:297`，可复用） |
| `exits.py` | `_get_grade_risk` | :2253 | `grade, risk_cfg` | `dict` | 有 |

> 说明：`_build_indicators` 归属 `exits.py`（它也被复用为退出复核路径的指标入口）；其依赖 `TechnicalIndicators` 来自 `shared.indicators`（已核实，非 `backtest`），保留在原位置共用，不搬迁。`_check_prohibited_conditions` 只做**纯计算部分**提取，IO 项在回放适配器中由快照外置数据替代或用「历史 kline 派生」口径，缺数据即 `BLOCKED_DATA`。

### 7.3 characterization / golden 测试冻结方式

1. **输入夹具**：为每个待提取方法构造代表性输入（真实/固定 K 线切片、固定 config dict、固定 timeframes），夹具文件放 `tests/test_strategies/fixtures/`（git 跟踪，内容冻结）。
2. **逐字段断言**：先对**原 strategy.py 方法**（`self.xxx` 绑定实例）跑，把每个字段/返回值写入 golden JSON；再对**提取后的 core 纯函数**跑，`assert 逐字段 == golden`。
3. **异常语义断言**：记录原方法在异常输入下的抛出类型与消息（如 `KeyError` 缺配置键、`ValueError` 非法 direction），提取后必须一致，**不得新增/吞掉/改变异常**。
4. **等价性门禁**：`tests/test_strategies/test_btc_eth_aggressive_core_equivalence.py` 全绿 + 原 `strategies/btc_eth_aggressive/tests/` 全绿，才允许 `strategy.py` 方法体改为转发到 `core`（只替换调用点，不删旧方法签名）。

---

## 8. 配置项清单

> 新增到 `ai_tuner/config.yaml`；口径「禁止硬编码」：所有数值只经此处注入，代码内不出现 `1.5`/`2.0`/`8`/`2` 等魔数。

### 8.1 `purpose_driven_mvp.replay`

| 键 | 类型 | 默认值 | 单位 | 说明 |
|---|---|---|---|---|
| `window.diagnostic_weeks` | `int` | 8 | 周 | 诊断区（沿用已有 `diagnostic_weeks` 口径） |
| `window.blind_weeks` | `int` | 2 | 周 | 盲测区（沿用已有 `blind_weeks`） |
| `initial_equity_usdt` | `Decimal` | `10000` | USDT | 同场同初始资金（**待核实**：需按真实 pilot 校准） |
| `fees.taker_bps` | `Decimal` | `5` | bps | 每边 taker（对齐 `backtest/btc_eth/replay/config.yaml` fees.taker_bps） |
| `slippage.baseline_bps` | `Decimal` | `2` | bps | 每边基准滑点 |
| `cost_multipliers` | `list[Decimal]` | `[1.0, 1.5, 2.0]` | 无 | 成本档；分摊 `fee_bps(m)=round(taker_bps*m)`、`slippage_bps(m)=round(baseline*bps*m)` |
| `stop_limit.offset_pct` | `Decimal` | `0.002` | 比例 | 止损触发后不利偏移（对齐生产 `stop_limit_order.offset_pct`） |
| `tp_limit.offset_pct` | `Decimal` | `0.0015` | 比例 | 止盈触发后有利偏移 |

### 8.2 `purpose_driven_mvp.regime`

| 键 | 类型 | 默认值 | 单位 | 说明 |
|---|---|---|---|---|
| `trend_return_pct` | `Decimal` | `2.0` | % | 区间累计涨跌幅 ≥2% 判趋势（对齐 `backtest.market_regime.trend_threshold`） |
| `high_volatility_atr_pct` | `Decimal` | `4.5` | % | ATR/价格 ≥4.5% 判高波动（对齐生产 `prohibition.atr_price_max`） |
| `min_segment_length` | `int` | `8` | 根 | 分段最少 K 线数 |
| `classes` | `list[str]` | `[up_trend,down_trend,ranging]×[normal,high_vol]` + `STRESS` | — | 枚举，仅用于分组/版本匹配 |

### 8.3 `purpose_driven_mvp.parity`

| 键 | 类型 | 默认值 | 单位 | 说明 |
|---|---|---|---|---|
| `price_tolerance_bps` | `Decimal` | `待校准` | bps | 成交价容差由真实数据校准后写入 |
| `time_tolerance_seconds` | `int` | `待校准` | 秒 | 时间容差 |
| `pnl_tolerance_usdt` | `Decimal` | `待校准` | USDT | 盈亏容差 |
| `min_golden_trades` | `int` | `100` | 笔 | 低于此 → `partial_parity`+`BLOCKED_DATA` |

### 8.4 `purpose_driven_mvp.qualification.gates`（10 项门禁阈值）

| 门禁 | 键 | 类型 | 默认值 | 单位 |
|---|---|---|---|---|
| ① 数据完整度 | `min_data_completeness` | `Decimal` | `1.0` | 比例 |
| ② 无未来数据 | `future_data_allow_zero` | `bool` | `true` | （固定，非阈值） |
| ③ 金样对账 | `golden_parity_required` | `bool` | `true` | — |
| ④ 最大回撤 | `max_drawdown_pct` | `Decimal` | `15` | % |
| ⑤ 最低样本量 | `min_effective_signals` | `int` | `100` | 条（沿用，不下调） |
| ⑥ 盲测样本 | `min_blind_trades` | `int` | `待校准` | 笔 |
| ⑦ 相邻扰动 | `adjacent_perturb_max_dd_delta_pct` | `Decimal` | `待校准` | % |
| ⑧ 1.5× 成本压力 | `cost_1_5x_capital_preservation_pct` | `Decimal` | `30` | %（1.5× 下不触发红线） |
| ⑨ 收益集中度 | `max_single_trade_concentration_pct` | `Decimal` | `待校准` | % |
| ⑩ 版本兼容 | `config_version_required` | `bool` | `true` | — |

> 「待校准」项为设计预留：必须在真实数据 stage 校准后写入 config，**代码不得出现 fallback 数字**；校准前相关门禁按未配置即 `BLOCKED_DATA` 处理（fail-closed）。

---

## 9. 测试矩阵

> 测试目录：`tests/test_ai_tuner_mvp/{replay,regime,qualification,parity}/` + `tests/test_strategies/test_btc_eth_aggressive_core_equivalence.py`。

| Task | 测试文件 | 必需用例类型 |
|---|---|---|
| Task 1 快照 | `replay/test_snapshot_manifest.py` | 逐字节一致（manifest 哈希）；缺文件 → `BLOCKED_DATA`；嵌套哈希篡改被顶层哈希拦截（fail-closed） |
| Task 2 as_of | `replay/test_data_portal.py` | 越界访问抛 `FutureDataAccess` 且不降级；空数据 fail-closed；覆盖度不足 → `BLOCKED_DATA` |
| Task 3 Broker | `replay/test_broker.py` | 市价/限价/止损/止盈成交价逐字段断言；reduce-only 不超仓；部分平仓精度舍 0；同根 K 线 5 级顺序；**成本单调性**（§4.6） |
| Task 3 确定性 | `replay/test_clock_engine_determinism.py` | 相同输入两次运行事件序一致；报告逐字节一致（canonical） |
| Task 4 MTPCS 适配器 | `replay/test_mtpcs_adapter.py` | 指标→意图管线字段断言；缺 interval → `BLOCKED_DATA` |
| Task 5 HRS 适配器 | `replay/test_hrs_adapter.py` | 空三表 → `BLOCKED_DATA` 且不实时补值；有完整快照 → 走现有 `scoring_engine` 不复刻 |
| Task 6 金样对账 | `parity/test_golden.py` | match/mismatch/partial_parity；样本不足 → `BLOCKED_DATA` 而非 `FAIL` |
| Task 7 regime | `regime/test_classifier.py` | 6 类 + STRESS；阈值全 config；不读未来 K 线 |
| Task 8 成绩卡/门禁 | `qualification/test_metrics_gates.py` | 10 项门禁逐条；样本不足不判 FAIL；成本档终值权益 |
| Task 9 资格考试 | `qualification/test_runner.py` | 同场同数据多候选；不跨策略比较；盲测排除；不写覆盖层/`.active`/`auto_apply` |
| Task 10 CLI/报告 | `replay/test_artifacts.py` + `test_cli.py` 扩展 | 六文件 schema 与状态传播（`BLOCKED_DATA` 优先于 `FAIL`）；退出码 0/2/3/1 |
| 等价提取 | `test_btc_eth_aggressive_core_equivalence.py` | 逐字段 golden；异常语义一致；原策略测试回归 |

---

## 10. 风险与开放问题

1. **6382 行 strategy.py 的提取回归风险（最高）**：`_calculate_score` 等 10 余方法深耦合 `self.risk_config`/`self.scoring_config`/`self.symbol_config`/`self.timeframes` 与 logger，等价提取需把配置改为显式传参，任何一处默认值/调用顺序偏差都会产生逐字段不一致；缓解 = 严格按 §7.3 characterization 冻结，任何不一致停手。
2. **同根 K 线保守顺序 vs 实盘真实时序的偏差（最高）**：实盘由交易所按 tick 时序成交，回放以「止损先成交」保守近似，金样对账可能因此系统性高估亏损（mismatch）；缓解 = 用真实数据校准 §8.3 容差，并将此类差异显式写入 `parity_report` 而非放宽通过。
3. **逐字节一致性对 dict 集合迭代顺序敏感**：`audit_events`、`exit_breakdown`、`per_symbol` 等若用普通 `dict`/`set`，跨 Python 运行或字符串哈希随机化（`PYTHONHASHSEED`）会导致字节漂移；缓解 = 报告一律 `sort_keys=True` + 事件按 `(ts,seq)` 显式排序 + 集合转 `tuple(sorted(...))`；需测试覆盖「不同 PYTHONHASHSEED 下逐字节一致」。
4. **HRS 适配器在零历史数据下可验证边界极窄**：三表（`ai_tuner_universe_scan_runs`/`ai_tuner_universe_snapshots`/`ai_tuner_market_snapshots`）当前 0 行，`tests` 只能验证「空数据 → BLOCKED_DATA」接线，无法验证真实 scoring 串接正确性；缓解 = 用合成夹具验证 `scoring_engine` 调用面（仅验证引擎接线，不记「金样对账通过」，D4）。
5. **运行期 Python 版本登记不一致（待核实）**：硬约束 C4 写「运行期 3.9/容器 3.10」，但 `strategies/btc_eth_aggressive/Dockerfile:1` 实为 `python:3.11-slim`、`ai_tuner/Dockerfile:2` 为 `python:3.10-slim`；提取后的 `core/` 会被打包进 3.11 的激进版镜像，需确认「禁用 3.10+ 语法」的约束是否覆盖 3.11，避免实现阶段误用 3.10/3.11 特性。
6. **`AI Tuner` 镜像不含策略代码与 backtest 的搬运边界**：`ai_tuner/Dockerfile:25-27` 仅 `COPY ai_tuner/ shared/ scripts/ai_tuner_mvp.py`，MTPCS 纯函数落在 `strategies/btc_eth_aggressive/core/`，回放若在 ai-tuner 容器内跑会 `ModuleNotFoundError`——但因 C1「只在开发机回放」，该边界仅影响未来若误配容器触发回放，需在 `engine` 入口显式断言「非容器环境」或仅以开发机 import 路径运行。
7. **资金费 mark_price 近似 `entry_price` 引入对账偏差**：`fees.py:64` 用入场价近似结算 mark_price，跨长持仓（尤其高波动）会偏离真实资金费，金样对账可能 mismatch；是否升级为「结算点 K 线均价」需在真实数据校准阶段定案（列开放问题）。
8. **`_check_prohibited_conditions` 的 IO 项（资金费率/24h 涨跌幅）在回放路径的数据来源未定**：`await self.binance.*` 无法在离线回放复用，需快照额外导出或由历史 K 线派生，否则相关禁止入场条件在回放中缺失（属「缺数据 BLOCKED_DATA」还是「降级为不检查」待裁定）。

---

## 附录：设计端到端数据流（示意）

```text
export_replay_snapshot.py ──> var/replay/snapshots/<strategy>/<as_of>/ (manifest+SHA-256)
        │ 只读
        ▼
data_portal (as_of 强制，越界 FutureDataAccess，缺数据 BLOCKED_DATA)
        │
        ▼
ReplayAdapter (mtpcs_aggressive ── core 纯函数 ｜ hrs ── 现有 scoring_engine) ──> OrderIntent
        │
        ▼
clock (确定性事件序) ──> engine (事件循环 + audit) ──> broker (费用/滑点/资金费/精度/撮合)
        │
        ▼
ReplayResult ──> artifacts (canonical JSON ×6，逐字节一致)
        │
        ▼
parity (golden 对账) / qualification (metrics + gates) ──> PASS / FAIL / BLOCKED_DATA
```