"""Optional real-SQL check, isolated to an explicitly acknowledged local test DB.

Set AI_TUNER_FUNDING_TEST_DSN to a localhost database ending ``_test`` and
AI_TUNER_FUNDING_TEST_DB_ACK=disposable. The test creates tables inside an
outer transaction and rolls that transaction back, leaving no test rows or
tables. It never connects when either guard is absent. Never use a production
database or an SSH tunnel to production here.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest

from ai_tuner.funding_schedule.models import FundingScheduleObservation
from ai_tuner.funding_schedule.repository import (
    FundingScheduleAttempt, FundingScheduleConflict, FundingScheduleRepository,
)


DSN_ENV = "AI_TUNER_FUNDING_TEST_DSN"
ACK_ENV = "AI_TUNER_FUNDING_TEST_DB_ACK"


class _OneConnectionPool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class _Database:
    def __init__(self, connection):
        self.pool = _OneConnectionPool(connection)


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.skipif(not os.getenv(DSN_ENV) or os.getenv(ACK_ENV) != "disposable", reason="dedicated disposable local PostgreSQL not explicitly configured")
def test_actual_postgres_sql_is_atomic_idempotent_and_conflict_closed():
    parsed = urlparse(os.environ[DSN_ENV])
    if parsed.scheme not in ("postgresql", "postgres") or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.query or not parsed.path.endswith("_test"):
        pytest.fail("funding schedule SQL test requires localhost and a database name ending _test")

    async def exercise():
        conn = await asyncpg.connect(dsn=os.environ[DSN_ENV])
        outer = conn.transaction(isolation="read_committed")
        await outer.start()
        try:
            for ddl in (
                "CREATE TABLE public.ai_tuner_universe_scan_runs (strategy_id varchar(64), observed_at timestamptz, symbol_count int, created_at timestamptz)",
                "CREATE TABLE public.ai_tuner_universe_snapshots (strategy_id varchar(64), observed_at timestamptz, symbol varchar(32), created_at timestamptz)",
                "CREATE TABLE public.ai_tuner_funding_schedule_runs (attempt_id uuid PRIMARY KEY, scheduled_at timestamptz, request_started_at timestamptz, received_at timestamptz, status varchar(16), failure_code varchar(64), payload_hash char(64), raw_payload jsonb)",
                "CREATE TABLE public.ai_tuner_funding_schedule_observations (attempt_id uuid REFERENCES public.ai_tuner_funding_schedule_runs(attempt_id), symbol varchar(32), source_time timestamptz, received_at timestamptz, next_funding_time timestamptz, funding_interval_hours int, payload_hash char(64), raw_payload jsonb, PRIMARY KEY (attempt_id, symbol))",
            ):
                await conn.execute(ddl)
            slot = datetime(2026, 10, 30, 8, 0, tzinfo=timezone.utc)
            await conn.execute(
                "INSERT INTO public.ai_tuner_universe_scan_runs VALUES ($1, $2, $3, $4)",
                "hrs", slot - timedelta(minutes=1), 1, slot - timedelta(minutes=1),
            )
            await conn.execute(
                "INSERT INTO public.ai_tuner_universe_snapshots VALUES ($1, $2, $3, $4)",
                "hrs", slot - timedelta(minutes=1), "SOLUSDT", slot - timedelta(minutes=1),
            )
            repo = FundingScheduleRepository(_Database(conn))
            assert await repo.latest_universe("hrs", slot) == ("SOLUSDT",)
            observation = FundingScheduleObservation(
                symbol="BTCUSDT", source_time=slot + timedelta(seconds=1),
                received_at=slot + timedelta(seconds=2),
                next_funding_time=slot + timedelta(hours=4),
                funding_interval_hours=4, payload_hash="",
            )
            observation = FundingScheduleObservation(**{**observation.__dict__, "payload_hash": _digest(observation.raw_payload)})
            payload = [observation.raw_payload]
            attempt = FundingScheduleAttempt(
                attempt_id=uuid4(), scheduled_at=slot,
                request_started_at=slot + timedelta(seconds=1),
                received_at=slot + timedelta(seconds=2), status="success",
                failure_code=None, payload_hash=_digest(payload), raw_payload=payload,
            )
            await repo.store_attempt(attempt, (observation,))
            await repo.store_attempt(attempt, (observation,))
            assert await conn.fetchval("SELECT count(*) FROM public.ai_tuner_funding_schedule_runs") == 1
            assert await conn.fetchval("SELECT count(*) FROM public.ai_tuner_funding_schedule_observations") == 1
            changed = FundingScheduleAttempt(**{**attempt.__dict__, "received_at": slot + timedelta(seconds=3)})
            with pytest.raises(FundingScheduleConflict):
                await repo.store_attempt(changed, (observation,))
        finally:
            await outer.rollback()
            await conn.close()

    asyncio.run(exercise())
