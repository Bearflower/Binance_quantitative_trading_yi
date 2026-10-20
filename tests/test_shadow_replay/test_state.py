"""有状态门槛状态机测试：日计数 / 冷却 / 连续亏损 / 持仓上限 / 保证金上限。"""
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import pytest

from backtest.btc_eth.replay.state import VirtualState

UTC = timezone.utc


def _make_state(**overrides):
    base = VirtualState(
        max_single_position_margin_usdt=Decimal("30"),
        max_concurrent_positions=1,
        max_leverage=3,
        initial_equity=Decimal("300"),
        equity=Decimal("300"),
        max_daily_trades=1,
        symbol_cooldown_hours=72,
        consecutive_loss_pause_threshold=2,
        pause_duration_hours=24,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_entry_allowed_when_empty():
    s = _make_state()
    result = s.check_entry_eligibility(
        at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        symbol="XAUUSDT",
        direction="LONG",
        required_margin=Decimal("20"),
    )
    assert result["allowed"] is True
    assert result["rejection_reasons"] == []


def test_rejects_when_position_open():
    s = _make_state()
    s.open_position = {"symbol": "XAUUSDT", "margin": Decimal("20")}
    result = s.check_entry_eligibility(
        at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        symbol="XAUUSDT",
        direction="LONG",
        required_margin=Decimal("20"),
    )
    assert result["allowed"] is False
    assert "position_limit" in result["rejection_reasons"]


def test_rejects_exceeds_max_margin():
    s = _make_state()
    result = s.check_entry_eligibility(
        at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        symbol="XAUUSDT",
        direction="LONG",
        required_margin=Decimal("31"),  # > 30
    )
    assert result["allowed"] is False
    assert "exceeds_max_single_margin" in result["rejection_reasons"]


def test_daily_trade_limit_reached():
    s = _make_state()
    day = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    s.daily_trade_count["2026-10-01"] = 1  # max_daily_trades=1
    result = s.check_entry_eligibility(
        at=day, symbol="XAUUSDT", direction="LONG", required_margin=Decimal("20"),
    )
    assert result["allowed"] is False
    assert "daily_trade_limit" in result["rejection_reasons"]


def test_daily_trade_limit_resets_new_day():
    s = _make_state()
    s.daily_trade_count["2026-10-01"] = 1
    # 下一天
    next_day = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
    result = s.check_entry_eligibility(
        at=next_day, symbol="XAUUSDT", direction="LONG", required_margin=Decimal("20"),
    )
    assert result["allowed"] is True


def test_symbol_cooldown_active():
    s = _make_state()
    s.symbol_cooldown_until["XAUUSDT"] = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
    at = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    result = s.check_entry_eligibility(at=at, symbol="XAUUSDT", direction="LONG", required_margin=Decimal("20"))
    assert result["allowed"] is False
    assert "symbol_cooldown" in result["rejection_reasons"]


def test_symbol_cooldown_expired():
    s = _make_state()
    s.symbol_cooldown_until["XAUUSDT"] = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    at = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    result = s.check_entry_eligibility(at=at, symbol="XAUUSDT", direction="LONG", required_margin=Decimal("20"))
    assert result["allowed"] is True


def test_consecutive_loss_pause():
    s = _make_state(consecutive_losses=2, consecutive_loss_pause_until=datetime(2026, 10, 2, 10, 0, tzinfo=UTC))
    at = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
    result = s.check_entry_eligibility(at=at, symbol="XAUUSDT", direction="LONG", required_margin=Decimal("20"))
    assert result["allowed"] is False
    assert "consecutive_loss_pause" in result["rejection_reasons"]


def test_consecutive_loss_counter_reset_on_win():
    s = _make_state(consecutive_losses=2)
    s.on_position_closed(
        at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        realized_pnl=Decimal("+5"),
        fee=Decimal("-0.1"),
        funding=Decimal("0"),
        slippage=Decimal("-0.02"),
        close_reason="TP1",
        # Round4 修复：整笔仓位级净盈亏（realized + fee + funding + slip = 5 - 0.1 - 0.02）
        position_net_pnl=Decimal("4.88"),
    )
    assert s.consecutive_losses == 0
    assert s.consecutive_loss_pause_until is None


def test_consecutive_loss_triggers_pause():
    s = _make_state()
    # 第一次亏损
    s.on_entry_approved(
        datetime(2026, 10, 1, 10, 0, tzinfo=UTC), "XAUUSDT", "LONG",
        Decimal("0.03"), Decimal("2000"), Decimal("20"), 3, Decimal("1980"), "A",
    )
    s.on_position_closed(
        at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        realized_pnl=Decimal("-10"), fee=Decimal("-0.03"), funding=Decimal("0"),
        slippage=Decimal("-0.012"), close_reason="STOP_LOSS",
        # Round4 修复：整笔净盈亏 = -10 - 0.03 - 0.012 = -10.042
        position_net_pnl=Decimal("-10.042"),
    )
    assert s.consecutive_losses == 1
    assert s.consecutive_loss_pause_until is None

    # 第二次亏损
    s.on_entry_approved(
        datetime(2026, 10, 1, 14, 0, tzinfo=UTC), "XAUUSDT", "LONG",
        Decimal("0.03"), Decimal("2000"), Decimal("20"), 3, Decimal("1980"), "A",
    )
    s.on_position_closed(
        at=datetime(2026, 10, 1, 16, 0, tzinfo=UTC),
        realized_pnl=Decimal("-10"), fee=Decimal("-0.03"), funding=Decimal("0"),
        slippage=Decimal("-0.012"), close_reason="STOP_LOSS",
        # Round4 修复：整笔净盈亏 = -10 - 0.03 - 0.012 = -10.042
        position_net_pnl=Decimal("-10.042"),
    )
    assert s.consecutive_losses == 2
    # pause 应该在 close 时间 + 24h
    expected_until = datetime(2026, 10, 2, 16, 0, tzinfo=UTC)
    assert s.consecutive_loss_pause_until == expected_until


def test_equity_updated_on_close():
    s = _make_state()
    s.equity = Decimal("300")
    s.on_entry_approved(
        datetime(2026, 10, 1, 10, 0, tzinfo=UTC), "XAUUSDT", "LONG",
        Decimal("0.03"), Decimal("2000"), Decimal("20"), 3, Decimal("1980"), "A",
    )
    s.on_position_closed(
        at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        realized_pnl=Decimal("+12"), fee=Decimal("-0.03"), funding=Decimal("0"),
        slippage=Decimal("-0.012"), close_reason="TP1",
        # Round4 修复：整笔净盈亏 = 12 - 0.03 - 0.012 = 11.958
        position_net_pnl=Decimal("11.958"),
    )
    # equity = 300 + 12 - 0.03 - 0.012 = 311.958
    assert abs(float(s.equity) - 311.958) < 0.0001


def test_symbol_cooldown_set_on_close():
    s = _make_state()
    at = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    s.on_entry_approved(at, "XAUUSDT", "LONG",
        Decimal("0.03"), Decimal("2000"), Decimal("20"), 3, Decimal("1980"), "A")
    s.on_position_closed(
        at=at + timedelta(hours=2),
        realized_pnl=Decimal("+5"), fee=Decimal("-0.03"), funding=Decimal("0"),
        slippage=Decimal("-0.012"), close_reason="TP1",
        # Round4 修复：整笔净盈亏 = 5 - 0.03 - 0.012 = 4.958
        position_net_pnl=Decimal("4.958"),
    )
    # 冷却期应该是 close_at + 72h
    expected = at + timedelta(hours=2) + timedelta(hours=72)
    assert s.symbol_cooldown_until["XAUUSDT"] == expected


def test_daily_trade_count_bumped_on_entry():
    s = _make_state()
    at = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    s.on_entry_approved(at, "XAUUSDT", "LONG",
        Decimal("0.03"), Decimal("2000"), Decimal("20"), 3, Decimal("1980"), "A")
    assert s.daily_trade_count["2026-10-01"] == 1


def test_reset_daily_counters_removes_stale_keys():
    """reset_daily_counters 只清理早于 today 的过期日键，当日计数保留。"""
    s = _make_state()
    s.daily_trade_count = {
        "2026-09-29": 2,   # 过期 → 应被删除
        "2026-09-30": 1,   # 过期 → 应被删除
        "2026-10-01": 1,   # 当日 → 保留
    }
    s.reset_daily_counters(datetime(2026, 10, 1, 0, 5, tzinfo=UTC))
    assert s.daily_trade_count == {"2026-10-01": 1}


def test_reset_daily_counters_empty_no_crash():
    """空计数表调用 reset：不崩溃、仍为空。"""
    s = _make_state()
    s.reset_daily_counters(datetime(2026, 10, 1, 0, 5, tzinfo=UTC))
    assert s.daily_trade_count == {}
