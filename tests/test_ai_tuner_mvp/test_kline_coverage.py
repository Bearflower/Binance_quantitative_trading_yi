# -*- coding: utf-8 -*-
"""
阶段二 Task 3：逐 symbol K 线覆盖审计单元测试

覆盖：
1. aggregate_coverage 加权聚合（缺表按 0%，BTC 24/24 + ETH 0/24 → 0.5）
2. 重复 open_time 不提高覆盖（SQL 必须用 COUNT(DISTINCT open_time)）
3. 闭合 bar 排除（window_end 落在桶中间时 expected 不含未闭合那根）
4. 表不存在 → coverage 0 + gap="table_missing"
5. HRS 历史 universe 缺失 → weighted 0.0 且 gaps 含 historical_universe_missing，
   且未查询任何 K 线表（mock 断言 db.fetch_one 未被用于 K 线表）
6. 未配置 universe 的 strategy_id → 0.0 + gap，不抛异常
7. missing_ranges 连续区间合并正确（3 个相邻缺失桶 → 1 个区间）
8. _query_kline_coverage 不再调用 _detect_dominant_symbol（mock 断言）
9. ReadinessResult.to_dict 在含 kline_symbols 时 json.dumps 不抛异常
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 仓库根常量：test_kline_coverage.py 在 tests/test_ai_tuner_mvp/ → parents[2] = 仓库根
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 注意：auditor 实例化时会读取 config 里的 universe 段（如果存在），
# 但 kline_coverage.compute_kline_coverage 接收的是显式 universe_cfg，
# 故这里需要提供一个包含 universe 段的最小 config。


# ===========================================================================
# 模型纯函数测试（不依赖 DB）
# ===========================================================================


def test_weighted_coverage_counts_missing_table_as_zero():
    """计划原文：BTC 24/24 + ETH 0/24 → 0.5"""
    from ai_tuner.readiness.kline_coverage import (
        SymbolCoverage, aggregate_coverage,
    )

    rows = [
        SymbolCoverage("BTCUSDT", "1h", expected=24, actual=24),
        SymbolCoverage("ETHUSDT", "1h", expected=24, actual=0, gap="table_missing"),
    ]
    result = aggregate_coverage(rows)
    assert result.weighted_coverage == 0.5
    assert len(result.symbols) == 2
    assert result.symbols[0].coverage == 1.0
    assert result.symbols[1].coverage == 0.0


def test_weighted_coverage_sum_expected_zero_is_zero():
    """expected 全部为 0 → 加权覆盖 0.0（除零守卫）"""
    from ai_tuner.readiness.kline_coverage import (
        SymbolCoverage, aggregate_coverage,
    )

    rows = [
        SymbolCoverage("BTCUSDT", "1h", expected=0, actual=0),
    ]
    result = aggregate_coverage(rows)
    assert result.weighted_coverage == 0.0


def test_generate_series_uses_half_open_window():
    """SQL 必须实现 [start, end)，不能把 end 边界当成额外一根 K 线。"""
    from ai_tuner.readiness.kline_coverage import _COUNT_SQL, _MISSING_SQL

    assert "$2::{boundary_cast} - $3::interval" in _COUNT_SQL
    assert "$2::{boundary_cast} - $3::interval" in _MISSING_SQL


def test_duplicate_open_times_do_not_raise_coverage():
    """Fake DB 返回 actual=23, expected=24 → 覆盖率约 0.9583
    同时断言 SQL 含 COUNT(DISTINCT open_time) 以证明重复行不提高覆盖。"""
    import asyncio

    from ai_tuner.readiness.kline_coverage import compute_kline_coverage
    from tests.test_ai_tuner_mvp.conftest import FakeDB

    async def run():
        strategy_id = "btc_eth_aggressive"
        universe_cfg = {
            "btc_eth_aggressive": {
                "source": "strategy_config",
                "config_path": "strategies/btc_eth_aggressive/config.yaml",
                "symbol_path": "strategy.symbols",
                "intervals": ["1h"],
            },
        }
        # 表存在 + 列类型为 naive timestamp
        # count SQL 精确子串路由 → 返回 23/24；missing SQL 返回空列表（不关心）
        responses = {
            **FakeDB().responses,
            "SELECT 1 FROM information_schema.tables": {"?column?": 1},
            "information_schema.columns": {"data_type": "timestamp without time zone"},
            "COUNT(*) AS expected": {"actual": 23, "expected": 24},
            "WHERE k.open_time IS NULL": [],
        }
        db = FakeDB(responses=responses)
        as_of = datetime(2026, 10, 17, tzinfo=timezone.utc)
        project_root = _PROJECT_ROOT
        result = await compute_kline_coverage(
            db,
            strategy_id=strategy_id,
            universe_cfg=universe_cfg,
            project_root=project_root,
            window_start=as_of - timedelta(hours=24),
            window_end=as_of,
        )
        # 覆盖率约 23/24 = 0.958333...
        assert abs(result.weighted_coverage - 23 / 24) < 1e-6, (
            f"覆盖率应为 23/24≈0.9583，实际 {result.weighted_coverage}"
        )
        # 断言 SQL 含 COUNT(DISTINCT open_time)
        generate_sql = next((s for s in db.sql_log if "generate_series" in s and "COUNT" in s), "")
        assert "COUNT(DISTINCT" in generate_sql, (
            f"K 线覆盖 SQL 必须含 COUNT(DISTINCT open_time)，实际: {generate_sql}"
        )

    asyncio.run(run())


# ===========================================================================
# 闭合 bar / 窗口对齐
# ===========================================================================


@pytest.mark.asyncio
async def test_closed_bar_excludes_partial_last_bar():
    """window_end 落在桶中间时 expected 不含未闭合那根。

    窗口: 2026-10-17 01:00 → 2026-10-18 01:30（中间落在 1h bar 01:30）
    闭合对齐后 window_end 应向下取整到 01:00 → 期望 24 根（不是 24.5）。
    """
    from ai_tuner.readiness.kline_coverage import compute_kline_coverage
    from tests.test_ai_tuner_mvp.conftest import FakeDB

    responses = {
        **FakeDB().responses,
        "SELECT 1 FROM information_schema.tables": {"?column?": 1},
        "information_schema.columns": {"data_type": "timestamp without time zone"},
        "COUNT(*) AS expected": {"actual": 24, "expected": 24},
        "WHERE k.open_time IS NULL": [],
    }
    db = FakeDB(responses=responses)

    universe_cfg = {
        "btc_eth_aggressive": {
            "source": "strategy_config",
            "config_path": "strategies/btc_eth_aggressive/config.yaml",
            "symbol_path": "strategy.symbols",
            "intervals": ["1h"],
        },
    }
    window_start = datetime(2026, 10, 17, 1, 0, tzinfo=timezone.utc)
    window_end = datetime(2026, 10, 18, 1, 30, tzinfo=timezone.utc)
    project_root = _PROJECT_ROOT

    await compute_kline_coverage(
        db,
        strategy_id="btc_eth_aggressive",
        universe_cfg=universe_cfg,
        project_root=project_root,
        window_start=window_start,
        window_end=window_end,
    )

    # 从 generate_series SQL 里找窗口绑定参数（有 COUNT(*) 那个是 count，不是 missing）
    count_calls = [
        c for c in db.call_args_log
        if "generate_series" in c[1] and "COUNT(*)" in c[1]
    ]
    assert count_calls, f"应有 generate_series COUNT 调用，实际 call_args_log={db.call_args_log}"
    _, sql, args = count_calls[0]
    ws, we, _ = args[:3]
    # window_end 向下对齐到 1h 边界
    assert we.minute == 0 and we.second == 0, (
        f"window_end 应向下对齐到 1h 边界，实际 minute={we.minute} second={we.second}"
    )
    # window_start 向上对齐到 1h 边界
    assert ws.minute == 0 and ws.second == 0


# ===========================================================================
# 表不存在 → coverage 0 + gap="table_missing"
# ===========================================================================


@pytest.mark.asyncio
async def test_missing_table_produces_zero_coverage_with_gap():
    """information_schema.tables 查不到表 → expected 仍按应有桶数，actual=0，
    gap="table_missing"。"""
    from ai_tuner.readiness.kline_coverage import compute_kline_coverage

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock(side_effect=[
        # 表不存在探测返回 None
        None,
        # 第二次 generate_series 计数仍执行（expected 按应有桶数计，actual=0）
        {"actual": 0, "expected": 24},
    ])
    fake_db.fetch_all = AsyncMock(return_value=[])

    universe_cfg = {
        "btc_eth_aggressive": {
            "source": "strategy_config",
            "config_path": "strategies/btc_eth_aggressive/config.yaml",
            "symbol_path": "strategy.symbols",
            "intervals": ["1h"],
        },
    }
    as_of = datetime(2026, 10, 18, tzinfo=timezone.utc)
    project_root = _PROJECT_ROOT

    result = await compute_kline_coverage(
        fake_db,
        strategy_id="btc_eth_aggressive",
        universe_cfg=universe_cfg,
        project_root=project_root,
        window_start=as_of - timedelta(hours=24),
        window_end=as_of,
    )
    # 加权覆盖为 0（actual=0, expected=24）
    assert result.weighted_coverage == 0.0
    # 至少有一个 symbol/interval 的 gap="table_missing"
    table_missing_rows = [s for s in result.symbols if s.gap == "table_missing"]
    assert table_missing_rows, "至少一个 pair 应带 gap=table_missing"
    expected_range = (
        (as_of - timedelta(hours=24)).replace(tzinfo=None),
        as_of.replace(tzinfo=None),
    )
    assert all(row.missing_ranges == (expected_range,) for row in table_missing_rows), (
        "缺表代表整个闭合窗口缺失，必须生成完整窗口的精确补采区间"
    )


@pytest.mark.asyncio
async def test_table_probe_failure_does_not_invent_backfill_range():
    """information_schema 查询失败是未知状态，不能伪装成确定缺表。"""
    from ai_tuner.readiness.kline_coverage import _single_pair_coverage

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock(side_effect=RuntimeError("database unavailable"))
    as_of = datetime(2026, 10, 18, tzinfo=timezone.utc)

    row = await _single_pair_coverage(
        fake_db,
        "BTCUSDT",
        "1h",
        as_of - timedelta(hours=24),
        as_of,
    )

    assert row.expected == 24
    assert row.actual == 0
    assert row.gap == "table_probe_failed"
    assert row.missing_ranges == ()


# ===========================================================================
# HRS 历史 universe 缺失 → weighted 0.0 + historical_universe_missing + 未查 K 线
# ===========================================================================


@pytest.mark.asyncio
async def test_hrs_missing_history_produces_zero_without_querying_kline():
    """HRS historical_snapshot 源且 public.ai_tuner_universe_snapshots 为空 →
    weighted 0.0 且 gaps 含 historical_universe_missing，且**未**查询任何 K 线表。"""
    from ai_tuner.readiness.kline_coverage import compute_kline_coverage

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock(return_value=None)
    fake_db.fetch_all = AsyncMock(return_value=[])

    universe_cfg = {
        "hrs": {
            "source": "historical_snapshot",
            "intervals": ["1h"],
        },
    }
    as_of = datetime(2026, 10, 18, tzinfo=timezone.utc)
    project_root = _PROJECT_ROOT

    result = await compute_kline_coverage(
        fake_db,
        strategy_id="hrs",
        universe_cfg=universe_cfg,
        project_root=project_root,
        window_start=as_of - timedelta(hours=24),
        window_end=as_of,
    )
    assert result.weighted_coverage == 0.0
    assert any("historical_universe_missing" in g for g in result.gaps), result.gaps
    # 断言：没有任何对 K 线表的查询（generate_series SQL 不应出现）
    all_fetch_one_sqls = [
        c.args[0] for c in fake_db.fetch_one.call_args_list
        if c.args
    ]
    assert not any("generate_series" in s for s in all_fetch_one_sqls), (
        "HRS 历史 universe 缺失时不应查询 K 线表"
    )


# ===========================================================================
# 未配置 universe → 0.0 + gap，不抛异常
# ===========================================================================


@pytest.mark.asyncio
async def test_strategy_without_universe_config_graceful():
    """候选策略未在 universe 段声明 → 0.0 + gap，不抛异常。"""
    from ai_tuner.readiness.kline_coverage import compute_kline_coverage

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock()
    fake_db.fetch_all = AsyncMock()

    # universe_cfg 里不包含目标 strategy_id
    universe_cfg = {
        "some_other_strategy": {
            "source": "strategy_config",
            "config_path": "strategies/btc_eth_aggressive/config.yaml",
            "symbol_path": "strategy.symbols",
            "intervals": ["1h"],
        },
    }
    as_of = datetime(2026, 10, 18, tzinfo=timezone.utc)
    project_root = _PROJECT_ROOT

    # 不抛任何异常
    result = await compute_kline_coverage(
        fake_db,
        strategy_id="btc_eth_aggressive",
        universe_cfg=universe_cfg,
        project_root=project_root,
        window_start=as_of - timedelta(hours=24),
        window_end=as_of,
    )
    assert result.weighted_coverage == 0.0
    assert len(result.symbols) == 0
    gap_text = " ".join(result.gaps)
    assert "universe_not_configured" in gap_text, result.gaps


# ===========================================================================
# missing_ranges 连续区间合并
# ===========================================================================


def test_missing_ranges_merge_adjacent_buckets():
    """3 个相邻缺失桶 → 合并成 1 个连续区间（左闭右开）"""
    from ai_tuner.readiness.kline_coverage import _merge_missing_starts

    base = datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc)
    # 三个连续缺失桶
    starts = (base, base + timedelta(hours=1), base + timedelta(hours=2))
    result = _merge_missing_starts(starts, interval="1h")
    # 合并后一个区间：[base, base + 3h)
    assert len(result) == 1
    start, end = result[0]
    assert start == base
    assert end == base + timedelta(hours=3)


def test_missing_ranges_preserves_gaps_above_threshold():
    """超过 200 个连续缺失桶仍按真实连续性合并为一个区间。"""
    from ai_tuner.readiness.kline_coverage import _merge_missing_starts

    base = datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc)
    starts = tuple(base + timedelta(hours=i) for i in range(250))
    result = _merge_missing_starts(starts, interval="1h")
    # 因为 250 个桶本来就连续，所以自然合并成一个区间
    assert len(result) == 1
    start, end = result[0]
    assert start == base
    assert end == base + timedelta(hours=250)


def test_many_disjoint_missing_ranges_are_not_falsely_joined():
    """大量离散缺口不能被首尾相连成一个虚假的连续缺口。"""
    from ai_tuner.readiness.kline_coverage import _merge_missing_starts

    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    starts = tuple(base + timedelta(hours=i * 2) for i in range(201))

    result = _merge_missing_starts(starts, interval="1h")

    assert len(result) == 201
    assert result[0] == (base, base + timedelta(hours=1))
    assert result[-1] == (
        base + timedelta(hours=400),
        base + timedelta(hours=401),
    )


# ===========================================================================
# 审计器集成：_query_kline_coverage 不再调用 _detect_dominant_symbol
# ===========================================================================


@pytest.mark.asyncio
async def test_auditor_query_kline_coverage_no_longer_uses_dominant_symbol(fake_db, config, as_of):
    """auditor._query_kline_coverage 必须只走新 calculator（kline_coverage.compute_kline_coverage）

    旧链路（_detect_dominant_symbol / _locate_kline_table 等）已从 auditor 中删除；
    这里用 spy 断言新入口被正确调用且关键参数透传。
    """
    from ai_tuner.readiness.auditor import ReadinessAuditor
    from ai_tuner.readiness.kline_coverage import KlineCoverageResult

    auditor = ReadinessAuditor(fake_db, config)

    dummy_result = KlineCoverageResult(
        weighted_coverage=1.0,
        symbols=(),
        evidence_start=None,
        evidence_end=None,
        gaps=(),
    )
    with patch(
        "ai_tuner.readiness.kline_coverage.compute_kline_coverage",
        new=AsyncMock(return_value=dummy_result),
    ) as patched:
        coverage, gaps, symbols = await auditor._query_kline_coverage(
            strategy_id="btc_eth_aggressive",
            window_start=as_of - timedelta(hours=24),
            window_end=as_of,
        )

    # 返回三元组 (weighted_coverage, gaps, symbols_detail)
    assert coverage == 1.0
    assert gaps == []
    assert symbols == []

    # 新入口被调用且关键 kwargs 透传
    assert patched.called, "新 kline_coverage.compute_kline_coverage 应被调用"
    call_kwargs = patched.await_args.kwargs
    for key in ("strategy_id", "universe_cfg", "project_root",
                "window_start", "window_end"):
        assert key in call_kwargs, (
            f"compute_kline_coverage 应接收 kwargs.{key}，"
            f"实际: {list(call_kwargs.keys())}"
        )

    # 旧链路方法确实已从 ReadinessAuditor 类移除
    assert hasattr(ReadinessAuditor, "_detect_dominant_symbol") is False


@pytest.mark.asyncio
async def test_compute_uses_each_universe_slice_bounds(monkeypatch):
    """动态 universe 的每个 symbol 只能审计其所在 slice 的有效区间。"""
    from ai_tuner.readiness import kline_coverage as kc
    from ai_tuner.readiness.universe import UniverseResolution, UniverseSlice

    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    middle = start + timedelta(days=5)
    end = start + timedelta(days=10)
    resolution = UniverseResolution(
        slices=(
            UniverseSlice("hrs", start, middle, ("BTCUSDT",), ("1h",), "historical_snapshot", "a" * 64),
            UniverseSlice("hrs", middle, end, ("ETHUSDT",), ("1h",), "historical_snapshot", "b" * 64),
        ),
        covered_from=start,
        gaps=(),
    )

    async def fake_resolve(self, strategy_id, valid_from, valid_to):
        return resolution

    calls = []

    async def fake_pair(db, symbol, interval, window_start, window_end):
        calls.append((symbol, window_start, window_end))
        return kc.SymbolCoverage(symbol, interval, expected=1, actual=1)

    monkeypatch.setattr("ai_tuner.readiness.universe.UniverseResolver.resolve", fake_resolve)
    monkeypatch.setattr(kc, "_single_pair_coverage", fake_pair)

    await kc.compute_kline_coverage(
        object(), strategy_id="hrs", universe_cfg={"hrs": {}},
        project_root=_PROJECT_ROOT, window_start=start, window_end=end,
    )

    assert calls == [
        ("BTCUSDT", start, middle),
        ("ETHUSDT", middle, end),
    ]


@pytest.mark.asyncio
async def test_same_symbol_across_slices_is_aggregated(monkeypatch):
    """逐 symbol 报告应合并同一 pair 的多个非重叠生效切片。"""
    from ai_tuner.readiness import kline_coverage as kc
    from ai_tuner.readiness.universe import UniverseResolution, UniverseSlice

    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    middle = start + timedelta(days=1)
    end = start + timedelta(days=2)
    resolution = UniverseResolution(
        slices=(
            UniverseSlice("hrs", start, middle, ("BTCUSDT",), ("1h",), "historical_snapshot", "a" * 64),
            UniverseSlice("hrs", middle, end, ("BTCUSDT",), ("1h",), "historical_snapshot", "b" * 64),
        ),
        covered_from=start,
        gaps=(),
    )

    async def fake_resolve(self, strategy_id, valid_from, valid_to):
        return resolution

    async def fake_pair(db, symbol, interval, window_start, window_end):
        return kc.SymbolCoverage(symbol, interval, expected=24, actual=23)

    monkeypatch.setattr("ai_tuner.readiness.universe.UniverseResolver.resolve", fake_resolve)
    monkeypatch.setattr(kc, "_single_pair_coverage", fake_pair)

    result = await kc.compute_kline_coverage(
        object(), strategy_id="hrs", universe_cfg={"hrs": {}},
        project_root=_PROJECT_ROOT, window_start=start, window_end=end,
    )

    assert len(result.symbols) == 1
    assert result.symbols[0].expected == 48
    assert result.symbols[0].actual == 46


@pytest.mark.asyncio
async def test_dynamic_slices_accept_naive_window_boundaries(monkeypatch):
    """公开契约允许 naive datetime，动态 slice 不得发生 naive/aware 比较异常。"""
    from ai_tuner.readiness import kline_coverage as kc
    from ai_tuner.readiness.universe import UniverseResolution, UniverseSlice

    aware_start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    aware_end = aware_start + timedelta(days=1)
    resolution = UniverseResolution(
        slices=(UniverseSlice(
            "hrs", aware_start, aware_end, ("BTCUSDT",), ("1h",),
            "historical_snapshot", "a" * 64,
        ),),
        covered_from=aware_start,
        gaps=(),
    )

    async def fake_resolve(self, strategy_id, valid_from, valid_to):
        return resolution

    async def fake_pair(db, symbol, interval, window_start, window_end):
        return kc.SymbolCoverage(symbol, interval, expected=24, actual=24)

    monkeypatch.setattr("ai_tuner.readiness.universe.UniverseResolver.resolve", fake_resolve)
    monkeypatch.setattr(kc, "_single_pair_coverage", fake_pair)

    result = await kc.compute_kline_coverage(
        object(), strategy_id="hrs", universe_cfg={"hrs": {}},
        project_root=_PROJECT_ROOT,
        window_start=aware_start.replace(tzinfo=None),
        window_end=aware_end.replace(tzinfo=None),
    )

    assert result.weighted_coverage == 1.0


@pytest.mark.asyncio
async def test_partial_historical_universe_is_fail_closed(monkeypatch):
    """首个快照晚于窗口起点时，即使已有局部 slice 也不得计算成高覆盖率。"""
    from ai_tuner.readiness import kline_coverage as kc
    from ai_tuner.readiness.universe import UniverseResolution, UniverseSlice

    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    observed = start + timedelta(days=5)
    end = start + timedelta(days=10)
    gap = f"historical_universe_missing_before={observed.isoformat()}, strategy_id=hrs"
    resolution = UniverseResolution(
        slices=(UniverseSlice(
            "hrs", observed, end, ("BTCUSDT",), ("1h",),
            "historical_snapshot", "a" * 64,
        ),),
        covered_from=observed,
        gaps=(gap,),
    )

    async def fake_resolve(self, strategy_id, valid_from, valid_to):
        return resolution

    pair = AsyncMock(return_value=kc.SymbolCoverage(
        "BTCUSDT", "1h", expected=120, actual=120,
    ))
    monkeypatch.setattr("ai_tuner.readiness.universe.UniverseResolver.resolve", fake_resolve)
    monkeypatch.setattr(kc, "_single_pair_coverage", pair)

    result = await kc.compute_kline_coverage(
        object(), strategy_id="hrs", universe_cfg={"hrs": {}},
        project_root=_PROJECT_ROOT, window_start=start, window_end=end,
    )

    assert result.weighted_coverage == 0.0
    assert result.gaps == (gap,)
    pair.assert_not_awaited()


# ===========================================================================
# ReadinessResult.to_dict 含 kline_symbols 时 json 可序列化
# ===========================================================================


def test_readiness_result_to_dict_with_kline_symbols_json_safe(as_of):
    """kline_symbols 里的 datetime 已 isoformat → json.dumps 不抛异常。"""
    from ai_tuner.readiness.models import CandidateReadiness, ReadinessResult

    cr = CandidateReadiness(
        strategy_id="s", closed_trades=1, effective_signals=1, regime_count=1,
        complete_trade_ratio=1.0, config_identity_ratio=1.0,
        kline_coverage=1.0, external_snapshot_coverage=0.0,
        external_snapshot_requirement="required",
        external_snapshot_reason="",
        failures=[], gaps=[],
        kline_symbols=[
            {
                "symbol": "BTCUSDT", "interval": "1h",
                "expected": 24, "actual": 24, "coverage": 1.0,
                "missing_ranges": [[
                    datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc).isoformat(),
                    datetime(2026, 10, 17, 1, 0, tzinfo=timezone.utc).isoformat(),
                ]],
                "gap": None,
            },
        ],
    )
    rr = ReadinessResult(
        as_of=as_of.isoformat(), window_days=90, decision="SELECT",
        selected_strategy="s", candidates={"s": cr},
    )
    # 必须可 JSON 序列化（datetime 已字符串化，不会抛 TypeError）
    dumped = json.dumps(rr.to_dict(), ensure_ascii=False)
    assert "BTCUSDT" in dumped
    loaded = json.loads(dumped)
    assert loaded["candidates"]["s"]["kline_symbols"][0]["symbol"] == "BTCUSDT"


async def test_count_query_exception_produces_gap_not_silent_zero():
    """修复2：_query_count 异常 → 透传 gap=f"count_query_failed:...", 不静默记 0

    旧实现：except 后 return (0, 0) → 审计无法区分"真的没 K 线" vs "查询炸了"。
    新实现：except 后 return (expected, 0, "count_query_failed:boom")，
    外层 SymbolCoverage.gap 透传，aggregate_coverage 拼接进 KlineCoverageResult.gaps。
    """
    from ai_tuner.readiness.kline_coverage import compute_kline_coverage
    from tests.test_ai_tuner_mvp.conftest import FakeDB

    strategy_id = "btc_eth_aggressive"
    universe_cfg = {
        "btc_eth_aggressive": {
            "source": "strategy_config",
            "config_path": "strategies/btc_eth_aggressive/config.yaml",
            "symbol_path": "strategy.symbols",
            "intervals": ["1h"],
        },
    }
    # 故意让 COUNT(*) AS expected 路由抛异常 → _query_count catch 后透传 gap
    db = FakeDB(errors={"COUNT(*) AS expected": RuntimeError("boom")})
    as_of = datetime(2026, 10, 17, tzinfo=timezone.utc)
    result = await compute_kline_coverage(
        db,
        strategy_id=strategy_id,
        universe_cfg=universe_cfg,
        project_root=_PROJECT_ROOT,
        window_start=as_of - timedelta(days=90),
        window_end=as_of,
    )
    # weighted coverage 应该是 0.0（actual=0 兜底）
    assert result.weighted_coverage == 0.0
    # gaps 里应有 count_query_failed 字样（不抛异常 + 产出 gap 说明是查询炸了）
    assert result.gaps, "至少应产出一条 gap"
    assert any("count_query_failed" in g for g in result.gaps), result.gaps
    assert any("boom" in g for g in result.gaps), result.gaps
    # 每个 SymbolCoverage 都有 gap
    assert len(result.symbols) >= 1
    for sym in result.symbols:
        assert sym.gap is not None, f"{sym.symbol}/{sym.interval} 应有 gap 说明"
        assert "count_query_failed" in sym.gap
        assert sym.actual == 0  # 兜底 actual=0


@pytest.mark.asyncio
async def test_missing_query_exception_produces_gap():
    """_query_missing_starts 异常 → 产出 gap="missing_query_failed:..."，不静默显示空 missing_ranges

    旧实现：except 后 return () → 审计报告"无缺失区间"而实际是查询炸了。
    新实现：except 后 return ((), "missing_query_failed:boom")，
    SymbolCoverage.gap 合并（已有 count_gap 时用 "; " 连接），aggregate_coverage 拼接进 gaps。
    """
    from ai_tuner.readiness.kline_coverage import compute_kline_coverage
    from tests.test_ai_tuner_mvp.conftest import FakeDB

    strategy_id = "btc_eth_aggressive"
    universe_cfg = {
        "btc_eth_aggressive": {
            "source": "strategy_config",
            "config_path": "strategies/btc_eth_aggressive/config.yaml",
            "symbol_path": "strategy.symbols",
            "intervals": ["1h"],
        },
    }
    # errors 路由键：missing SQL 的 WHERE k.xxx IS NULL 子串是独有特征
    db = FakeDB(errors={"WHERE k.open_time IS NULL": RuntimeError("missboom")})
    as_of = datetime(2026, 10, 17, tzinfo=timezone.utc)
    result = await compute_kline_coverage(
        db,
        strategy_id=strategy_id,
        universe_cfg=universe_cfg,
        project_root=_PROJECT_ROOT,
        window_start=as_of - timedelta(hours=24),
        window_end=as_of,
    )
    # 至少应产出一条 gap 且含 missing_query_failed
    assert result.gaps, "至少应产出一条 gap"
    assert any("missing_query_failed" in g for g in result.gaps), result.gaps
    assert any("missboom" in g for g in result.gaps), result.gaps
    # 每个 SymbolCoverage 的 gap 应透传 missing_query_failed
    assert len(result.symbols) >= 1
    for sym in result.symbols:
        assert sym.gap is not None, f"{sym.symbol}/{sym.interval} 应有 gap 说明"
        assert "missing_query_failed" in sym.gap, sym.gap


# ===========================================================================
# generate_series 窗口边界 cast 跟随列 data_type（决策 D4 修复）
# ===========================================================================


def _patch_kline_col_dtype(responses: dict, dtype: str) -> dict:
    """把 FakeDB responses 里所有 btc_eth schema 下 kline_ 表的列探测
    路由覆盖为指定的 data_type，返回修改后的新 dict（不污染原 responses）。"""
    patched = dict(responses)
    for key in list(patched.keys()):
        if not isinstance(key, tuple) or len(key) != 2:
            continue
        sql_key, args_prefix = key
        if sql_key != "FROM information_schema.columns":
            continue
        # 精确 args 前缀：("btc_eth", "kline_xxx", "open_time") 三元组
        if (args_prefix is not None and len(args_prefix) == 3
                and args_prefix[0] == "btc_eth"
                and args_prefix[1].startswith("kline_")):
            patched[key] = {"data_type": dtype}
    # 兜底路由 ("FROM information_schema.columns", None) 也覆盖（覆盖任何漏网的 kline 表探测）
    patched[("FROM information_schema.columns", None)] = {"data_type": dtype}
    return patched


@pytest.mark.asyncio
async def test_generate_series_cast_follows_column_type():
    """generate_series 窗口边界的 cast 应跟随 K 线表 open_time 列的 data_type。

    修复背景：原 SQL 模板硬写 $1::timestamp / $2::timestamp（naive），
    若某 K 线表 open_time 是 timestamptz（带时区），容器 TZ=Asia/Shanghai
    会让窗口边界整体偏移 8 小时 → 覆盖率错算（决策 D4）。

    子场景 A：列类型 timestamptz → count SQL 和 missing SQL 都应含 ::timestamptz
    子场景 B：列类型 timestamp   → count SQL 应含 ::timestamp 且**不含** ::timestamptz
    """
    import asyncio

    from ai_tuner.readiness.kline_coverage import compute_kline_coverage
    from tests.test_ai_tuner_mvp.conftest import FakeDB, BASELINE_RESPONSES

    strategy_id = "btc_eth_aggressive"
    universe_cfg = {
        "btc_eth_aggressive": {
            "source": "strategy_config",
            "config_path": "strategies/btc_eth_aggressive/config.yaml",
            "symbol_path": "strategy.symbols",
            "intervals": ["1h"],
        },
    }
    as_of = datetime(2026, 10, 17, tzinfo=timezone.utc)

    # ---- 子场景 A：列类型 aware → cast 为 timestamptz ----
    responses_aware = _patch_kline_col_dtype(BASELINE_RESPONSES, "timestamp with time zone")
    db_a = FakeDB(responses=responses_aware)
    await compute_kline_coverage(
        db_a, strategy_id=strategy_id, universe_cfg=universe_cfg,
        project_root=_PROJECT_ROOT,
        window_start=as_of - timedelta(hours=24), window_end=as_of,
    )

    # 从 sql_log 里找 generate_series SQL，断言含 ::timestamptz 不含 ::timestamp 裸写
    gen_sqls_a = [s for s in db_a.sql_log if "generate_series" in s]
    assert gen_sqls_a, "应有 generate_series SQL"
    for sql in gen_sqls_a:
        assert "$1::timestamptz" in sql, (
            f"列类型 aware 时 generate_series 窗口边界应为 $1::timestamptz，实际: {sql}"
        )

    # ---- 子场景 B：列类型 naive → cast 为 timestamp ----
    responses_naive = _patch_kline_col_dtype(BASELINE_RESPONSES, "timestamp without time zone")
    db_b = FakeDB(responses=responses_naive)
    await compute_kline_coverage(
        db_b, strategy_id=strategy_id, universe_cfg=universe_cfg,
        project_root=_PROJECT_ROOT,
        window_start=as_of - timedelta(hours=24), window_end=as_of,
    )

    gen_sqls_b = [s for s in db_b.sql_log if "generate_series" in s]
    assert gen_sqls_b, "应有 generate_series SQL"
    for sql in gen_sqls_b:
        assert "$1::timestamp" in sql, (
            f"列类型 naive 时 generate_series 窗口边界应为 $1::timestamp，实际: {sql}"
        )
        # 关键：不应出现 ::timestamptz（证明没有被错误地 cast 成 aware）
        assert "::timestamptz" not in sql, (
            f"列类型 naive 时不应出现 ::timestamptz，实际: {sql}"
        )
