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


def test_exit_notification_shows_exact_net_and_separates_trade_and_latest_epoch():
    from dataclasses import replace
    from shared.notification_time import beijing_time

    fact = TradeFact(11, "1", "9013012816", "SELL", Decimal("0.162"),
                     Decimal("1791.68"), Decimal("0.00485999"), Decimal("0.14512607"), "USDT")
    position = PositionSnapshot(*(Decimal(0) for _ in range(4)))
    totals = summarize([fact], 11, position, Decimal(0), Decimal(0))
    text = render_report(fact, position, replace(totals, phase_epoch_id=12),
                         role="EXIT", recorded_ms=1794945901638, epoch_status="CLOSED")
    assert beijing_time(1794945901638) == "2026-11-18 04:05:01"
    assert "网格退出成交 · 第 11 轮" in text and "#12 已结束" in text
    assert "本笔扣费后 -0.1403 USDT" in text and "毛盈亏 +0.0049" in text
    assert "推送时间（北京时间）" in text and "04:05:01" in text
    assert "空仓" in text and "持仓均价" not in text
