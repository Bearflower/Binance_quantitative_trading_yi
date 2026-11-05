from datetime import datetime, timezone
from decimal import Decimal

import pytest

from scripts.export_unattributed_review import make_review_rows, digest_identity


def _fact():
    return dict(account_id="default", income_type="COMMISSION", tran_id=123,
                symbol="ABCUSDT", income=Decimal("-0.12345678"), asset="USDT",
                event_time_utc=datetime(2026, 11, 2, tzinfo=timezone.utc),
                trade_id=456, order_id=1234567890123456789)


def test_export_keeps_long_ids_as_text_and_never_preapproves():
    event = dict(account_id="default", income_type="COMMISSION", tran_id="123",
                 symbol="ABCUSDT", amount="-0.12345678", asset="USDT",
                 reason="order_owner_missing")
    candidate = dict(account_id="default", income_type="COMMISSION", tran_id="123",
                     symbol="ABCUSDT", income="-0.12345678",
                     order_id="1234567890123456789", strategy="hrs", algo_id="999",
                     algo_payload_sha256="abc",
                     event_time_utc="2026-11-02T00:00:00+00:00")
    rows = make_review_rows([event], [_fact()], [candidate], [])
    assert rows[0]["order_id"] == "1234567890123456789"
    assert rows[0]["suggested_strategy"] == "hrs"
    assert rows[0]["review_decision"] == ""
    assert rows[0]["confirmed_strategy"] == ""
    assert rows[0]["identity_sha256"] == digest_identity(rows[0])


def test_export_rejects_conflicting_candidate_amount():
    event = dict(account_id="default", income_type="COMMISSION", tran_id="123",
                 symbol="ABCUSDT", amount="-0.12345678", asset="USDT",
                 reason="order_owner_missing")
    candidate = dict(account_id="default", income_type="COMMISSION", tran_id="123",
                     symbol="ABCUSDT", income="-0.2", order_id="1234567890123456789",
                     strategy="hrs", algo_id="999", algo_payload_sha256="abc",
                     event_time_utc="2026-11-02T00:00:00+00:00")
    with pytest.raises(ValueError, match="候选与收入事实不一致"):
        make_review_rows([event], [_fact()], [candidate], [])
