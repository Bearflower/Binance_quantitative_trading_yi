# AI Tuner 阶段二并行准备实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不降低既有 readiness 门禁、不改变实盘策略参数的前提下，并行完成 K 线数据补齐与逐 symbol 覆盖审计、HRS 外部时点快照、准备度报告精度修正，并把确定性回放资格考试推进到“数据一达标即可正式执行”的状态。

**Architecture:** 将工作拆成四个可独立交付的工作包：`KlineCoverage` 只负责定义 universe、逐 symbol 计算和缺口清单；`ExternalSnapshot` 只负责按时点保存 HRS 真实外部输入及来源；`ReadinessAuditor` 只读取证据并执行原有门禁；确定性回放继续遵循既有 replay/qualification 计划，通过 as-of 数据门户消费前两类证据。采集、审计和回放彼此解耦，任何采集失败都不得改变实盘交易决策或自动放宽门禁。

**Tech Stack:** Python 3.10、asyncio、PostgreSQL、现有 `DatabaseManager`、现有 K 线服务与 `BinanceClient`、CoinGecko provenance、市值/资金费率/OI API、pytest/pytest-asyncio、Docker Compose、GitHub Actions。

---

## 1. 当前事实基线

截至 2026-10-21，生产 readiness 审计结果为 `STOP`：

| 候选策略 | 有效信号 | K 线覆盖 | 外部快照 | 当前结论 |
|---|---:|---:|---:|---|
| `btc_eth_aggressive` | 4 / 100，仍在自然累积 | 97.96% / 98%（旧口径：主力合约抽样） | `not_required_for_current_manifest` | 未通过 |
| `hrs` | 5446 / 100 | 83.70% / 98%（旧口径：主力合约抽样） | 0% / 95%，必需 | 未通过 |

以下规则已经由前序设计确认，本计划不得改变：

- `min_effective_signals=100` 不下调、不豁免。
- `min_kline_coverage=0.98` 不下调。
- HRS `min_external_snapshot_coverage=0.95` 且 `required`。
- MTPCS 激进版当前 manifest 不依赖外部快照，继续标记 `not_required_for_current_manifest`，不能写成“快照合格”。
- `purpose_driven_mvp.mode` 在资格考试和时间机器通过前保持 `audit`。
- 不允许本计划修改策略参数、覆盖层、`.active` 指针或 `selected_strategy`。
- 不允许用当前值伪造历史快照；无法可靠回填的时间段必须显式记为缺失。

## 2. 范围与优先级

### 2.1 P0：优先解除 MTPCS 激进版的数据阻断

1. 修正 readiness 百分比显示精度。
2. 把 K 线覆盖从“主力合约抽样”升级为“策略 universe 逐 symbol、按应有 bar 数加权”。
3. 输出逐 symbol 缺口，使用现有 K 线服务补采。
4. 保持信号自然累积；达到 100 条后自动复审。

### 2.2 P1：立即启动 HRS 证据留存

1. 保存每次 HRS 候选池 universe 快照。
2. 保存候选币种的 OI、资金费率、市值及来源。
3. 保存由这些原始值确定性计算出的情绪/拥挤度特征及公式版本。
4. readiness 只认可字段齐全、来源合格且处于有效时间桶的快照。

### 2.3 P1：并行建设确定性回放

回放内核、as-of 数据门户、费用模型、市场状态、指标和资格门禁可以立即开发；正式候选资格考试必须等待所选策略 readiness 通过。

### 2.4 明确非目标

- 本计划不把 HRS 的“情绪”扩展为新的社交媒体或新闻因子。HRS 现行业务逻辑中的情绪输入来自资金费率和拥挤度；先保存真实原始输入与确定性派生值。
- 本计划不改变 AI Tuner 的优化目标、参数 manifest 或 LLM 提示词。
- 本计划不新增 Web 页面。
- 本计划不以补数为理由修改任何真实交易、订单或盈亏记录。

## 3. 文件结构

### 3.1 新建文件

- `ai_tuner/readiness/universe.py`：策略 universe 解析协议、静态 MTPCS universe、历史 HRS universe 解析。
- `ai_tuner/readiness/kline_coverage.py`：逐 symbol 覆盖计算、加权聚合、缺口区间模型。
- `ai_tuner/snapshots/models.py`：外部快照与 universe 快照的不可变模型。
- `ai_tuner/snapshots/repository.py`：快照幂等写入与 as-of 查询。
- `ai_tuner/snapshots/collector.py`：HRS universe 与外部数据采集编排。
- `ai_tuner/snapshots/cli.py`：`collect-once`、`backfill-supported`、`coverage` 命令。
- `database/postgres/init-scripts/13-ai-tuner-market-snapshots.sql`：两张历史快照表、唯一键和索引。
- `tests/test_ai_tuner_mvp/test_universe_resolver.py`。
- `tests/test_ai_tuner_mvp/test_kline_coverage.py`。
- `tests/test_ai_tuner_mvp/test_snapshot_repository.py`。
- `tests/test_ai_tuner_mvp/test_snapshot_collector.py`。
- `tests/test_ai_tuner_mvp/test_snapshot_coverage.py`。

### 3.2 修改文件

- `ai_tuner/readiness/auditor.py`：改为调用 universe/coverage/snapshot repository；修正百分比格式。
- `ai_tuner/readiness/models.py`：增加逐 symbol 细目和证据窗口字段，保持 JSON 向后兼容。
- `ai_tuner/config.yaml`：增加 universe 来源、K 线周期、快照频率和合格来源配置；原门禁阈值不变。
- `scripts/ai_tuner_mvp.py`：增加 `kline-audit` 与 `snapshot-coverage` 只读子命令。
- `services/kline_service/scripts/fetch_history_bulk.py`：支持精确 UTC 起止区间和失败汇总，不再只接受 `--days`。
- `tests/test_kline_service/test_fetch_history_bulk.py`：补充分页、闭合 K 线、精确区间和局部失败测试。
- `ai_tuner/Dockerfile`：打包 snapshot 模块和 CLI。
- `docker-compose.yml`：增加独立的 `ai-tuner-snapshot` 采集进程，复用 ai-tuner 镜像。
- `docs/superpowers/plans/2026-10-17-ai-tuner-mvp-safety-readiness.md`：实现后回标阶段二前置项进度。
- `docs/superpowers/specs/2026-10-16-purpose-driven-ai-tuner-design.md`：实现后记录真实口径，不改写原始设计。

### 3.3 复用但不重复实现

- `shared/binance_api.py`：复用 `get_open_interest_hist()`、`get_funding_rate_history()` 和当前 OI 方法。
- `shared/market_cap.py`：复用 provenance 接口，只接受 `coingecko`/未过期真实缓存来源。
- `services/kline_service/core/table_name_guard.py`：所有动态 K 线表名继续经过白名单校验。
- `docs/superpowers/plans/2026-10-17-ai-tuner-mvp-replay-qualification.md`：确定性回放主体的权威任务清单；本计划只定义其启动门禁和与新数据层的连接点。

## 4. 数据口径

### 4.1 策略 universe

`btc_eth_aggressive` 是静态 universe，来源固定为对应策略生效配置中的 `strategy.symbols`。审计必须记录配置文件哈希，防止后来修改配置后重解释历史结果。

HRS 是动态 universe，来源为每次候选池扫描完成时的 `active_symbols`。现有 `public.strategy_states` 只保留最后状态，不能作为历史证据，因此新增 append-only 历史表：

```sql
CREATE TABLE IF NOT EXISTS public.ai_tuner_universe_snapshots (
    strategy_id VARCHAR(64) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    membership VARCHAR(16) NOT NULL CHECK (membership IN ('short', 'long', 'both')),
    source_run_id VARCHAR(128) NOT NULL,
    config_hash CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (strategy_id, observed_at, symbol)
);
```

首次上线前不存在的 HRS universe 历史不得从当前候选池反推。审计报告必须写 `historical_universe_missing_before=<timestamp>`。

### 4.2 K 线覆盖

对每个 universe 成员、每个策略真实需要的周期分别计算：

```text
expected(symbol, interval) = 完整闭合时间桶数量
actual(symbol, interval)   = 窗口内 DISTINCT open_time 数量
coverage(symbol, interval) = min(actual / expected, 1.0)
weighted_coverage          = sum(actual) / sum(expected)
```

约束：

- 尚未闭合的最后一根 K 线不进入分母或分子。
- 重复行不增加覆盖率。
- 缺表按 0%，不能从其他 symbol 或其他周期替代。
- MTPCS 激进版需要 `1h/4h/1d`；HRS 由实际读取路径确定，首期至少覆盖 `1h/4h/1d`。
- readiness 硬门禁继续使用 `weighted_coverage >= 0.98`。
- 报告同时列出每个 symbol/interval 的 coverage 和连续缺口区间；暂不新增“单 symbol 最低覆盖率”硬门禁，避免未经校准增加业务阈值。

### 4.3 外部快照

```sql
CREATE TABLE IF NOT EXISTS public.ai_tuner_market_snapshots (
    strategy_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    bucket_start TIMESTAMPTZ NOT NULL,
    open_interest_contracts NUMERIC,
    open_interest_usd NUMERIC,
    funding_rate NUMERIC,
    market_cap_usd NUMERIC,
    oi_market_cap_ratio NUMERIC,
    sentiment_score NUMERIC,
    sentiment_formula_version VARCHAR(32) NOT NULL,
    oi_source VARCHAR(32) NOT NULL,
    funding_source VARCHAR(32) NOT NULL,
    market_cap_source VARCHAR(32) NOT NULL,
    source_observed_at TIMESTAMPTZ NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload_hash CHAR(64) NOT NULL,
    raw_payload JSONB NOT NULL,
    PRIMARY KEY (strategy_id, symbol, bucket_start)
);
```

一小时为首期统一时间桶。一个 `(symbol, bucket_start)` 只有在以下字段全部有效时才算完整：

- `open_interest_usd > 0`；
- `funding_rate` 非空；
- `market_cap_usd > 0`，且来源是 `coingecko` 或未过期、可追溯到 CoinGecko 的真实缓存；
- `oi_market_cap_ratio` 可由同一时点输入重算；
- `sentiment_score` 使用 HRS 当前已有的资金费率年化口径：`funding_rate * settlements_per_day * days_per_year * 100`，公式版本固定为 `funding_annual_pct_v1`；OI 拥挤度继续由独立的 `oi_market_cap_ratio` 表达，不额外发明组合权重；
- `source_observed_at <= bucket_end`，禁止使用未来值。

`oi_proxy` 和 `missing` 可保存用于诊断，但不得计入 HRS 95% 合格覆盖率。

覆盖率口径：

```text
expected = HRS universe snapshot 中全部 symbol-hour membership 数
complete = 同 symbol-hour 存在完整 market snapshot 的数量
external_snapshot_coverage = complete / expected
```

### 4.4 百分比格式

门禁比较使用原始浮点值，展示使用至少两位小数：

```python
def format_ratio(value: float) -> str:
    return f"{value:.2%}"
```

因此生产报告应显示 `97.96% < 98.00%`，不能显示为 `98.0% < 98.0%`。

## 5. 实施任务

### Task 1：修正 readiness 百分比显示精度

**Files:**
- Modify: `ai_tuner/readiness/auditor.py`
- Modify: `scripts/ai_tuner_mvp.py`
- Test: `tests/test_ai_tuner_mvp/test_readiness_auditor.py`
- Test: `tests/test_ai_tuner_mvp/test_cli.py`

- [ ] **Step 1：先写失败测试**

```python
def test_kline_failure_keeps_two_decimal_percentage(auditor):
    failures, _ = auditor._apply_gate_thresholds(
        closed_trades=100,
        signals_count=100,
        complete_ratio=1.0,
        identity_ratio=1.0,
        kline_coverage=0.9796,
        snapshot_coverage=1.0,
        regime_count=3,
        snapshot_threshold=0.95,
        snapshot_requirement="required",
    )
    assert "K线覆盖 97.96% < 98.00%" in failures
```

- [ ] **Step 2：验证测试先失败**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_readiness_auditor.py -q`

Expected: FAIL，现实现输出 `98.0% < 98.0%`。

- [ ] **Step 3：集中实现格式函数并替换门禁文案**

```python
def _format_ratio(value: float) -> str:
    return f"{value:.2%}"
```

所有 coverage/completeness/identity 的失败文案统一调用该函数；CLI 摘要同步使用相同函数，禁止两套精度。

- [ ] **Step 4：运行测试并提交**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_readiness_auditor.py tests/test_ai_tuner_mvp/test_cli.py -q`

Expected: 全部通过。

```bash
git add ai_tuner/readiness/auditor.py scripts/ai_tuner_mvp.py tests/test_ai_tuner_mvp/test_readiness_auditor.py tests/test_ai_tuner_mvp/test_cli.py
git commit -m "fix(ai-tuner): 精确显示 readiness 覆盖率"
```

### Task 2：定义策略 universe 解析契约

**Files:**
- Create: `ai_tuner/readiness/universe.py`
- Modify: `ai_tuner/config.yaml`
- Test: `tests/test_ai_tuner_mvp/test_universe_resolver.py`

- [ ] **Step 1：写静态与动态 universe 的失败测试**

```python
@pytest.mark.asyncio
async def test_aggressive_universe_comes_from_effective_config(resolver, as_of):
    result = await resolver.resolve("btc_eth_aggressive", as_of - timedelta(days=1), as_of)
    assert result.slices[0].symbols == ("BNBUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")


@pytest.mark.asyncio
async def test_hrs_missing_history_is_not_inferred_from_current_state(resolver, as_of):
    result = await resolver.resolve("hrs", as_of - timedelta(days=90), as_of)
    assert result.covered_from is None
    assert "historical_universe_missing" in result.gaps[0]
```

- [ ] **Step 2：验证导入失败**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_universe_resolver.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3：实现不可变模型和解析器**

```python
@dataclass(frozen=True)
class UniverseSlice:
    strategy_id: str
    valid_from: datetime
    valid_to: datetime
    symbols: tuple[str, ...]
    intervals: tuple[str, ...]
    source: str
    config_hash: str


@dataclass(frozen=True)
class UniverseResolution:
    slices: tuple[UniverseSlice, ...]
    covered_from: datetime | None
    gaps: tuple[str, ...]
```

实现 `StaticConfigUniverseResolver` 和 `HistoricalSnapshotUniverseResolver`。HRS 查询必须带 `observed_at <= as_of`；空历史返回 gap，不允许退回 `public.strategy_states` 当前值。

- [ ] **Step 4：在配置中声明来源和周期**

```yaml
purpose_driven_mvp:
  readiness:
    universe:
      btc_eth_aggressive:
        source: strategy_config
        config_path: strategies/btc_eth_aggressive/config.yaml
        symbol_path: strategy.symbols
        intervals: [1h, 4h, 1d]
      hrs:
        source: historical_snapshot
        intervals: [1h, 4h, 1d]
```

- [ ] **Step 5：运行测试并提交**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_universe_resolver.py -q`

Expected: 全部通过。

```bash
git add ai_tuner/readiness/universe.py ai_tuner/config.yaml tests/test_ai_tuner_mvp/test_universe_resolver.py
git commit -m "feat(ai-tuner): 定义策略 universe 历史口径"
```

### Task 3：实现逐 symbol K 线覆盖审计

**Files:**
- Create: `ai_tuner/readiness/kline_coverage.py`
- Modify: `ai_tuner/readiness/models.py`
- Modify: `ai_tuner/readiness/auditor.py`
- Test: `tests/test_ai_tuner_mvp/test_kline_coverage.py`

- [ ] **Step 1：写覆盖率不变量测试**

```python
def test_weighted_coverage_counts_missing_table_as_zero():
    rows = [
        SymbolCoverage("BTCUSDT", "1h", expected=24, actual=24),
        SymbolCoverage("ETHUSDT", "1h", expected=24, actual=0, gap="table_missing"),
    ]
    result = aggregate_coverage(rows)
    assert result.weighted_coverage == 0.5


def test_duplicate_open_times_do_not_raise_coverage(fake_db):
    fake_db.fetch_one.return_value = {"actual": 23, "expected": 24}
    # SQL 必须使用 COUNT(DISTINCT open_time)
```

- [ ] **Step 2：验证测试先失败**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_kline_coverage.py -q`

Expected: FAIL because module is missing。

- [ ] **Step 3：实现结果模型**

```python
@dataclass(frozen=True)
class SymbolCoverage:
    symbol: str
    interval: str
    expected: int
    actual: int
    missing_ranges: tuple[tuple[datetime, datetime], ...] = ()
    gap: str | None = None

    @property
    def coverage(self) -> float:
        return min(self.actual / self.expected, 1.0) if self.expected else 0.0


@dataclass(frozen=True)
class KlineCoverageResult:
    weighted_coverage: float
    symbols: tuple[SymbolCoverage, ...]
    evidence_start: datetime | None
    evidence_end: datetime | None
    gaps: tuple[str, ...]
```

- [ ] **Step 4：实现数据库查询**

每个 universe slice 使用 `generate_series()` 生成期望闭合桶，与实际表按 `open_time` 左连接。动态表名必须先通过 `table_name_guard`；值参数继续绑定，不拼接。

```sql
SELECT COUNT(*) AS expected,
       COUNT(DISTINCT k.open_time) AS actual
FROM generate_series($1::timestamp, $2::timestamp, $3::interval) AS expected(open_time)
LEFT JOIN <validated_table> k ON k.open_time = expected.open_time;
```

- [ ] **Step 5：替换旧主力合约口径**

删除 `_locate_kline_table()` 对 readiness 主流程的依赖。可以暂时保留函数供旧测试兼容，但 `_query_kline_coverage()` 必须只调用新 resolver/calculator。报告 JSON 新增 `kline_symbols`，旧 `kline_coverage` 字段保留并改为加权值。

- [ ] **Step 6：运行测试并提交**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_kline_coverage.py tests/test_ai_tuner_mvp/test_readiness_auditor.py tests/test_ai_tuner_mvp/test_cli.py -q`

Expected: 全部通过。

```bash
git add ai_tuner/readiness tests/test_ai_tuner_mvp/test_kline_coverage.py tests/test_ai_tuner_mvp/test_readiness_auditor.py tests/test_ai_tuner_mvp/test_cli.py
git commit -m "feat(ai-tuner): 按策略 universe 审计 K 线覆盖"
```

### Task 4：生成缺口计划并安全补采 K 线

**Files:**
- Modify: `services/kline_service/scripts/fetch_history_bulk.py`
- Modify: `scripts/ai_tuner_mvp.py`
- Test: `tests/test_kline_service/test_fetch_history_bulk.py`
- Test: `tests/test_ai_tuner_mvp/test_cli.py`

- [ ] **Step 1：写精确区间与局部失败测试**

```python
@pytest.mark.asyncio
async def test_collect_range_never_requests_outside_utc_window(collector):
    await collect_range(collector, "BTCUSDT", "1h", START, END)
    for call in collector.collect_klines.await_args_list:
        assert call.kwargs["start_time"] >= int(START.timestamp() * 1000)
        assert call.kwargs["end_time"] <= int(END.timestamp() * 1000)


@pytest.mark.asyncio
async def test_one_symbol_failure_is_reported_and_exit_is_nonzero(runner):
    result = await runner.run(plan_with_one_failure)
    assert result.failed == [("ETHUSDT", "4h")]
```

- [ ] **Step 2：扩展补采参数**

新增互斥参数：

```text
--days N
或
--start 2026-07-23T00:00:00Z --end 2026-10-21T00:00:00Z
```

结束时间必须按 interval 向下对齐，只采闭合 K 线。任一 symbol/interval 失败时最终 exit code 为 1，并打印结构化失败列表，不能“部分失败但整体成功”。

- [ ] **Step 3：增加只读缺口命令**

`python3 scripts/ai_tuner_mvp.py kline-audit --strategy btc_eth_aggressive --as-of 2026-10-21T00:00:00Z --output /tmp/btc-eth-aggressive-kline-audit.json`

输出字段固定为：`strategy_id`、`window`、`weighted_coverage`、`symbols[]`、`missing_ranges[]`、`recommended_commands[]`。命令默认只生成建议，不自动补采。

- [ ] **Step 4：验证 dry-run**

Run: `python3 scripts/ai_tuner_mvp.py kline-audit --strategy btc_eth_aggressive --as-of 2026-10-21T00:00:00Z --output /tmp/kline-audit.json`

Expected: exit 0；JSON 可解析；不产生数据库写入。

- [ ] **Step 5：运行测试并提交**

Run: `python3 -m pytest tests/test_kline_service/test_fetch_history_bulk.py tests/test_ai_tuner_mvp/test_cli.py -q`

Expected: 全部通过。

```bash
git add services/kline_service/scripts/fetch_history_bulk.py scripts/ai_tuner_mvp.py tests/test_kline_service/test_fetch_history_bulk.py tests/test_ai_tuner_mvp/test_cli.py
git commit -m "feat(kline): 按审计缺口精确补采历史数据"
```

### Task 5：建立 append-only 外部快照表与仓储层

**Files:**
- Create: `database/postgres/init-scripts/13-ai-tuner-market-snapshots.sql`
- Create: `ai_tuner/snapshots/__init__.py`
- Create: `ai_tuner/snapshots/models.py`
- Create: `ai_tuner/snapshots/repository.py`
- Test: `tests/test_ai_tuner_mvp/test_snapshot_repository.py`

- [ ] **Step 1：写幂等、时点和来源测试**

```python
@pytest.mark.asyncio
async def test_upsert_same_bucket_is_idempotent(repository, complete_snapshot):
    await repository.store_market_snapshot(complete_snapshot)
    await repository.store_market_snapshot(complete_snapshot)
    assert await repository.count_bucket(complete_snapshot.key) == 1


def test_future_source_timestamp_is_rejected(complete_snapshot):
    with pytest.raises(ValueError, match="future source"):
        replace(complete_snapshot, source_observed_at=complete_snapshot.bucket_end + timedelta(seconds=1))


def test_proxy_market_cap_is_saved_but_not_complete(complete_snapshot):
    snapshot = replace(complete_snapshot, market_cap_source="oi_proxy")
    assert snapshot.is_complete is False
```

- [ ] **Step 2：验证测试先失败**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_snapshot_repository.py -q`

Expected: FAIL because snapshot modules are missing。

- [ ] **Step 3：实现 DDL**

使用 §4.1 与 §4.3 的字段；额外建立：

```sql
CREATE INDEX IF NOT EXISTS idx_ai_tuner_market_snapshots_lookup
ON public.ai_tuner_market_snapshots(strategy_id, symbol, bucket_start DESC);

CREATE INDEX IF NOT EXISTS idx_ai_tuner_universe_snapshots_lookup
ON public.ai_tuner_universe_snapshots(strategy_id, observed_at DESC, symbol);
```

只授予 `trading_user` 所需的 `SELECT/INSERT/UPDATE`；不授予 `DELETE`，历史证据禁止运行时删除。

- [ ] **Step 4：实现仓储层**

写入使用主键冲突更新，但只允许相同 bucket 用更完整、来源更好的数据替换；禁止完整真实值被 `missing/oi_proxy` 降级覆盖。所有查询必须接受显式 `as_of`。

- [ ] **Step 5：运行测试并提交**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_snapshot_repository.py -q`

Expected: 全部通过。

```bash
git add database/postgres/init-scripts/13-ai-tuner-market-snapshots.sql ai_tuner/snapshots tests/test_ai_tuner_mvp/test_snapshot_repository.py
git commit -m "feat(ai-tuner): 建立 HRS 外部时点快照仓储"
```

### Task 6：实现 HRS universe 与外部数据采集器

**Files:**
- Create: `ai_tuner/snapshots/collector.py`
- Create: `ai_tuner/snapshots/cli.py`
- Modify: `strategies/hrs/strategy.py`
- Test: `tests/test_ai_tuner_mvp/test_snapshot_collector.py`

- [ ] **Step 1：写“只旁路留证、不影响交易”测试**

```python
@pytest.mark.asyncio
async def test_snapshot_failure_never_changes_candidate_scan_result(strategy):
    strategy.snapshot_repository.store_universe.side_effect = RuntimeError("db down")
    result = await strategy._perform_candidate_scan("00:00")
    assert strategy.candidate_pool.get_active_symbols()
    assert strategy.logger.warning.called


@pytest.mark.asyncio
async def test_collector_uses_only_observed_universe(collector):
    await collector.collect_once(as_of=AS_OF)
    requested = {c.args[0] for c in collector.binance.get_open_interest.await_args_list}
    assert requested == {"BTCUSDT", "ETHUSDT"}
```

- [ ] **Step 2：在 HRS 候选池扫描后追加 universe 历史快照**

在现有 `save_strategy_state(self.db, "hrs", {}, extra_data={"symbols": pool_symbols, "scanned_at": observed_at.isoformat()}, state_key="candidate_pool")` 之后调用 repository。异常只记录 warning，不得抛出到候选池流程；写入内容包括 short/long/both membership、scan time、配置哈希和稳定 `source_run_id`。

- [ ] **Step 3：实现按小时外部采集**

```python
async def collect_once(as_of: datetime) -> CollectionSummary:
    universe = await repository.latest_universe("hrs", as_of)
    for symbol in universe.symbols:
        oi = await binance.get_open_interest(symbol)
        funding = await binance.get_funding_rate(symbol)
        ticker = await ticker_source.get(symbol)
        oi_usd = oi * float(ticker["lastPrice"])
        cap = await market_cap.get_market_cap_with_provenance(
            symbol, oi_usd=oi_usd, volume_24h=float(ticker["quoteVolume"])
        )
        snapshot = build_snapshot(
            strategy_id="hrs", symbol=symbol, observed_at=as_of,
            open_interest_contracts=oi, open_interest_usd=oi_usd,
            funding_rate=funding, market_cap=cap,
            formula_version="funding_annual_pct_v1",
        )
        await repository.store_market_snapshot(snapshot)
```

使用有界并发和现有 retry；单币失败不能取消其他币种。摘要必须返回 requested/complete/proxy/missing/failed 计数。

- [ ] **Step 4：实现受支持的历史回填**

`backfill-supported` 只回填 Binance 明确返回的历史 OI 与资金费率；市值只有在来源能提供对应历史时点且许可证/API 权限允许时才填。否则保持 `market_cap_usd=NULL`，不得拿当前市值覆盖过去。

- [ ] **Step 5：运行测试并提交**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_snapshot_collector.py strategies/hrs/tests -q`

Expected: 全部通过。

```bash
git add ai_tuner/snapshots strategies/hrs/strategy.py tests/test_ai_tuner_mvp/test_snapshot_collector.py
git commit -m "feat(ai-tuner): 采集 HRS universe 与外部快照"
```

### Task 7：把真实快照覆盖接入 readiness

**Files:**
- Modify: `ai_tuner/readiness/auditor.py`
- Modify: `ai_tuner/readiness/models.py`
- Modify: `scripts/ai_tuner_mvp.py`
- Test: `tests/test_ai_tuner_mvp/test_snapshot_coverage.py`

- [ ] **Step 1：写覆盖率与 fail-closed 测试**

```python
@pytest.mark.asyncio
async def test_snapshot_coverage_uses_complete_symbol_hours_only(auditor):
    result = await auditor._query_external_snapshot("hrs", START, END)
    assert result.expected == 10
    assert result.complete == 9
    assert result.coverage == 0.9


@pytest.mark.asyncio
async def test_missing_universe_history_cannot_pass_snapshot_gate(auditor):
    result = await auditor._query_external_snapshot("hrs", START, END)
    assert result.coverage == 0.0
    assert "historical_universe_missing" in result.gaps
```

- [ ] **Step 2：替换恒返回 0 的 stub**

把 `_query_external_snapshot()` 改为接收 `strategy_id/window_start/window_end`，查询 universe symbol-hour 后与完整 snapshot 左连接。MTPCS 激进版仍执行查询以生成报告项，但 gate 根据 `not_required_for_current_manifest` 忽略该值。

- [ ] **Step 3：扩展 JSON 证据**

增加 `external_snapshot_expected`、`external_snapshot_complete`、`external_snapshot_evidence_start`、`external_snapshot_sources`；保留原 `external_snapshot_coverage` 字段。

- [ ] **Step 4：运行测试并提交**

Run: `python3 -m pytest tests/test_ai_tuner_mvp/test_snapshot_coverage.py tests/test_ai_tuner_mvp/test_readiness_auditor.py tests/test_ai_tuner_mvp/test_cli.py -q`

Expected: 全部通过。

```bash
git add ai_tuner/readiness scripts/ai_tuner_mvp.py tests/test_ai_tuner_mvp
git commit -m "feat(ai-tuner): 用真实 HRS 快照执行 readiness 门禁"
```

### Task 8：部署独立快照采集进程并建立可观察性

**Files:**
- Modify: `ai_tuner/Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `ai_tuner/config.yaml`
- Test: `tests/test_ai_tuner_mvp/test_image_packaging.py`

- [ ] **Step 1：写镜像和 compose 失败测试**

```python
def test_ai_tuner_image_contains_snapshot_cli():
    text = Path("ai_tuner/Dockerfile").read_text()
    assert "ai_tuner/snapshots" in text


def test_snapshot_service_cannot_enable_auto_apply():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    service = compose["services"]["ai-tuner-snapshot"]
    assert service["command"] == ["python3", "-m", "ai_tuner.snapshots.cli", "daemon"]
    assert "AUTO_APPLY" not in service.get("environment", {})
```

- [ ] **Step 2：增加独立服务**

服务复用 `trading-ai-tuner:latest`，每小时第 20 分钟采集；配置 `restart: unless-stopped`、数据库健康依赖和只读策略配置挂载。它不得挂载 `tuning_overrides` 为可写。

- [ ] **Step 3：增加健康与停滞指标**

日志至少包含：`bucket_start`、universe 数、完整快照数、proxy 数、missing 数、失败数、连续失败次数。连续 3 个时间桶失败时发送现有错误通知，但不停止 HRS 实盘策略。

- [ ] **Step 4：运行全套测试并提交**

Run: `python3 -m pytest tests/test_ai_tuner_mvp tests/test_kline_service strategies/hrs/tests -q`

Expected: 全部通过，无新增 warning。

```bash
git add ai_tuner/Dockerfile docker-compose.yml ai_tuner/config.yaml tests/test_ai_tuner_mvp/test_image_packaging.py
git commit -m "feat(ai-tuner): 部署独立 HRS 快照采集器"
```

### Task 9：执行生产 K 线审计、补采和复审

**Files:**
- Create after execution: `docs/reports/ai-tuner-kline-coverage-<UTC审计日期>.json`，日期由审计命令的 `--as-of` 自动生成，不接受人工自由命名
- Modify after evidence: `docs/superpowers/plans/2026-10-17-ai-tuner-mvp-safety-readiness.md`

- [x] **Step 1：先部署只读审计能力**

按常规流程完成测试、代码审查、合并、push、Actions 和生产容器版本核验。尚未执行补采前，先运行 `kline-audit` 保存真实缺口报告。

- [x] **Step 2：人工核对补采计划边界**

确认所有建议命令只包含两个候选策略真实 universe、批准周期和 90 天窗口；不得采集任意未登记 symbol，不得写交易表。

- [x] **Step 3：执行补采并记录结果**

逐 symbol/interval 执行精确区间补采。每批后重新运行只读审计；失败项保留，不得用手工 SQL 伪造 bar。

- [x] **Step 4：重跑 readiness**

Expected:

- `btc_eth_aggressive.kline_coverage >= 0.98`，否则保持 STOP 并列出残余缺口；
- HRS 可能仍因历史 universe/外部快照不足而 STOP，这是预期结果；
- 激进版有效信号未满 100 时仍为 STOP，不调整阈值。

- [x] **Step 5：提交证据文档**

真实报告中不得包含凭据、数据库连接串或原始敏感 payload。

**执行结果（2026-10-21）**：生产补采共新增 260 根 K 线，激进版 90 天完整 universe 加权覆盖率达到 `1.0`，`missing_ranges=[]`、`recommended_commands=[]`；完整 readiness 保持 `STOP`，原因转为激进版有效信号 `6 < 100`，HRS 仍缺历史 universe 与外部快照。证据文件：`docs/reports/ai-tuner-kline-coverage-2026-10-21.json`。

### Task 10：启动确定性回放基础设施

**Authoritative plan:** `docs/superpowers/plans/2026-10-17-ai-tuner-mvp-replay-qualification.md`

本任务不是重新实现另一套回放器，而是按权威计划依次执行以下内容：

- [ ] **Step 1：完成 replay models + deterministic clock**

执行权威计划 Task 1，验证相同输入两次运行事件顺序完全一致。

- [ ] **Step 2：完成 as-of data portal**

执行权威计划 Task 2；Postgres portal 的 K 线读取改为消费本计划 Task 2/3 的 universe 与覆盖证据，外部输入改为消费 Task 5/7 的 snapshot repository。任何未来数据访问抛 `FutureDataAccess`。

- [ ] **Step 3：完成 broker、费用和资金费率模型**

执行权威计划 Task 3；基础费率来自配置，1.5 倍成本压力由资格考试层注入，不能在 broker 内硬编码。

- [ ] **Step 4：完成 MTPCS 激进版 replay adapter**

优先只接 `btc_eth_aggressive`。HRS adapter 可以建立接口和测试桩，但在 HRS 外部快照覆盖未达 95% 前不得生成“通过”结果。

- [ ] **Step 5：完成市场状态、指标、硬门禁与金样对账**

继续执行权威计划的 regime、metrics、qualification、parity tasks。`regime_count` 只有在真实 classifier 输出落地后才能替换当前 proxy。

- [ ] **Step 6：增加 readiness 前置检查**

`replay`、`parity` 和 `qualify` 命令启动前先读取同一 `as_of` 的 readiness：

```python
if not readiness.candidates[strategy_id].eligible:
    raise QualificationBlocked(readiness.candidates[strategy_id].failures)
```

允许 `--diagnostic-only` 在未达标时运行框架自测，但结果必须带 `NOT_QUALIFICATION_EVIDENCE`，且不得进入排名或自动生效。

- [ ] **Step 7：执行权威计划测试矩阵**

Run: `python3 -m pytest tests/test_ai_tuner_mvp -q`

Expected: 全部通过；确定性测试连续运行两次结果哈希相同。

### Task 11：阶段二综合验收与文档回标

**Files:**
- Modify: `docs/superpowers/plans/2026-10-17-ai-tuner-mvp-roadmap.md`
- Modify: `docs/superpowers/plans/2026-10-17-ai-tuner-mvp-safety-readiness.md`
- Modify: `docs/superpowers/specs/2026-10-16-purpose-driven-ai-tuner-design.md`
- Create after execution: `docs/reports/ai-tuner-stage2-readiness-acceptance-<UTC验收日期>.md`，日期由验收脚本自动生成

- [ ] **Step 1：运行完整自动测试**

Run: `python3 -m pytest tests/test_ai_tuner_mvp tests/test_kline_service strategies/hrs/tests -q`

Expected: 全部通过。

- [ ] **Step 2：运行生产只读审计**

报告必须同时包含旧顶层字段和新逐 symbol/快照证据，且 `decision` 由真实数据决定。

- [ ] **Step 3：核对五条不可伪造事实**

1. 激进版有效信号来自 `trade_signals`，不是交易记录推断。
2. K 线 coverage 来自完整 universe 的实际闭合 bar。
3. HRS 历史 universe 来自 append-only snapshot，不是当前状态反推。
4. 市值 proxy 不计为合格快照。
5. readiness 未通过时资格考试和 auto-apply 均被阻断。

- [ ] **Step 4：按结果给出阶段结论**

- `READY_FOR_PARITY`：所选策略 readiness 全部通过，可以开始金样对账；
- `COLLECTING_EVIDENCE`：采集正常但时间覆盖不足，继续自动积累；
- `DATA_PIPELINE_FAILED`：采集停滞、来源失真或覆盖下降，先修数据；
- 不得在本任务直接输出 `GO_LIVE`。

- [ ] **Step 5：提交文档**

```bash
git add docs/superpowers docs/reports
git commit -m "docs(ai-tuner): 回标阶段二数据准备与回放状态"
```

## 6. 开发与上线顺序

```text
批次 A（可立即上线，低风险）
  Task 1 报告精度
  Task 2 universe 契约
  Task 3 逐 symbol K 线覆盖
  Task 4 只读缺口计划
        ↓ 门禁 A：审计输出可信、无写入

批次 B（数据修复）
  Task 4 精确补采
  Task 9 生产 K 线复审
        ↓ 门禁 B：激进版 K 线 >= 98%

批次 C（与 A/B 并行）
  Task 5 快照表与仓储
  Task 6 采集器
  Task 7 readiness 接线
  Task 8 独立服务部署
        ↓ 门禁 C：采集持续运行、无当前值伪造历史

批次 D（与证据积累并行）
  Task 10 回放内核与 diagnostic-only
        ↓ 门禁 D：金样对账通过后才能正式 qualify

最终
  Task 11 综合验收
```

每个批次必须独立提交、独立审查、独立部署。批次门禁不通过时停止后续生产动作，但不阻止其他独立批次继续开发。

## 7. 时间预估

以下是开发工期，不是数据自然积累时间：

| 工作包 | 预计开发与测试 | 数据生效等待 |
|---|---:|---:|
| 报告精度 + universe/K 线审计 | 2～4 个工作日 | 补采完成后可立即复审 |
| HRS 快照表、采集器、readiness 接线 | 4～7 个工作日 | 若无法可靠回填历史市值/universe，达到 90 天窗口 95% 覆盖约需 86 天 |
| 确定性回放基础与 MTPCS 激进版 adapter | 10～15 个工作日 | 正式资格考试仍等待激进版 100 条有效信号 |
| 金样对账、资格门禁与综合验收 | 5～10 个工作日 | 取决于 readiness 与金样差异修复次数 |

激进版当前信号从 2 条增长到 4 条，说明采集链路正常，但样本仍太少。按当前非常早期速度只能给出 2～8 周区间，自动任务每日复审；工程开发不应等待这个计数。

## 8. 风险与对策

| 风险 | 处理 |
|---|---|
| 加权覆盖掩盖单币严重缺口 | 报告逐 symbol 明细与缺口；首期不擅自新增阈值，基准回放后再校准单币门禁 |
| HRS 历史 universe 不存在 | 从上线时点 append-only 留存；旧时间段 fail-closed，不用当前池反推 |
| CoinGecko 无可靠历史市值 | 不回填；只保存真实 forward snapshot，HRS 继续 `COLLECTING_EVIDENCE` |
| OI/资金费率 API 局部失败 | 单币隔离、重试、保存失败摘要；不以 0 代替缺失值 |
| 快照采集影响 HRS 实盘 | 独立进程；HRS 内 universe 旁路写入失败只告警，不改变候选池结果 |
| K 线补采触发限流 | 按缺口区间分页、有界并发、批次复审，不全库盲目重采 |
| 回放框架先完成但数据未达标 | 只允许 `diagnostic-only`，显式标记非资格证据，禁止自动生效 |
| 旧 AI Tuner 越过门禁 | `purpose_driven_mvp.mode=audit` 保持不变；回归测试验证候选策略不写覆盖层 |

## 9. 完成定义

只有同时满足以下条件，本计划才算完成：

- readiness 报告精确显示 `97.96% < 98.00%` 类比较。
- K 线覆盖使用完整策略 universe 的逐 symbol 明细和加权聚合，不再使用主力合约抽样。
- 缺口补采可按精确 UTC 区间运行，部分失败会整体报错且可重试。
- HRS universe 与外部数据按时点 append-only 保存，来源和公式版本可追溯。
- HRS 快照覆盖率由完整 symbol-hour 计算，proxy/缺失不算合格。
- 快照采集失败不影响 HRS 实盘交易流程。
- 回放的数据门户禁止未来数据，并能消费新的 universe/K 线/快照证据。
- readiness 未通过时 `qualify` 和候选自动生效均被程序阻断。
- 生产审计、容器版本、测试结果和差异报告均留有证据。
- 所有既有门禁阈值保持不变。

## 10. 交付后的实际效果

完成本计划不会立即让 AI Tuner 自动改实盘参数；它会得到四项关键能力：

1. 清楚知道每个策略、每个 symbol、每个周期缺哪段 K 线，并能安全补齐。
2. 从现在开始持续积累 HRS 可回放的真实外部时点证据。
3. 在信号等待期并行完成确定性回放框架，不浪费时间。
4. 一旦 MTPCS 激进版达到 100 条信号且 K 线门禁通过，可以立即进入金样对账，而不是届时才开始搭基础设施。

在阶段二金样对账、资格考试和后续时间机器均通过之前，生产状态仍应保持 `audit + selected_strategy=null`。
