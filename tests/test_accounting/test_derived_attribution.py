from datetime import datetime, timedelta, timezone
from decimal import Decimal

from shared.accounting.derived_attribution import derive_income
from shared.accounting.position_replay import replay_position


T0 = datetime(2026, 11, 9, tzinfo=timezone.utc)


def income(kind="COMMISSION", amount="-0.1", trade_id=7):
    return {"account_id": "main", "symbol": "ABCUSDT", "tran_id": 42,
            "income_type": kind, "income": Decimal(amount), "asset": "USDT",
            "event_time_utc": T0 + timedelta(seconds=2), "trade_id": trade_id}


def fill():
    return {"account_id": "main", "symbol": "ABCUSDT", "trade_id": 7,
            "order_id": 10, "commission": Decimal("0.1"),
            "commission_asset": "USDT", "realized_pnl": Decimal("2")}


def evidence():
    return {"account_id": "main", "symbol": "ABCUSDT", "order_id": 10,
            "strategy": "hrs", "evidence": {"source": "order"}}


def test_exact_order_income_derives_exchange_amount_and_evidence():
    row, reason = derive_income(income(), {7: fill()}, {"10": evidence()}, None)
    assert reason is None
    assert row["income"] == Decimal("-0.1")
    assert row["method"] == "exact_order"
    assert row["strategy"] == "hrs"
    assert row["evidence"]["trade_id"] == "7"


def test_wrong_amount_and_non_usdt_fail_closed():
    assert derive_income(income(amount="-0.2"), {7: fill()}, {"10": evidence()}, None)[1] == "fill_income_amount_mismatch"
    other = {**income(), "asset": "BNB"}
    assert derive_income(other, {7: fill()}, {"10": evidence()}, None)[1] == "non_usdt_asset"


def test_funding_uses_only_verified_position_interval():
    baseline = {"account_id": "main", "symbol": "ABCUSDT", "flat_at": T0,
                "exchange_qty": 0, "evidence": {"position": "zero"}}
    opening = {"account_id": "main", "symbol": "ABCUSDT", "trade_id": 7,
               "order_id": 10, "trade_time_utc": T0 + timedelta(seconds=1),
               "side": "BUY", "qty": Decimal("1")}
    replay = replay_position(baseline, [opening], {"10": evidence()},
                             T0 + timedelta(seconds=3), Decimal("1"))
    row, reason = derive_income(income("FUNDING_FEE", "-0.1", None), {}, {}, replay)
    assert reason is None
    assert row["method"] == "verified_position_interval"
    assert row["strategy"] == "hrs"
    assert row["evidence"]["fill_ids_before_event"] == ["7"]
    assert derive_income(income("FUNDING_FEE", "-0.1", None), {}, {}, None)[1] == "position_unverified"
