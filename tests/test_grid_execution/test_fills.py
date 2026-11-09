from decimal import Decimal

import pytest

from strategies.grid.execution.fills import FillCoordinator, TradeUpdate
from strategies.grid.execution.store import DecisionStore, migrate


def _store(tmp_path):
    path = tmp_path / "execution.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("decision-1")
    store.prepare_order(epoch, 1, "ENTRY", "BUY", Decimal("1950"),
                        Decimal("0.02"), "eg-entry")
    return store, epoch


def _event(qty="0.01", cumulative="0.01", status="PARTIALLY_FILLED", trade="11"):
    return {"e": "ORDER_TRADE_UPDATE", "o": {"s": "ETHUSDT", "c": "eg-entry",
            "i": 42, "S": "BUY", "x": "TRADE", "X": status,
            "l": qty, "z": cumulative, "L": "1950", "t": trade}}


def test_trade_event_parsing_is_strict():
    event = TradeUpdate.parse(_event())
    assert event.quantity == Decimal("0.01")
    assert event.cumulative_quantity == Decimal("0.01")
    with pytest.raises(ValueError):
        TradeUpdate.parse({"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT"}})


def test_duplicate_fill_is_idempotent_and_older_status_does_not_regress(tmp_path):
    store, epoch = _store(tmp_path)
    coordinator = FillCoordinator(store)
    assert coordinator.record(_event()) is True
    assert coordinator.record(_event()) is False
    assert len(store.fills("eg-entry")) == 1
    assert store.get_order("eg-entry")["executed_qty"] == "0.01"
    stale = _event(qty="0.005", cumulative="0.005", trade="10")
    with pytest.raises(ValueError, match="cumulative"):
        coordinator.record(stale)
    store.close()


def test_partial_fills_create_one_reverse_slot_and_remainder(tmp_path):
    store, epoch = _store(tmp_path)
    coordinator = FillCoordinator(store)
    assert coordinator.record(_event()) is True
    levels = (Decimal("1900"), Decimal("1950"), Decimal("2000"))
    first = coordinator.prepare_reverse("eg-entry", levels, Decimal("0.001"),
                                        Decimal("0.01"), Decimal("5"))
    assert first.status == "READY"
    assert first.quantity == Decimal("0.01")
    assert first.side == "SELL" and first.price == Decimal("2000")
    assert store.get_order(first.client_order_id)["status"] == "PREPARED"
    assert coordinator.prepare_reverse("eg-entry", levels, Decimal("0.001"),
                                        Decimal("0.01"), Decimal("5")).status == "REMAINDER"

    assert coordinator.record(_event(qty="0.005", cumulative="0.015", trade="12"))
    assert coordinator.prepare_reverse("eg-entry", levels, Decimal("0.001"),
                                        Decimal("0.01"), Decimal("5")).status == "REMAINDER"
    assert coordinator.record(_event(qty="0.005", cumulative="0.02", status="FILLED", trade="13"))
    assert coordinator.prepare_reverse("eg-entry", levels, Decimal("0.001"),
                                        Decimal("0.01"), Decimal("5")).status == "WAITING_LEVEL"
    assert len(store.conn.execute("SELECT * FROM pair").fetchall()) == 1
    store.close()


def test_existing_entry_at_reverse_level_blocks_pair_order(tmp_path):
    store, epoch = _store(tmp_path)
    store.prepare_order(epoch, 2, "ENTRY", "SELL", Decimal("2000"),
                        Decimal("0.02"), "eg-other")
    coordinator = FillCoordinator(store)
    coordinator.record(_event())
    result = coordinator.prepare_reverse("eg-entry",
        (Decimal("1900"), Decimal("1950"), Decimal("2000")),
        Decimal("0.001"), Decimal("0.01"), Decimal("5"))
    assert result.status == "WAITING_LEVEL"
    assert result.blocked_client_order_ids == ("eg-other",)
    assert store.conn.execute("SELECT count(*) FROM pair").fetchone()[0] == 0
    store.close()


def test_missing_trade_facts_prevent_pairing(tmp_path):
    store, epoch = _store(tmp_path)
    coordinator = FillCoordinator(store)
    coordinator.record(_event(qty="0.005", cumulative="0.02"))
    with pytest.raises(ValueError, match="reconcile"):
        coordinator.prepare_reverse("eg-entry",
            (Decimal("1900"), Decimal("1950"), Decimal("2000")),
            Decimal("0.001"), Decimal("0.01"), Decimal("5"))
    store.close()


def test_reordered_new_event_does_not_downgrade_partial_or_terminal_status(tmp_path):
    store, epoch = _store(tmp_path)
    coordinator = FillCoordinator(store)
    coordinator.record(_event())
    newer = _event(qty="0", cumulative="0.01", status="NEW", trade="0")
    newer["o"]["x"] = "NEW"
    assert coordinator.record(newer) is False
    assert store.get_order("eg-entry")["status"] == "PARTIALLY_FILLED"
    closed = _event(qty="0", cumulative="0.01", status="CANCELED", trade="0")
    closed["o"]["x"] = "CANCELED"
    coordinator.record(closed)
    coordinator.record(newer)
    assert store.get_order("eg-entry")["status"] == "CANCELED"
    store.close()


def test_partial_fill_then_cancel_remains_pairable(tmp_path):
    store, epoch = _store(tmp_path)
    coordinator = FillCoordinator(store)
    coordinator.record(_event())
    cancel = _event(qty="0", cumulative="0.01", status="CANCELED", trade="0")
    cancel["o"]["x"] = "CANCELED"
    assert coordinator.record(cancel) is False
    reverse = coordinator.prepare_reverse("eg-entry",
        (Decimal("1900"), Decimal("1950"), Decimal("2000")),
        Decimal("0.001"), Decimal("0.01"), Decimal("5"))
    assert reverse.status == "READY" and reverse.quantity == Decimal("0.01")
    store.close()


def test_conflicting_duplicate_trade_and_order_id_change_are_rejected(tmp_path):
    store, epoch = _store(tmp_path)
    coordinator = FillCoordinator(store)
    coordinator.record(_event())
    bad_qty = _event(qty="0.005", cumulative="0.01")
    with pytest.raises(ValueError, match="conflicting duplicate"):
        coordinator.record(bad_qty)
    changed_order = _event(qty="0.005", cumulative="0.015", trade="12")
    changed_order["o"]["i"] = 43
    with pytest.raises(ValueError, match="order ID changed"):
        coordinator.record(changed_order)
    assert len(store.fills("eg-entry")) == 1
    store.close()
