"""Task 3 确定性：相同输入两次运行的事件序、权益曲线与报告字节完全一致。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ai_tuner.replay.broker import SymbolFilters, VirtualBroker
from ai_tuner.replay.data_portal import DataPortal
from ai_tuner.replay.engine import run_replay
from ai_tuner.replay.models import ConfigHashes, OrderIntent, UniverseSlice

from .conftest import ScriptedAdapter, make_bars, write_snapshot

UTC = timezone.utc
AS_OF = datetime(2026, 10, 22, tzinfo=UTC)
WINDOW_START = AS_OF - timedelta(hours=4)

PRICES = [
    ("100", "101", "99", "100.5"),
    ("100.5", "102", "100", "101.5"),
    ("101.5", "103", "101", "102.5"),
    ("102.5", "103.5", "98", "99"),
]


def _portal(tmp_path):
    directory = str(tmp_path / "snapshot")
    write_snapshot(
        directory,
        "fixture",
        AS_OF,
        WINDOW_START,
        {"BTCUSDT|1h": make_bars("BTCUSDT", "1h", WINDOW_START, PRICES)},
        ["BTCUSDT"],
        ["1h"],
        funding_by_symbol={"BTCUSDT": [(WINDOW_START + timedelta(hours=2), "0.0001")]},
    )
    return DataPortal(directory, "fixture", AS_OF, WINDOW_START)


def _universe():
    return UniverseSlice(
        strategy_id="fixture",
        valid_from=WINDOW_START,
        valid_to=AS_OF + timedelta(days=30),
        symbols=("BTCUSDT",),
        intervals=("1h",),
        source="strategy_config",
        config_hash="a" * 64,
    )


def _config_hashes():
    return ConfigHashes(
        strategy_id="fixture",
        as_of=AS_OF,
        base_config_hash="b" * 64,
        params_manifest_hash="c" * 64,
        overlay_hash="d" * 64,
    )


def _entry_once(bar, ctx):
    if ctx.portfolio.positions:
        return ()
    return (
        OrderIntent(
            strategy_id="fixture",
            symbol="BTCUSDT",
            seq=1,
            side="LONG",
            order_type="MARKET",
            quantity=Decimal("1"),
            reason="ENTRY",
        ),
    )


def _broker():
    return VirtualBroker(
        filters={
            "BTCUSDT": SymbolFilters(
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.001"),
                min_notional=Decimal("5"),
            )
        },
        fee_bps=Decimal("5"),
        slippage_bps=Decimal("2"),
        stop_offset_pct=Decimal("0.002"),
        tp_offset_pct=Decimal("0.0015"),
        initial_equity=Decimal("10000"),
        max_leverage=Decimal("0"),
        tp1_close_ratio=Decimal("0.5"),
    )


def _run(portal):
    adapter = ScriptedAdapter(_universe(), _config_hashes(), _entry_once)
    return run_replay(portal, adapter, _broker())


def test_two_runs_produce_identical_equity_and_events(tmp_path):
    first = _run(_portal(tmp_path))
    second = _run(_portal(tmp_path))

    assert first.equity_curve == second.equity_curve
    assert first.audit_events == second.audit_events
    assert first.closed_trades == second.closed_trades
    assert first.manifest_ref == second.manifest_ref
    assert first.invariant_violations == ()


def test_audit_events_are_ordered_and_contain_run_start(tmp_path):
    result = _run(_portal(tmp_path))
    assert result.audit_events[0]["type"] == "run_start"
    keys = [(event["ts"], event["seq"]) for event in result.audit_events]
    assert keys == sorted(keys)
    assert any(event["type"] == "fill" for event in result.audit_events)


def test_equity_curve_is_monotonic_in_time(tmp_path):
    result = _run(_portal(tmp_path))
    timestamps = [point.ts for point in result.equity_curve]
    assert timestamps == sorted(timestamps)
    assert result.equity_curve[-1].ts == AS_OF


def test_final_position_is_reported_when_never_closed(tmp_path):
    result = _run(_portal(tmp_path))
    assert result.final_position is not None
    assert result.final_position.symbol == "BTCUSDT"
    assert result.closed_trades == ()
