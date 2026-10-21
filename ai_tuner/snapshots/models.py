from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple


_REAL_MARKET_CAP_SOURCES = frozenset({"coingecko", "cache_hit", "coingecko_cache"})


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
        if len(self.payload_hash) != 64:
            raise ValueError("payload_hash must be a 64-character SHA-256 hex digest")

    @property
    def bucket_end(self) -> datetime:
        return self.bucket_start + timedelta(hours=1)

    @property
    def key(self) -> Tuple[str, str, datetime]:
        return self.strategy_id, self.symbol, self.bucket_start

    @property
    def is_complete(self) -> bool:
        return bool(
            self.open_interest_usd is not None
            and self.open_interest_usd > 0
            and self.funding_rate is not None
            and self.market_cap_usd is not None
            and self.market_cap_usd > 0
            and self.oi_market_cap_ratio is not None
            and self.sentiment_score is not None
            and self.market_cap_source in _REAL_MARKET_CAP_SOURCES
            and self.source_observed_at <= self.bucket_end
        )

    @property
    def quality_rank(self) -> int:
        if self.is_complete:
            return 2
        if self.market_cap_source == "oi_proxy":
            return 1
        return 0
