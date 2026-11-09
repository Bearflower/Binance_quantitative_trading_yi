from decimal import Decimal

import pytest

from strategies.grid.execution.store import DecisionStore, ExecutionLock, migrate


def test_intent_is_durable_and_active_level_is_unique(tmp_path):
    path = tmp_path / "journal.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("decision-1")
    order = store.prepare_order(epoch, 1, "ENTRY", "BUY", Decimal("1900"),
                                Decimal("0.02"), "eg-a")
    assert order["status"] == "PREPARED"
    with pytest.raises(ValueError, match="active"):
        store.prepare_order(epoch, 1, "ENTRY", "BUY", Decimal("1900"),
                            Decimal("0.02"), "eg-b")
    store.close()
    reopened = DecisionStore(path)
    assert reopened.get_order("eg-a")["status"] == "PREPARED"
    reopened.close()


def test_fill_dedupe_by_exchange_order_and_trade_id(tmp_path):
    path = tmp_path / "journal.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("decision-1")
    store.prepare_order(epoch, 1, "ENTRY", "BUY", Decimal("1900"),
                        Decimal("0.02"), "eg-a")
    store.record_order("eg-a", "42", "PARTIALLY_FILLED", Decimal("0.01"))
    assert store.record_fill("eg-a", "42", "trade-1", Decimal("0.01"),
                             Decimal("1900")) is True
    assert store.record_fill("eg-a", "42", "trade-1", Decimal("0.01"),
                             Decimal("1900")) is False
    assert len(store.fills("eg-a")) == 1
    store.close()


def test_single_instance_lock_fails_second_process(tmp_path):
    path = tmp_path / "executor.lock"
    with ExecutionLock(path):
        with pytest.raises(RuntimeError, match="lock"):
            with ExecutionLock(path):
                pass
    with ExecutionLock(path):
        pass
