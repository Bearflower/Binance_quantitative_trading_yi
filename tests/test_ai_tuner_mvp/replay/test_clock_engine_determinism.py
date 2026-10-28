"""Task 3 确定性：相同输入两次运行的事件序、权益曲线与报告字节完全一致。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_tuner.replay import clock
from ai_tuner.replay.broker import SymbolFilters, VirtualBroker
from ai_tuner.replay.data_portal import DataPortal
from ai_tuner.replay.engine import run_replay
from ai_tuner.replay.models import ConfigHashes, DataBlocked, EquityPoint, OrderIntent, Position, UniverseSlice

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
        funding_by_symbol={
            "BTCUSDT": [
                (WINDOW_START + timedelta(hours=2), "0.0001"),
                (AS_OF, "0.0001"),
            ]
        },
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


def _window_portal(tmp_path, start, as_of, bars_by_key, symbols, funding=None):
    directory = str(tmp_path / "window_snapshot")
    write_snapshot(
        directory, "fixture", as_of, start, bars_by_key, symbols, ["1h"],
        funding_by_symbol=funding,
    )
    return DataPortal(directory, "fixture", as_of, start)


def _cash_adapter(start, as_of, symbols):
    universe = UniverseSlice(
        strategy_id="fixture", valid_from=start, valid_to=as_of + timedelta(days=1),
        symbols=tuple(symbols), intervals=("1h",), source="strategy_config",
        config_hash="a" * 64,
    )
    return ScriptedAdapter(universe, _config_hashes(), lambda bar, ctx: ())


def _open_position(symbol, start):
    return Position(
        position_id=symbol, symbol=symbol, direction="LONG",
        entry_price=Decimal("100"), entry_quantity=Decimal("1"),
        current_quantity=Decimal("1"), atr=Decimal("0"), grade="A",
        entry_time=start,
    )


def test_same_timestamp_equity_uses_both_symbols_closing_prices(tmp_path):
    start = datetime(2026, 10, 21, 20, tzinfo=UTC)
    as_of = start + timedelta(hours=1)
    portal = _window_portal(
        tmp_path, start, as_of,
        {
            "BTCUSDT|1h": make_bars("BTCUSDT", "1h", start, [("100", "110", "90", "110")]),
            "ETHUSDT|1h": make_bars("ETHUSDT", "1h", start, [("100", "120", "80", "120")]),
        },
        ["BTCUSDT", "ETHUSDT"],
    )
    broker = _broker()
    broker.positions = {symbol: _open_position(symbol, start) for symbol in ("BTCUSDT", "ETHUSDT")}
    broker.last_prices = {symbol: Decimal("100") for symbol in broker.positions}

    result = run_replay(portal, _cash_adapter(start, as_of, broker.positions), broker)

    assert result.equity_curve[-1].marked_equity == Decimal("10030")


def test_funding_only_timestamp_has_equity_point(tmp_path):
    start = datetime(2026, 10, 21, 20, tzinfo=UTC)
    as_of = start + timedelta(hours=3)
    bars = make_bars("BTCUSDT", "1h", start, [("100", "100", "100", "100")])
    bars += make_bars(
        "BTCUSDT", "1h", start + timedelta(hours=2),
        [("100", "100", "100", "100")],
    )
    funding_time = start + timedelta(hours=2)
    portal = _window_portal(
        tmp_path, start, as_of, {"BTCUSDT|1h": bars}, ["BTCUSDT"],
        {"BTCUSDT": [(funding_time, "0.01")]},
    )
    broker = _broker()
    broker.positions["BTCUSDT"] = _open_position("BTCUSDT", start)
    broker.last_prices["BTCUSDT"] = Decimal("100")

    result = run_replay(portal, _cash_adapter(start, as_of, ["BTCUSDT"]), broker)

    assert funding_time in [point.ts for point in result.equity_curve]
    funding_point = next(point for point in result.equity_curve if point.ts == funding_time)
    assert funding_point.marked_equity != Decimal("10000")


def test_daily_mark_events_cover_each_utc_midnight_inside_window():
    start = datetime(2026, 10, 21, 12, tzinfo=UTC)
    end = datetime(2026, 10, 24, 0, tzinfo=UTC)
    events = clock.collect_daily_mark_events(start, end)

    assert [event.time for event in events] == [
        datetime(2026, 10, day, tzinfo=UTC) for day in (22, 23, 24)
    ]
    assert all(event.kind == clock.DAILY_MARK for event in events)


def test_cash_only_window_marks_midnight_without_bar_close(tmp_path):
    start = datetime(2026, 10, 21, 20, tzinfo=UTC)
    as_of = datetime(2026, 10, 23, 4, tzinfo=UTC)
    bars = make_bars("BTCUSDT", "1h", start, [("100", "100", "100", "100")])
    portal = _window_portal(tmp_path, start, as_of, {"BTCUSDT|1h": bars}, ["BTCUSDT"])

    result = run_replay(portal, _cash_adapter(start, as_of, ["BTCUSDT"]), _broker())

    assert [point.ts for point in result.equity_curve if point.ts.hour == 0] == [
        datetime(2026, 10, 22, tzinfo=UTC), datetime(2026, 10, 23, tzinfo=UTC)
    ]
    assert all(point.marked_equity == Decimal("10000") for point in result.equity_curve)


def test_open_position_without_midnight_close_blocks_daily_mark(tmp_path):
    start = datetime(2026, 10, 21, 20, tzinfo=UTC)
    as_of = datetime(2026, 10, 22, 4, tzinfo=UTC)
    bars = make_bars("BTCUSDT", "1h", start, [("100", "100", "100", "100")])
    portal = _window_portal(
        tmp_path, start, as_of, {"BTCUSDT|1h": bars}, ["BTCUSDT"],
        {"BTCUSDT": [(datetime(2026, 10, 22, tzinfo=UTC), "0.01")]},
    )
    broker = _broker()
    broker.positions["BTCUSDT"] = _open_position("BTCUSDT", start)

    with pytest.raises(DataBlocked) as error:
        run_replay(portal, _cash_adapter(start, as_of, ["BTCUSDT"]), broker)

    assert error.value.gaps == ("daily_equity_gap:BTCUSDT:2026-10-22T00:00:00Z",)


def test_equity_has_single_initial_and_final_point(tmp_path):
    result = _run(_portal(tmp_path))
    timestamps = [point.ts for point in result.equity_curve]

    assert timestamps[0] == WINDOW_START
    assert result.equity_curve[0].marked_equity == Decimal("10000")
    assert timestamps[-1] == AS_OF
    assert len(timestamps) == len(set(timestamps))


def test_pre_window_bars_are_warmup_only_and_initial_equity_is_preserved(tmp_path):
    start = datetime(2026, 10, 21, 20, tzinfo=UTC)
    as_of = start + timedelta(hours=1)
    bars = make_bars(
        "BTCUSDT", "1h", start - timedelta(hours=2),
        [("100", "100", "100", "100"), ("100", "100", "100", "100")],
    )
    bars += make_bars("BTCUSDT", "1h", start, [("100", "100", "100", "100")])
    portal = _window_portal(tmp_path, start, as_of, {"BTCUSDT|1h": bars}, ["BTCUSDT"])
    adapter = _cash_adapter(start, as_of, ["BTCUSDT"])

    result = run_replay(portal, adapter, _broker())

    assert [bar.close_time for bar in adapter.seen_bars] == [as_of]
    assert [point.ts for point in result.equity_curve] == [start, as_of]
    assert result.equity_curve[0].marked_equity == Decimal("10000")
    assert all(event["ts"] >= "2026-10-21T20:00:00Z" for event in result.audit_events)


@pytest.mark.parametrize("hour", [0, 8, 16])
def test_missing_funding_settlement_blocks_open_position(tmp_path, hour):
    as_of = datetime(2026, 10, 22, hour, tzinfo=UTC)
    start = as_of - timedelta(hours=4)
    bars = make_bars("BTCUSDT", "1h", start, [("100", "100", "100", "100")] * 4)
    portal = _window_portal(tmp_path, start, as_of, {"BTCUSDT|1h": bars}, ["BTCUSDT"])
    broker = _broker()
    broker.positions["BTCUSDT"] = _open_position("BTCUSDT", start)

    with pytest.raises(DataBlocked) as error:
        run_replay(portal, _cash_adapter(start, as_of, ["BTCUSDT"]), broker)

    assert error.value.gaps == ("funding_gap:BTCUSDT:{0}".format(
        as_of.strftime("%Y-%m-%dT%H:%M:%SZ")
    ),)


def test_present_funding_settlement_marks_after_funding_and_bar(tmp_path):
    start = datetime(2026, 10, 21, 20, tzinfo=UTC)
    as_of = datetime(2026, 10, 22, tzinfo=UTC)
    bars = make_bars("BTCUSDT", "1h", start, [("100", "100", "100", "100")] * 4)
    portal = _window_portal(
        tmp_path, start, as_of, {"BTCUSDT|1h": bars}, ["BTCUSDT"],
        {"BTCUSDT": [(as_of, "0.01")]},
    )
    broker = _broker()
    broker.positions["BTCUSDT"] = _open_position("BTCUSDT", start)

    result = run_replay(portal, _cash_adapter(start, as_of, ["BTCUSDT"]), broker)

    assert result.equity_curve[-1].marked_equity == Decimal("9999")
    assert [event["type"] for event in result.audit_events].count("funding_settlement") == 1


def test_cash_only_window_does_not_require_funding_settlement(tmp_path):
    start = datetime(2026, 10, 21, 20, tzinfo=UTC)
    as_of = datetime(2026, 10, 22, tzinfo=UTC)
    bars = make_bars("BTCUSDT", "1h", start, [("100", "100", "100", "100")] * 4)
    portal = _window_portal(tmp_path, start, as_of, {"BTCUSDT|1h": bars}, ["BTCUSDT"])

    result = run_replay(portal, _cash_adapter(start, as_of, ["BTCUSDT"]), _broker())

    assert result.equity_curve[-1].marked_equity == Decimal("10000")


def test_funding_check_does_not_change_intent_calls_or_audit_order(tmp_path):
    start = datetime(2026, 10, 21, 20, tzinfo=UTC)
    as_of = datetime(2026, 10, 22, tzinfo=UTC)
    bars = make_bars("BTCUSDT", "1h", start, [("100", "100", "100", "100")] * 4)
    portal = _window_portal(
        tmp_path, start, as_of, {"BTCUSDT|1h": bars}, ["BTCUSDT"],
        {"BTCUSDT": [(as_of, "0.01")]},
    )
    calls = []

    def script(bar, ctx):
        calls.append(bar.close_time)
        return _entry_once(bar, ctx)

    adapter = _cash_adapter(start, as_of, ["BTCUSDT"])
    adapter._script = script
    result = run_replay(portal, adapter, _broker())

    assert calls == [start + timedelta(hours=hour) for hour in (1, 2, 3, 4)]
    kinds = [event["type"] for event in result.audit_events]
    assert kinds == ["run_start", "fill", "funding_settlement"]
    assert [event["seq"] for event in result.audit_events] == list(range(len(kinds)))


def test_pre_window_external_snapshots_are_context_only(tmp_path):
    start = datetime(2026, 10, 21, 20, tzinfo=UTC)
    as_of = start + timedelta(hours=1)
    bars = make_bars("BTCUSDT", "1h", start, [("100", "100", "100", "100")])
    snapshot_times = [start - timedelta(hours=1), start + timedelta(minutes=30)]
    snapshots = [
        {
            "symbol": "BTCUSDT", "bucket_start": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bucket_end": (time + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "observed_at": (time + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "is_complete": False,
        }
        for time in snapshot_times
    ]
    directory = str(tmp_path / "snapshot_with_warmup")
    write_snapshot(
        directory, "fixture", as_of, start, {"BTCUSDT|1h": bars}, ["BTCUSDT"], ["1h"],
        market_snapshots=snapshots,
    )
    portal = DataPortal(directory, "fixture", as_of, start)

    class SnapshotAdapter(ScriptedAdapter):
        requires_external_snapshot = True

        def prepare(self, data):
            self.available_at_prepare = [s.bucket_start for s in data.external_snapshot("BTCUSDT")]
            return super().prepare(data)

        def on_snapshot(self, snap, ctx):
            self.seen_snapshots.append(snap.bucket_start)

    base = _cash_adapter(start, as_of, ["BTCUSDT"])
    adapter = SnapshotAdapter(base._universe, _config_hashes(), lambda bar, ctx: ())
    adapter.seen_snapshots = []

    result = run_replay(portal, adapter, _broker(), include_snapshots=True)

    assert adapter.available_at_prepare == snapshot_times
    assert adapter.seen_snapshots == [snapshot_times[1]]
    assert [point.ts for point in result.equity_curve] == [start, snapshot_times[1], as_of]
    assert result.closed_trades == ()
    assert [event["type"] for event in result.audit_events] == ["run_start"]


def test_initial_point_is_literal_initial_equity_even_with_preloaded_position(tmp_path):
    start = datetime(2026, 10, 21, 20, tzinfo=UTC)
    as_of = start + timedelta(hours=1)
    bars = make_bars("BTCUSDT", "1h", start, [("110", "110", "110", "110")])
    portal = _window_portal(tmp_path, start, as_of, {"BTCUSDT|1h": bars}, ["BTCUSDT"])
    broker = _broker()
    broker.positions["BTCUSDT"] = _open_position("BTCUSDT", start)
    broker.last_prices["BTCUSDT"] = Decimal("110")

    result = run_replay(portal, _cash_adapter(start, as_of, ["BTCUSDT"]), broker)

    assert result.equity_curve[0] == EquityPoint(start, Decimal("10000"), Decimal("10000"), False)
    assert result.equity_curve[-1].marked_equity == Decimal("10010")
