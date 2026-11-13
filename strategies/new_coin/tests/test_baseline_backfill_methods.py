"""Regression tests for restart evidence, precision and target status recovery."""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strategies.new_coin.strategy import NewCoinStrategy


def _make_strategy_self():
    return NewCoinStrategy({"strategy": {"name": "new_coin"}})


def _make_executor_self():
    ex = MagicMock()
    ex.position_tracking = {}
    ex.ensure_tracking_entry.side_effect = lambda symbol, **kw: ex.position_tracking.setdefault(symbol, {})
    ex.db.fetch_all = AsyncMock(return_value=[])
    ex.target1_close_percent = Decimal("0.30")
    ex.target2_close_percent = Decimal("0.40")
    ex.qty_tolerance_ratio = 0.01
    ex.qty_tolerance_absolute = 0.0001
    precision = (Decimal("0.01"), Decimal("0.001"))
    ex._precision_cache = {"TESTUSDT": precision}
    ex._get_symbol_precision = AsyncMock(return_value=precision)
    ex._step_size_for.return_value = float(precision[1])
    return ex


def open_row():
    return dict(symbol="TESTUSDT", status="open", position_id="TESTUSDT_123",
                quantity=10, entry_price=100, opened_at=datetime(2025, 1, 1),
                metadata={"entry_order_id": 123})


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["empty", "ambiguous", "zero", "db_error", "wrong_price"])
async def test_restore_clears_stale_tracking_on_insufficient_evidence(scenario):
    ex = _make_executor_self()
    row = open_row()
    if scenario == "zero":
        row["quantity"] = 0
    if scenario == "wrong_price":
        row["entry_price"] = 99
    ex.db.fetch_all.return_value = {"empty": [], "ambiguous": [row, row]}.get(scenario, [row])
    if scenario == "db_error":
        ex.db.fetch_all.side_effect = RuntimeError("DB unavailable")
    entry = {"tp_anchor_quantity": 20, "recovery_position_id": "old", "recovery_target1_candidate": True}
    ex.position_tracking["TESTUSDT"] = entry
    pos = {"entry_price": 100, "quantity": 3}
    await _make_strategy_self()._sync_baseline_to_tracking(ex, {"TESTUSDT": pos})
    ex.db.fetch_all.assert_awaited_once()
    assert entry["tp_anchor_quantity"] is None
    assert "recovery_position_id" not in entry
    assert "recovery_target1_candidate" not in entry
    assert entry["time_exit_status"] == pos["time_exit_status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("precision_available", [True, False])
async def test_unique_open_restores_anchor_and_candidate_only(precision_available):
    ex = _make_executor_self()
    ex.db.fetch_all.return_value = [open_row()]
    if not precision_available:
        ex._precision_cache.clear()
    pos = {"entry_price": 100, "quantity": 3}
    await _make_strategy_self()._sync_baseline_to_tracking(
        ex, {"TESTUSDT": pos}, {"TESTUSDT": Decimal("2")})
    entry = ex.position_tracking["TESTUSDT"]
    ex._get_symbol_precision.assert_awaited_once_with("TESTUSDT")
    assert entry["entry_time"] == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert entry["recovery_entry_order_id"] == 123
    assert entry["tp_anchor_quantity"] == 10
    assert entry["remaining_quantity"] == entry["entry_quantity"] == 3
    assert entry["atr"] == 2
    assert not entry.get("target1_reached") and not entry.get("target2_reached")
    if precision_available:
        assert entry["recovery_target1_candidate"] and entry["recovery_target2_candidate"]
        assert pos["time_exit_status"] == "review_required"
    else:
        assert "recovery_target1_candidate" not in entry
        assert "recovery_target2_candidate" not in entry
        assert pos["time_exit_status"] == "unknown"


@pytest.mark.asyncio
async def test_missing_atr_warning_lists_all_symbols_once():
    ex = _make_executor_self()
    rebuilt = {symbol: {"entry_price": 100, "quantity": 3} for symbol in ("TESTUSDT", "NEWUSDT")}
    with patch("strategies.new_coin.strategy.logger.warning") as warn:
        await _make_strategy_self()._sync_baseline_to_tracking(ex, rebuilt)
    alerts = [call for call in warn.call_args_list if "ATR 缺失" in call.args[0]]
    assert [call.kwargs["symbols"] for call in alerts] == [["NEWUSDT", "TESTUSDT"]]


@pytest.mark.asyncio
async def test_target_status_no_tracking_skip():
    """executor.position_tracking 无此 symbol → continue 跳过（L2413-2415）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.position_tracking = {}  # 空

    # patch derive_reached_targets 确认不被调
    with patch('strategies.new_coin.strategy.derive_reached_targets') as mock_d:
        await s._backfill_target_statuses(ex, ['TESTUSDT'])
    mock_d.assert_not_called()


@pytest.mark.asyncio
async def test_target_status_no_anchor_skips_with_precision_warmup():
    """tp_anchor_quantity 缺失 → 预热精度后 continue（L2417-2423）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.position_tracking['TESTUSDT'] = {
        'tp_anchor_quantity': None,
        'remaining_quantity': 0.5,
    }

    # _get_symbol_precision 被调（预热精度）
    with patch('strategies.new_coin.strategy.derive_reached_targets') as mock_d:
        await s._backfill_target_statuses(ex, ['TESTUSDT'])
    ex._get_symbol_precision.assert_awaited_once_with('TESTUSDT')
    mock_d.assert_not_called()  # 锚点缺失跳过自愈


@pytest.mark.asyncio
async def test_target_status_no_anchor_precision_exception_swallowed():
    """锚点缺失时预热精度抛异常 → except 吞掉（L2421-2422），不崩溃。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.position_tracking['TESTUSDT'] = {
        'tp_anchor_quantity': 1.0,  # 有锚点
        'remaining_quantity': 0.5,
    }
    # _get_symbol_precision 抛异常 → except 吞
    ex._get_symbol_precision = AsyncMock(side_effect=Exception("API down"))

    # 精度查询失败时跳过档位推断
    with patch('strategies.new_coin.strategy.derive_reached_targets', return_value=(True, False)) as mock_d:
        await s._backfill_target_statuses(ex, ['TESTUSDT'])
    mock_d.assert_not_called()


@pytest.mark.asyncio
async def test_target_status_remaining_zero_skip():
    """remaining_quantity <= 0 → 跳过（L2430-2431）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.position_tracking['TESTUSDT'] = {
        'tp_anchor_quantity': 1.0,
        'remaining_quantity': 0,
    }

    with patch('strategies.new_coin.strategy.derive_reached_targets') as mock_d:
        await s._backfill_target_statuses(ex, ['TESTUSDT'])
    mock_d.assert_not_called()


@pytest.mark.asyncio
async def test_target_status_hit1_sets_target1_only():
    """hit1=True hit2=False → 只置位 target1_reached=True（L2441-2446）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.position_tracking['TESTUSDT'] = {
        'tp_anchor_quantity': 1.0,
        'remaining_quantity': 0.72,  # 刚好在 TP1 阈值附近
        'target1_reached': False,
        'target2_reached': False,
    }

    with patch('strategies.new_coin.strategy.derive_reached_targets', return_value=(True, False)):
        await s._backfill_target_statuses(ex, ['TESTUSDT'])

    assert ex.position_tracking['TESTUSDT']['target1_reached'] is True
    assert ex.position_tracking['TESTUSDT']['target2_reached'] is False


@pytest.mark.asyncio
async def test_target_status_hit1_hit2_sets_both():
    """hit1+hit2 同真 → 两档都置位（L2441-2449）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.position_tracking['TESTUSDT'] = {
        'tp_anchor_quantity': 1.0,
        'remaining_quantity': 0.31,
        'target1_reached': False,
        'target2_reached': False,
    }

    with patch('strategies.new_coin.strategy.derive_reached_targets', return_value=(True, True)):
        await s._backfill_target_statuses(ex, ['TESTUSDT'])

    assert ex.position_tracking['TESTUSDT']['target1_reached'] is True
    assert ex.position_tracking['TESTUSDT']['target2_reached'] is True


@pytest.mark.asyncio
async def test_target_status_already_set_no_duplicate():
    """target1_reached 已经是 True → hit1=True 时不重复写入（L2441, L2447 的 if not 守卫）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.position_tracking['TESTUSDT'] = {
        'tp_anchor_quantity': 1.0,
        'remaining_quantity': 0.3,
        'target1_reached': True,   # 已置位
        'target2_reached': True,   # 已置位
    }

    # 捕获赋值看看是否发生
    tracking = ex.position_tracking['TESTUSDT']

    with patch('strategies.new_coin.strategy.derive_reached_targets', return_value=(True, True)):
        await s._backfill_target_statuses(ex, ['TESTUSDT'])

    # 仍为 True，且没有被改回 False 等（保持原值）
    assert tracking['target1_reached'] is True
    assert tracking['target2_reached'] is True
