from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any, Dict, Optional, Tuple


_REAL_MARKET_CAP_SOURCES = frozenset({"coingecko", "cache_hit", "coingecko_cache"})
_SENTIMENT_FORMULA_VERSION = "funding_annual_pct_v1"
_REL_TOLERANCE = 1e-9


def _close(actual: Optional[float], expected: float) -> bool:
    if actual is None:
        return False
    return abs(actual - expected) <= _REL_TOLERANCE * max(1.0, abs(expected))


@dataclass(frozen=True)
class MarketSnapshot:
    strategy_id: str
    symbol: str
    observed_at: datetime
    bucket_start: datetime
    open_interest_contracts: Optional[float]
    open_interest_usd: Optional[float]
    funding_rate: Optional[float]
    market_cap_usd: Optional[float]
    oi_market_cap_ratio: Optional[float]
    sentiment_score: Optional[float]
    sentiment_formula_version: str
    oi_source: str
    funding_source: str
    market_cap_source: str
    source_observed_at: datetime
    payload_hash: str
    raw_payload: Dict[str, Any]

    def __post_init__(self) -> None:
        if self.source_observed_at > self.bucket_end:
            raise ValueError("future source timestamp exceeds bucket end")
        canonical_payload = json.dumps(
            self.raw_payload, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        expected_hash = hashlib.sha256(canonical_payload).hexdigest()
        if self.payload_hash != expected_hash:
            raise ValueError("payload_hash does not match canonical raw_payload SHA-256")

    @property
    def bucket_end(self) -> datetime:
        return self.bucket_start + timedelta(hours=1)

    @property
    def key(self) -> Tuple[str, str, datetime]:
        return self.strategy_id, self.symbol, self.bucket_start

    @property
    def is_complete(self) -> bool:
        if not (
            self.open_interest_usd is not None and self.open_interest_usd > 0
            and self.funding_rate is not None
            and self.market_cap_usd is not None and self.market_cap_usd > 0
            and self.market_cap_source in _REAL_MARKET_CAP_SOURCES
            and self.source_observed_at <= self.bucket_end
            and self.sentiment_formula_version == _SENTIMENT_FORMULA_VERSION
        ):
            return False
        return _close(
            self.oi_market_cap_ratio,
            self.open_interest_usd / self.market_cap_usd,
        ) and _close(self.sentiment_score, self.funding_rate * 3 * 365 * 100)

    @property
    def quality_rank(self) -> int:
        direct_source = self.market_cap_source == "coingecko"
        cached_source = self.market_cap_source in {"cache_hit", "coingecko_cache"}
        if self.is_complete and direct_source:
            return 5
        if self.is_complete and cached_source:
            return 4
        if direct_source:
            return 3
        if cached_source:
            return 2
        if self.market_cap_source == "oi_proxy":
            return 1
        return 0
