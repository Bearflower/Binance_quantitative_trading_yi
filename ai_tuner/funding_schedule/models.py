"""Pure validation for exchange funding-time forecasts observed at collection time.

``normalize_observations`` accepts the all-symbol Binance ``premiumIndex`` and
optional ``fundingInfo`` responses. It returns a canonical selected-field
batch SHA-256 and sorted frozen observation records. Only symbols in
``required_symbols`` participate in the hash; irrelevant response fields and
exchange response order cannot alter it. This is not a hash of HTTP bytes or
an exchange signature.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


class FundingScheduleDataError(ValueError):
    """A required prospective observation is missing, stale, or ambiguous."""

    def __init__(self, code: str, message: str, *, symbol: Optional[str] = None) -> None:
        self.code = code
        self.symbol = symbol
        super().__init__(message)


@dataclass(frozen=True)
class FundingScheduleObservation:
    symbol: str
    source_time: datetime
    received_at: datetime
    next_funding_time: datetime
    funding_interval_hours: Optional[int]
    payload_hash: str

    @property
    def raw_payload(self) -> Dict[str, Any]:
        """Return fresh selected fields so callers cannot mutate retained evidence."""
        return {
            "symbol": self.symbol,
            "time": int(self.source_time.timestamp() * 1000),
            "nextFundingTime": int(self.next_funding_time.timestamp() * 1000),
            "fundingIntervalHours": self.funding_interval_hours,
        }


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise FundingScheduleDataError("INVALID_ENVELOPE", f"{name} must be an aware UTC datetime")
    return value.astimezone(timezone.utc)


def _milliseconds(value: Any, name: str, symbol: str) -> Tuple[int, datetime]:
    if type(value) is not int or value <= 0:
        raise FundingScheduleDataError("INVALID_TIMESTAMP", f"{name} must be positive integer milliseconds", symbol=symbol)
    try:
        return value, datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError) as error:
        raise FundingScheduleDataError("INVALID_TIMESTAMP", f"{name} is outside datetime range", symbol=symbol) from error


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_symbols(symbols: Iterable[str]) -> Tuple[str, ...]:
    try:
        result = tuple(sorted(set(symbols)))
    except (TypeError, ValueError) as error:
        raise FundingScheduleDataError("INVALID_ENVELOPE", "required_symbols must be an iterable of symbols") from error
    if not result or any(not isinstance(item, str) or not item or item != item.upper() for item in result):
        raise FundingScheduleDataError("INVALID_ENVELOPE", "required_symbols must contain uppercase nonempty symbols")
    return result


def _required_rows(rows: Sequence[Mapping[str, Any]], symbols: Tuple[str, ...], name: str) -> Dict[str, Mapping[str, Any]]:
    if not isinstance(rows, (list, tuple)):
        raise FundingScheduleDataError("INVALID_RESPONSE", f"{name} must be a sequence of rows")
    selected: Dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise FundingScheduleDataError("INVALID_RESPONSE", f"{name} contains a non-object row")
        symbol = row.get("symbol")
        if symbol not in symbols:
            continue
        if symbol in selected:
            raise FundingScheduleDataError("DUPLICATE_SYMBOL", f"{name} has duplicate symbol {symbol}", symbol=symbol)
        selected[symbol] = row
    return selected


def normalize_observations(
    *,
    scheduled_at: datetime,
    received_at: datetime,
    required_symbols: Iterable[str],
    premium_rows: Sequence[Mapping[str, Any]],
    funding_info_rows: Sequence[Mapping[str, Any]],
) -> Tuple[str, Tuple[FundingScheduleObservation, ...]]:
    """Validate current forecasts without inventing missing historical intervals.

    Raises ``FundingScheduleDataError`` for a missing/duplicate required symbol,
    invalid exchange millisecond timestamp, future/stale source, or malformed
    optional interval. ``funding_info_rows`` may omit a required symbol; its
    interval then remains ``None``. The returned hash covers exactly the
    selected fields retained in ``raw_payload`` for each sorted observation.
    """
    scheduled = _utc(scheduled_at, "scheduled_at")
    received = _utc(received_at, "received_at")
    if received < scheduled:
        raise FundingScheduleDataError("INVALID_ENVELOPE", "received_at predates scheduled_at")
    symbols = _required_symbols(required_symbols)
    premiums = _required_rows(premium_rows, symbols, "premiumIndex")
    intervals = _required_rows(funding_info_rows, symbols, "fundingInfo")
    observations = []
    for symbol in symbols:
        if symbol not in premiums:
            raise FundingScheduleDataError("MISSING_SYMBOL", f"premiumIndex missing required symbol {symbol}", symbol=symbol)
        premium = premiums[symbol]
        source_ms, source_time = _milliseconds(premium.get("time"), f"{symbol}.time", symbol)
        next_ms, next_time = _milliseconds(premium.get("nextFundingTime"), f"{symbol}.nextFundingTime", symbol)
        if source_time > received:
            raise FundingScheduleDataError("FUTURE_SOURCE", f"{symbol} source time exceeds received_at", symbol=symbol)
        if received - source_time > timedelta(seconds=120):
            raise FundingScheduleDataError("STALE_SOURCE", f"{symbol} source time is stale", symbol=symbol)
        if next_time <= source_time:
            raise FundingScheduleDataError("INVALID_NEXT_FUNDING_TIME", f"{symbol} next funding time must follow source time", symbol=symbol)
        interval = None
        if symbol in intervals:
            interval = intervals[symbol].get("fundingIntervalHours")
            if type(interval) is not int or interval <= 0:
                raise FundingScheduleDataError("INVALID_INTERVAL", f"{symbol} invalid fundingIntervalHours", symbol=symbol)
        selected = {
            "symbol": symbol,
            "time": source_ms,
            "nextFundingTime": next_ms,
            "fundingIntervalHours": interval,
        }
        observations.append(FundingScheduleObservation(
            symbol=symbol,
            source_time=source_time,
            received_at=received,
            next_funding_time=next_time,
            funding_interval_hours=interval,
            payload_hash=_hash(selected),
        ))
    return _hash([observation.raw_payload for observation in observations]), tuple(observations)
