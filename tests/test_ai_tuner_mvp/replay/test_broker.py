"""Task 3：统一虚拟 Broker 的撮合、reduce-only、部分平仓、优先级与成本单调性。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_tuner.replay.broker import (
    SymbolFilters,
    VirtualBroker,
    check_notional_above_min,
    check_quantity_in_range,
    market_fill_price,
    q_round,
    round_price,
    round_quantity,
    slippage_cost,
    sum_funding_fee,
    taker_fee_notional,
)
from ai_tuner.replay.models import Bar, OrderIntent

UTC = timezone.utc
T0 = datetime(2026, 10, 21, tzinfo=UTC)


def make_bar(open_p, high_p, low_p, close_p, index):
    start = T0 + timedelta(hours=index)
    return Bar(
        symbol="BTCUSDT",
        interval="1h",
        open_time=start,
        close_time=start + timedelta(hours=1),
        open=Decimal(open_p),
        high=Decimal(high_p),
        low=Decimal(low_p),
        close=Decimal(close_p),
        volume=Decimal("10"),
        quote_volume=Decimal("1000"),
        trades=10,
    )


def make_broker(fee_bps="5", slippage_bps="2", min_notional="5"):
    return VirtualBroker(
        filters={
            "BTCUSDT": SymbolFilters(
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.001"),
                min_notional=Decimal(min_notional),
            )
        },
        fee_bps=Decimal(fee_bps),
        slippage_bps=Decimal(slippage_bps),
        stop_offset_pct=Decimal("0.002"),
        tp_offset_pct=Decimal("0.0015"),
        initial_equity=Decimal("10000"),
        max_leverage=Decimal("0"),
        tp1_close_ratio=Decimal("0.5"),
    )


def entry_intent(quantity="1", seq=1):
    return OrderIntent(
        strategy_id="fixture",
        symbol="BTCUSDT",
        seq=seq,
        side="LONG",
        order_type="MARKET",
        quantity=Decimal(quantity),
        reason="ENTRY",
    )


def exit_intent(reason, trigger, seq=2, ratio="1", order_type="STOP"):
    return OrderIntent(
        strategy_id="fixture",
        symbol="BTCUSDT",
        seq=seq,
        side="LONG",
        order_type=order_type,
        quantity=Decimal("1"),
        reason=reason,
        trigger_price=Decimal(trigger),
        reduce_only=True,
        close_ratio=Decimal(ratio),
    )


def test_pure_functions_match_decimal_contract():
    assert q_round(Decimal("123.456"), Decimal("0.01")) == Decimal("123.45")
    assert round_price(Decimal("1.005"), Decimal("0.01")) == Decimal("1.01")
    assert round_quantity(Decimal("1.0009"), Decimal("0.001")) == Decimal("1.000")
    assert taker_fee_notional(Decimal("1000"), Decimal("5")) == Decimal("-0.5")
    assert slippage_cost(Decimal("1000"), Decimal("2")) == Decimal("-0.2")
    assert market_fill_price(Decimal("100"), Decimal("2"), "LONG") == Decimal("100.02")
    assert check_quantity_in_range(Decimal("1.000"), Decimal("0.001")) is None
    assert check_quantity_in_range(Decimal("1.0005"), Decimal("0.001")) is not None
    assert check_notional_above_min(Decimal("1"), Decimal("100"), Decimal("5")) is None
    assert check_notional_above_min(Decimal("0.01"), Decimal("100"), Decimal("5")) is not None
    assert sum_funding_fee(
        Decimal("1"), Decimal("100"), "LONG", (Decimal("0.0001"),)
    ) == Decimal("-0.01")


def test_market_entry_fills_at_next_bar_open():
    broker = make_broker()
    bar1 = make_bar("100", "101", "99", "100", 0)
    broker.on_bar("BTCUSDT", bar1, bar1.close_time)
    broker.submit((entry_intent(),), bar1, bar1.close_time)
    assert "BTCUSDT" not in broker.positions

    bar2 = make_bar("101", "102", "100", "101.5", 1)
    broker.on_bar("BTCUSDT", bar2, bar2.close_time)
    position = broker.positions["BTCUSDT"]
    assert position.entry_price == Decimal("101")
    assert position.current_quantity == Decimal("1.000")


def test_stop_loss_fills_with_adverse_offset():
    broker = make_broker()
    bar1 = make_bar("100", "101", "99", "100", 0)
    broker.on_bar("BTCUSDT", bar1, bar1.close_time)
    broker.submit((entry_intent(), exit_intent("STOP_LOSS", "100")), bar1, bar1.close_time)

    bar2 = make_bar("101", "102", "100", "101.5", 1)
    broker.on_bar("BTCUSDT", bar2, bar2.close_time)
    bar3 = make_bar("101", "101", "99", "99.5", 2)
    broker.on_bar("BTCUSDT", bar3, bar3.close_time)

    trade = broker.closed_trades[0]
    assert trade.exit_reason == "STOP_LOSS"
    assert trade.exit_price == Decimal("99.80")
    assert trade.realized_pnl == Decimal("-1.20")


def test_tp1_partial_close_uses_configured_ratio():
    broker = make_broker()
    bar1 = make_bar("100", "101", "99", "100", 0)
    broker.on_bar("BTCUSDT", bar1, bar1.close_time)
    broker.submit(
        (entry_intent(), exit_intent("TP1", "110", order_type="TAKE_PROFIT")),
        bar1,
        bar1.close_time,
    )

    bar2 = make_bar("101", "111", "100", "110", 1)
    broker.on_bar("BTCUSDT", bar2, bar2.close_time)

    trade = broker.closed_trades[0]
    assert trade.quantity == Decimal("0.500")
    # 110 * (1 + 0.0015) = 110.165，按 tickSize 0.01 做 ROUND_HALF_UP → 110.17
    assert trade.exit_price == Decimal("110.17")
    assert broker.positions["BTCUSDT"].current_quantity == Decimal("0.500")
    assert broker.positions["BTCUSDT"].tp1_hit is True


def test_same_bar_priority_stop_loss_wins_over_take_profit():
    broker = make_broker()
    bar1 = make_bar("100", "101", "99", "100", 0)
    broker.on_bar("BTCUSDT", bar1, bar1.close_time)
    broker.submit(
        (
            entry_intent(),
            exit_intent("TP1", "110", seq=2, order_type="TAKE_PROFIT"),
            exit_intent("STOP_LOSS", "100", seq=3),
        ),
        bar1,
        bar1.close_time,
    )

    bar2 = make_bar("101", "112", "99", "105", 1)
    broker.on_bar("BTCUSDT", bar2, bar2.close_time)

    assert [trade.exit_reason for trade in broker.closed_trades] == ["STOP_LOSS"]


def test_reduce_to_zero_is_rejected_without_partial_fill():
    broker = make_broker(min_notional="0")
    bar1 = make_bar("100", "101", "99", "100", 0)
    broker.on_bar("BTCUSDT", bar1, bar1.close_time)
    broker.submit(
        (entry_intent(quantity="0.001"), exit_intent("REDUCE_ONLY", "100", ratio="0.1")),
        bar1,
        bar1.close_time,
    )
    bar2 = make_bar("101", "102", "100", "101.5", 1)
    broker.on_bar("BTCUSDT", bar2, bar2.close_time)

    assert broker.closed_trades == []
    assert broker.positions["BTCUSDT"].current_quantity == Decimal("0.001")
    assert any(item["type"] == "reject" for item in broker.audit)


def test_reduce_only_quantity_cannot_exceed_position():
    broker = make_broker()
    bar1 = make_bar("100", "101", "99", "100", 0)
    broker.on_bar("BTCUSDT", bar1, bar1.close_time)
    broker.submit((entry_intent(quantity="1"),), bar1, bar1.close_time)
    bar2 = make_bar("101", "102", "100", "101.5", 1)
    broker.on_bar("BTCUSDT", bar2, bar2.close_time)

    oversized = OrderIntent(
        strategy_id="fixture",
        symbol="BTCUSDT",
        seq=9,
        side="LONG",
        order_type="MARKET",
        quantity=Decimal("99"),
        reason="TIME_STOP",
        reduce_only=True,
        close_ratio=Decimal("1"),
        reference_price=Decimal("101.5"),
    )
    broker.submit((oversized,), bar2, bar2.close_time)
    assert broker.positions["BTCUSDT"].current_quantity == Decimal("0")
    assert broker.closed_trades[0].quantity == Decimal("1.000")


def test_higher_costs_never_increase_final_equity():
    equities = []
    for fee_bps in ("5", "50"):
        broker = make_broker(fee_bps=fee_bps)
        bar1 = make_bar("100", "101", "99", "100", 0)
        broker.on_bar("BTCUSDT", bar1, bar1.close_time)
        broker.submit(
            (entry_intent(), exit_intent("TP1", "110", order_type="TAKE_PROFIT")),
            bar1,
            bar1.close_time,
        )
        bar2 = make_bar("101", "111", "100", "110", 1)
        broker.on_bar("BTCUSDT", bar2, bar2.close_time)
        equities.append(broker.mark(bar2.close_time).marked_equity)

    assert equities[0] > equities[1]


def test_funding_settlement_reduces_long_equity():
    broker = make_broker()
    bar1 = make_bar("100", "101", "99", "100", 0)
    broker.on_bar("BTCUSDT", bar1, bar1.close_time)
    broker.submit((entry_intent(),), bar1, bar1.close_time)
    bar2 = make_bar("101", "102", "100", "101.5", 1)
    broker.on_bar("BTCUSDT", bar2, bar2.close_time)

    before = broker.realized_equity
    broker.settle_funding("BTCUSDT", Decimal("0.0001"), bar2.close_time)
    assert broker.realized_equity == before - Decimal("0.0101")


def test_position_requires_symbol_filters(tmp_path):
    broker = make_broker()
    broker.filters.pop("BTCUSDT")
    bar1 = make_bar("100", "101", "99", "100", 0)
    broker.on_bar("BTCUSDT", bar1, bar1.close_time)
    broker.submit((entry_intent(),), bar1, bar1.close_time)
    bar2 = make_bar("101", "102", "100", "101.5", 1)
    broker.on_bar("BTCUSDT", bar2, bar2.close_time)
    assert broker.positions == {}
    assert any(
        item["type"] == "reject" and item["payload"]["reason"] == "SYMBOL_FILTERS_MISSING"
        for item in broker.audit
    )
