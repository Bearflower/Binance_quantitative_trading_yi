from decimal import Decimal

from ai_tuner.monitor.trial_risk_evidence import mark_shock, stressed_net


def test_stress_adds_losses_on_both_sides_without_netting():
    rows = [
        dict(symbol="BTCUSDT", positionSide="BOTH", positionAmt="1", markPrice="100", notional="100"),
        dict(symbol="ETHUSDT", positionSide="BOTH", positionAmt="-2", markPrice="10", notional="-20"),
    ]
    assert mark_shock(rows, {"BTCUSDT", "ETHUSDT"}) == Decimal("6")
    assert mark_shock(rows * 2, {"BTCUSDT", "ETHUSDT"}) is None
    del rows[0]["notional"]
    assert mark_shock(rows, {"BTCUSDT", "ETHUSDT"}) is None


def test_monthly_stress_includes_extra_commission_and_both_fill_sides():
    assert stressed_net("10", "2", "3000") == Decimal("3")
    assert stressed_net("10", "-2", "3000") is None


def test_frozen_review_requires_a_full_month_and_enforces_exact_drawdown_boundary():
    from datetime import datetime
    from ai_tuner.monitor.trial_risk_evidence import BEIJING, review_month

    evidence = dict(
        start=datetime(2026, 12, 1, tzinfo=BEIJING), end=datetime(2027, 1, 1, tzinfo=BEIJING),
        income_complete=True, capital_complete=True, funnel_complete=True,
        equity_complete=True, pressure_complete=True, policy_continuous=True,
        closed_lots=20, policy_breaches=0, strategy_drawdown="0.049", account_drawdown="0.04",
        net_usdt="10", commission_usdt="2", turnover_usdt="3000",
    )
    assert review_month(evidence)["status"] == "MONTH_PASSED"
    assert review_month(dict(evidence, equity_complete=False))["status"] == "BLOCKED_DATA"
    assert review_month(dict(evidence, end=datetime(2026, 12, 14, tzinfo=BEIJING)))["status"] == "BLOCKED_DATA"
    assert review_month(dict(evidence, strategy_drawdown="0.05"))["reasons"] == ["drawdown_gate_failed"]
    assert review_month(dict(evidence, closed_lots=19, turnover_usdt="6000"))["reasons"] == [
        "insufficient_closed_lots", "profit_gate_failed",
    ]


def test_risk_budget_counts_verified_intact_lot_once_and_rejects_partial_position():
    from ai_tuner.monitor.trial_risk_evidence import covered_reservation_budget

    positions = [dict(symbol='BTCUSDT', positionSide='BOTH', positionAmt='1')]
    intents = [dict(symbol='BTCUSDT', margin='20', quantity='1', orig_qty='1', side='BUY',
                    executed_qty='1', fill_qty='1', cursor_complete=True)]
    assert covered_reservation_budget(positions, {'BTCUSDT'}, intents) == Decimal('20')
    assert covered_reservation_budget(positions, {'BTCUSDT'}, intents * 2) is None
    assert covered_reservation_budget([dict(positions[0], positionAmt='0.5')], {'BTCUSDT'}, intents) is None
    assert covered_reservation_budget(positions, {'BTCUSDT'}, [dict(intents[0], cursor_complete=False)]) is None


def test_remaining_reserved_budget_requires_conserved_position_after_exits():
    from ai_tuner.monitor.trial_risk_evidence import covered_reservation_budget

    positions = [dict(symbol='BTCUSDT', positionSide='BOTH', positionAmt='0.5')]
    intent = dict(symbol='BTCUSDT', margin='20', quantity='1', orig_qty='1', side='BUY',
                  executed_qty='1', fill_qty='1', exit_qty='0.5', cursor_complete=True)
    assert covered_reservation_budget(positions, {'BTCUSDT'}, [intent]) is None
    assert covered_reservation_budget(positions, {'BTCUSDT'}, [intent], allow_exits=True) == Decimal('10')
    assert covered_reservation_budget(positions, {'BTCUSDT'}, [dict(intent, exit_qty='0.4')],
                                      allow_exits=True) is None
    assert covered_reservation_budget(positions, {'BTCUSDT'}, [dict(intent, fill_qty=None)],
                                      allow_exits=True) is None
