from datetime import datetime, timezone
from decimal import Decimal
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from scripts.reconcile_income_attribution import (
    plan_reconciliation, apply_plan, verified_position_at_cutoff,
    build_replay_by_symbol,
    verified_fact_cutoff,
)


AT = datetime(2026, 11, 9, tzinfo=timezone.utc)


def _income():
    return {"account_id": "main", "symbol": "ABCUSDT", "tran_id": 21,
            "income_type": "COMMISSION", "income": Decimal("-0.1"),
            "asset": "USDT", "event_time_utc": AT, "trade_id": 7}


def _fill():
    return {"account_id": "main", "symbol": "ABCUSDT", "trade_id": 7,
            "order_id": 10, "commission": Decimal("0.1"),
            "commission_asset": "USDT", "realized_pnl": Decimal("0")}


def _trade():
    return {"id": 1, "account_id": "main", "symbol": "ABCUSDT",
            "order_id": "10", "strategy": "hrs"}


def test_shadow_plan_never_assigns_by_symbol_and_preserves_amount():
    plan = plan_reconciliation([_income()], [_fill()], [_trade()], [], [], [], [], {})
    assert plan["derived_rows"][0]["strategy"] == "hrs"
    assert plan["derived_rows"][0]["income"] == Decimal("-0.1")
    assert plan["order_rows"][0]["order_id"] == 10
    unknown = plan_reconciliation([_income()], [_fill()], [], [], [], [], [], {})
    assert unknown["derived_rows"] == []
    assert unknown["rejected"][0]["reason"] == "order_owner_missing"


def test_manual_conflict_blocks_writer_and_same_manual_owner_is_skipped():
    manual = {**_income(), "strategy": "btc_eth"}
    plan = plan_reconciliation([_income()], [_fill()], [_trade()], [], [], [manual], [], {})
    assert plan["conflicts"][0]["reason"] == "manual_owner_conflict"
    with pytest.raises(ValueError, match="conflict"):
        apply_plan(None, plan, coverage_complete=True)
    manual["strategy"] = "hrs"
    plan = plan_reconciliation([_income()], [_fill()], [_trade()], [], [], [manual], [], {})
    assert plan["derived_rows"] == []


def test_existing_derived_identity_is_immutable():
    old = {**_income(), "strategy": "btc_eth"}
    plan = plan_reconciliation([_income()], [_fill()], [_trade()], [], [], [], [old], {})
    assert plan["conflicts"][0]["reason"] == "derived_owner_conflict"
    old = {**_income(), "strategy": "hrs", "method": "exact_order",
           "evidence": {"different": True}, "algorithm_version": 1}
    plan = plan_reconciliation([_income()], [_fill()], [_trade()], [], [], [], [old], {})
    assert plan["conflicts"][0]["reason"] == "derived_evidence_conflict"


def test_apply_requires_full_coverage_before_any_write():
    plan = plan_reconciliation([_income()], [_fill()], [_trade()], [], [], [], [], {})
    with pytest.raises(ValueError, match="coverage"):
        apply_plan(None, plan, coverage_complete=False)


def test_safe_cutoff_uses_earliest_income_or_fill_watermark():
    cursors = [
        {"account_id": "main", "source": "um/income", "income_type": kind,
         "symbol": None, "window_start_ms": 0, "covered_until_ms": end,
         "page_complete": True, "request_status": "ok"}
        for kind, end in (("REALIZED_PNL", 100), ("COMMISSION", 90),
                          ("FUNDING_FEE", 80))]
    cursors.append({"account_id": "main", "source": "userTrades",
                    "income_type": None, "symbol": "ABCUSDT", "window_start_ms": 0,
                    "covered_until_ms": 70, "page_complete": True,
                    "request_status": "ok"})
    assert verified_fact_cutoff(cursors, "main", 0, 100, {"ABCUSDT"}) == 70


@pytest.mark.asyncio
async def test_position_snapshot_requires_no_trades_between_cutoff_and_observation(monkeypatch):
    client = type("Client", (), {})()
    client._request = AsyncMock(side_effect=[
        [{"symbol": "ABCUSDT", "positionSide": "BOTH", "positionAmt": "0"}], []])
    monkeypatch.setattr("scripts.reconcile_income_attribution.time.time", lambda: 1800000000)
    qty, reason, proof = await verified_position_at_cutoff(client, "ABCUSDT", 1799999999000)
    assert qty == Decimal(0) and reason is None
    assert proof["position_side"] == "BOTH"
    client._request.side_effect = [
        [{"symbol": "ABCUSDT", "positionSide": "BOTH", "positionAmt": "0"}], [{"id": 1}]]
    assert (await verified_position_at_cutoff(client, "ABCUSDT", 1799999999000))[1] == "post_cutoff_fill"
    client._request.side_effect = [
        [{"symbol": "ABCUSDT", "positionSide": "BOTH", "positionAmt": "0"}], {"error": "unavailable"}]
    assert (await verified_position_at_cutoff(client, "ABCUSDT", 1799999999000))[1] == "post_cutoff_fill_coverage_incomplete"


def test_funding_replay_from_existing_flat_baseline():
    baseline = {"account_id": "main", "symbol": "ABCUSDT",
                "flat_at": AT - timedelta(seconds=3), "exchange_qty": Decimal(0),
                "evidence": {"position": "zero"}}
    opening = {**_fill(), "side": "BUY", "qty": Decimal("1"),
               "trade_time_utc": AT - timedelta(seconds=1)}
    replay = build_replay_by_symbol("main", {"ABCUSDT": baseline}, [opening],
                                     [_trade()], [], [],
                                     {"ABCUSDT": Decimal("1")}, AT + timedelta(seconds=1),
                                     {"ABCUSDT": True})
    funding = {**_income(), "income_type": "FUNDING_FEE", "trade_id": None}
    plan = plan_reconciliation([funding], [opening], [_trade()], [], [], [], [], replay)
    assert plan["derived_rows"][0]["method"] == "verified_position_interval"
    old = dict(plan["derived_rows"][0])
    old["evidence"] = {**old["evidence"], "verification_cutoff": "older-cutoff"}
    repeat = plan_reconciliation([funding], [opening], [_trade()], [], [], [], [old], replay)
    assert repeat["conflicts"] == []
    assert repeat["derived_rows"] == []
