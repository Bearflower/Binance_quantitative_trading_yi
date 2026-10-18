# AI Tuner MVP Replay and Qualification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, no-future-data replay and qualification engine that can reproduce the selected strategy's live history before evaluating parameter candidates.

**Architecture:** A shared event clock, as-of data portal, portfolio/broker simulator, regime classifier, and metric calculator are strategy-neutral. Thin strategy adapters reuse live calculation functions and translate their outputs to replay orders. Qualification compares all versions on identical slices and costs, then applies hard gates before calculating a score.

**Tech Stack:** Python 3.10, dataclasses, pandas/numpy already in the project, PostgreSQL historical K-lines, existing MTPCS aggressive and HRS strategy modules, pytest/pytest-asyncio.

---

## File map

- Create `ai_tuner/replay/models.py`: immutable bars, snapshots, orders, fills, positions, trades, and replay results.
- Create `ai_tuner/replay/clock.py`: deterministic event ordering.
- Create `ai_tuner/replay/data_portal.py`: timestamp-bounded reads and coverage checks.
- Create `ai_tuner/replay/broker.py`: order fills, fees, slippage, funding, and equity.
- Create `ai_tuner/replay/engine.py`: shared event loop and audit trail.
- Create `ai_tuner/replay/adapters/base.py`: replay adapter protocol.
- Create `ai_tuner/replay/adapters/mtpcs_aggressive.py`: live-function wrapper for MTPCS aggressive.
- Create `ai_tuner/replay/adapters/hrs.py`: live-function wrapper for HRS when its snapshot data passes readiness.
- Create `ai_tuner/regime/features.py`, `classifier.py`: six normal regimes plus `STRESS`.
- Create `ai_tuner/qualification/metrics.py`: net return, drawdown, Sortino, Calmar, expectancy, concentration.
- Create `ai_tuner/qualification/gates.py`: blind, sample, drawdown, stability, cost, and parity gates.
- Create `ai_tuner/qualification/runner.py`: current/new/history same-slice comparison.
- Create `ai_tuner/parity/golden.py`: live-history event normalization and parity report.
- Extend `scripts/ai_tuner_mvp.py`: `replay`, `parity`, and `qualify` commands.
- Create `tests/test_ai_tuner_mvp/replay/`, `regime/`, `qualification/`, and `parity/`.

### Task 1: Define immutable replay events and deterministic ordering

**Files:**
- Create: `ai_tuner/replay/__init__.py`
- Create: `ai_tuner/replay/models.py`
- Create: `ai_tuner/replay/clock.py`
- Test: `tests/test_ai_tuner_mvp/replay/test_clock.py`

- [ ] **Step 1: Write failing clock tests**

```python
from datetime import datetime, timezone
import pytest

from ai_tuner.replay.clock import EventClock
from ai_tuner.replay.models import Bar


UTC = timezone.utc


def bar(symbol, opened, closed):
    return Bar(symbol, "1h", datetime.fromisoformat(opened).replace(tzinfo=UTC),
        datetime.fromisoformat(closed).replace(tzinfo=UTC), 100, 102, 99, 101, 10)


def test_clock_orders_by_close_time_then_symbol_then_interval():
    events = [bar("ETHUSDT", "2026-01-01T00:00:00", "2026-01-01T01:00:00"),
              bar("BTCUSDT", "2026-01-01T00:00:00", "2026-01-01T01:00:00")]
    assert [x.symbol for x in EventClock(events)] == ["BTCUSDT", "ETHUSDT"]


def test_bar_rejects_non_closed_or_invalid_ohlc():
    with pytest.raises(ValueError):
        bar("BTCUSDT", "2026-01-01T01:00:00", "2026-01-01T00:00:00")
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/replay/test_clock.py -q`

Expected: FAIL because replay models do not exist.

- [ ] **Step 3: Implement immutable models and clock**

```python
# ai_tuner/replay/models.py
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Bar:
    symbol: str
    interval: str
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self):
        if self.close_time <= self.open_time:
            raise ValueError("bar close_time must be after open_time")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("invalid OHLC envelope")


@dataclass(frozen=True)
class ReplayOrder:
    order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    created_at: datetime
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    reduce_only: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ReplayFill:
    order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    filled_at: datetime
    reason: str


@dataclass
class ReplayResult:
    strategy_id: str
    version: str
    started_at: datetime
    ended_at: datetime
    fills: List[ReplayFill]
    closed_trades: List[Dict[str, Any]]
    equity_curve: List[tuple[datetime, float]]
    audit_events: List[Dict[str, Any]]
```

```python
# ai_tuner/replay/clock.py
class EventClock:
    def __init__(self, events):
        self._events = tuple(sorted(events, key=lambda x: (x.close_time, x.symbol, x.interval)))

    def __iter__(self):
        return iter(self._events)
```

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/replay/test_clock.py -q`

Expected: `2 passed`.

```bash
git add ai_tuner/replay tests/test_ai_tuner_mvp/replay/test_clock.py
git commit -m "feat(ai-tuner): add deterministic replay events"
```

### Task 2: Enforce as-of data access and closed-bar semantics

**Files:**
- Create: `ai_tuner/replay/data_portal.py`
- Test: `tests/test_ai_tuner_mvp/replay/test_data_portal.py`

- [ ] **Step 1: Write failing no-lookahead tests**

```python
from datetime import datetime, timezone
import pytest

from ai_tuner.replay.data_portal import FutureDataAccess, InMemoryDataPortal


UTC = timezone.utc


def test_snapshot_never_returns_rows_after_as_of(sample_bars):
    portal = InMemoryDataPortal(sample_bars)
    as_of = datetime(2026, 1, 1, 3, tzinfo=UTC)
    rows = portal.bars("BTCUSDT", "1h", as_of=as_of, limit=20)
    assert rows
    assert max(row.close_time for row in rows) <= as_of


def test_explicit_future_query_raises(sample_bars):
    portal = InMemoryDataPortal(sample_bars, decision_time=datetime(2026, 1, 1, 3, tzinfo=UTC))
    with pytest.raises(FutureDataAccess):
        portal.bars("BTCUSDT", "1h", as_of=datetime(2026, 1, 1, 4, tzinfo=UTC), limit=20)
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/replay/test_data_portal.py -q`

Expected: FAIL because the data portal is missing.

- [ ] **Step 3: Implement the portal contract**

```python
class FutureDataAccess(RuntimeError):
    pass


class DataPortal:
    def __init__(self, decision_time=None):
        self.decision_time = decision_time

    def _guard(self, as_of):
        if self.decision_time is not None and as_of > self.decision_time:
            raise FutureDataAccess(f"requested {as_of.isoformat()} after {self.decision_time.isoformat()}")


class InMemoryDataPortal(DataPortal):
    def __init__(self, bars, decision_time=None):
        super().__init__(decision_time)
        self._bars = tuple(bars)

    def bars(self, symbol, interval, as_of, limit):
        self._guard(as_of)
        rows = [b for b in self._bars if b.symbol == symbol and b.interval == interval and b.close_time <= as_of]
        return rows[-limit:]
```

Add `PostgresDataPortal` with parameterized SQL using `close_time <= $3`, deterministic ascending order, and the K-line table-name guard from `services/kline_service/core/table_name_guard.py`. Add timestamped readers for funding and candidate snapshots; a missing table or gap returns a structured coverage failure, never a current-value fallback.

- [ ] **Step 4: Add an invariant test for query boundaries**

Use a fake `DatabaseManager` and assert every query includes a supplied `as_of` argument and `close_time <=` or snapshot equivalent.

```python
@pytest.mark.asyncio
async def test_postgres_portal_binds_as_of(fake_db, as_of):
    portal = PostgresDataPortal(fake_db, decision_time=as_of)
    await portal.bars("BTCUSDT", "1h", as_of=as_of, limit=20)
    query, args = fake_db.fetch_all.await_args.args
    assert "close_time <=" in query
    assert as_of in args
```

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/replay/test_data_portal.py -q`

Expected: all tests pass.

```bash
git add ai_tuner/replay/data_portal.py tests/test_ai_tuner_mvp/replay/test_data_portal.py
git commit -m "feat(ai-tuner): prevent future data access"
```

### Task 3: Simulate orders, costs, funding, and equity deterministically

**Files:**
- Create: `ai_tuner/replay/broker.py`
- Test: `tests/test_ai_tuner_mvp/replay/test_broker.py`

- [ ] **Step 1: Write failing broker invariants**

```python
from ai_tuner.replay.broker import BrokerConfig, SimulatedBroker


def test_increasing_cost_never_increases_final_equity(single_round_trip):
    base = SimulatedBroker(BrokerConfig(1000, 0.0004, 0.0002))
    stressed = SimulatedBroker(BrokerConfig(1000, 0.0006, 0.0003))
    for broker in (base, stressed):
        broker.execute_market(single_round_trip.open_order, price=100)
        broker.execute_market(single_round_trip.close_order, price=110)
    assert stressed.equity(110) < base.equity(110)


def test_reduce_only_cannot_reverse_position(reduce_only_order):
    broker = SimulatedBroker(BrokerConfig(1000, 0.0004, 0))
    broker.open_position("BTCUSDT", quantity=1, price=100)
    fill = broker.execute_market(reduce_only_order(quantity=2), price=101)
    assert fill.quantity == 1
    assert broker.position("BTCUSDT").quantity == 0
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/replay/test_broker.py -q`

Expected: FAIL because `SimulatedBroker` is missing.

- [ ] **Step 3: Implement the minimum production-equivalent broker**

```python
@dataclass(frozen=True)
class BrokerConfig:
    initial_cash: float
    taker_fee_rate: float
    slippage_rate: float
    maker_fee_rate: float = 0.0002


class SimulatedBroker:
    def __init__(self, config):
        self.config = config
        self.cash = config.initial_cash
        self.positions = {}
        self.fills = []
        self.equity_curve = []

    def _fill_price(self, side, reference):
        sign = 1 if side == "BUY" else -1
        return reference * (1 + sign * self.config.slippage_rate)

    def execute_market(self, order, price):
        quantity = self._reduce_only_quantity(order) if order.reduce_only else order.quantity
        fill_price = self._fill_price(order.side, price)
        fee = fill_price * quantity * self.config.taker_fee_rate
        self._apply_fill(order, quantity, fill_price, fee)
        return self.fills[-1]
```

Complete `_apply_fill()` with weighted entry, realized PnL, partial closes, long/short symmetry, and no position reversal for reduce-only. Add deterministic intrabar ordering configured as `stop_first` when stop and take-profit are both touched in one bar. Accrue funding only at timestamped settlement events from the portal. Reject orders below supplied quantity precision or minimum notional instead of silently rounding upward.

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/replay/test_broker.py -q`

Expected: all broker and cost monotonicity tests pass.

```bash
git add ai_tuner/replay/broker.py tests/test_ai_tuner_mvp/replay/test_broker.py
git commit -m "feat(ai-tuner): simulate replay execution and costs"
```

### Task 4: Add the six-state market-regime classifier and `STRESS`

**Files:**
- Create: `ai_tuner/regime/__init__.py`
- Create: `ai_tuner/regime/features.py`
- Create: `ai_tuner/regime/classifier.py`
- Test: `tests/test_ai_tuner_mvp/regime/test_classifier.py`

- [ ] **Step 1: Write boundary and hysteresis tests**

```python
from ai_tuner.regime.classifier import RegimeClassifier, RegimeThresholds


def test_direction_and_volatility_form_six_normal_states(feature_factory):
    classifier = RegimeClassifier(RegimeThresholds())
    assert classifier.classify(feature_factory(ret14=0.08, adx=30, vol_pct=0.40)).label == "uptrend_normal_vol"
    assert classifier.classify(feature_factory(ret14=-0.08, adx=30, vol_pct=0.85)).label == "downtrend_high_vol"
    assert classifier.classify(feature_factory(ret14=0.01, adx=14, vol_pct=0.40)).label == "range_normal_vol"


def test_stress_overrides_normal_label(feature_factory):
    classifier = RegimeClassifier(RegimeThresholds(stress_return_1d=0.12))
    assert classifier.classify(feature_factory(ret1=0.14)).label == "STRESS"


def test_low_confidence_does_not_switch_before_confirmation(feature_factory):
    classifier = RegimeClassifier(RegimeThresholds(confirmations=3))
    labels = [classifier.update(feature_factory(ret14=0.07, adx=27)).label for _ in range(2)]
    assert labels == ["unconfirmed", "unconfirmed"]
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/regime/test_classifier.py -q`

Expected: FAIL because the regime package is missing.

- [ ] **Step 3: Implement feature snapshots and classification**

```python
@dataclass(frozen=True)
class RegimeFeatures:
    as_of: datetime
    return_1d: float
    return_7d: float
    return_14d: float
    ema20_over_ema50_4h: float
    ema20_slope_4h: float
    adx_4h: float
    breadth_above_ema20: float
    atr_price_percentile_180d: float
    realized_vol_percentile_180d: float


@dataclass(frozen=True)
class RegimeSnapshot:
    label: str
    confidence: float
    features: RegimeFeatures
```

Use BTC/ETH plus local-universe breadth. Direction is trend only when return, EMA relation/slope, and ADX agree; otherwise range. High volatility is the maximum of ATR and realized-volatility percentile crossing the configured enter threshold. Use lower exit thresholds and consecutive confirmations for switching. `STRESS` fires immediately on 1-day return shock, top-tail realized volatility, or configured liquidity-gap flag.

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/regime/test_classifier.py -q`

Expected: all tests pass.

```bash
git add ai_tuner/regime tests/test_ai_tuner_mvp/regime
git commit -m "feat(ai-tuner): classify market regimes"
```

### Task 5: Add portfolio metrics and the hard qualification gates

**Files:**
- Create: `ai_tuner/qualification/__init__.py`
- Create: `ai_tuner/qualification/metrics.py`
- Create: `ai_tuner/qualification/gates.py`
- Test: `tests/test_ai_tuner_mvp/qualification/test_metrics.py`
- Test: `tests/test_ai_tuner_mvp/qualification/test_gates.py`

- [ ] **Step 1: Write failing metric and gate tests**

```python
from ai_tuner.qualification.gates import GateConfig, QualificationGates
from ai_tuner.qualification.metrics import calculate_metrics


def test_larger_loss_cannot_improve_drawdown():
    base = calculate_metrics([1000, 1040, 1010, 1060], [40, -30, 50])
    worse = calculate_metrics([1000, 1040, 960, 1010], [40, -80, 50])
    assert worse.max_drawdown > base.max_drawdown


def test_candidate_fails_before_scoring_when_drawdown_exceeds_cap(metric_factory):
    gates = QualificationGates(GateConfig(max_drawdown=0.12, min_trades=20))
    result = gates.evaluate(metric_factory(max_drawdown=0.15, trades=40),
                            metric_factory(max_drawdown=0.10, trades=40))
    assert result.eligible is False
    assert "max_drawdown" in result.failures


def test_stability_requires_neighbors_not_to_collapse(metric_factory):
    gates = QualificationGates(GateConfig(max_drawdown=0.12, min_trades=20, max_neighbor_score_drop=8.0))
    result = gates.evaluate(metric_factory(score=70), metric_factory(score=60),
                            neighbors=[metric_factory(score=40), metric_factory(score=69)])
    assert result.eligible is False
    assert "stability_plateau" in result.failures
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/qualification/test_metrics.py tests/test_ai_tuner_mvp/qualification/test_gates.py -q`

Expected: FAIL because qualification modules are missing.

- [ ] **Step 3: Implement metric calculation**

`MetricSet` must include net return, max drawdown, Sortino, Calmar, expectancy, trade count, win count, loss count, fee/slippage/funding totals, worst-trade share, best-trade profit share, symbol profit concentration, and per-window results. Use daily equity returns for Sortino and elapsed years for Calmar; return `0` rather than infinity when the denominator is zero.

```python
score = (
    0.35 * normalize_risk_adjusted(metrics.sortino, metrics.calmar)
    + 0.25 * normalize_net_return(metrics.net_return)
    + 0.20 * normalize_drawdown(metrics.max_drawdown, config.max_drawdown)
    + 0.10 * normalize_window_stability(metrics.window_returns)
    + 0.10 * normalize_sample_expectancy(metrics.trade_count, metrics.expectancy)
)
```

- [ ] **Step 4: Implement hard gates before score comparison**

Reject on: drawdown cap, blind-window deterioration beyond tolerance, insufficient trades/signals, incompatible code/config/schema identity, unstable OAT neighbors at `-10%, -5%, candidate, +5%, +10%`, failure at 1.5× fees/slippage, excessive single-trade/symbol concentration, or any data-integrity error. A 2× cost run is recorded as diagnostic evidence but is not an MVP pass gate.

```python
def evaluate(self, candidate, current, neighbors=(), stressed=None):
    failures = []
    if candidate.max_drawdown > self.config.max_drawdown:
        failures.append("max_drawdown")
    if candidate.trade_count < self.config.min_trades:
        failures.append("minimum_sample")
    if candidate.blind_score < current.blind_score - self.config.blind_tolerance:
        failures.append("blind_deterioration")
    if neighbors and min(x.score for x in neighbors) < candidate.score - self.config.max_neighbor_score_drop:
        failures.append("stability_plateau")
    if stressed is not None and not stressed.passes_hard_limits:
        failures.append("cost_stress_1_5x")
    return GateResult(eligible=not failures, failures=failures)
```

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/qualification -q`

Expected: all tests pass.

```bash
git add ai_tuner/qualification tests/test_ai_tuner_mvp/qualification
git commit -m "feat(ai-tuner): add qualification metrics and gates"
```

### Task 6: Wrap live strategy calculations in replay adapters

**Files:**
- Create: `ai_tuner/replay/adapters/__init__.py`
- Create: `ai_tuner/replay/adapters/base.py`
- Create: `ai_tuner/replay/adapters/mtpcs_aggressive.py`
- Create: `ai_tuner/replay/adapters/hrs.py`
- Modify: `strategies/btc_eth_aggressive/strategy.py:1089-1466`
- Modify: `strategies/hrs/scoring_engine.py:1127-1360`
- Test: `tests/test_ai_tuner_mvp/replay/test_strategy_adapters.py`

- [ ] **Step 1: Write failing adapter parity tests for pure calculations**

```python
def test_mtpcs_replay_score_equals_live_score(mtpcs_live_strategy, mtpcs_adapter, mtpcs_snapshot):
    live = mtpcs_live_strategy._calculate_score(
        mtpcs_snapshot.indicators, mtpcs_snapshot.klines, mtpcs_snapshot.market_state)
    replay = mtpcs_adapter.calculate_signal(mtpcs_snapshot, mtpcs_live_strategy.config)
    assert replay.score == live


def test_hrs_replay_entry_decision_equals_live_scoring(hrs_engine, hrs_adapter, hrs_snapshot):
    live_result = hrs_engine.score(**hrs_snapshot.score_inputs)
    replay = hrs_adapter.calculate_signal(hrs_snapshot, hrs_engine.config)
    assert replay.should_enter == hrs_engine.should_entry(live_result)
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/replay/test_strategy_adapters.py -q`

Expected: FAIL because replay adapters are missing.

- [ ] **Step 3: Define the adapter protocol**

```python
class ReplayAdapter(Protocol):
    strategy_id: str
    required_intervals: tuple[str, ...]

    def build_universe(self, portal, at_time): ...
    def build_snapshot(self, portal, symbol, at_time): ...
    def calculate_signal(self, snapshot, config): ...
    def create_orders(self, signal, portfolio, config): ...
    def manage_open_position(self, snapshot, position, config): ...
    def apply_strategy_guards(self, state, config): ...
```

- [ ] **Step 4: Extract dependency-free calculation entry points**

Move no trading rules. Add pure wrappers that accept already-closed K-lines and configuration, and have both live and replay paths call those wrappers. MTPCS must reuse score, grade, market-state guard, ATR filter, entry-limit price, partial TP, trailing, and time-stop calculations. HRS must reuse pattern recognition, scoring, trend filters, and entry threshold checks; its adapter refuses to build a universe unless timestamped candidate/OI/market-cap/funding snapshots are complete.

```python
def calculate_signal(self, snapshot, config):
    strategy = self._strategy_factory(config)
    score = strategy._calculate_score(snapshot.indicators, snapshot.klines, snapshot.market_state)
    grade = strategy._determine_grade(score, snapshot.symbol)
    return ReplaySignal(
        symbol=snapshot.symbol,
        direction=snapshot.direction,
        score=score,
        grade=grade,
        should_enter=score >= strategy.scoring_config["min_score"],
    )
```

- [ ] **Step 5: Run strategy adapter and existing strategy tests**

Run:

```bash
python3 -m pytest \
  tests/test_ai_tuner_mvp/replay/test_strategy_adapters.py \
  tests/test_strategies/test_btc_eth_aggressive_adopt_positions.py \
  strategies/hrs/tests/test_scoring_engine.py \
  strategies/hrs/tests/test_pattern.py -q
```

Expected: all tests pass and live calculation outputs are unchanged.

- [ ] **Step 6: Commit**

```bash
git add ai_tuner/replay/adapters strategies/btc_eth_aggressive/strategy.py strategies/hrs/scoring_engine.py tests/test_ai_tuner_mvp/replay/test_strategy_adapters.py
git commit -m "feat(ai-tuner): reuse live logic in replay adapters"
```

### Task 7: Build the replay engine and same-slice qualification runner

**Files:**
- Create: `ai_tuner/replay/engine.py`
- Create: `ai_tuner/qualification/runner.py`
- Test: `tests/test_ai_tuner_mvp/replay/test_engine.py`
- Test: `tests/test_ai_tuner_mvp/qualification/test_runner.py`

- [ ] **Step 1: Write failing determinism and same-slice tests**

```python
def test_same_input_produces_byte_identical_result(replay_runner, replay_request):
    first = replay_runner.run(replay_request).canonical_json()
    second = replay_runner.run(replay_request).canonical_json()
    assert first == second


def test_all_versions_receive_identical_data_slice(qualification_runner, candidates):
    result = qualification_runner.run(candidates)
    fingerprints = {run.data_fingerprint for run in result.runs}
    assert len(fingerprints) == 1


def test_ineligible_candidate_is_never_ranked(qualification_runner, candidates):
    candidates[1].force_drawdown = 0.40
    result = qualification_runner.run(candidates)
    assert candidates[1].version not in [x.version for x in result.ranking]
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/replay/test_engine.py tests/test_ai_tuner_mvp/qualification/test_runner.py -q`

Expected: FAIL because the engine and runner are missing.

- [ ] **Step 3: Implement the deterministic event loop**

The engine processes a stable universe and closed bars at each clock time, manages existing positions before opening new ones, assigns order IDs from `(run_id, event_sequence)`, records every read boundary and decision, and fingerprints sorted input rows plus code/config/schema identities.

```python
for event in clock:
    portal.advance_to(event.close_time)
    broker.process_resting_orders(event, intrabar_policy=request.intrabar_policy)
    adapter.manage_all_open_positions(portal, broker, event.close_time, request.config)
    for symbol in adapter.build_universe(portal, event.close_time):
        snapshot = adapter.build_snapshot(portal, symbol, event.close_time)
        signal = adapter.calculate_signal(snapshot, request.config)
        if adapter.apply_strategy_guards(signal, broker.state, request.config):
            for order in adapter.create_orders(signal, broker.portfolio, request.config):
                broker.submit(order)
    broker.mark_to_market(event.close_time, portal)
```

- [ ] **Step 4: Implement the qualification matrix**

The runner accepts current, one proposed, and up to three compatible historical configurations. It evaluates 8-week diagnosis, 2-week blind, similar-regime slices, rolling windows, OAT neighbors, baseline costs, 1.5× cost, and 2× diagnostic cost. It applies gates first, then sorts eligible versions by conservative score, max drawdown, and version string for deterministic ties.

```python
runs = [self._evaluate(candidate, shared_slice) for candidate in candidates[:5]]
eligible = [run for run in runs if run.gates.eligible]
ranking = sorted(
    eligible,
    key=lambda run: (-run.metrics.conservative_score, run.metrics.max_drawdown, run.version),
)
return QualificationResult(runs=runs, ranking=ranking, data_fingerprint=shared_slice.fingerprint)
```

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/replay tests/test_ai_tuner_mvp/qualification -q`

Expected: all tests pass.

```bash
git add ai_tuner/replay/engine.py ai_tuner/qualification/runner.py tests/test_ai_tuner_mvp/replay tests/test_ai_tuner_mvp/qualification
git commit -m "feat(ai-tuner): qualify versions on identical replay data"
```

### Task 8: Add golden live-history parity as the unlock gate

**Files:**
- Create: `ai_tuner/parity/__init__.py`
- Create: `ai_tuner/parity/golden.py`
- Modify: `scripts/ai_tuner_mvp.py`
- Test: `tests/test_ai_tuner_mvp/parity/test_golden.py`

- [ ] **Step 1: Write failing parity tests**

```python
def test_parity_fails_when_exit_reason_differs(golden_comparator, live_trade, replay_trade):
    replay_trade.exit_reason = "TIME_STOP"
    report = golden_comparator.compare([live_trade], [replay_trade])
    assert report.passed is False
    assert report.mismatches[0].field == "exit_reason"


def test_parity_requires_signal_order_exit_fee_and_equity_thresholds(golden_comparator, matched_history):
    report = golden_comparator.compare(*matched_history)
    assert report.passed is True
    assert report.signal_direction_match >= 0.98
    assert report.exit_reason_match >= 0.95
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/parity/test_golden.py -q`

Expected: FAIL because parity comparison is missing.

- [ ] **Step 3: Implement explicit initial tolerances**

Use configurable starting tolerances: signal direction ≥98%, entry timestamp within one strategy cycle for ≥95%, entry price relative error ≤0.20%, exit reason ≥95%, partial-close quantity relative error ≤0.5%, total fees relative error ≤1%, final equity relative error ≤0.5%, and daily equity correlation ≥0.99. Every mismatch includes symbol, timestamp, live value, replay value, and rule.

```python
@dataclass(frozen=True)
class ParityTolerances:
    signal_direction_match: float = 0.98
    entry_time_match: float = 0.95
    entry_price_relative_error: float = 0.002
    exit_reason_match: float = 0.95
    partial_quantity_relative_error: float = 0.005
    fee_relative_error: float = 0.01
    final_equity_relative_error: float = 0.005
    daily_equity_correlation: float = 0.99
```

- [ ] **Step 4: Add CLI commands**

```text
python3 scripts/ai_tuner_mvp.py replay --strategy btc_eth_aggressive --start ... --end ... --version V20260910 --output var/ai_tuner/replay.json
python3 scripts/ai_tuner_mvp.py parity --strategy btc_eth_aggressive --golden-range ranges.json --output var/ai_tuner/parity.json
python3 scripts/ai_tuner_mvp.py qualify --strategy btc_eth_aggressive --candidate candidate.json --as-of ... --output var/ai_tuner/qualification.json
```

`qualify` exits `3` if the latest parity report is absent, stale for the current code/config/schema identity, or failed.

- [ ] **Step 5: Run Plan 2 acceptance**

Run:

```bash
python3 -m pytest \
  tests/test_ai_tuner_mvp/replay \
  tests/test_ai_tuner_mvp/regime \
  tests/test_ai_tuner_mvp/qualification \
  tests/test_ai_tuner_mvp/parity \
  tests/test_strategies/test_btc_eth_aggressive_ai_tuner.py \
  tests/test_strategies/test_hrs_ai_tuner.py -q
```

Expected: all tests pass. Production mode remains `audit` or `shadow`; no candidate is activated by this plan.

- [ ] **Step 6: Commit**

```bash
git add ai_tuner/parity scripts/ai_tuner_mvp.py tests/test_ai_tuner_mvp/parity
git commit -m "feat(ai-tuner): gate qualification on live parity"
```
