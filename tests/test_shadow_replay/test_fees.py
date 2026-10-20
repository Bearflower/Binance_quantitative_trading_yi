"""费用模块测试：5bps 手续费、资金费结算时点、滑点三档。"""
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import pytest

from backtest.btc_eth.replay.fees import (
    taker_fee_notional, slippage_cost, calculate_pnl, sum_funding_fee,
    total_roundtrip_fee, total_roundtrip_slippage,
)


UTC = timezone.utc


def test_taker_fee_5bps():
    notional = Decimal("60")  # 30 USDT margin * 3x lever
    fee = taker_fee_notional(notional, Decimal("5"))
    # 60 * 0.0005 = 0.03 → 负
    assert fee == Decimal("-0.03")


def test_roundtrip_fee_5bps():
    entry = Decimal("60")
    exit_ = Decimal("62")
    total = total_roundtrip_fee(entry, exit_, Decimal("5"))
    # -60*0.0005 - 62*0.0005 = -0.03 - 0.031 = -0.061
    expected = Decimal("-0.03") + Decimal("-0.031")
    assert total == expected


def test_slippage_baseline_2bps():
    notional = Decimal("60")
    slip = slippage_cost(notional, Decimal("2"), "LONG")
    # -60*0.0002 = -0.012
    assert slip == Decimal("-0.012")


def test_slippage_short_same_cost():
    # SHORT 少收同样多的 USDT，对 PnL 负面影响与 LONG 多付一致
    assert slippage_cost(Decimal("100"), Decimal("2"), "LONG") == slippage_cost(Decimal("100"), Decimal("2"), "SHORT")


def test_pnl_long_winning():
    pnl = calculate_pnl("LONG", Decimal("2000"), Decimal("2100"), Decimal("1"))
    assert pnl == Decimal("100")


def test_pnl_short_winning():
    pnl = calculate_pnl("SHORT", Decimal("2000"), Decimal("1900"), Decimal("1"))
    assert pnl == Decimal("100")


def test_pnl_long_losing():
    pnl = calculate_pnl("LONG", Decimal("2000"), Decimal("1950"), Decimal("2"))
    assert pnl == Decimal("-100")


def test_sum_funding_long_rate_positive_pays():
    """LONG + funding_rate=+0.01% → 多付资金费"""
    qty = Decimal("0.03")
    entry_price = Decimal("2000")
    # 三个资金费事件，rate 都是 +0.01%
    events = [
        (datetime(2026, 10, 1, 8, 0, tzinfo=UTC), Decimal("0.0001")),
        (datetime(2026, 10, 1, 16, 0, tzinfo=UTC), Decimal("0.0001")),
        (datetime(2026, 10, 2, 0, 0, tzinfo=UTC), Decimal("0.0001")),
    ]
    total = sum_funding_fee(qty, entry_price, Decimal("2100"), "LONG", events)
    # notional = 0.03 * 2000 = 60；每笔 -60 * 0.0001 = -0.006；三笔合计 -0.018
    assert total == Decimal("-0.018")


def test_sum_funding_short_rate_positive_collects():
    """SHORT + funding_rate=+0.01% → 收取资金费"""
    qty = Decimal("0.03")
    entry_price = Decimal("2000")
    events = [
        (datetime(2026, 10, 1, 8, 0, tzinfo=UTC), Decimal("0.0001")),
    ]
    total = sum_funding_fee(qty, entry_price, Decimal("1900"), "SHORT", events)
    assert total == Decimal("0.006")


def test_sum_funding_zero_events():
    total = sum_funding_fee(
        Decimal("0.03"), Decimal("2000"), Decimal("2100"), "LONG", []
    )
    assert total == Decimal("0")
