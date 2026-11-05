from datetime import datetime, timezone
from decimal import Decimal

import pytest

from scripts.apply_reviewed_attribution import identity_digest, validate_entry


def _entry():
    row = dict(account_id="default", income_type="COMMISSION", tran_id="123",
               symbol="ABCUSDT", income_usdt="-0.20000000", asset="USDT",
               event_time_utc="2026-11-02T00:00:00+00:00",
               trade_id="456", order_id="789", strategy="hrs",
               reviewed_by="yl", evidence_note="币安订单号与策略日志逐笔一致")
    row["identity_sha256"] = identity_digest(row)
    return row


def _live():
    return dict(account_id="default", income_type="COMMISSION", tran_id=123,
                symbol="ABCUSDT", income=Decimal("-0.20000000"), asset="USDT",
                event_time_utc=datetime(2026, 11, 2, tzinfo=timezone.utc),
                trade_id=456, order_id=789)


def test_exact_review_entry_accepts_original_fact():
    assert validate_entry(_entry(), _live())["strategy"] == "hrs"


def test_changed_fact_or_unknown_strategy_fails_closed():
    row = _entry()
    row["income_usdt"] = "-0.3"
    with pytest.raises(ValueError, match="身份摘要"):
        validate_entry(row, _live())
    row = _entry()
    row["strategy"] = "unknown"
    with pytest.raises(ValueError, match="策略"):
        validate_entry(row, _live())
    row = _entry()
    live = _live()
    live["order_id"] = 790
    with pytest.raises(ValueError, match="订单号"):
        validate_entry(row, live)
