"""Prospective-only public funding schedule capture; never a replay job."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import Callable, Optional
from uuid import UUID

from .models import FundingScheduleDataError, normalize_observations
from .repository import FundingScheduleAttempt, MissingHistoricalUniverse


logger = logging.getLogger(__name__)
_REQUIRED_MTPCS_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT"})
_XAU_SYMBOL = "XAUUSDT"
_BOUNDARY_REFRESH_ATTEMPTS = 3
_BOUNDARY_REFRESH_TIMEOUT_SECONDS = 10


def _normalize_current_batch(*, scheduled_at, received_at, required_symbols,
                             premium_rows, funding_info_rows):
    """Validate every required symbol before retrying settlement expirations.

    Expired symbols are skipped only to inspect remaining required evidence;
    no partial result is returned when any required forecast has expired.
    """
    pending = set(required_symbols)
    expired = None
    while pending:
        try:
            normalized = normalize_observations(
                scheduled_at=scheduled_at, received_at=received_at,
                required_symbols=pending, premium_rows=premium_rows,
                funding_info_rows=funding_info_rows,
            )
        except FundingScheduleDataError as error:
            if error.code != "INVALID_NEXT_FUNDING_TIME" or error.symbol not in pending:
                raise
            expired = expired or error
            pending.remove(error.symbol)
            continue
        for observation in normalized[1]:
            if observation.next_funding_time <= received_at:
                expired = expired or FundingScheduleDataError(
                    "INVALID_NEXT_FUNDING_TIME",
                    f"{observation.symbol} forecast expired before receipt",
                    symbol=observation.symbol,
                )
        if expired is None:
            return normalized
        break
    raise expired


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
            try:
                # Required symbols must all validate before optional XAU can
                # trigger a retry or a fallback to the original capture.
                _normalize_current_batch(
                    scheduled_at=scheduled_at,
                    received_at=received_at,
                    required_symbols=required_symbols,
                    premium_rows=premium_rows,
                    funding_info_rows=funding_info_rows,
                )
                payload_hash, observations = _normalize_current_batch(
                    scheduled_at=scheduled_at,
                    received_at=received_at,
                    required_symbols=required_symbols | {_XAU_SYMBOL},
                    premium_rows=premium_rows,
                    funding_info_rows=funding_info_rows,
                )
            except FundingScheduleDataError as error:
                if error.symbol != _XAU_SYMBOL and error.code != "INVALID_NEXT_FUNDING_TIME":
                    raise
                refreshed = None
                if error.code == "INVALID_NEXT_FUNDING_TIME":
                    refreshed = await self._refresh_batch(
                        scheduled_at, required_symbols,
                        required_refresh=error.symbol in required_symbols,
                    )
                if refreshed is not None:
                    request_started_at, received_at, payload_hash, observations = refreshed
                else:
                    if error.symbol in required_symbols:
                        raise
                    logger.warning("XAUUSDT funding schedule unavailable for %s: %s",
                                   scheduled_at.isoformat(), error.code)
                    payload_hash, observations = _normalize_current_batch(
                        scheduled_at=scheduled_at,
                        received_at=received_at,
                        required_symbols=required_symbols,
                        premium_rows=premium_rows,
                        funding_info_rows=funding_info_rows,
                    )
        except FundingScheduleDataError as error:
            return await self._failed(
                scheduled_at, attempt_id, request_started_at, received_at,
                error.code, http_attempted=True, symbol=error.symbol,
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

    async def _refresh_batch(self, scheduled_at: datetime, required_symbols, *, required_refresh: bool):
        """Replace a boundary batch only with a complete, current public capture.

        Failed refreshes never change the initial batch's evidence or timing.
        No historical request, guessed interval, or previously saved row can
        supply a replacement forecast. If required forecasts need replacement,
        a valid fresh required-only batch can survive optional XAU failures.
        """
        deadline = scheduled_at + timedelta(seconds=120)
        required_fallback = None

        async def fetch_batch():
            premiums = await self.binance._request("GET", "/fapi/v1/premiumIndex", signed=False)
            if not _current_slot(scheduled_at, self.clock()):
                raise asyncio.TimeoutError("funding refresh slot expired")
            intervals = await self.binance._request("GET", "/fapi/v1/fundingInfo", signed=False)
            return premiums, intervals

        for _ in range(_BOUNDARY_REFRESH_ATTEMPTS):
            now = self.clock()
            if not _current_slot(scheduled_at, now) or (deadline - now).total_seconds() <= 1:
                break
            await asyncio.sleep(1)
            started = self.clock()
            if not _current_slot(scheduled_at, started) or started >= deadline:
                break
            timeout = min(_BOUNDARY_REFRESH_TIMEOUT_SECONDS, (deadline - started).total_seconds())
            try:
                premiums, intervals = await asyncio.wait_for(fetch_batch(), timeout=timeout)
                received = self.clock()
                if not _current_slot(scheduled_at, received):
                    break
                payload_hash, observations = _normalize_current_batch(
                    scheduled_at=scheduled_at,
                    received_at=received,
                    required_symbols=required_symbols,
                    premium_rows=premiums,
                    funding_info_rows=intervals,
                )
                if required_refresh and _XAU_SYMBOL not in required_symbols:
                    required_fallback = started, received, payload_hash, observations
                payload_hash, observations = _normalize_current_batch(
                    scheduled_at=scheduled_at,
                    received_at=received,
                    required_symbols=required_symbols | {_XAU_SYMBOL},
                    premium_rows=premiums,
                    funding_info_rows=intervals,
                )
                return started, received, payload_hash, observations
            except Exception as error:
                logger.warning("Funding schedule refresh unavailable for %s: %s",
                               scheduled_at.isoformat(), type(error).__name__)
        return required_fallback

    async def _failed(
        self, scheduled_at: datetime, attempt_id: UUID,
        started_at: datetime, received_at: datetime,
        code: str, *, http_attempted: bool, symbol: Optional[str] = None,
    ) -> CollectionResult:
        raw_payload = {"failure_code": code, "http_attempted": http_attempted}
        if symbol is not None:
            raw_payload["symbol"] = symbol
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
