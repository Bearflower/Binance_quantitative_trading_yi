"""STOP intent survives restart and cannot disappear on an invalid replay."""

import json

import pytest

from shared.accounting.stop_intent_journal import StopIntentJournal, StopJournalError


def prepare(journal):
    return journal.prepare(
        account_id="default", strategy="btc_eth_aggressive", symbol="ETHUSDT",
        episode_entry_order_id=100, stop_side="SELL", stop_quantity="0.3",
    )


def test_fsynced_intent_survives_new_journal_instance(tmp_path):
    path = tmp_path / "stop-intents.jsonl"
    first = StopIntentJournal(path)
    first.probe()
    client_id = prepare(first)

    restarted = StopIntentJournal(path)
    assert restarted.pending("ETHUSDT")[0]["client_algo_id"] == client_id
    restarted.resolve(client_id, 200)
    assert StopIntentJournal(path).pending("ETHUSDT") == []


def test_invalid_or_corrupt_journal_fails_closed(tmp_path):
    path = tmp_path / "stop-intents.jsonl"
    journal = StopIntentJournal(path)
    with pytest.raises(StopJournalError):
        journal.prepare(account_id="", strategy="a", symbol="ETHUSDT",
                        episode_entry_order_id=100, stop_side="SELL", stop_quantity="1")
    with pytest.raises(StopJournalError):
        journal.prepare(account_id="default", strategy="a", symbol="ETHUSDT",
                        episode_entry_order_id=100, stop_side="SELL", stop_quantity="NaN")
    path.write_text('{"kind":"intent"\n')
    with pytest.raises(StopJournalError, match="corrupted"):
        journal.pending()


def test_missing_persistent_directory_denies_probe(tmp_path):
    journal = StopIntentJournal(tmp_path / "missing" / "stop-intents.jsonl")
    with pytest.raises(OSError):
        journal.probe()


def test_resolution_without_intent_or_conflicting_id_fails_closed(tmp_path):
    path = tmp_path / "stop-intents.jsonl"
    path.write_text(json.dumps({"kind": "resolved", "client_algo_id": "sa1", "algo_id": 20}) + "\n")
    with pytest.raises(StopJournalError, match="no intent"):
        StopIntentJournal(path).pending()


def test_invalid_resolution_cannot_hide_pending_intent(tmp_path):
    path = tmp_path / "stop-intents.jsonl"
    journal = StopIntentJournal(path)
    client_id = prepare(journal)
    with path.open("a") as handle:
        handle.write(json.dumps({"kind": "resolved", "client_algo_id": client_id}) + "\n")
    with pytest.raises(StopJournalError, match="resolution invalid"):
        journal.pending("ETHUSDT")
