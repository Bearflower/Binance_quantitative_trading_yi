"""Prospective-only public funding schedule capture; never a replay job."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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


def _failure_hash(code: str) -> str:
    return hashlib.sha256(
        json.dumps({"failure_code": code}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class FundingScheduleCollector:
    def __init__(self, repository, binance, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.repository = repository
        self.binance = binance
        self.clock = clock

    async def collect_once(self, scheduled_at: datetime, attempt_id: UUID) -> CollectionResult:
        """Capture one current slot and preserve failures as immutable batches.

        ``scheduled_at`` identifies the intended slot only; no past slot can
        be reconstructed by this method. Request and receipt timestamps come
        from the injected real clock, not from the slot.
        """
        request_started_at = self.clock()
        received_at = request_started_at
        try:
            universe = await self.repository.latest_universe("hrs", scheduled_at)
        except MissingHistoricalUniverse:
            received_at = self.clock()
            failure_code = "MISSING_UNIVERSE"
        except Exception:
            received_at = self.clock()
            failure_code = "UNIVERSE_READ_FAILED"
        else:
            try:
                required_symbols = _REQUIRED_MTPCS_SYMBOLS | set(universe)
                premium_rows = await self.binance._request("GET", "/fapi/v1/premiumIndex", signed=False)
                funding_info_rows = await self.binance._request("GET", "/fapi/v1/fundingInfo", signed=False)
                received_at = self.clock()
            except Exception:
                received_at = self.clock()
                failure_code = "FETCH_FAILED"
            else:
                try:
                    payload_hash, observations = normalize_observations(
                        scheduled_at=scheduled_at,
                        received_at=received_at,
                        required_symbols=required_symbols,
                        premium_rows=premium_rows,
                        funding_info_rows=funding_info_rows,
                    )
                except FundingScheduleDataError as error:
                    failure_code = error.code
                else:
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
        attempt = FundingScheduleAttempt(
            attempt_id=attempt_id,
            scheduled_at=scheduled_at,
            request_started_at=request_started_at,
            received_at=received_at,
            status="failed",
            failure_code=failure_code,
            payload_hash=_failure_hash(failure_code),
            raw_payload={"failure_code": failure_code},
        )
        await self.repository.store_attempt(attempt, ())
        return CollectionResult("failed", failure_code, 0)
