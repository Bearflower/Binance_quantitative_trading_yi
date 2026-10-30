"""Append-only storage for prospective exchange funding-time observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Mapping, Optional, Tuple
from uuid import UUID

from .models import FundingScheduleObservation


class FundingScheduleConflict(ValueError):
    """An attempt id already names different immutable evidence."""


class MissingHistoricalUniverse(ValueError):
    """No HRS scan run existed at the scheduled collection instant."""


@dataclass(frozen=True)
class FundingScheduleAttempt:
    attempt_id: UUID
    scheduled_at: datetime
    request_started_at: datetime
    received_at: datetime
    status: str
    failure_code: Optional[str]
    payload_hash: str
    raw_payload: Any


_RUN_COLUMNS = (
    "attempt_id", "scheduled_at", "request_started_at", "received_at",
    "status", "failure_code", "payload_hash", "raw_payload",
)
_OBS_COLUMNS = (
    "attempt_id", "symbol", "source_time", "received_at", "next_funding_time",
    "funding_interval_hours", "payload_hash", "raw_payload",
)
_RUN_SELECT = """
SELECT attempt_id, scheduled_at, request_started_at, received_at,
       status, failure_code, payload_hash, raw_payload
FROM public.ai_tuner_funding_schedule_runs WHERE attempt_id = $1::uuid
"""
_OBS_SELECT = """
SELECT attempt_id, symbol, source_time, received_at, next_funding_time,
       funding_interval_hours, payload_hash, raw_payload
FROM public.ai_tuner_funding_schedule_observations
WHERE attempt_id = $1::uuid ORDER BY symbol
"""
_RUN_INSERT = """
INSERT INTO public.ai_tuner_funding_schedule_runs (
    attempt_id, scheduled_at, request_started_at, received_at,
    status, failure_code, payload_hash, raw_payload
) VALUES ($1::uuid, $2::timestamptz, $3::timestamptz, $4::timestamptz,
          $5::varchar(16), $6::varchar(64), $7::char(64), $8::jsonb)
"""
_OBS_INSERT = """
INSERT INTO public.ai_tuner_funding_schedule_observations (
    attempt_id, symbol, source_time, received_at, next_funding_time,
    funding_interval_hours, payload_hash, raw_payload
) VALUES ($1::uuid, $2::varchar(32), $3::timestamptz, $4::timestamptz,
          $5::timestamptz, $6::integer, $7::char(64), $8::jsonb)
"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _run_values(attempt: FundingScheduleAttempt) -> Tuple[Any, ...]:
    return (
        attempt.attempt_id, attempt.scheduled_at, attempt.request_started_at,
        attempt.received_at, attempt.status, attempt.failure_code,
        attempt.payload_hash, attempt.raw_payload,
    )


def _observation_values(attempt_id: UUID, observation: FundingScheduleObservation) -> Tuple[Any, ...]:
    return (
        attempt_id, observation.symbol, observation.source_time, observation.received_at,
        observation.next_funding_time, observation.funding_interval_hours,
        observation.payload_hash, observation.raw_payload,
    )


def _stored_values(row: Mapping[str, Any], columns: Tuple[str, ...]) -> Tuple[Any, ...]:
    values = []
    for column in columns:
        value = row[column]
        # asyncpg's default JSONB codec returns JSON text; FakeDB and custom
        # codecs may return an already-decoded mapping.
        if column == "raw_payload" and isinstance(value, str):
            value = json.loads(value)
        values.append(value)
    return tuple(values)


class FundingScheduleRepository:
    def __init__(self, db) -> None:
        self.db = db

    async def latest_universe(self, strategy_id: str, scheduled_at: datetime) -> Tuple[str, ...]:
        """Distinguish a proven empty scan from missing historical membership."""
        if not self.db.pool:
            await self.db.connect()
        async with self.db.pool.acquire() as conn:
            run = await conn.fetchrow(
                """SELECT observed_at, symbol_count
                   FROM public.ai_tuner_universe_scan_runs
                   WHERE strategy_id = $1::varchar(64)
                     AND observed_at <= $2::timestamptz
                     AND created_at <= $2::timestamptz
                   ORDER BY observed_at DESC LIMIT 1""",
                strategy_id, scheduled_at,
            )
            observed_at = run["observed_at"] if run else None
            if observed_at is None:
                raise MissingHistoricalUniverse(f"no {strategy_id} universe scan as of scheduled_at")
            rows = await conn.fetch(
                """SELECT symbol FROM public.ai_tuner_universe_snapshots
                   WHERE strategy_id = $1::varchar(64)
                     AND observed_at = $2::timestamptz
                     AND created_at <= $3::timestamptz
                   ORDER BY symbol""",
                strategy_id, observed_at, scheduled_at,
            )
            if len(rows) != run["symbol_count"]:
                raise MissingHistoricalUniverse(f"incomplete {strategy_id} universe scan at {observed_at}")
            return tuple(row["symbol"] for row in rows)

    async def store_attempt(
        self,
        attempt: FundingScheduleAttempt,
        observations: Tuple[FundingScheduleObservation, ...],
    ) -> None:
        """Serialize same-id retries and compare all fields before any insertion.

        The transaction-scoped advisory lock is keyed by attempt UUID. A hash
        collision only causes extra serialization, never evidence coalescing.
        No UPDATE or DELETE privilege is needed by this writer.
        """
        if attempt.status not in ("success", "failed"):
            raise ValueError("invalid attempt status")
        if attempt.status == "failed" and observations:
            raise ValueError("failed attempt cannot have observations")
        if attempt.status == "success" and (not observations or attempt.failure_code is not None):
            raise ValueError("success requires observations and no failure code")
        if attempt.status == "failed" and not attempt.failure_code:
            raise ValueError("failed attempt requires failure code")
        expected_run = _run_values(attempt)
        expected_observations = tuple(
            _observation_values(attempt.attempt_id, row) for row in sorted(observations, key=lambda row: row.symbol)
        )
        if len({row.symbol for row in observations}) != len(observations):
            raise ValueError("duplicate observation symbol")
        if _digest(attempt.raw_payload) != attempt.payload_hash:
            raise ValueError("attempt payload hash differs from retained payload")
        for row in observations:
            if _digest(row.raw_payload) != row.payload_hash:
                raise ValueError(f"{row.symbol} payload hash differs from retained payload")
        if attempt.status == "success" and attempt.raw_payload != [row.raw_payload for row in sorted(observations, key=lambda row: row.symbol)]:
            raise ValueError("run payload must match all observations")
        if not self.db.pool:
            await self.db.connect()
        async with self.db.pool.acquire() as conn:
            async with conn.transaction(isolation="read_committed"):
                # READ COMMITTED observes the first retry's committed rows after
                # this lock is granted, so concurrent same-id writes cannot race.
                await conn.fetchrow(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0)) AS lock",
                    str(attempt.attempt_id),
                )
                existing = await conn.fetchrow(_RUN_SELECT, attempt.attempt_id)
                if existing is not None:
                    stored_observations = await conn.fetch(_OBS_SELECT, attempt.attempt_id)
                    if (_stored_values(existing, _RUN_COLUMNS) != expected_run or
                            tuple(_stored_values(row, _OBS_COLUMNS) for row in stored_observations) != expected_observations):
                        raise FundingScheduleConflict(f"attempt {attempt.attempt_id} differs from stored evidence")
                    return
                await conn.execute(*(_RUN_INSERT, *expected_run[:-1], _json(expected_run[-1])))
                for values in expected_observations:
                    await conn.execute(*(_OBS_INSERT, *values[:-1], _json(values[-1])))
