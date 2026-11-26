"""Settlement-boundary refreshes retain only genuine prospective observations."""

import asyncio
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from ai_tuner.funding_schedule.collector import FundingScheduleCollector
from ai_tuner.funding_schedule.models import normalize_observations
from backtest.btc_eth.replay.funding_verification import verify_funding_schedule

SLOT = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
INITIAL_RECEIVED = SLOT + timedelta(milliseconds=190)
SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
INFO = [{"symbol": "XAUUSDT", "fundingIntervalHours": 4}]


def ms(value):
    return int(value.timestamp() * 1000)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def rows(source=None, target=None):
    source = source or SLOT - timedelta(seconds=2)
    return [{"symbol": symbol, "time": ms(source),
             "nextFundingTime": ms(target if symbol == "XAUUSDT" and target else SLOT + timedelta(hours=4))}
            for symbol in sorted(SYMBOLS | {"XAUUSDT"})]


class Clock:
    def __init__(self):
        self.now = SLOT
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


class Repository:
    def __init__(self, xau_required=False):
        self.universe = SYMBOLS | ({"XAUUSDT"} if xau_required else set())
        self.attempts = []

    async def latest_universe(self, strategy, slot):
        assert strategy == "hrs" and slot == SLOT
        return self.universe

    async def store_attempt(self, attempt, observations):
        self.attempts.append((attempt, observations))


class Client:
    def __init__(self, clock, refresh=None, first=None):
        self.clock = clock
        self.refresh = refresh
        self.first = first if first is not None else rows(target=SLOT)
        self.calls = []
        self.batches = 0

    async def _request(self, method, endpoint, params=None, signed=True):
        self.calls.append((method, endpoint, signed, self.clock.now))
        assert method == "GET" and signed is False and params is None
        assert endpoint in ("/fapi/v1/premiumIndex", "/fapi/v1/fundingInfo")
        if endpoint.endswith("premiumIndex"):
            self.batches += 1
            if self.batches == 1:
                return copy.deepcopy(self.first)
            return self.refresh(self) if self.refresh else rows(target=SLOT)
        self.clock.now += timedelta(milliseconds=190)
        return copy.deepcopy(INFO)


def collect(monkeypatch, refresh=None, *, xau_required=False, first=None, clock=None):
    clock = clock or Clock()
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    repo = Repository(xau_required)
    client = Client(clock, refresh, first)
    result = asyncio.run(FundingScheduleCollector(repo, client, clock=clock).collect_once(SLOT, uuid4()))
    assert len(repo.attempts) == 1
    return result, repo.attempts[0], client, clock


def verifier_row(attempt, observation):
    return {"scheduled_at_ms": ms(attempt.scheduled_at),
            "request_started_at_ms": ms(attempt.request_started_at),
            "received_at_ms": ms(attempt.received_at),
            "raw_payload": observation.raw_payload, "payload_hash": observation.payload_hash}


def assert_original_normal_rows(attempt, observations):
    expected_hash, expected = normalize_observations(
        scheduled_at=SLOT, received_at=INITIAL_RECEIVED, required_symbols=SYMBOLS,
        premium_rows=rows(target=SLOT), funding_info_rows=INFO)
    assert attempt.request_started_at == SLOT
    assert attempt.received_at == INITIAL_RECEIVED
    assert observations == expected
    assert attempt.raw_payload == [item.raw_payload for item in expected]
    assert attempt.payload_hash == expected_hash == digest(attempt.raw_payload)


@pytest.mark.parametrize("xau_required", [False, True])
def test_expired_boundary_refresh_passes_unchanged_real_verifier(monkeypatch, xau_required):
    original = rows(target=SLOT)
    refreshed = []

    def refresh(client):
        batch = rows(source=client.clock.now)
        refreshed.extend(copy.deepcopy(batch))
        return batch

    result, (attempt, observations), client, clock = collect(monkeypatch, refresh, first=original, xau_required=xau_required)
    assert result.status == "success" and result.symbol_count == 4
    assert client.batches == 2
    assert clock.sleeps == [1]
    assert attempt.request_started_at == INITIAL_RECEIVED + timedelta(seconds=1)
    assert attempt.received_at == attempt.request_started_at + timedelta(milliseconds=190)
    assert attempt.payload_hash == digest(attempt.raw_payload)
    assert all(item.received_at == attempt.received_at for item in observations)
    assert all(item.payload_hash == digest(item.raw_payload) for item in observations)
    assert [item.raw_payload["time"] for item in observations] == [item["time"] for item in refreshed]
    xau = next(item for item in observations if item.symbol == "XAUUSDT")
    evidence = verifier_row(attempt, xau)
    prior = copy.deepcopy(evidence)
    prior.update(scheduled_at_ms=ms(SLOT - timedelta(minutes=5)),
                 request_started_at_ms=ms(SLOT - timedelta(minutes=5)),
                 received_at_ms=ms(SLOT - timedelta(minutes=5) + timedelta(milliseconds=190)))
    prior["raw_payload"].update(time=ms(SLOT - timedelta(minutes=5)), nextFundingTime=ms(SLOT))
    prior["payload_hash"] = digest(prior["raw_payload"])
    audit = verify_funding_schedule([prior, evidence], [(SLOT, "0.0001")],
                                   start_at=SLOT, end_at=SLOT + timedelta(minutes=5), symbol="XAUUSDT")
    assert audit["verified"] and len(audit["matches"]) == 1
    # The historic expired snapshot stays invalid; no receipt or forecast rewriting.
    historical = copy.deepcopy(evidence)
    historical.update(request_started_at_ms=ms(SLOT), received_at_ms=ms(INITIAL_RECEIVED))
    historical["raw_payload"].update(time=ms(SLOT - timedelta(seconds=2)), nextFundingTime=ms(SLOT))
    historical["payload_hash"] = digest(historical["raw_payload"])
    assert verify_funding_schedule([prior, historical], [(SLOT, "0.0001")],
                                  start_at=SLOT, end_at=SLOT + timedelta(minutes=5),
                                  symbol="XAUUSDT")["invalid_observations"] == 1
    assert original == rows(target=SLOT)


@pytest.mark.parametrize("failure", ["expired", "network", "malformed", "missing_required", "future_source"])
def test_unusable_refresh_retains_original_normal_symbol_evidence(monkeypatch, failure):
    def refresh(client):
        if failure == "network":
            raise ConnectionError("unavailable")
        if failure == "malformed":
            return {"unexpected": "object"}
        if failure == "missing_required":
            return [item for item in rows(source=client.clock.now) if item["symbol"] != "BTCUSDT"]
        if failure == "future_source":
            return rows(source=client.clock.now + timedelta(seconds=10))
        return rows(target=SLOT)

    result, (attempt, observations), client, clock = collect(monkeypatch, refresh)
    assert result.status == "success" and result.symbol_count == 3
    assert_original_normal_rows(attempt, observations)
    assert client.batches == 4  # Initial capture plus at most three refreshes.
    assert clock.sleeps == [1, 1, 1]


def test_required_expired_xau_fails_closed_after_bounded_refresh(monkeypatch):
    result, (attempt, observations), client, _ = collect(monkeypatch, xau_required=True)
    assert result.status == "failed" and result.failure_code == "INVALID_NEXT_FUNDING_TIME"
    assert observations == () and attempt.status == "failed"
    assert attempt.request_started_at == SLOT and attempt.received_at == INITIAL_RECEIVED
    assert client.batches == 4


def test_refresh_received_outside_slot_never_replaces_original_evidence(monkeypatch):
    def refresh(client):
        client.clock.now = SLOT + timedelta(seconds=121)
        return rows(source=client.clock.now)

    result, (attempt, observations), client, _ = collect(monkeypatch, refresh)
    assert result.symbol_count == 3
    assert_original_normal_rows(attempt, observations)
    assert client.batches == 2


def test_no_refresh_starts_if_retry_sleep_exhausts_slot(monkeypatch):
    clock = Clock()

    async def oversleep(seconds):
        clock.now = SLOT + timedelta(seconds=121)

    clock.sleep = oversleep
    result, (attempt, observations), client, _ = collect(monkeypatch, clock=clock)
    assert result.symbol_count == 3
    assert_original_normal_rows(attempt, observations)
    assert client.batches == 1


def test_future_xau_needs_no_refresh(monkeypatch):
    result, (_, observations), client, clock = collect(monkeypatch, first=rows())
    assert result.symbol_count == 4
    assert client.batches == 1 and len(client.calls) == 2
    assert clock.sleeps == []
    assert all(item.received_at == INITIAL_RECEIVED for item in observations)


def test_initial_invalid_required_symbol_still_fails_without_refresh(monkeypatch):
    first = [item for item in rows(target=SLOT) if item["symbol"] != "BTCUSDT"]
    result, (_, observations), client, clock = collect(monkeypatch, first=first)
    assert result.failure_code == "MISSING_SYMBOL" and observations == ()
    assert client.batches == 1 and clock.sleeps == []


def test_refresh_timeout_cancels_request_and_preserves_original_evidence(monkeypatch):
    import ai_tuner.funding_schedule.collector as collector_module

    class HangingClient(Client):
        def __init__(self, clock):
            super().__init__(clock)
            self.cancelled = 0

        async def _request(self, method, endpoint, params=None, signed=True):
            if self.batches and endpoint.endswith("premiumIndex"):
                self.batches += 1
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled += 1
            return await super()._request(method, endpoint, params=params, signed=signed)

    clock = Clock()
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(collector_module, "_BOUNDARY_REFRESH_TIMEOUT_SECONDS", 0.001)
    repo, client = Repository(), HangingClient(clock)
    result = asyncio.run(FundingScheduleCollector(repo, client, clock=clock).collect_once(SLOT, uuid4()))
    assert result.symbol_count == 3
    assert client.batches == 4 and client.cancelled == 3
    assert_original_normal_rows(*repo.attempts[0])


def test_invalid_refresh_funding_info_never_replaces_original_batch(monkeypatch):
    class InvalidInfoClient(Client):
        async def _request(self, method, endpoint, params=None, signed=True):
            response = await super()._request(method, endpoint, params=params, signed=signed)
            if self.batches > 1 and endpoint.endswith("fundingInfo"):
                return [{"symbol": "XAUUSDT", "fundingIntervalHours": "4"}]
            return response

    clock = Clock()
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    repo = Repository()
    client = InvalidInfoClient(clock, lambda client: rows(source=client.clock.now))
    result = asyncio.run(FundingScheduleCollector(repo, client, clock=clock).collect_once(SLOT, uuid4()))
    assert result.symbol_count == 3 and client.batches == 4
    assert_original_normal_rows(*repo.attempts[0])


def test_refresh_gets_do_not_continue_after_slot_deadline(monkeypatch):
    def refresh(client):
        client.clock.now = SLOT + timedelta(seconds=121)
        return rows(source=client.clock.now)

    result, (attempt, observations), client, _ = collect(monkeypatch, refresh)
    assert result.symbol_count == 3
    assert [endpoint for _, endpoint, _, _ in client.calls] == [
        "/fapi/v1/premiumIndex", "/fapi/v1/fundingInfo", "/fapi/v1/premiumIndex"]
    assert_original_normal_rows(attempt, observations)


@pytest.mark.parametrize("source_offset_ms", [0, 100])
@pytest.mark.parametrize("xau_required", [False, True])
def test_expired_target_at_or_before_valid_source_is_refreshed(monkeypatch, source_offset_ms, xau_required):
    first = rows(target=SLOT)
    next(item for item in first if item["symbol"] == "XAUUSDT")["time"] = ms(
        SLOT + timedelta(milliseconds=source_offset_ms))
    frozen = copy.deepcopy(first)
    result, (attempt, observations), client, clock = collect(
        monkeypatch, lambda client: rows(source=client.clock.now),
        first=first, xau_required=xau_required)
    assert result.status == "success" and result.symbol_count == 4
    assert client.batches == 2 and clock.sleeps == [1]
    assert attempt.request_started_at == INITIAL_RECEIVED + timedelta(seconds=1)
    xau = next(item for item in observations if item.symbol == "XAUUSDT")
    assert xau.next_funding_time > xau.received_at
    assert first == frozen
    assert attempt.payload_hash == digest(attempt.raw_payload)
    assert verify_funding_schedule([verifier_row(attempt, xau)], [], start_at=SLOT,
                                  end_at=SLOT + timedelta(minutes=5), symbol="XAUUSDT")["invalid_observations"] == 0


@pytest.mark.parametrize("source_offset_ms", [0, 100])
def test_unrefreshable_target_at_or_before_source_preserves_other_symbols(monkeypatch, source_offset_ms):
    first = rows(target=SLOT)
    next(item for item in first if item["symbol"] == "XAUUSDT")["time"] = ms(
        SLOT + timedelta(milliseconds=source_offset_ms))
    result, (attempt, observations), client, _ = collect(monkeypatch, lambda _: first, first=first)
    assert result.symbol_count == 3
    assert_original_normal_rows(attempt, observations)
    assert client.batches == 4


@pytest.mark.parametrize("patch", [
    {"time": ms(SLOT + timedelta(seconds=1))},
    {"time": ms(SLOT - timedelta(seconds=121))},
    {"time": "invalid"},
    {"nextFundingTime": "invalid"},
    {"nextFundingTime": 0},
])
def test_invalid_xau_source_or_target_never_triggers_boundary_refresh(monkeypatch, patch):
    first = rows(target=SLOT)
    next(item for item in first if item["symbol"] == "XAUUSDT").update(patch)
    result, (attempt, observations), client, clock = collect(monkeypatch, first=first)
    assert result.symbol_count == 3
    assert_original_normal_rows(attempt, observations)
    assert client.batches == 1 and clock.sleeps == []


def test_xau_normalization_error_cannot_mask_later_required_symbol_failure(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    repo = Repository()
    repo.universe.add("ZECUSDT")
    first = rows(target=SLOT)
    next(item for item in first if item["symbol"] == "XAUUSDT")["time"] = ms(SLOT)
    client = Client(clock, lambda client: rows(source=client.clock.now), first)
    result = asyncio.run(FundingScheduleCollector(repo, client, clock=clock).collect_once(SLOT, uuid4()))
    assert result.status == "failed" and result.failure_code == "MISSING_SYMBOL"
    assert repo.attempts[0][1] == ()
    assert client.batches == 1 and clock.sleeps == []


@pytest.mark.parametrize("symbol", ["SOLUSDT", "ZECUSDT"])
@pytest.mark.parametrize("source_offset_ms", [-2000, 0, 100])
@pytest.mark.parametrize("xau_expired", [False, True])
def test_expired_required_symbol_refreshes_whole_batch(monkeypatch, symbol, source_offset_ms, xau_expired):
    clock = Clock()
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    repo = Repository()
    repo.universe.add(symbol)
    first = rows(target=SLOT if xau_expired else None)
    if symbol == "ZECUSDT":
        first.append({"symbol": symbol, "time": ms(SLOT), "nextFundingTime": ms(SLOT)})
    next(item for item in first if item["symbol"] == symbol).update(
        time=ms(SLOT + timedelta(milliseconds=source_offset_ms)), nextFundingTime=ms(SLOT))
    frozen = copy.deepcopy(first)

    def refresh(client):
        return [{"symbol": item, "time": ms(client.clock.now),
                 "nextFundingTime": ms(SLOT + timedelta(hours=4))}
                for item in sorted(repo.universe | {"XAUUSDT"})]

    client = Client(clock, refresh, first)
    result = asyncio.run(FundingScheduleCollector(repo, client, clock=clock).collect_once(SLOT, uuid4()))
    assert result.status == "success"
    attempt, observations = repo.attempts[0]
    assert {item.symbol for item in observations} == repo.universe | {"XAUUSDT"}
    assert client.batches == 2 and clock.sleeps == [1]
    assert all(item.source_time == attempt.request_started_at for item in observations)
    assert all(item.next_funding_time > item.received_at for item in observations)
    assert attempt.payload_hash == digest(attempt.raw_payload)
    assert first == frozen


def test_required_boundary_failure_retains_initial_timing_and_symbol(monkeypatch):
    first = rows()
    next(item for item in first if item["symbol"] == "SOLUSDT").update(
        time=ms(SLOT), nextFundingTime=ms(SLOT))
    result, (attempt, observations), client, clock = collect(monkeypatch, lambda _: first, first=first)
    assert result.failure_code == "INVALID_NEXT_FUNDING_TIME" and observations == ()
    assert client.batches == 4 and clock.sleeps == [1, 1, 1]
    assert attempt.request_started_at == SLOT and attempt.received_at == INITIAL_RECEIVED
    assert attempt.raw_payload == {"failure_code": "INVALID_NEXT_FUNDING_TIME",
                                   "http_attempted": True, "symbol": "SOLUSDT"}
    assert attempt.payload_hash == digest(attempt.raw_payload)


def test_required_boundary_cannot_mask_other_required_failure(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    repo = Repository()
    repo.universe.add("ZECUSDT")
    first = rows()
    next(item for item in first if item["symbol"] == "SOLUSDT").update(
        time=ms(SLOT), nextFundingTime=ms(SLOT))
    client = Client(clock, first=first)
    result = asyncio.run(FundingScheduleCollector(repo, client, clock=clock).collect_once(SLOT, uuid4()))
    assert result.failure_code == "MISSING_SYMBOL"
    assert repo.attempts[0][0].raw_payload["symbol"] == "ZECUSDT"
    assert client.batches == 1 and clock.sleeps == []


def test_required_refresh_accepts_valid_required_batch_when_optional_xau_unavailable(monkeypatch):
    first = rows()
    next(item for item in first if item["symbol"] == "SOLUSDT").update(
        time=ms(SLOT), nextFundingTime=ms(SLOT))

    def refresh(client):
        return [item for item in rows(source=client.clock.now) if item["symbol"] != "XAUUSDT"]

    result, (attempt, observations), client, clock = collect(monkeypatch, refresh, first=first)
    assert result.status == "success" and result.symbol_count == 3
    assert {item.symbol for item in observations} == SYMBOLS
    assert all(item.source_time == attempt.request_started_at for item in observations)
    assert attempt.received_at > INITIAL_RECEIVED
    assert client.batches <= 4 and len(clock.sleeps) <= 3


@pytest.mark.parametrize("failure", ["missing_required", "future_source", "malformed", "network"])
def test_invalid_required_refresh_fails_closed_with_original_failure_evidence(monkeypatch, failure):
    first = rows()
    next(item for item in first if item["symbol"] == "SOLUSDT").update(
        time=ms(SLOT), nextFundingTime=ms(SLOT))

    def refresh(client):
        if failure == "network":
            raise ConnectionError("unavailable")
        if failure == "malformed":
            return {"unexpected": "object"}
        if failure == "future_source":
            return rows(source=client.clock.now + timedelta(seconds=10))
        return [item for item in rows(source=client.clock.now) if item["symbol"] != "BTCUSDT"]

    result, (attempt, observations), client, clock = collect(monkeypatch, refresh, first=first)
    assert result.status == "failed" and observations == ()
    assert attempt.request_started_at == SLOT and attempt.received_at == INITIAL_RECEIVED
    assert attempt.raw_payload["symbol"] == "SOLUSDT"
    assert client.batches == 4 and clock.sleeps == [1, 1, 1]


def test_multiple_expired_required_symbols_share_one_refresh_budget(monkeypatch):
    first = rows(target=SLOT)
    for item in first:
        item.update(time=ms(SLOT), nextFundingTime=ms(SLOT))
    result, (attempt, observations), client, clock = collect(monkeypatch, lambda _: first, first=first)
    assert result.status == "failed" and observations == ()
    assert attempt.raw_payload["symbol"] == "BTCUSDT"
    assert client.batches == 4 and clock.sleeps == [1, 1, 1]
