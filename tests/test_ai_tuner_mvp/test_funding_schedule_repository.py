"""One immutable attempt transaction, including concurrent retries."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from ai_tuner.funding_schedule.models import FundingScheduleObservation
from ai_tuner.funding_schedule.repository import (
    FundingScheduleAttempt,
    FundingScheduleConflict,
    FundingScheduleRepository,
    MissingHistoricalUniverse,
)


UTC = timezone.utc
SLOT = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)


class FakeConnection:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def transaction(self, *, isolation=None):
        self.db.isolation = isolation
        async with self.db.lock:
            yield

    async def execute(self, sql, *args):
        self.db.sql.append(sql)
        if "INSERT INTO public.ai_tuner_funding_schedule_runs" in sql:
            attempt_id = args[0]
            if attempt_id in self.db.runs:
                raise AssertionError("duplicate insert before comparison")
            self.db.runs[attempt_id] = args
        elif "INSERT INTO public.ai_tuner_funding_schedule_observations" in sql:
            self.db.observations[(args[0], args[1])] = args
        return "OK"

    async def fetchrow(self, sql, *args):
        self.db.sql.append(sql)
        if "MAX(scheduled_at)" in sql:
            return {"scheduled_at": self.db.latest_run_slot}
        if "pg_advisory_xact_lock" in sql:
            return {"lock": 1}
        if "ai_tuner_universe_scan_runs" in sql:
            if (self.db.universe_run is not None and "created_at <= $2" in sql
                    and self.db.universe_run["created_at"] > args[1]):
                return None
            return self.db.universe_run
        stored = self.db.runs.get(args[0])
        if stored is None:
            return None
        return dict(zip(("attempt_id", "scheduled_at", "request_started_at", "received_at", "status", "failure_code", "payload_hash", "raw_payload"), stored))

    async def fetch(self, sql, *args):
        self.db.sql.append(sql)
        if "SELECT DISTINCT scheduled_at" in sql:
            return [{"scheduled_at": slot} for slot in self.db.persisted_slots
                    if args[0] <= slot <= args[1]]
        if "bool_or(status='success')" in sql:
            return self.db.failed_slot_rows
        if "ai_tuner_universe_snapshots" in sql:
            if "created_at <= $3" in sql and self.db.universe_snapshot_created_at > args[2]:
                return []
            return [{"symbol": s} for s in self.db.universe_symbols]
        return [dict(zip(("attempt_id", "symbol", "source_time", "received_at", "next_funding_time", "funding_interval_hours", "payload_hash", "raw_payload"), values)) for (aid, _), values in self.db.observations.items() if aid == args[0]]


class FakePool:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def acquire(self):
        yield FakeConnection(self.db)


class FakeDB:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.pool = FakePool(self)
        self.runs = {}
        self.observations = {}
        self.sql = []
        self.isolation = None
        self.universe_run = {"observed_at": SLOT - timedelta(minutes=1), "symbol_count": 1, "created_at": SLOT - timedelta(minutes=1)}
        self.universe_symbols = ["SOLUSDT"]
        self.universe_snapshot_created_at = SLOT - timedelta(minutes=1)
        self.failed_slot_rows = []
        self.persisted_slots = set()
        self.latest_run_slot = None


def fixture():
    aid = uuid4()
    attempt = FundingScheduleAttempt(
        attempt_id=aid, scheduled_at=SLOT,
        request_started_at=SLOT + timedelta(seconds=2),
        received_at=SLOT + timedelta(seconds=3), status="success",
        failure_code=None, payload_hash="a" * 64,
        raw_payload=[],
    )
    obs = FundingScheduleObservation(
        symbol="BTCUSDT", source_time=SLOT + timedelta(seconds=2),
        received_at=SLOT + timedelta(seconds=3),
        next_funding_time=SLOT + timedelta(hours=4),
        funding_interval_hours=4, payload_hash="b" * 64,
    )
    obs = FundingScheduleObservation(**{**obs.__dict__, "payload_hash": digest(obs.raw_payload)})
    attempt = FundingScheduleAttempt(**{**attempt.__dict__, "raw_payload": [obs.raw_payload], "payload_hash": digest([obs.raw_payload])})
    return attempt, (obs,)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_store_attempt_and_observations_is_one_transaction_and_idempotent():
    async def exercise():
        db = FakeDB()
        repo = FundingScheduleRepository(db)
        attempt, observations = fixture()
        await repo.store_attempt(attempt, observations)
        await repo.store_attempt(attempt, observations)
        assert len(db.runs) == 1 and len(db.observations) == 1
        assert db.isolation == "read_committed"
        assert any("pg_advisory_xact_lock" in sql for sql in db.sql)
        assert not any("UPDATE " in sql or "DELETE " in sql for sql in db.sql)
    asyncio.run(exercise())


def test_same_attempt_id_with_changed_rows_or_status_is_rejected():
    async def exercise():
        db = FakeDB()
        repo = FundingScheduleRepository(db)
        attempt, observations = fixture()
        await repo.store_attempt(attempt, observations)
        changed = FundingScheduleObservation(**{**observations[0].__dict__, "funding_interval_hours": 8})
        changed = FundingScheduleObservation(**{**changed.__dict__, "payload_hash": digest(changed.raw_payload)})
        changed_attempt = FundingScheduleAttempt(**{**attempt.__dict__, "raw_payload": [changed.raw_payload], "payload_hash": digest([changed.raw_payload])})
        with pytest.raises(FundingScheduleConflict):
            await repo.store_attempt(changed_attempt, (changed,))
        failed = FundingScheduleAttempt(**{**attempt.__dict__, "status": "failed", "failure_code": "FETCH_FAILED", "raw_payload": {"failure_code": "FETCH_FAILED"}, "payload_hash": digest({"failure_code": "FETCH_FAILED"})})
        with pytest.raises(FundingScheduleConflict):
            await repo.store_attempt(failed, ())
        assert len(db.observations) == 1
    asyncio.run(exercise())


def test_rejects_hash_that_does_not_match_retained_run_or_observation_payload():
    async def exercise():
        db = FakeDB()
        repo = FundingScheduleRepository(db)
        attempt, observations = fixture()
        bad_run = FundingScheduleAttempt(**{**attempt.__dict__, "payload_hash": "a" * 64})
        with pytest.raises(ValueError, match="payload hash"):
            await repo.store_attempt(bad_run, observations)
        bad_obs = FundingScheduleObservation(**{**observations[0].__dict__, "payload_hash": "b" * 64})
        with pytest.raises(ValueError, match="payload hash"):
            await repo.store_attempt(attempt, (bad_obs,))
        assert db.runs == {}
    asyncio.run(exercise())


def test_rejects_success_run_whose_selected_payload_omits_an_observation():
    async def exercise():
        repo = FundingScheduleRepository(FakeDB())
        attempt, observations = fixture()
        changed = FundingScheduleAttempt(**{**attempt.__dict__, "raw_payload": [], "payload_hash": digest([])})
        with pytest.raises(ValueError, match="observations"):
            await repo.store_attempt(changed, observations)
    asyncio.run(exercise())


def test_concurrent_retry_cannot_bypass_immutable_compare():
    async def exercise():
        db = FakeDB()
        repo = FundingScheduleRepository(db)
        attempt, observations = fixture()
        await asyncio.gather(repo.store_attempt(attempt, observations), repo.store_attempt(attempt, observations))
        assert len(db.runs) == 1 and len(db.observations) == 1
    asyncio.run(exercise())


def test_missing_universe_scan_is_different_from_proven_empty_scan():
    async def exercise():
        db = FakeDB()
        repo = FundingScheduleRepository(db)
        db.universe_run = None
        with pytest.raises(MissingHistoricalUniverse):
            await repo.latest_universe("hrs", SLOT)
        db.universe_run = {"observed_at": SLOT, "symbol_count": 0, "created_at": SLOT}
        db.universe_symbols = []
        assert await repo.latest_universe("hrs", SLOT) == ()
    asyncio.run(exercise())


def test_incomplete_historical_universe_snapshot_blocks_collection():
    async def exercise():
        db = FakeDB()
        db.universe_run = {"observed_at": SLOT, "symbol_count": 2, "created_at": SLOT}
        repo = FundingScheduleRepository(db)
        with pytest.raises(MissingHistoricalUniverse):
            await repo.latest_universe("hrs", SLOT)
    asyncio.run(exercise())


def test_scan_created_after_slot_cannot_supply_historical_universe():
    async def exercise():
        db = FakeDB()
        db.universe_run = {"observed_at": SLOT - timedelta(minutes=1), "symbol_count": 1, "created_at": SLOT + timedelta(seconds=1)}
        with pytest.raises(MissingHistoricalUniverse):
            await FundingScheduleRepository(db).latest_universe("hrs", SLOT)
    asyncio.run(exercise())


def test_snapshot_created_after_slot_cannot_supply_historical_membership():
    async def exercise():
        db = FakeDB()
        db.universe_snapshot_created_at = SLOT + timedelta(seconds=1)
        with pytest.raises(MissingHistoricalUniverse):
            await FundingScheduleRepository(db).latest_universe("hrs", SLOT)
    asyncio.run(exercise())


def test_persisted_streak_counts_only_contiguous_failed_slots():
    async def exercise():
        db = FakeDB()
        repo = FundingScheduleRepository(db)
        next_slot = SLOT + timedelta(minutes=5)
        db.failed_slot_rows = [
            {"scheduled_at": SLOT, "succeeded": False},
            {"scheduled_at": SLOT - timedelta(minutes=5), "succeeded": False},
        ]
        assert await repo.recent_failed_streak(next_slot, limit=3) == 2
        db.failed_slot_rows = [
            {"scheduled_at": SLOT, "succeeded": False},
            {"scheduled_at": SLOT - timedelta(minutes=10), "succeeded": False},
        ]
        assert await repo.recent_failed_streak(next_slot, limit=3) == 1
        db.failed_slot_rows = [
            {"scheduled_at": SLOT, "succeeded": True},
            {"scheduled_at": SLOT - timedelta(minutes=5), "succeeded": False},
        ]
        assert await repo.recent_failed_streak(next_slot, limit=3) == 0
        assert any("GROUP BY scheduled_at" in sql for sql in db.sql)
    asyncio.run(exercise())


def test_monitor_queries_only_persisted_run_slots_and_does_not_write():
    async def exercise():
        db = FakeDB()
        db.persisted_slots = {SLOT, SLOT + timedelta(minutes=10)}
        db.latest_run_slot = SLOT + timedelta(minutes=10)
        repo = FundingScheduleRepository(db)
        assert await repo.latest_run_slot() == SLOT + timedelta(minutes=10)
        assert await repo.slots_with_runs(SLOT, SLOT + timedelta(minutes=15)) == db.persisted_slots
        assert any("SELECT DISTINCT scheduled_at" in sql for sql in db.sql)
        assert not any("UPDATE " in sql or "DELETE " in sql for sql in db.sql)
    asyncio.run(exercise())
