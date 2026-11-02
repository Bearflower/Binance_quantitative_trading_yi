"""Prospective-only public funding schedule capture; never a replay job."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Callable, Optional
from uuid import UUID

from .models import FundingScheduleDataError, normalize_observations
from .repository import FundingScheduleAttempt, MissingHistoricalUniverse


_REQUIRED_MTPCS_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT"})


@dataclass(frozen=True)
class CollectionResult:
    status: str
    failure_code: Optional[str]
    symbol_count: int


def _failure_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _current_slot(scheduled_at: datetime, now: datetime) -> bool:
    """Only collect a current UTC five-minute slot, never backfill a prior one."""
    if not isinstance(scheduled_at, datetime) or not isinstance(now, datetime):
        return False
    if (scheduled_at.tzinfo is None or scheduled_at.utcoffset() != timedelta(0)
            or now.tzinfo is None or now.utcoffset() != timedelta(0)):
        return False
    if scheduled_at.minute % 5 or scheduled_at.second or scheduled_at.microsecond:
        return False
    return scheduled_at <= now <= scheduled_at + timedelta(seconds=120)


class FundingScheduleCollector:
    def __init__(self, repository, binance, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.repository = repository
        self.binance = binance
        self.clock = clock

    async def collect_once(self, scheduled_at: datetime, attempt_id: UUID) -> CollectionResult:
        """Capture one current slot and preserve failures as immutable batches.

        ``scheduled_at`` identifies only a current, aligned UTC slot. For a
        request that reaches Binance, ``request_started_at`` is captured
        immediately before the first HTTP GET. For a pre-HTTP failure, the
        non-null DB column instead carries attempt-start time and the retained
        payload explicitly records ``http_attempted=false``.
        """
        attempt_started_at = self.clock()
        if not _current_slot(scheduled_at, attempt_started_at):
            return await self._failed(
                scheduled_at, attempt_id, attempt_started_at, self.clock(),
                "MISSED_SCHEDULE_SLOT", http_attempted=False,
            )
        try:
            universe = await self.repository.latest_universe("hrs", scheduled_at)
        except MissingHistoricalUniverse:
            return await self._failed(
                scheduled_at, attempt_id, attempt_started_at, self.clock(),
                "MISSING_UNIVERSE", http_attempted=False,
            )
        except Exception:
            return await self._failed(
                scheduled_at, attempt_id, attempt_started_at, self.clock(),
                "UNIVERSE_READ_FAILED", http_attempted=False,
            )
        request_started_at = self.clock()
        if not _current_slot(scheduled_at, request_started_at):
            return await self._failed(
                scheduled_at, attempt_id, attempt_started_at, request_started_at,
                "MISSED_SCHEDULE_SLOT", http_attempted=False,
            )
        try:
            required_symbols = _REQUIRED_MTPCS_SYMBOLS | set(universe)
            premium_rows = await self.binance._request("GET", "/fapi/v1/premiumIndex", signed=False)
            funding_info_rows = await self.binance._request("GET", "/fapi/v1/fundingInfo", signed=False)
        except Exception:
            return await self._failed(
                scheduled_at, attempt_id, request_started_at, self.clock(),
                "FETCH_FAILED", http_attempted=True,
            )
        received_at = self.clock()
        if not _current_slot(scheduled_at, received_at):
            return await self._failed(
                scheduled_at, attempt_id, request_started_at, received_at,
                "MISSED_SCHEDULE_SLOT", http_attempted=True,
            )
        try:
            payload_hash, observations = normalize_observations(
                scheduled_at=scheduled_at,
                received_at=received_at,
                required_symbols=required_symbols,
                premium_rows=premium_rows,
                funding_info_rows=funding_info_rows,
            )
        except FundingScheduleDataError as error:
            return await self._failed(
                scheduled_at, attempt_id, request_started_at, received_at,
                error.code, http_attempted=True,
            )
        attempt = FundingScheduleAttempt(
            attempt_id=attempt_id,
            scheduled_at=scheduled_at,
            request_started_at=request_started_at,
            received_at=received_at,
            status="success",
            failure_code=None,
            payload_hash=payload_hash,
            raw_payload=[item.raw_payload for item in observations],
        )
        await self.repository.store_attempt(attempt, observations)
        return CollectionResult("success", None, len(observations))

    async def _failed(
        self, scheduled_at: datetime, attempt_id: UUID,
        started_at: datetime, received_at: datetime,
        code: str, *, http_attempted: bool,
    ) -> CollectionResult:
        raw_payload = {"failure_code": code, "http_attempted": http_attempted}
        attempt = FundingScheduleAttempt(
            attempt_id=attempt_id,
            scheduled_at=scheduled_at,
            request_started_at=started_at,
            received_at=received_at,
            status="failed",
            failure_code=code,
            payload_hash=_failure_hash(raw_payload),
            raw_payload=raw_payload,
        )
        await self.repository.store_attempt(attempt, ())
        return CollectionResult("failed", code, 0)
