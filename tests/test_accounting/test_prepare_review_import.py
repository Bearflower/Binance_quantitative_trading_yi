from decimal import Decimal

import pytest

from scripts.prepare_review_import import validate_review_row


def _source():
    return dict(account_id="default", income_type="COMMISSION",
                tran_id="1689424597180460360", symbol="ABCUSDT",
                income_usdt="-0.12345678", asset="USDT",
                event_time_utc="2026-11-01T00:00:00+00:00",
                trade_id="1234567890123456789", order_id="9876543210123456789",
                unattributed_reason="order_owner_missing",
                suggested_strategy="hrs", suggestion_basis="exact algo",
                evidence_ref="algo:123", identity_sha256="abc")


def _cells():
    s = _source()
    return [s["income_type"], s["tran_id"], "2026/11/1 08:00:00",
            s["symbol"], -0.12345678, s["unattributed_reason"],
            s["trade_id"], s["order_id"], s["suggested_strategy"],
            s["suggestion_basis"], s["evidence_ref"],
            "确认", "hrs", "yl", "核对币安条件单与成交订单号", s["account_id"],
            "UTC " + s["event_time_utc"], s["asset"], s["identity_sha256"]]


def test_confirmed_row_preserves_long_identity_and_requires_note():
    result = validate_review_row(_cells(), _source())
    assert result["tran_id"] == "1689424597180460360"
    assert result["order_id"] == "9876543210123456789"
    assert result["strategy"] == "hrs"

    cells = _cells()
    cells[14] = ""
    with pytest.raises(ValueError, match="证据备注"):
        validate_review_row(cells, _source())


def test_changed_fact_or_numeric_id_is_rejected():
    cells = _cells()
    cells[4] = Decimal("-0.12")
    with pytest.raises(ValueError, match="金额"):
        validate_review_row(cells, _source())

    cells = _cells()
    cells[1] = 1689424597180460288
    with pytest.raises(ValueError, match="流水号"):
        validate_review_row(cells, _source())


def test_blank_and_skip_rows_are_not_imported():
    cells = _cells()
    cells[11:15] = ["", "", "", ""]
    assert validate_review_row(cells, _source()) is None
    cells[11] = "跳过"
    assert validate_review_row(cells, _source()) is None
