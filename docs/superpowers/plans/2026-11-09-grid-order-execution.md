# ETHUSDT 子账号订单网格自动执行 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Work only in the isolated `codex/grid-auto-executor-20261109` branch. Do not enable production trading while implementing.

**Goal:** 让现有 V2.5.5 结构化网格信号驱动独立子账号的 ETHUSDT 永续合约订单网格，完成建单、配对、暂停、终止、保护、对账及自动重建。

**Architecture:** 现有 `grid-strategy` 每次巡检把 `GridSignal` 写入共享卷的 `grid_execution.sqlite3`，与飞书通知分开；新 `grid-executor` 进程读取决策、维护订单账本并使用独立子账号 API。实盘模式默认关闭。交易所订单、持仓及条件单是对账依据，SQLite 记录意图与执行阶段。

**Tech Stack:** Python 3.11、pytest、SQLite WAL、现有 `BinanceClient` 普通 USDT-M 接口、币安 USDⓈ-M REST 与用户数据流、Docker Compose。

**Approved design:** `docs/superpowers/specs/2026-11-09-grid-order-execution-design.md`。实现中不修改策略阈值、EMA/ADX/ATR 计算或 V2.5.5 的区间、格数与建议保证金公式。

---

## File map and gates

| File | Responsibility |
| --- | --- |
| `strategies/grid/execution/contracts.py` | 不可变决策、订单意图、账户快照与状态枚举 |
| `strategies/grid/execution/store.py` | SQLite schema、决策/outbox、订单账本、游标、单实例锁 |
| `strategies/grid/execution/policy.py` | 市场状态到建网格/暂停/终止的纯映射 |
| `strategies/grid/execution/planner.py` | V2.5.5 参数到价格、数量、最坏敞口的纯规划 |
| `strategies/grid/execution/exchange.py` | 子账号普通合约 API、现行 algo 条件单与交易所快照 |
| `strategies/grid/execution/engine.py` | 幂等执行、部分成交、暂停、终止、保护、自动重建与对账 |
| `strategies/grid/execution/main.py` | 独立进程入口、密钥与风险参数校验、健康心跳 |
| `strategies/grid/signal_bot.py` | 每次成功巡检后写结构化决策；通知仍走原路径 |
| `docker-compose.yml`、`.env.example`、`.github/workflows/deploy.yml` | 独立服务、默认关闭与迁移/检查部署入口 |
| `tests/test_grid_execution/` | 纯逻辑、SQLite、假交易所、故障注入及端到端模拟 |

Global invariants for every task: only `ETHUSDT`; `BOTH` one-way and isolated USDT-M; all exchange writes require a persisted intent with stable client ID; no order on stale/unknown signal; no new exposure while exiting or locked; never infer order fill from disappearance from open-order list; never parse notification text; user data stream events are reconciled against REST before restart or completion claims.

Knowledge-base constraints reviewed before implementation (`docs/knowledge/`): quantities and exchange filters use ETH units (`min_quantity=0.01 ETH`), never contract counts; do not silently increase margin to force a feasible plan; the ordinary-account `get_income_history()` returns `[]` without requesting Binance, so daily-loss evidence must use a direct, verified ordinary USD-M source or stop as unknown; persist fills before any event cursor; account-wide positions/orders require explicit ownership checks even on the dedicated subaccount; a silent WebSocket is unhealthy even without an exception, and queue overflow must preserve the latest event. A gap in raw trade IDs alone is not evidence of missing fills. See the dated knowledge records for the original incidents.

Keep these public contracts stable across tasks (implement the bodies in their listed modules):

```python
class Action(str, Enum):
    START = "START"
    REPLAN = "REPLAN"
    PAUSE_EXPOSURE = "PAUSE_EXPOSURE"
    PAUSE_ALL = "PAUSE_ALL"
    EXIT = "EXIT"
    WAIT = "WAIT"
    LOCKED = "LOCKED"

@dataclass(frozen=True)
class Decision:
    decision_id: str
    symbol: str
    calculated_at_ms: int
    market_state: MarketState
    grid_params: DynamicGridParams | None
    feasibility: CapitalFeasibility | None
    margin_used: Decimal | None
    config_hash: str
    last_closed_kline_ms: dict[str, int]
```

`policy.decide` takes `(Decision, AccountSnapshot, RiskSnapshot)` and returns `Action`; `planner.build_plan` takes `(DynamicGridParams, CapitalFeasibility, market_price, ExchangeFilters, HardLimits)` and returns `GridPlan`. Tasks 3–7 define their behavior and assertions.

## Task 1: Establish clean test baseline and disabled runtime config

**Files:** Modify `tests/integration/test_grid_strategy.py`; create `strategies/grid/execution/contracts.py`, `tests/test_grid_execution/test_config.py`; modify `.env.example`.

- [ ] Write a failing test that the legacy analyze fixture returns `Decimal('60000')`, matching `BinanceClient.get_ticker_price()`; the current fixture returns `{'price':'60000'}` and causes the known 1/106 failure. Run `python3 -m pytest -q tests/integration/test_grid_strategy.py::TestGridStrategy::test_strategy_analyze` and confirm failure is the known type error.
- [ ] Correct only that fixture, then run the same test and the three baseline files. Expected: `106 passed` with no code change to production strategy.
- [ ] Add a failing config test: `ExecutionSettings.from_env({})` yields `mode='off'`; `GRID_EXECUTION_MODE=live` without explicit positive `GRID_MAX_MARGIN_USDT`, `GRID_MAX_NOTIONAL_USDT`, `GRID_MAX_DAILY_LOSS_USDT`, `GRID_MAX_DRAWDOWN_USDT`, `GRID_MAX_OPEN_ORDERS` or `GRID_SUBACCOUNT_API_KEY`/`GRID_SUBACCOUNT_API_SECRET` raises `ValueError`. A live setting cannot derive these from `trading.margin=500`.
- [ ] Implement frozen `ExecutionSettings` with `symbol='ETHUSDT'`, `mode in {'off','testnet','live'}`, positive hard limits, `use_unified_account=False`; do not instantiate a trading client when mode is `off`. Add only variable names and comments to `.env.example`, never sample credentials.
- [ ] Run `python3 -m pytest -q tests/test_grid_execution/test_config.py tests/integration/test_grid_strategy.py` and `git diff --check`; commit `test(grid): align ticker fixture and gate executor config`.

## Task 2: Durable decision handoff independent of notifications

**Files:** Create `strategies/grid/execution/store.py`, `tests/test_grid_execution/test_store.py`; modify `strategies/grid/signal_bot.py`, `tests/test_grid_execution/test_signal_handoff.py`.

- [ ] Write failing SQLite tests for `grid_execution.sqlite3`: schema version check, deterministic `decision_id`, `INSERT OR IGNORE` on duplicate, monotonically read `calculated_at_ms`, no cursor advance before acknowledgement, and schema mismatch refusing writer/reader startup. Use temporary files, not the production data volume.
- [ ] Implement `decisions(decision_id, calculated_at_ms, payload_json, config_hash, created_at_ms)` plus `execution_cursor` and `schema_meta`; use WAL and `synchronous=FULL` for intent durability. Add `python -m strategies.grid.execution.store --db-path /app/data/grid_execution.sqlite3` as one-time migration; business processes only call `check_schema`.
- [ ] Write a failing `GridSignalBot.run_loop` test where `_should_notify()` is false and a feasible `GridSignal` is still written exactly once to the decision store. Also assert notification failure does not roll back a persisted decision. Add Kline freshness cases: last closed 15m older than 30 minutes, 1h older than 90 minutes, or 4h older than 5 hours must mark the decision non-executable.
- [ ] Publish after each successful `run_once()` and before notification handling. Extend `MarketAnalysis` with the three last-closed Kline times captured from `tf_data`; serialize typed `MarketState`, `DynamicGridParams`, `CapitalFeasibility`, margin, Kline close times, calculation time and config hash. Reject malformed payload on read. Do not reuse `realtime.reference_store` because its snapshots represent successfully sent suggestions, not every decision.
- [ ] Run `python3 -m pytest -q tests/test_grid_execution/test_store.py tests/test_grid_execution/test_signal_handoff.py tests/test_strategies/test_grid_capital_linkage.py`; commit `feat(grid): persist every structured decision`.

## Task 3: Pure execution policy and grid planner

**Files:** Create `strategies/grid/execution/policy.py`, `strategies/grid/execution/planner.py`, `tests/test_grid_execution/test_policy.py`, `tests/test_grid_execution/test_planner.py`.

- [ ] Write a failing parameterized policy test for all nine `MarketState` values: `OSCILLATION`/`WEAK_TREND` allow plans; `EARLY_WARNING_15M`/`TREND_ACCELERATING` cancel exposure-increasing ordinary orders and stop replenishment; `VOLATILITY_ABNORMAL` cancels all ordinary grid orders; `PRICE_EMERGENCY`/`TREND_CONFIRMED_1H`/`EXTREME_STRONG_TREND`/`NORMAL_STRONG_TREND` enter `EXITING`. Unknown state, stale decision or infeasible capital never opens orders.
- [ ] Implement a pure `decide(decision, account, risk) -> Action` with explicit `START`, `REPLAN`, `PAUSE_EXPOSURE`, `PAUSE_ALL`, `EXIT`, `WAIT`, `LOCKED` values. A previously locked safety state cannot be cleared by a tradable signal.
- [ ] Write planner tests for arithmetic and geometric prices from `DynamicGridParams`, below-market BUY/above-market SELL, no crossing order at the current price, one ordinary order per price, `qty_per_grid` rounded down to `stepSize`, all Binance symbol filters, and no plan if rounding merges adjacent prices or breaches profit/risk limits.
- [ ] Implement `build_plan(params, feasibility, market_price, exchange_filters, hard_limits) -> GridPlan`. Validate whole-plan worst-case filled quantity and notional, reserved margin, order count, tick/step/min-notional, and stop boundaries. The function never changes grid count, bounds, leverage or suggested margin to make a bad plan pass.
- [ ] Run `python3 -m pytest -q tests/test_grid_execution/test_policy.py tests/test_grid_execution/test_planner.py`; commit `feat(grid): plan V2.5.5 orders without changing signals`.

## Task 4: Current Binance subaccount and conditional-order adapter

**Files:** Create `strategies/grid/execution/exchange.py`, `tests/test_grid_execution/test_exchange.py`.

- [ ] Write failing HTTP-contract tests with a fake `_request`: ordinary LIMIT and `reduceOnly` MARKET use `/fapi/v1/order`; `STOP_MARKET` with `algoType=CONDITIONAL`, `closePosition=true` uses `/fapi/v1/algoOrder`; live protection query uses `/fapi/v1/openAlgoOrders`, exact lookup/cancel uses `/fapi/v1/algoOrder`. Assert the adapter never calls the existing `get_open_algo_orders()` method that currently returns `[]`.
- [ ] Implement a dedicated grid adapter around `BinanceClient(api_key=key, api_secret=secret, testnet=mode=='testnet', use_unified_account=False)`; preserve existing shared client behavior for other strategies. Verify `BOTH`, single-asset mode, isolated margin, leverage, balance, position, open ordinary orders, open algo orders, symbol filters and clock before allowing any opening order. A mode mismatch raises `AccountModeError`, without silently changing account settings.
- [ ] Test request timeout after `POST`: use the same stable client ID and query exact order/algo order until accepted or conclusively absent; unresolved results raise `UnknownOrderResultError` and lock openings. Never reissue a write solely because a timeout occurred.
- [ ] Run `python3 -m pytest -q tests/test_grid_execution/test_exchange.py tests/test_shared/test_open_algo_orders.py`; commit `feat(grid): add subaccount exchange adapter`.

## Task 5: Transactional order journal and startup reconciliation

**Files:** Extend `strategies/grid/execution/store.py`; create `tests/test_grid_execution/test_journal.py`, `tests/test_grid_execution/test_reconcile.py`.

- [ ] Write failing tests for `grid_epoch`, `order_intent`, `fill`, `pair`, `protection`, `engine_state`, and `equity_checkpoint` rows. Unique constraints must reject a duplicate `(grid_epoch, level_index, role)` active intent and duplicate `(exchange_order_id, trade_id)` fill. Persist each intent before API send; advance state and decision cursor in one transaction only after required exchange facts are stored.
- [ ] Implement SQLite `BEGIN IMMEDIATE` transactions and an exclusive `/app/data/grid_execution.lock` file lock held for process lifetime. A second process must fail startup; after a crash the new process must take the lock and reconcile before issuing writes.
- [ ] Write a fake-exchange restart test with: one accepted-but-unacknowledged order, one partially filled/canceled order, one stale local order, an unmatched position, and one foreign ETHUSDT order. Expected: known orders repaired from exchange facts; unknown/foreign facts cause `LOCKED`, no new opening order.
- [ ] Implement `reconcile()` using account position, ordinary open orders, open algo orders and exact order queries. Match only the executor client ID prefix; never assume a missing open order was filled. Make `FLAT` conditional on zero exchange position and zero owned ordinary/algo orders.
- [ ] Run `python3 -m pytest -q tests/test_grid_execution/test_journal.py tests/test_grid_execution/test_reconcile.py`; commit `feat(grid): journal intents and reconcile exchange state`.

## Task 6: Grid fills, pairing, and protection

**Files:** Create `strategies/grid/execution/engine.py`, `tests/test_grid_execution/test_fills.py`, `tests/test_grid_execution/test_protection.py`.

- [ ] Write failing fill tests: initial grid below/above price, duplicate fill event, partial fill, partial fill followed by cancel, cumulative fill correction, reverse order at adjacent level, and quantity below step/min-notional retained as an unmatched journal remainder. Assert no duplicate reverse order on replay.
- [ ] Implement user data stream `ORDER_TRADE_UPDATE` intake with listen-key creation, renewal before 60 minutes, reconnect before the 24-hour connection limit, dedupe by exchange trade ID and REST reconciliation after disconnect/restart. Use exchange-reported executed quantity and trade price, not planned quantity or open-order disappearance. Persist a pair before placing its reverse intent.
- [ ] Write failing protection tests for long and short net positions at `stop_loss_low`/`stop_loss_high`, position flip, canceled/expired protection, stale stop boundary, and `STOP_MARKET` placement failure. Expected: no opening while protection is missing or uncertain; if a position is unprotected, enter `EXITING` and alert.
- [ ] Implement protection reconciliation around `/fapi/v1/openAlgoOrders`; use close-all conditional orders, not quantity-dependent stale stops. Confirm replacement before retiring old protection when possible; any exchange rejection triggers the tested fail-closed exit path.
- [ ] Run `python3 -m pytest -q tests/test_grid_execution/test_fills.py tests/test_grid_execution/test_protection.py`; commit `feat(grid): pair fills and maintain exchange stop protection`.

## Task 7: Pause, full exit, risk latch, and automatic restart

**Files:** Extend `strategies/grid/execution/engine.py`; create `tests/test_grid_execution/test_exit.py`, `tests/test_grid_execution/test_risk.py`, `tests/test_grid_execution/test_rebuild.py`.

- [ ] Write failing pause tests: early warning/acceleration leave only orders that cannot increase absolute net position, volatility abnormal leaves no ordinary grid orders, all paused states stop replenishment while maintaining exchange protection.
- [ ] Write failing exit tests where a cancel races with a fill and a market-close result times out. Assert `EXITING` is persisted before cancellation, REST re-reads final fills/position, close uses `reduceOnly`, and a second close occurs only for confirmed residual position. `FLAT` requires zero position and zero owned ordinary/algo orders.
- [ ] Implement the exit state machine and risk latch. With current risk thresholds, equity loss from Beijing-day start of at least 3% or from stored peak of at least 15% enters `LOCKED` and exits; the independent positive USDT hard-loss caps may stop earlier. Reject account transfers or missing equity evidence as unknown-risk states; they never silently reset the day baseline. Unlock is an explicit operator action with a recorded reason.
- [ ] Write a failing rebuild test: after a normal market exit, first subsequent valid tradable decision and clean exchange state creates a new `grid_epoch`; an old decision, same decision, active `EXITING`, safety `LOCKED`, foreign order, or residual position does not rebuild.
- [ ] Implement rebuild with the same policy/planner path as initial entry. Run `python3 -m pytest -q tests/test_grid_execution/test_exit.py tests/test_grid_execution/test_risk.py tests/test_grid_execution/test_rebuild.py`; commit `feat(grid): exit and rebuild only from reconciled state`.

## Task 8: Separate service and fault-injection release evidence

**Files:** Create `strategies/grid/execution/main.py`, `tests/test_grid_execution/test_main.py`, `tests/test_grid_execution/test_faults.py`, `docs/deployment/grid-order-executor.md`; modify `docker-compose.yml`, `.github/workflows/deploy.yml`, `.env.example`.

- [ ] Write a failing boot test: absent `GRID_EXECUTION_MODE` runs no exchange writer; `testnet`/`live` without subaccount credentials, positive hard limits or migrated schema refuses startup. No API key appears in logs. The executor must not use main-account `BINANCE_API_KEY`.
- [ ] Add `grid-executor` Compose service using the existing grid image, command `python -m strategies.grid.execution.main`, shared `./data/grid:/app/data`, its own subaccount variables, `logging.driver: journald`, and a healthcheck that verifies process heartbeat and decision/store freshness. Leave the service disabled behind a Compose profile until validation.
- [ ] Add one-time `grid_execution.sqlite3` migration before service start in deploy workflow, mirroring the existing `grid_realtime.sqlite3` migration. Do not start the executor automatically through existing `grid-strategy` startup.
- [ ] Run fake-exchange fault cases for WS loss, REST timeout, duplicate/reordered events, process crash between journal write/API reply, partial fill during cancel, rejected stop, rate limit, stale Kline, balance change and foreign order. Assert each case either reconciles or locks openings; no case creates duplicate net exposure.
- [ ] Run `python3 -m pytest -q tests/test_grid_execution tests/test_strategies/test_grid_kline.py tests/test_strategies/test_grid_capital_linkage.py tests/integration/test_grid_strategy.py`, `git diff --check`, and `docker compose --profile grid-executor config`. Save exact counts and failures to `docs/deployment/grid-order-executor.md`; commit `feat(grid): wire disabled subaccount executor service`.

## Task 9: Simulated exchange acceptance and live activation gate

**Files:** Extend `docs/deployment/grid-order-executor.md`; create `tests/test_grid_execution/test_end_to_end.py`.

- [ ] Reproduce at least one full oscillation→BUY fill→SELL pair→warning pause→strong-trend exit→flat→tradable rebuild cycle with a deterministic fake exchange; compare journal, exchange position, ordinary and algo orders after every transition.
- [ ] On Binance futures testnet with a separate testnet key, verify symbol filters, one-way/isolated account checks, normal and algo endpoint response shapes, cancellation/fill race and stop order visibility. Record sanitized order IDs, timestamps and results; no production credentials or orders.
- [ ] Before any production activation, obtain the user's actual subaccount ID, approved positive hard limits, API IP allowlist and explicit authorization to enable the `live` profile. Confirm the code image and migration version, run a read-only account/order reconciliation, then begin supervised small-capital validation. Activation is a distinct later action and is not implied by this plan's approval.

## Self-review checklist

- [ ] Each approved state in the design has a policy and fault test; each order mutation has a persisted stable ID and exchange reconciliation.
- [ ] The new service never parses Feishu text or uses the V2.5.4 shadow signal as a trading command.
- [ ] No real trading path can run with omitted limits, account mode mismatch, missing protection, stale decision, foreign order, or unknown write result.
- [ ] Review `git diff --check`, all test output, Compose merged config, version identity and exact file diff before declaring implementation complete.
