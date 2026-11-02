"""Prospective schedule collection never uses trading APIs or historical guesses."""

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from ai_tuner.funding_schedule.collector import FundingScheduleCollector


UTC = timezone.utc
SLOT = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self, premium=None, info=None, fail=False):
        self.calls = []
        self.premium = premium
        self.info = info if info is not None else []
        self.fail = fail

    async def _request(self, method, endpoint, params=None, signed=True):
        self.calls.append((method, endpoint, params, signed))
        if self.fail:
            raise ConnectionError("network unavailable")
        return self.premium if endpoint.endswith("premiumIndex") else self.info


class FakeRepository:
    def __init__(self, universe=("SOLUSDT",), missing_universe=False):
        self.universe = universe
        self.missing_universe = missing_universe
        self.attempts = []
        self.universe_calls = []

    async def latest_universe(self, strategy_id, at):
        self.universe_calls.append((strategy_id, at))
        assert strategy_id == "hrs" and at == SLOT
        if self.missing_universe:
            raise ValueError("no historic scan")
        return self.universe

    async def store_attempt(self, attempt, observations):
        self.attempts.append((attempt, observations))


def row(symbol, now):
    return {
        "symbol": symbol,
        "time": int(now.timestamp() * 1000),
        "nextFundingTime": int((now + timedelta(hours=4)).timestamp() * 1000),
    }


def test_collects_required_union_using_only_unsigned_public_gets():
    now = SLOT + timedelta(seconds=10)
    client = FakeClient([row(s, now) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")])
    repo = FakeRepository(("SOLUSDT", "BTCUSDT"))
    ticks = iter((now, now + timedelta(seconds=1), now + timedelta(seconds=2)))
    result = asyncio.run(FundingScheduleCollector(repo, client, clock=lambda: next(ticks)).collect_once(SLOT, uuid4()))
    assert result.status == "success"
    assert [item.symbol for item in repo.attempts[0][1]] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert client.calls == [
        ("GET", "/fapi/v1/premiumIndex", None, False),
        ("GET", "/fapi/v1/fundingInfo", None, False),
    ]
    assert repo.attempts[0][0].request_started_at == now + timedelta(seconds=1)
    assert repo.attempts[0][0].received_at == now + timedelta(seconds=2)
    frozen = json.dumps(repo.attempts[0][0].raw_payload, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(frozen).hexdigest() == repo.attempts[0][0].payload_hash


def test_network_failure_records_failed_batch_without_observations():
    now = SLOT + timedelta(seconds=10)
    repo = FakeRepository()
    client = FakeClient(fail=True)
    ticks = iter((now, now + timedelta(seconds=1), now + timedelta(seconds=2)))
    result = asyncio.run(FundingScheduleCollector(repo, client, clock=lambda: next(ticks)).collect_once(SLOT, uuid4()))
    assert result.status == "failed"
    assert result.failure_code == "FETCH_FAILED"
    attempt, observations = repo.attempts[0]
    assert attempt.status == "failed" and attempt.failure_code == "FETCH_FAILED"
    assert attempt.request_started_at == now + timedelta(seconds=1)
    assert attempt.received_at == now + timedelta(seconds=2)
    assert observations == ()


def test_missing_historical_universe_fails_closed_and_records_attempt():
    now = SLOT + timedelta(seconds=10)
    repo = FakeRepository(missing_universe=True)
    client = FakeClient()
    ticks = iter((now, now + timedelta(seconds=2)))
    result = asyncio.run(FundingScheduleCollector(repo, client, clock=lambda: next(ticks)).collect_once(SLOT, uuid4()))
    assert result.status == "failed"
    assert result.failure_code == "UNIVERSE_READ_FAILED"
    assert client.calls == []
    assert repo.attempts[0][1] == ()
    assert repo.attempts[0][0].received_at == now + timedelta(seconds=2)


def test_invalid_exchange_response_records_failed_attempt_without_partial_rows():
    now = SLOT + timedelta(seconds=10)
    repo = FakeRepository()
    client = FakeClient([row("BTCUSDT", now)])
    result = asyncio.run(FundingScheduleCollector(repo, client, clock=lambda: now).collect_once(SLOT, uuid4()))
    assert result.status == "failed"
    assert result.failure_code == "MISSING_SYMBOL"
    assert repo.attempts[0][1] == ()


@pytest.mark.parametrize("slot,now", [
    (SLOT - timedelta(minutes=5), SLOT + timedelta(seconds=10)),
    (SLOT + timedelta(minutes=5), SLOT + timedelta(seconds=10)),
    (SLOT + timedelta(minutes=1), SLOT + timedelta(seconds=10)),
    (SLOT.replace(second=1), SLOT + timedelta(seconds=10)),
    (SLOT.replace(microsecond=1), SLOT + timedelta(seconds=10)),
])
def test_stale_future_or_misaligned_slot_fails_before_any_http_or_universe_read(slot, now):
    repo = FakeRepository()
    client = FakeClient()
    ticks = iter((now, now + timedelta(seconds=1)))
    result = asyncio.run(FundingScheduleCollector(repo, client, clock=lambda: next(ticks)).collect_once(slot, uuid4()))
    assert result.status == "failed" and result.failure_code == "MISSED_SCHEDULE_SLOT"
    assert repo.universe_calls == []
    assert client.calls == []
    attempt, observations = repo.attempts[0]
    assert attempt.request_started_at == now and attempt.received_at == now + timedelta(seconds=1)
    assert attempt.raw_payload["http_attempted"] is False
    assert observations == ()


def test_slow_universe_lookup_fails_before_http_and_does_not_backdate_request_start():
    start = SLOT + timedelta(seconds=10)
    late = SLOT + timedelta(seconds=121)
    repo = FakeRepository()
    client = FakeClient()
    ticks = iter((start, late))
    result = asyncio.run(FundingScheduleCollector(repo, client, clock=lambda: next(ticks)).collect_once(SLOT, uuid4()))
    assert result.failure_code == "MISSED_SCHEDULE_SLOT"
    assert repo.universe_calls == [("hrs", SLOT)]
    assert client.calls == []
    attempt, observations = repo.attempts[0]
    assert attempt.request_started_at == start
    assert attempt.received_at == late
    assert attempt.raw_payload["http_attempted"] is False
    assert observations == ()


def test_slow_http_is_not_saved_as_current_slot_success():
    start = SLOT + timedelta(seconds=10)
    request_start = SLOT + timedelta(seconds=11)
    late = SLOT + timedelta(seconds=121)
    repo = FakeRepository()
    client = FakeClient([row(s, request_start) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")])
    ticks = iter((start, request_start, late))
    result = asyncio.run(FundingScheduleCollector(repo, client, clock=lambda: next(ticks)).collect_once(SLOT, uuid4()))
    assert result.status == "failed" and result.failure_code == "MISSED_SCHEDULE_SLOT"
    assert len(client.calls) == 2
    attempt, observations = repo.attempts[0]
    assert attempt.request_started_at == request_start and attempt.received_at == late
    assert attempt.raw_payload["http_attempted"] is True
    assert observations == ()
