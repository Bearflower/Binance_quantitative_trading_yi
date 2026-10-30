"""Contract for prospective, per-symbol funding schedule observations."""

from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from ai_tuner.funding_schedule.models import FundingScheduleDataError, normalize_observations


NOW = datetime(2026, 10, 30, 8, 0, tzinfo=timezone.utc)


def ms(value):
    return int(value.timestamp() * 1000)


def premium(symbol, *, source=NOW, next_at=NOW + timedelta(hours=4)):
    return {"symbol": symbol, "time": ms(source), "nextFundingTime": ms(next_at),
            "lastFundingRate": "0.0001"}


def normalize(**overrides):
    args = {
        "scheduled_at": NOW,
        "received_at": NOW + timedelta(seconds=3),
        "required_symbols": ["ETHUSDT", "BTCUSDT", "ETHUSDT"],
        "premium_rows": [premium("BTCUSDT"), premium("ETHUSDT")],
        "funding_info_rows": [
            {"symbol": "BTCUSDT", "fundingIntervalHours": 4},
            {"symbol": "ETHUSDT", "fundingIntervalHours": 8},
        ],
    }
    args.update(overrides)
    return normalize_observations(**args)


def test_normalizes_required_union_and_individual_intervals():
    digest, observations = normalize()
    assert len(digest) == 64
    assert [row.symbol for row in observations] == ["BTCUSDT", "ETHUSDT"]
    assert [row.funding_interval_hours for row in observations] == [4, 8]
    assert all(row.source_time == NOW for row in observations)
    assert all(row.received_at == NOW + timedelta(seconds=3) for row in observations)
    assert all(row.next_funding_time == NOW + timedelta(hours=4) for row in observations)


def test_hash_ignores_response_order_and_unselected_fields():
    first_hash, first_rows = normalize()
    second_hash, second_rows = normalize(
        required_symbols=["BTCUSDT", "ETHUSDT"],
        premium_rows=[{**premium("ETHUSDT"), "lastFundingRate": "999"}, premium("BTCUSDT")],
        funding_info_rows=[
            {"symbol": "ETHUSDT", "fundingIntervalHours": 8},
            {"symbol": "BTCUSDT", "fundingIntervalHours": 4},
        ],
    )
    assert second_hash == first_hash
    assert second_rows == first_rows


def test_hash_is_canonical_selected_field_json_for_batch_and_each_row():
    batch_hash, rows = normalize()
    def sha256(value):
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()
    assert batch_hash == sha256([row.raw_payload for row in rows])
    assert all(row.payload_hash == sha256(row.raw_payload) for row in rows)


def test_source_delay_at_exact_limit_remains_valid():
    _, rows = normalize(received_at=NOW + timedelta(seconds=120))
    assert len(rows) == 2


def test_missing_auxiliary_interval_is_none_not_default_eight():
    _, rows = normalize(funding_info_rows=[])
    assert [row.funding_interval_hours for row in rows] == [None, None]


@pytest.mark.parametrize("premium_rows", [
    [premium("BTCUSDT")],
    [premium("BTCUSDT"), premium("ETHUSDT"), premium("ETHUSDT")],
    [{"symbol": "BTCUSDT", "time": ms(NOW)}, premium("ETHUSDT")],
    [{**premium("BTCUSDT"), "time": ms(NOW - timedelta(seconds=121))}, premium("ETHUSDT")],
    [{**premium("BTCUSDT"), "time": ms(NOW + timedelta(seconds=4))}, premium("ETHUSDT")],
    [{**premium("BTCUSDT"), "nextFundingTime": ms(NOW)}, premium("ETHUSDT")],
    [{**premium("BTCUSDT"), "time": 0}, premium("ETHUSDT")],
    [{**premium("BTCUSDT"), "time": True}, premium("ETHUSDT")],
])
def test_required_premium_response_invalidity_fails_closed(premium_rows):
    with pytest.raises(FundingScheduleDataError):
        normalize(premium_rows=premium_rows)


@pytest.mark.parametrize("changed", [
    {"scheduled_at": NOW.replace(tzinfo=None)},
    {"received_at": NOW.replace(tzinfo=None)},
    {"received_at": NOW.astimezone(timezone(timedelta(hours=8)))},
    {"required_symbols": []},
    {"funding_info_rows": [{"symbol": "BTCUSDT", "fundingIntervalHours": 0}]},
    {"funding_info_rows": [{"symbol": "BTCUSDT", "fundingIntervalHours": 4},
                           {"symbol": "BTCUSDT", "fundingIntervalHours": 4}]},
])
def test_invalid_envelope_or_auxiliary_response_fails_closed(changed):
    with pytest.raises(FundingScheduleDataError):
        normalize(**changed)
