import sqlite3

import pytest

from strategies.grid.execution.store import DecisionStore, migrate


def test_requires_explicit_schema_migration(tmp_path):
    path = tmp_path / "execution.sqlite3"
    with pytest.raises(RuntimeError, match="schema"):
        DecisionStore(path)
    migrate(path)
    store = DecisionStore(path)
    assert store.next_decision() is None
    store.close()


def test_decision_insert_is_idempotent_and_cursor_requires_ack(tmp_path):
    path = tmp_path / "execution.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    payload = {"symbol": "ETHUSDT", "state": "OSCILLATION", "calculated_at_ms": 1000,
               "last_closed_kline_ms": {"15m": 900, "1h": 900, "4h": 900}}
    first = store.publish(payload, "hash")
    assert first == store.publish(payload, "hash")
    assert store.next_decision()["decision_id"] == first
    assert store.cursor() is None
    store.acknowledge(first)
    assert store.cursor() == first
    assert store.next_decision() is None
    store.close()


def test_schema_mismatch_refuses_startup(tmp_path):
    path = tmp_path / "execution.sqlite3"
    migrate(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_meta SET value='999' WHERE key='version'")
    with pytest.raises(RuntimeError, match="schema"):
        DecisionStore(path)


def test_rejects_malformed_and_nonmonotonic_decisions(tmp_path):
    path = tmp_path / "execution.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    base = {"symbol": "ETHUSDT", "state": "OSCILLATION", "calculated_at_ms": 1000,
            "last_closed_kline_ms": {"15m": 900, "1h": 900, "4h": 900}}
    store.publish(base, "hash")
    with pytest.raises(ValueError):
        store.publish({**base, "calculated_at_ms": 999}, "hash")
    with pytest.raises(ValueError):
        store.publish({**base, "symbol": "BTCUSDT", "calculated_at_ms": 1001}, "hash")
    store.close()
