from __future__ import annotations

import json
from datetime import datetime
from typing import Tuple

from .models import MarketSnapshot


_STORE_MARKET_SNAPSHOT_SQL = """
INSERT INTO public.ai_tuner_market_snapshots (
    strategy_id, symbol, observed_at, bucket_start,
    open_interest_contracts, open_interest_usd, market_cap_usd, funding_rate,
    oi_market_cap_ratio, sentiment_score, sentiment_formula_version,
    oi_source, funding_source, market_cap_source, source_observed_at,
    payload_hash, raw_payload
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
    $16, $17::jsonb
)
ON CONFLICT (strategy_id, symbol, bucket_start) DO UPDATE SET
    observed_at = EXCLUDED.observed_at,
    open_interest_contracts = EXCLUDED.open_interest_contracts,
    open_interest_usd = EXCLUDED.open_interest_usd,
    market_cap_usd = EXCLUDED.market_cap_usd,
    funding_rate = EXCLUDED.funding_rate,
    oi_market_cap_ratio = EXCLUDED.oi_market_cap_ratio,
    sentiment_score = EXCLUDED.sentiment_score,
    sentiment_formula_version = EXCLUDED.sentiment_formula_version,
    oi_source = EXCLUDED.oi_source,
    funding_source = EXCLUDED.funding_source,
    market_cap_source = EXCLUDED.market_cap_source,
    source_observed_at = EXCLUDED.source_observed_at,
    payload_hash = EXCLUDED.payload_hash,
    raw_payload = EXCLUDED.raw_payload,
    collected_at = NOW()
WHERE $18 > CASE
    WHEN ai_tuner_market_snapshots.market_cap_source = 'coingecko'
         AND ai_tuner_market_snapshots.open_interest_usd > 0
         AND ai_tuner_market_snapshots.market_cap_usd > 0
         AND ai_tuner_market_snapshots.funding_rate IS NOT NULL
         AND ai_tuner_market_snapshots.sentiment_formula_version = 'funding_annual_pct_v1'
         AND ABS(
             ai_tuner_market_snapshots.oi_market_cap_ratio
             - ai_tuner_market_snapshots.open_interest_usd
               / NULLIF(ai_tuner_market_snapshots.market_cap_usd, 0)
         ) <= 1e-9 * GREATEST(
             1.0, ABS(
                 ai_tuner_market_snapshots.open_interest_usd
                 / NULLIF(ai_tuner_market_snapshots.market_cap_usd, 0)
             )
         )
         AND ABS(
             ai_tuner_market_snapshots.sentiment_score
             - ai_tuner_market_snapshots.funding_rate * 3 * 365 * 100
         ) <= 1e-9 * GREATEST(
             1.0, ABS(ai_tuner_market_snapshots.funding_rate * 3 * 365 * 100)
         ) THEN 5
    WHEN ai_tuner_market_snapshots.market_cap_source IN ('cache_hit', 'coingecko_cache')
         AND ai_tuner_market_snapshots.open_interest_usd > 0
         AND ai_tuner_market_snapshots.market_cap_usd > 0
         AND ai_tuner_market_snapshots.funding_rate IS NOT NULL
         AND ai_tuner_market_snapshots.sentiment_formula_version = 'funding_annual_pct_v1'
         AND ABS(
             ai_tuner_market_snapshots.oi_market_cap_ratio
             - ai_tuner_market_snapshots.open_interest_usd
               / NULLIF(ai_tuner_market_snapshots.market_cap_usd, 0)
         ) <= 1e-9 * GREATEST(
             1.0, ABS(
                 ai_tuner_market_snapshots.open_interest_usd
                 / NULLIF(ai_tuner_market_snapshots.market_cap_usd, 0)
             )
         )
         AND ABS(
             ai_tuner_market_snapshots.sentiment_score
             - ai_tuner_market_snapshots.funding_rate * 3 * 365 * 100
         ) <= 1e-9 * GREATEST(
             1.0, ABS(ai_tuner_market_snapshots.funding_rate * 3 * 365 * 100)
         ) THEN 4
    WHEN ai_tuner_market_snapshots.market_cap_source = 'coingecko' THEN 3
    WHEN ai_tuner_market_snapshots.market_cap_source IN ('cache_hit', 'coingecko_cache') THEN 2
    WHEN ai_tuner_market_snapshots.market_cap_source = 'oi_proxy' THEN 1
    ELSE 0
END
"""


class SnapshotRepository:
    def __init__(self, db) -> None:
        self.db = db

    async def store_market_snapshot(self, snapshot: MarketSnapshot) -> None:
        await self.db.execute(
            _STORE_MARKET_SNAPSHOT_SQL,
            snapshot.strategy_id,
            snapshot.symbol,
            snapshot.observed_at,
            snapshot.bucket_start,
            snapshot.open_interest_contracts,
            snapshot.open_interest_usd,
            snapshot.market_cap_usd,
            snapshot.funding_rate,
            snapshot.oi_market_cap_ratio,
            snapshot.sentiment_score,
            snapshot.sentiment_formula_version,
            snapshot.oi_source,
            snapshot.funding_source,
            snapshot.market_cap_source,
            snapshot.source_observed_at,
            snapshot.payload_hash,
            json.dumps(snapshot.raw_payload, sort_keys=True, default=str),
            snapshot.quality_rank,
        )

    async def count_bucket(self, key: Tuple[str, str, datetime]) -> int:
        row = await self.db.fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM public.ai_tuner_market_snapshots
            WHERE strategy_id = $1 AND symbol = $2 AND bucket_start = $3
            """,
            *key,
        )
        return int((row or {}).get("count", 0))

    async def store_universe(
        self,
        strategy_id: str,
        observed_at: datetime,
        short_symbols,
        long_symbols,
        source_run_id: str,
        config_hash: str,
    ) -> None:
        short_set = {str(symbol).upper() for symbol in short_symbols}
        long_set = {str(symbol).upper() for symbol in long_symbols}
        symbols = sorted(short_set | long_set)
        queries = [(
            """
            INSERT INTO public.ai_tuner_universe_scan_runs
                (strategy_id, observed_at, source_run_id, config_hash, symbol_count)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (strategy_id, observed_at) DO NOTHING
            """,
            (strategy_id, observed_at, source_run_id, config_hash, len(symbols)),
        )]
        memberships = []
        for symbol in symbols:
            membership = "both" if symbol in short_set and symbol in long_set else (
                "short" if symbol in short_set else "long"
            )
            memberships.append(membership)
        if symbols:
            # 关于参数显式类型转换（::varchar(64)/::timestamptz/::char(64) 等）：
            # strategy_id/source_run_id 在目标列是 varchar，而下方 WHERE 中的
            # "= $1" 会按 texteq 操作符把参数推定为 text；同一参数先后被推定为
            # text 与 varchar 会触发
            # "inconsistent types deduced for parameter $1: text versus character varying"。
            # 因此 SELECT 列表里对涉及 WHERE 等值比较的参数一律显式 :: 转换。
            queries.append((
                """
                INSERT INTO public.ai_tuner_universe_snapshots
                    (strategy_id, observed_at, symbol, membership, source_run_id, config_hash)
                SELECT $1::varchar(64), $2::timestamptz, members.symbol, members.membership,
                       $5::varchar(128), $6::char(64)
                FROM UNNEST($3::text[], $4::text[]) AS members(symbol, membership)
                WHERE EXISTS (
                    SELECT 1 FROM public.ai_tuner_universe_scan_runs r
                    WHERE r.strategy_id = $1 AND r.observed_at = $2
                      AND r.source_run_id = $5 AND r.config_hash = $6
                      AND r.symbol_count = CARDINALITY($3::text[])
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM public.ai_tuner_universe_snapshots existing
                    WHERE existing.strategy_id = $1 AND existing.observed_at = $2
                )
                ON CONFLICT (strategy_id, observed_at, symbol) DO NOTHING
                """,
                (strategy_id, observed_at, symbols, memberships, source_run_id, config_hash),
            ))
        await self.db.execute_transaction(queries)

    async def latest_universe(self, strategy_id: str, as_of: datetime):
        rows = await self.db.fetch_all(
            """
            SELECT u.symbol
            FROM public.ai_tuner_universe_scan_runs r
            LEFT JOIN public.ai_tuner_universe_snapshots u
              ON u.strategy_id = r.strategy_id AND u.observed_at = r.observed_at
            WHERE r.strategy_id = $1
              AND r.observed_at = (
                  SELECT MAX(observed_at)
                  FROM public.ai_tuner_universe_scan_runs
                  WHERE strategy_id = $1 AND observed_at <= $2
              )
            ORDER BY u.symbol
            """,
            strategy_id,
            as_of,
        )
        return {row["symbol"] for row in rows if row.get("symbol")}

    async def latest_market_snapshots(self, strategy_id: str, as_of: datetime):
        return await self.db.fetch_all(
            """
            SELECT DISTINCT ON (symbol) *
            FROM public.ai_tuner_market_snapshots
            WHERE strategy_id = $1
              AND bucket_start <= $2
              AND source_observed_at <= $2
              AND collected_at <= $2
            ORDER BY symbol, bucket_start DESC
            """,
            strategy_id,
            as_of,
        )
