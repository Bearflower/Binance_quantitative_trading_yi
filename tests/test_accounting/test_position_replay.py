from datetime import datetime, timedelta, timezone
from decimal import Decimal

from shared.accounting.position_replay import owner_at, replay_position


T0 = datetime(2026, 11, 9, 0, tzinfo=timezone.utc)


def _fill(minutes, trade_id, side, qty, order_id):
    return dict(account_id="pm-main", symbol="SOLUSDT", trade_id=trade_id,
                order_id=order_id, side=side, qty=Decimal(qty),
                trade_time_utc=T0 + timedelta(minutes=minutes))


def _owner(order_id, strategy="btc_eth_aggressive"):
    return dict(account_id="pm-main", symbol="SOLUSDT", order_id=order_id,
                strategy=strategy)


def _replay(fills, owners, final_qty="0", **kwargs):
    return replay_position(
        dict(account_id="pm-main", symbol="SOLUSDT", flat_at=T0,
             exchange_qty=Decimal("0")), fills,
        {str(row["order_id"]): row for row in owners},
        T0 + timedelta(minutes=60), Decimal(final_qty), **kwargs,
    )


def test_open_partial_reduce_and_flat_funding_ownership():
    result = _replay([
        _fill(1, 1, "BUY", "2", 101),
        _fill(10, 2, "SELL", "1", 102),
        _fill(20, 3, "SELL", "1", 103),
    ], [_owner(101)])
    assert result.verified
    assert owner_at(result, T0 + timedelta(minutes=5)) == ("btc_eth_aggressive", None)
    assert owner_at(result, T0 + timedelta(minutes=15)) == ("btc_eth_aggressive", None)
    assert owner_at(result, T0 + timedelta(minutes=25)) == (None, "no_open_position")
    assert owner_at(result, T0 + timedelta(minutes=1)) == (None, "funding_fill_time_collision")


def test_crossing_zero_requires_new_exact_owner():
    fills = [_fill(1, 1, "BUY", "1", 101), _fill(10, 2, "SELL", "2", 202)]
    missing = _replay(fills, [_owner(101)], final_qty="-1")
    assert not missing.verified and missing.reason == "opening_order_owner_missing"
    result = _replay(fills, [_owner(101), _owner(202, "btc_eth")], final_qty="-1")
    assert result.verified
    assert owner_at(result, T0 + timedelta(minutes=5)) == ("btc_eth_aggressive", None)
    assert owner_at(result, T0 + timedelta(minutes=15)) == ("btc_eth", None)


def test_unknown_entry_mixed_owner_gap_and_qty_mismatch_fail_closed():
    fill = _fill(1, 1, "BUY", "1", 101)
    assert _replay([fill], [], final_qty="1").reason == "opening_order_owner_missing"
    mixed = _replay([fill, _fill(2, 2, "BUY", "1", 202)],
                    [_owner(101), _owner(202, "btc_eth")], final_qty="2")
    assert mixed.reason == "multiple_position_owners"
    assert _replay([fill], [_owner(101)], final_qty="1",
                   coverage_complete=False).reason == "fill_coverage_incomplete"
    assert _replay([fill], [_owner(101)], final_qty="2").reason == "exchange_quantity_mismatch"


def test_wrong_account_order_evidence_cannot_open_interval():
    bad = _owner(101)
    bad["account_id"] = "other"
    result = _replay([_fill(1, 1, "BUY", "1", 101)], [bad], final_qty="1")
    assert result.reason == "opening_order_owner_missing"
