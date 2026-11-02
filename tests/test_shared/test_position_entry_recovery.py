from datetime import datetime, timezone
from decimal import Decimal

from shared.position_entry_recovery import verified_anchored_lot, verified_entry_time


def test_recovers_first_fill_after_last_flat_and_matches_exchange():
    trades = [
        {"id": 1, "time": 1000, "side": "BUY", "qty": "2"},
        {"id": 2, "time": 2000, "side": "SELL", "qty": "2"},
        {"id": 3, "time": 3000, "side": "BUY", "qty": "1"},
        {"id": 4, "time": 4000, "side": "BUY", "qty": "0.5"},
    ]
    assert verified_entry_time(trades, Decimal("1.5")) == datetime.fromtimestamp(3, timezone.utc)


def test_rejects_missing_flat_boundary_or_incomplete_history():
    trades = [{"id": 3, "time": 3000, "side": "BUY", "qty": "1"}]
    assert verified_entry_time(trades, Decimal("1")) is None
    assert verified_entry_time(trades, Decimal("2")) is None


def test_rejects_direction_or_quantity_mismatch():
    trades = [
        {"id": 1, "time": 1000, "side": "BUY", "qty": "2"},
        {"id": 2, "time": 2000, "side": "SELL", "qty": "2"},
        {"id": 3, "time": 3000, "side": "BUY", "qty": "1"},
    ]
    assert verified_entry_time(trades, Decimal("-1")) is None
    assert verified_entry_time(trades, Decimal("0.9")) is None


def test_rejects_full_page_that_may_be_truncated():
    trades = [
        {"id": i, "time": i * 1000, "side": "BUY", "qty": "1"}
        for i in range(1000)
    ]
    assert verified_entry_time(trades, Decimal("1000"), page_limit=1000) is None


def test_verified_anchor_allows_old_history_before_query_window():
    trades = [
        {"id": 3, "time": 3000, "side": "BUY", "qty": "10"},
        {"id": 4, "time": 4000, "side": "SELL", "qty": "2"},
    ]
    assert verified_anchored_lot(trades, Decimal("8"), 3000)
    assert not verified_anchored_lot(trades, Decimal("9"), 3000)


def test_verified_anchor_rejects_reopened_lot_and_truncated_page():
    trades = [
        {"id": 3, "time": 3000, "side": "BUY", "qty": "10"},
        {"id": 4, "time": 4000, "side": "SELL", "qty": "10"},
        {"id": 5, "time": 5000, "side": "BUY", "qty": "8"},
    ]
    assert not verified_anchored_lot(trades, Decimal("8"), 3000)
    assert not verified_anchored_lot(trades, Decimal("8"), 3000, page_limit=3)
