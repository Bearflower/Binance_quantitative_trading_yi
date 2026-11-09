"""Unproven STOPs stay blocked; terminal no-fill takes two observations."""

from unittest.mock import AsyncMock, patch

import pytest

from shared.accounting.stop_exit_audit import (
    audit_due_stop_bindings, unresolved_stop_binding_exists,
)


def binding(observations=0, observed=None):
    return {
        "account_id": "default", "strategy": "btc_eth_aggressive", "symbol": "ETHUSDT",
        "episode_entry_order_id": 10, "algo_id": 20, "stop_side": "SELL",
        "stop_quantity": "1", "terminal_observations": observations,
        "last_observed_status": observed,
    }


@pytest.mark.asyncio
async def test_canceled_without_actual_order_needs_two_separate_scans():
    db = AsyncMock()
    client = AsyncMock()
    client.get_algo_order.return_value = {
        "algoId": 20, "symbol": "ETHUSDT", "algoStatus": "CANCELED",
        "actualOrderId": None,
    }
    with patch("shared.accounting.stop_exit_audit.fetch_verified_closed_stop_episode",
               new=AsyncMock(return_value=None)):
        db.fetch_all.return_value = [binding()]
        first = await audit_due_stop_bindings(db, client, "default", "btc_eth_aggressive")
        db.fetch_all.return_value = [binding(1, "CANCELED")]
        second = await audit_due_stop_bindings(db, client, "default", "btc_eth_aggressive")
    assert first["pending"] == 1
    assert second["no_fill"] == 1
    assert db.execute.await_args_list[0].args[4] == "pending"
    assert db.execute.await_args_list[1].args[4] == "no_fill"


@pytest.mark.asyncio
async def test_verified_episode_persists_before_marking_resolved():
    db = AsyncMock()
    db.fetch_all.return_value = [binding()]
    client = AsyncMock()
    with patch("shared.accounting.stop_exit_audit.fetch_verified_closed_stop_episode",
               new=AsyncMock(return_value={"proof_kind": "closed_stop_episode_v1"})), patch(
               "shared.accounting.stop_exit_audit.persist_verified_stop_exit",
               new=AsyncMock(return_value=True)) as persist:
        result = await audit_due_stop_bindings(db, client, "default", "btc_eth_aggressive")
    assert result["verified"] == 1
    persist.assert_awaited_once()
    assert db.execute.await_args.args[4] == "verified"


@pytest.mark.asyncio
async def test_triggered_without_complete_proof_enters_review_after_retry():
    db = AsyncMock()
    db.fetch_all.return_value = [binding(1, "FINISHED")]
    client = AsyncMock()
    client.get_algo_order.return_value = {
        "algoId": 20, "symbol": "ETHUSDT", "algoStatus": "FINISHED",
        "actualOrderId": 30,
    }
    with patch("shared.accounting.stop_exit_audit.fetch_verified_closed_stop_episode",
               new=AsyncMock(return_value=None)):
        result = await audit_due_stop_bindings(db, client, "default", "btc_eth_aggressive")
    assert result["review"] == 1


@pytest.mark.asyncio
async def test_review_binding_is_scanned_again_when_late_fills_arrive():
    db = AsyncMock()
    db.fetch_all.return_value = [binding(2, "FINISHED")]
    client = AsyncMock()
    with patch("shared.accounting.stop_exit_audit.fetch_verified_closed_stop_episode",
               new=AsyncMock(return_value={"proof_kind": "closed_stop_episode_v1"})), patch(
               "shared.accounting.stop_exit_audit.persist_verified_stop_exit",
               new=AsyncMock(return_value=True)):
        result = await audit_due_stop_bindings(db, client, "default", "btc_eth_aggressive")
    assert result["verified"] == 1
    assert "audit_state IN ('pending','review')" in db.fetch_all.await_args.args[0]
    assert "audit_state IN ('pending','review')" in db.execute.await_args.args[0]


@pytest.mark.asyncio
async def test_unresolved_gate_rejects_missing_read():
    db = AsyncMock()
    db.fetch_one.return_value = {"unresolved": True}
    assert await unresolved_stop_binding_exists(db, "default", "btc_eth_aggressive", "ETHUSDT")
    db.fetch_one.return_value = None
    with pytest.raises(ValueError, match="read unavailable"):
        await unresolved_stop_binding_exists(db, "default", "btc_eth_aggressive", "ETHUSDT")
