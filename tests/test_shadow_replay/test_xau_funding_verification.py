import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from backtest.btc_eth.replay.funding_verification import verify_funding_schedule


START = datetime(2026, 11, 18, 3, 50, tzinfo=timezone.utc)
END = START + timedelta(minutes=20)
SETTLEMENT = START + timedelta(minutes=10)


def ms(value):
    return int(value.timestamp() * 1000)


def observations():
    rows = []
    for n in (-1, 0, 1, 2, 3):
        slot = START + timedelta(minutes=n * 5)
        forecast = SETTLEMENT if slot < SETTLEMENT else SETTLEMENT + timedelta(hours=4)
        payload = {
            "symbol": "XAUUSDT", "time": ms(slot),
            "nextFundingTime": ms(forecast), "fundingIntervalHours": 4,
        }
        rows.append({
            "scheduled_at_ms": ms(slot), "request_started_at_ms": ms(slot),
            "received_at_ms": ms(slot + timedelta(milliseconds=500)),
            "raw_payload": payload,
            "payload_hash": hashlib.sha256(json.dumps(
                payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        })
    return rows


def audit(rows, settlements):
    return verify_funding_schedule(rows, [(value, "0.0001") for value in settlements],
                                   start_at=START, end_at=END, symbol="XAUUSDT")


def test_success_requires_prior_forecast_and_matches_millisecond_timestamp():
    result = audit(observations(), [SETTLEMENT + timedelta(milliseconds=1)])
    assert result["verified"]
    assert len(result["matches"]) == 1
    assert result["matches"][0]["expected_at"] == SETTLEMENT.isoformat()


@pytest.mark.parametrize("settlements, issue", [
    ([], "missing_settlement"),
    ([SETTLEMENT, SETTLEMENT + timedelta(milliseconds=1)], "ambiguous_settlement"),
    ([SETTLEMENT, SETTLEMENT + timedelta(minutes=5)], "unexpected_settlement"),
])
def test_incomplete_or_ambiguous_actual_history_stays_unverified(settlements, issue):
    result = audit(observations(), settlements)
    assert not result["verified"]
    assert issue in result["reasons"]


def test_missing_prior_slot_is_not_hidden_by_a_valid_settlement():
    result = audit(observations()[1:], [SETTLEMENT])
    assert not result["verified"]
    assert result["missing_observation_slots"] == 1
    assert len(result["matches"]) == 1


def test_late_or_tampered_observations_cannot_certify_a_payment():
    rows = observations()
    rows[0]["raw_payload"]["nextFundingTime"] += 1
    for row in rows[1:3]:
        row["received_at_ms"] = ms(SETTLEMENT)
    result = audit(rows, [SETTLEMENT])
    assert not result["verified"]
    assert result["invalid_observations"] == 3
    assert result["matches"] == []
