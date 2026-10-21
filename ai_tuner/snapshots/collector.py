from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import MarketSnapshot


@dataclass(frozen=True)
class CollectionSummary:
    requested: int
    complete: int
    proxy: int
    missing: int
    failed: int


class SnapshotCollector:
    def __init__(self, repository, binance, market_cap, concurrency: int = 5) -> None:
        self.repository = repository
        self.binance = binance
        self.market_cap = market_cap
        self._semaphore = asyncio.Semaphore(max(1, concurrency))

    async def collect_once(self, as_of: datetime) -> CollectionSummary:
        universe = sorted(await self.repository.latest_universe("hrs", as_of))
        results = await asyncio.gather(
            *(self._collect_symbol(symbol, as_of) for symbol in universe),
            return_exceptions=True,
        )
        complete = proxy = missing = failed = 0
        for result in results:
            if isinstance(result, Exception):
                failed += 1
            elif result == "complete":
                complete += 1
            elif result == "proxy":
                proxy += 1
            else:
                missing += 1
        return CollectionSummary(len(universe), complete, proxy, missing, failed)

    async def _collect_symbol(self, symbol: str, as_of: datetime) -> str:
        async with self._semaphore:
            oi = float(await self.binance.get_open_interest(symbol))
            funding = float(await self.binance.get_funding_rate(symbol))
            ticker = await self.binance.get_ticker(symbol)
            price = float(ticker["lastPrice"])
            volume = float(ticker.get("quoteVolume", ticker.get("volume", 0)) or 0)
            oi_usd = oi * price
            cap = await self.market_cap.get_market_cap_with_provenance(
                symbol=symbol,
                oi_usd=oi_usd,
                _volume_24h_usd=volume,
            )
            market_cap_usd = float(cap.value) if cap.value else None
            ratio = oi_usd / market_cap_usd if market_cap_usd and oi_usd > 0 else None
            bucket_start = as_of.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
            raw_payload = {
                "open_interest_contracts": oi,
                "last_price": price,
                "funding_rate": funding,
                "market_cap_source": cap.source,
                "market_cap_missing_reason": cap.missing_reason,
            }
            payload_hash = hashlib.sha256(
                json.dumps(raw_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            source_ts = datetime.fromtimestamp(float(cap.as_of), tz=timezone.utc)
            snapshot = MarketSnapshot(
                strategy_id="hrs",
                symbol=symbol,
                observed_at=as_of,
                bucket_start=bucket_start,
                open_interest_contracts=oi,
                open_interest_usd=oi_usd,
                funding_rate=funding,
                market_cap_usd=market_cap_usd,
                oi_market_cap_ratio=ratio,
                sentiment_score=funding * 3 * 365 * 100,
                sentiment_formula_version="funding_annual_pct_v1",
                oi_source="binance",
                funding_source="binance",
                market_cap_source=cap.source,
                source_observed_at=source_ts,
                payload_hash=payload_hash,
                raw_payload=raw_payload,
            )
            await self.repository.store_market_snapshot(snapshot)
            if snapshot.is_complete:
                return "complete"
            if cap.source == "oi_proxy":
                return "proxy"
            return "missing"

    async def backfill_supported(self, start: datetime, end: datetime) -> CollectionSummary:
        requested = missing = failed = 0
        bucket = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        while bucket < end:
            bucket_end = min(bucket + timedelta(hours=1), end)
            universe = sorted(await self.repository.latest_universe("hrs", bucket_end))
            for symbol in universe:
                requested += 1
                try:
                    start_ms = int(bucket.timestamp() * 1000)
                    end_ms = int(bucket_end.timestamp() * 1000) - 1
                    oi_rows, funding_rows = await asyncio.gather(
                        self.binance.get_open_interest_hist(
                            symbol, period="1h", start_time=start_ms, end_time=end_ms, limit=1
                        ),
                        self.binance.get_funding_rate_history(
                            symbol, start_time=start_ms, end_time=end_ms, limit=1
                        ),
                    )
                    if not oi_rows or not funding_rows:
                        failed += 1
                        continue
                    oi_row = oi_rows[-1]
                    funding_row = funding_rows[-1]
                    oi = float(oi_row.get("sumOpenInterest", 0) or 0)
                    oi_usd = float(oi_row.get("sumOpenInterestValue", 0) or 0)
                    funding = float(funding_row.get("fundingRate", 0) or 0)
                    source_ms = max(
                        int(oi_row.get("timestamp", start_ms)),
                        int(funding_row.get("fundingTime", start_ms)),
                    )
                    raw_payload = {"open_interest": oi_row, "funding": funding_row}
                    snapshot = MarketSnapshot(
                        strategy_id="hrs", symbol=symbol, observed_at=bucket_end,
                        bucket_start=bucket, open_interest_contracts=oi,
                        open_interest_usd=oi_usd or None, funding_rate=funding,
                        market_cap_usd=None, oi_market_cap_ratio=None,
                        sentiment_score=funding * 3 * 365 * 100,
                        sentiment_formula_version="funding_annual_pct_v1",
                        oi_source="binance_history", funding_source="binance_history",
                        market_cap_source="missing",
                        source_observed_at=datetime.fromtimestamp(source_ms / 1000, tz=timezone.utc),
                        payload_hash=hashlib.sha256(
                            json.dumps(raw_payload, sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest(), raw_payload=raw_payload,
                    )
                    await self.repository.store_market_snapshot(snapshot)
                    missing += 1
                except Exception:
                    failed += 1
            bucket = bucket_end
        return CollectionSummary(requested, 0, 0, missing, failed)
