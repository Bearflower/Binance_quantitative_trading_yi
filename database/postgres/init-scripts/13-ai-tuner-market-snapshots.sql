CREATE TABLE IF NOT EXISTS public.ai_tuner_universe_snapshots (
    strategy_id VARCHAR(64) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    membership VARCHAR(16) NOT NULL CHECK (membership IN ('short', 'long', 'both')),
    source_run_id VARCHAR(128) NOT NULL,
    config_hash CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (strategy_id, observed_at, symbol)
);

CREATE TABLE IF NOT EXISTS public.ai_tuner_market_snapshots (
    strategy_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    bucket_start TIMESTAMPTZ NOT NULL,
    open_interest_contracts NUMERIC,
    open_interest_usd NUMERIC,
    funding_rate NUMERIC,
    market_cap_usd NUMERIC,
    oi_market_cap_ratio NUMERIC,
    sentiment_score NUMERIC,
    sentiment_formula_version VARCHAR(32) NOT NULL,
    oi_source VARCHAR(32) NOT NULL,
    funding_source VARCHAR(32) NOT NULL,
    market_cap_source VARCHAR(32) NOT NULL,
    source_observed_at TIMESTAMPTZ NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload_hash CHAR(64) NOT NULL,
    raw_payload JSONB NOT NULL,
    PRIMARY KEY (strategy_id, symbol, bucket_start)
);

CREATE INDEX IF NOT EXISTS idx_ai_tuner_market_snapshots_lookup
ON public.ai_tuner_market_snapshots(strategy_id, symbol, bucket_start DESC);

CREATE INDEX IF NOT EXISTS idx_ai_tuner_universe_snapshots_lookup
ON public.ai_tuner_universe_snapshots(strategy_id, observed_at DESC, symbol);

GRANT SELECT, INSERT, UPDATE ON public.ai_tuner_market_snapshots TO trading_user;
GRANT SELECT, INSERT, UPDATE ON public.ai_tuner_universe_snapshots TO trading_user;
