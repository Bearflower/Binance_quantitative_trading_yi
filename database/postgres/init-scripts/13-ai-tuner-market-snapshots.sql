CREATE TABLE IF NOT EXISTS public.ai_tuner_universe_scan_runs (
    strategy_id VARCHAR(64) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    source_run_id VARCHAR(128) NOT NULL,
    config_hash CHAR(64) NOT NULL,
    symbol_count INTEGER NOT NULL CHECK (symbol_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (strategy_id, observed_at),
    UNIQUE (strategy_id, source_run_id)
);

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

-- 运行时使用独立最小权限角色；密码由部署脚本从服务器 .env 注入，绝不入库。
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_tuner_snapshot_owner') THEN
        CREATE ROLE ai_tuner_snapshot_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_tuner_snapshot_writer') THEN
        CREATE ROLE ai_tuner_snapshot_writer LOGIN;
    END IF;
END
$$;

ALTER TABLE public.ai_tuner_universe_scan_runs OWNER TO ai_tuner_snapshot_owner;
ALTER TABLE public.ai_tuner_universe_snapshots OWNER TO ai_tuner_snapshot_owner;
ALTER TABLE public.ai_tuner_market_snapshots OWNER TO ai_tuner_snapshot_owner;

CREATE INDEX IF NOT EXISTS idx_ai_tuner_market_snapshots_lookup
ON public.ai_tuner_market_snapshots(strategy_id, symbol, bucket_start DESC);

CREATE INDEX IF NOT EXISTS idx_ai_tuner_universe_snapshots_lookup
ON public.ai_tuner_universe_snapshots(strategy_id, observed_at DESC, symbol);

CREATE INDEX IF NOT EXISTS idx_ai_tuner_universe_scan_runs_lookup
ON public.ai_tuner_universe_scan_runs(strategy_id, observed_at DESC);

REVOKE ALL ON public.ai_tuner_market_snapshots FROM PUBLIC, trading_user;
REVOKE ALL ON public.ai_tuner_universe_snapshots FROM PUBLIC, trading_user;
REVOKE ALL ON public.ai_tuner_universe_scan_runs FROM PUBLIC, trading_user;
GRANT SELECT ON public.ai_tuner_market_snapshots TO trading_user;
GRANT SELECT ON public.ai_tuner_universe_snapshots TO trading_user;
GRANT SELECT ON public.ai_tuner_universe_scan_runs TO trading_user;
GRANT USAGE ON SCHEMA public TO ai_tuner_snapshot_writer;
GRANT SELECT, INSERT, UPDATE ON public.ai_tuner_market_snapshots TO ai_tuner_snapshot_writer;
GRANT SELECT, INSERT ON public.ai_tuner_universe_snapshots TO ai_tuner_snapshot_writer;
GRANT SELECT, INSERT ON public.ai_tuner_universe_scan_runs TO ai_tuner_snapshot_writer;
