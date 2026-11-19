from datetime import datetime

from ai_tuner.ops.hrs_acceptance import (
    BEIJING, evaluate_policies, monthly_result, sample_candidates, verify_reservation,
)


def test_mixed_policy_revisions_and_expiry_are_checked_individually():
    now = datetime(2026, 11, 18, 7, 33, tzinfo=BEIJING)
    rows = [
        dict(strategy_id=s, revision=r, mode=m, freeze_allocation=True, allow_add=False,
             effective_at=now, expires_at=datetime(2026, 12, 14, 11, tzinfo=BEIJING),
             audited=True, entry_ceiling="26.18")
        for s, r, m in [
            ("hrs", 6, "reduced"), ("btc_eth", 5, "capped"),
            ("btc_eth_aggressive", 5, "reduced"), ("new_coin", 5, "capped"),
        ]
    ]
    assert evaluate_policies(rows, now) == []
    rows[0]["expires_at"] = now
    assert evaluate_policies(rows, now) == ["hrs r6 已到期 2026-11-18T07:33:00+08:00"]


def test_live_hrs_reservation_requires_exact_exchange_order_and_position():
    row = dict(symbol="WLDUSDT", strategy_id="hrs", state="submitting",
               client_order_id="entry", quantity="28", position_quantity="28")
    order = dict(symbol="WLDUSDT", clientOrderId="entry", status="FILLED", side="BUY",
                 executedQty="28", origQty="28")
    position = dict(symbol="WLDUSDT", positionAmt="28")
    assert verify_reservation(row, order, [position])
    assert not verify_reservation(row, order, [])
    assert not verify_reservation(row, dict(order, clientOrderId="other"), [position])
    assert not verify_reservation(dict(row, position_quantity="0"), order, [position])


def test_unlinked_signal_cannot_be_promoted_to_live_acceptance():
    signals = [
        ("btc_eth", "2026-11-18", "ETHUSDT 新入场成功"),
        ("btc_eth", "2026-11-18", "ETHUSDT TP2数量精度调整后为0，标记为不可挂并跳过"),
    ]
    assert sample_candidates(signals) == [
        ("btc_eth", "2026-11-18", "ETHUSDT", "微仓 N/A"),
    ]


def test_allocation_requires_complete_income_and_no_write_on_failure():
    row = {"month": "2026-12", "status": "active", "total_capital": "100", "strategy_count": 1,
           "entries": [{"strategy_id": "hrs", "realized_pnl": "2", "initial_capital": "25",
                        "return_rate": ".08", "allocated_ratio": ".2", "allocated_amount": "20"}]}
    assert "门禁失败后出现分配写入" in monthly_result(row, {}, blocked=True)
    assert "收入覆盖或唯一归属未证实" in monthly_result(row, {}, blocked=False)
    proof = {"account_coverage_complete": True, "strategy_attribution_complete": True,
             "by_strategy": {"hrs": {"realized_pnl_usdt": "2"}}}
    assert monthly_result(row, proof, blocked=False) == "2026-12 分配行与月度收入及金额算式一致"


def test_report_conclusion_keeps_shadow_block_visible_without_daily_anomalies():
    from ai_tuner.ops.hrs_acceptance import report_conclusion

    assert report_conclusion(0, 0, None, "BLOCKED_DATA") == (
        "【结论】本次门禁与风控核验通过。月度分配记录无变化，影子核验仍受阻。"
    )
    assert report_conclusion(1, 2, None, "BLOCKED_DATA") == (
        "【结论】1 项异常待核对，2 个新增实盘候选待核验。"
        "月度分配记录无变化，影子核验仍受阻。"
    )
