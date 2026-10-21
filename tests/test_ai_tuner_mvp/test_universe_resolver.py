# -*- coding: utf-8 -*-
"""
策略 universe 解析契约测试（TDD：先写失败测试）

覆盖：
- StaticConfigUniverseResolver：从策略生效配置读 symbol 列表（btc_eth_aggressive）
- HistoricalSnapshotUniverseResolver：从 append-only 历史表读 HRS universe
  * 只查 observed_at <= as_of
  * 空历史 → 返回 gap，严禁回退到 public.strategy_states
- 顶层 UniverseResolver：按 source 字段路由
- 边界：未知 strategy_id、config 路径不存在、历史缺失时 covered_from 为 None
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_ai_tuner_mvp.conftest import FakeDB, PROJECT_ROOT


# ---------------------------------------------------------------------------
# 构造 universe 段（与 ai_tuner/config.yaml purpose_driven_mvp.readiness.universe 同构）
# 测试阶段先独立构造，实现完成后再在真实 config.yaml 上补
# ---------------------------------------------------------------------------

def _build_universe_cfg() -> dict[str, dict[str, Any]]:
    """返回 purpose_driven_mvp.readiness.universe 段的完整 dict"""
    return {
        "btc_eth_aggressive": {
            "source": "strategy_config",
            "config_path": "strategies/btc_eth_aggressive/config.yaml",
            "symbol_path": "strategy.symbols",
            "intervals": ["1h", "4h", "1d"],
        },
        "hrs": {
            "source": "historical_snapshot",
            "intervals": ["1h", "4h", "1d"],
        },
    }


def _make_resolver(universe_cfg, db=None):
    """延迟导入，让 ModuleNotFoundError 发生在 Step 2 的 pytest 跑时"""
    from ai_tuner.readiness.universe import UniverseResolver
    return UniverseResolver(universe_cfg=universe_cfg, project_root=PROJECT_ROOT, db=db)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def as_of():
    return datetime(2026, 10, 17, tzinfo=timezone.utc)


@pytest.fixture
def universe_cfg():
    return _build_universe_cfg()


@pytest.fixture
def empty_history_db():
    """HistoricalSnapshotUniverseResolver 的 FakeDB：返回空历史（空 fetch_all）"""
    responses = {
        # universe 历史查询的 SQL 子串（observed_at 列 data_type 探测）
        "FROM information_schema.columns": None,  # 让探测返回 None → 降级 naive
        # universe 历史查询 fetch_all（空结果）
        "FROM public.ai_tuner_universe_snapshots": [],
    }
    return FakeDB(responses=responses)


@pytest.fixture
def populated_history_db(as_of):
    """FakeDB 返回 2 行 HRS 历史（早于 as_of）"""
    observed_1 = as_of - timedelta(days=30)
    observed_2 = as_of - timedelta(days=10)
    responses = {
        "FROM information_schema.columns": None,  # 探测走 naive 降级
        "FROM public.ai_tuner_universe_snapshots": [
            {
                "observed_at": observed_1.replace(tzinfo=None),
                "symbol": "BTCUSDT",
                "config_hash": "aaa" * 21 + "00",
            },
            {
                "observed_at": observed_2.replace(tzinfo=None),
                "symbol": "ETHUSDT",
                "config_hash": "aaa" * 21 + "11",
            },
            {
                "observed_at": observed_2.replace(tzinfo=None),
                "symbol": "SOLUSDT",
                "config_hash": "aaa" * 21 + "11",
            },
        ],
    }
    return FakeDB(responses=responses)


# ---------------------------------------------------------------------------
# 计划给出的两条核心断言（事实核查已修正 aggressive symbol 真实顺序）
# 真实配置顺序：BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT
# 计划示例写 "BNBUSDT, BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT" 属幻觉，以真实配置为准
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_aggressive_universe_comes_from_effective_config(universe_cfg, as_of):
    """断言 1：静态 universe 来自策略生效配置，symbol 顺序与真实配置一致"""
    resolver = _make_resolver(universe_cfg)
    result = await resolver.resolve(
        "btc_eth_aggressive",
        as_of - timedelta(days=1),
        as_of,
    )
    # 真实配置顺序：BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT
    assert result.slices[0].symbols == ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")
    # 静态 source 标签
    assert result.slices[0].source == "strategy_config"
    # config_hash 是 sha256 16 进制 64 字符（审计可复现）
    assert len(result.slices[0].config_hash) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", result.slices[0].config_hash)
    # intervals 从 config 读
    assert result.slices[0].intervals == ("1h", "4h", "1d")


@pytest.mark.asyncio
async def test_hrs_missing_history_is_not_inferred_from_current_state(
    universe_cfg, empty_history_db, as_of
):
    """断言 2：HRS 空历史 → covered_from=None + historical_universe_missing gap
    严禁回退到 public.strategy_states 当前值"""
    resolver = _make_resolver(universe_cfg, db=empty_history_db)
    result = await resolver.resolve(
        "hrs",
        as_of - timedelta(days=90),
        as_of,
    )
    assert result.covered_from is None
    assert len(result.slices) == 0  # 无历史 → 不产出 slice
    assert len(result.gaps) >= 1
    assert result.gaps[0].startswith("historical_universe_missing")
    # gap 文案含 before=<timestamp> 信息（审计可定位到哪段时间没历史）
    assert "before=" in result.gaps[0]


# ---------------------------------------------------------------------------
# 补：HRS 有历史时能解析，且 covered_from 取最早 observed_at
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hrs_with_history_parses_symbols(universe_cfg, populated_history_db, as_of):
    """HRS 有 2 行历史时：symbols 去重、covered_from 取最早 observed_at"""
    resolver = _make_resolver(universe_cfg, db=populated_history_db)
    result = await resolver.resolve(
        "hrs",
        as_of - timedelta(days=90),
        as_of,
    )
    assert result.covered_from is not None
    # 最早 observed_at = as_of - 30 天
    expected_early = as_of - timedelta(days=30)
    assert result.covered_from.replace(tzinfo=None) == expected_early.replace(tzinfo=None)
    # 两次快照分别还原，跨切片观察到的 symbols 合集保持完整
    assert set().union(*(set(s.symbols) for s in result.slices)) == {
        "BTCUSDT", "ETHUSDT", "SOLUSDT",
    }
    assert result.slices[0].source == "historical_snapshot"
    assert any("historical_universe_missing_before" in gap for gap in result.gaps)
    # intervals 从 config 读
    assert result.slices[0].intervals == ("1h", "4h", "1d")


@pytest.mark.asyncio
async def test_hrs_history_builds_time_slices_and_marks_uncovered_prefix(
    universe_cfg, populated_history_db, as_of
):
    """动态 universe 必须按快照生效时间切片，首个快照前保持 fail-closed。"""
    resolver = _make_resolver(universe_cfg, db=populated_history_db)
    window_start = as_of - timedelta(days=90)

    result = await resolver.resolve("hrs", window_start, as_of)

    assert len(result.slices) == 2
    first, second = result.slices
    assert first.valid_from == as_of - timedelta(days=30)
    assert first.valid_to == as_of - timedelta(days=10)
    assert first.symbols == ("BTCUSDT",)
    assert second.valid_from == as_of - timedelta(days=10)
    assert second.valid_to == as_of
    assert second.symbols == ("ETHUSDT", "SOLUSDT")
    assert any("historical_universe_missing_before" in gap for gap in result.gaps)


# ---------------------------------------------------------------------------
# 补：HRS 查询 SQL 含 observed_at <= as_of 约束
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hrs_sql_filters_by_as_of(universe_cfg, empty_history_db, as_of):
    """HistoricalSnapshotUniverseResolver 必须带 observed_at <= as_of 约束，
    禁止任何回退到 public.strategy_states 的 SQL"""
    resolver = _make_resolver(universe_cfg, db=empty_history_db)
    await resolver.resolve("hrs", as_of - timedelta(days=90), as_of)
    # 检查 SQL 日志
    sqls = empty_history_db.sql_log
    # 至少有一条查询 ai_tuner_universe_snapshots 的 SQL
    snap_sqls = [s for s in sqls if "ai_tuner_universe_snapshots" in s]
    assert len(snap_sqls) >= 1
    # 必须存在一条 SQL 同时含 observed_at 和 <= (或 <) 约束
    filtered = [
        s for s in snap_sqls
        if "observed_at" in s and ("<=" in s or "<" in s)
    ]
    assert len(filtered) >= 1, "HRS 查询 SQL 缺 observed_at <= as_of 约束"
    # 禁止出现 strategy_states
    for s in sqls:
        assert "strategy_states" not in s, "严禁回退 public.strategy_states"


# ---------------------------------------------------------------------------
# 补：未知 strategy_id → 明确 ValueError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_strategy_id_raises(universe_cfg, as_of):
    """未在 universe_cfg 中声明的 strategy_id → ValueError，不静默返回空"""
    resolver = _make_resolver(universe_cfg)
    with pytest.raises(ValueError, match="unknown.*strategy|universe.*config"):
        await resolver.resolve(
            "totally_unknown_strategy",
            as_of - timedelta(days=1),
            as_of,
        )


# ---------------------------------------------------------------------------
# 补：config 路径不存在 → 明确异常
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_static_config_path_missing_raises(as_of):
    """strategy_config 源的 config_path 指向不存在的文件 → ValueError"""
    universe_cfg = {
        "ghost_strategy": {
            "source": "strategy_config",
            "config_path": "strategies/does_not_exist/config.yaml",
            "symbol_path": "strategy.symbols",
            "intervals": ["1h"],
        },
    }
    resolver = _make_resolver(universe_cfg)
    with pytest.raises(FileNotFoundError):
        await resolver.resolve(
            "ghost_strategy",
            as_of - timedelta(days=1),
            as_of,
        )


# ---------------------------------------------------------------------------
# 补：symbol_path 用点号路径（resolver 自行解析）
# ---------------------------------------------------------------------------

def test_dot_path_resolution():
    """点号路径 resolver 在嵌套 dict 上能正确返回叶子值"""
    from ai_tuner.readiness.universe import _resolve_dot_path
    data = {"strategy": {"symbols": ["BTC", "ETH"]}}
    assert _resolve_dot_path(data, "strategy.symbols") == ["BTC", "ETH"]


def test_dot_path_missing_raises():
    """点号路径指向不存在的键 → KeyError"""
    from ai_tuner.readiness.universe import _resolve_dot_path
    with pytest.raises(KeyError):
        _resolve_dot_path({"strategy": {}}, "strategy.symbols")
