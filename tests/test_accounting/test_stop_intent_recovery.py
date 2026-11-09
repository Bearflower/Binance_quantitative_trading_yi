"""Unknown STOP POST outcome is reconciled by the prewritten clientAlgoId."""

from unittest.mock import AsyncMock, patch

import pytest

from shared.accounting.stop_intent_journal import StopIntentJournal, StopJournalError
from shared.accounting.stop_intent_recovery import reconcile_stop_intents


def prepared(tmp_path):
    journal = StopIntentJournal(tmp_path / "stop-intents.jsonl")
    client_id = journal.prepare(
        account_id="default", strategy="btc_eth_aggressive", symbol="ETHUSDT",
        episode_entry_order_id=100, stop_side="SELL", stop_quantity="0.3",
    )
    journal.start_post(client_id)
    return journal, client_id


@pytest.mark.asyncio
async def test_restart_resolves_exchange_order_only_after_binding_is_durable(tmp_path):
    journal, client_id = prepared(tmp_path)
    client = AsyncMock()
    client.get_algo_order.return_value = {
        "algoId": 200, "clientAlgoId": client_id, "symbol": "ETHUSDT",
        "side": "SELL", "orderType": "STOP", "reduceOnly": True,
        "quantity": "0.3",
    }
    with patch("shared.accounting.stop_intent_recovery.record_stop_binding",
               new=AsyncMock(return_value=False)):
        assert await reconcile_stop_intents(journal, object(), client,
                                            "default", "btc_eth_aggressive") == 0
    assert len(StopIntentJournal(journal.path).pending("ETHUSDT")) == 1

    with patch("shared.accounting.stop_intent_recovery.record_stop_binding",
               new=AsyncMock(return_value=True)) as bind:
        assert await reconcile_stop_intents(StopIntentJournal(journal.path), object(), client,
                                            "default", "btc_eth_aggressive") == 1
    assert StopIntentJournal(journal.path).pending("ETHUSDT") == []
    assert bind.await_args.kwargs["episode_entry_order_id"] == 100
    client.get_algo_order.assert_awaited_with(client_algo_id=client_id)


@pytest.mark.asyncio
async def test_wrong_exchange_identity_stays_pending(tmp_path):
    journal, client_id = prepared(tmp_path)
    client = AsyncMock()
    client.get_algo_order.return_value = {
        "algoId": 200, "clientAlgoId": "other", "symbol": "ETHUSDT",
        "side": "SELL", "orderType": "STOP", "reduceOnly": True,
        "quantity": "0.3",
    }
    with patch("shared.accounting.stop_intent_recovery.record_stop_binding",
               new=AsyncMock()) as bind:
        assert await reconcile_stop_intents(journal, object(), client,
                                            "default", "btc_eth_aggressive") == 0
        bind.assert_not_awaited()
    assert journal.pending("ETHUSDT")[0]["client_algo_id"] == client_id


@pytest.mark.asyncio
async def test_account_mismatch_fails_closed(tmp_path):
    journal, _ = prepared(tmp_path)
    with pytest.raises(StopJournalError, match="account"):
        await reconcile_stop_intents(journal, object(), AsyncMock(),
                                     "another-account", "btc_eth_aggressive")


@pytest.mark.asyncio
async def test_recovery_restores_position_before_intent_is_released(tmp_path):
    journal, client_id = prepared(tmp_path)
    client = AsyncMock()
    client.get_algo_order.return_value = {
        "algoId": 200, "clientAlgoId": client_id, "symbol": "ETHUSDT",
        "side": "SELL", "orderType": "STOP", "reduceOnly": True,
        "quantity": "0.3",
    }
    callback = AsyncMock(side_effect=OSError("metadata unavailable"))
    with patch("shared.accounting.stop_intent_recovery.record_stop_binding",
               new=AsyncMock(return_value=True)):
        with pytest.raises(OSError, match="metadata unavailable"):
            await reconcile_stop_intents(journal, object(), client,
                                         "default", "btc_eth_aggressive",
                                         on_resolved=callback)
    assert len(journal.pending("ETHUSDT")) == 1
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_prepost_cancel_uncertainty_repairs_without_placing_new_order(tmp_path):
    journal = StopIntentJournal(tmp_path / "stop-intents.jsonl")
    journal.prepare(
        account_id="default", strategy="btc_eth_aggressive", symbol="ETHUSDT",
        episode_entry_order_id=100, stop_side="SELL", stop_quantity="0.3",
        previous_stop_algo_id=77,
    )
    client = AsyncMock()
    client.get_algo_order.return_value = {
        "algoId": 77, "symbol": "ETHUSDT", "algoStatus": "CANCELED",
        "actualOrderId": None,
    }
    callback = AsyncMock()
    assert await reconcile_stop_intents(
        journal, object(), client, "default", "btc_eth_aggressive",
        on_prepost=callback,
    ) == 1
    assert journal.pending("ETHUSDT") == []
    callback.assert_awaited_once()
    assert callback.await_args.args[1]["algoId"] == 77


@pytest.mark.asyncio
async def test_legacy_prepost_intent_is_closed_without_lot_attribution(tmp_path):
    journal = StopIntentJournal(tmp_path / "stop-intents.jsonl")
    journal.prepare(
        account_id="default", strategy="btc_eth_aggressive", symbol="ETHUSDT",
        episode_entry_order_id=None, stop_side="SELL", stop_quantity="0.3",
    )
    client = AsyncMock()
    assert await reconcile_stop_intents(
        journal, object(), client, "default", "btc_eth_aggressive",
    ) == 1
    assert journal.pending("ETHUSDT") == []
    client.get_algo_order.assert_not_awaited()
