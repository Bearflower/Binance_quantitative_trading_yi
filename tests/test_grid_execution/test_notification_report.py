from decimal import Decimal

import pytest

from strategies.grid.execution.notification_report import (
    TradeFact, PositionSnapshot, summarize, render_report,
)


def test_phase_and_cumulative_pnl_use_actual_pm_facts_and_negative_fee():
    facts = (
        TradeFact(1, "101", "501", "BUY", Decimal("0.01"),
                  Decimal("2000"), Decimal("0"), Decimal("0.02"), "USDT"),
        TradeFact(1, "102", "502", "SELL", Decimal("0.01"),
                  Decimal("2020"), Decimal("0.20"), Decimal("0.02"), "USDT"),
        TradeFact(2, "103", "503", "BUY", Decimal("0.01"),
                  Decimal("2010"), Decimal("0"), Decimal("0.02"), "USDT"),
    )
    position = PositionSnapshot(Decimal("0.01"), Decimal("2010"),
                                Decimal("2012"), Decimal("0.02"))
    totals = summarize(facts, 2, position, Decimal("0.01"), Decimal("0.03"))
    assert totals.phase == Decimal("0.01")
    assert totals.cumulative == Decimal("0.19")
    text = render_report(facts[-1], position, totals)
    assert "当前网格阶段盈亏: +0.0100 USDT" in text
    assert "执行器累计盈亏: +0.1900 USDT" in text
    assert "当前净仓: 多 0.01 ETH" in text


def test_non_usdt_commission_cannot_be_silently_added():
    fact = TradeFact(1, "101", "501", "BUY", Decimal("0.01"),
                     Decimal("2000"), Decimal("0"), Decimal("0.001"), "BNB")
    with pytest.raises(ValueError, match="commission asset"):
        summarize((fact,), 1, PositionSnapshot(Decimal("0"), Decimal("0"),
                  Decimal("2000"), Decimal("0")), Decimal("0"), Decimal("0"))
