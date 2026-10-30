-- Funding schedule evidence is append-only. Hashes cover the retained canonical
-- selected response fields, not exchange-signed payloads or raw HTTP bytes.
CREATE TABLE IF NOT EXISTS public.ai_tuner_funding_schedule_runs (
    attempt_id UUID NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    request_started_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('success', 'failed')),
    failure_code VARCHAR(64),
    payload_hash CHAR(64) NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    raw_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (attempt_id),
    CHECK (request_started_at <= received_at),
    CHECK (
        (status = 'success' AND failure_code IS NULL)
        OR (status = 'failed' AND failure_code IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS public.ai_tuner_funding_schedule_observations (
    attempt_id UUID NOT NULL REFERENCES public.ai_tuner_funding_schedule_runs(attempt_id),
    symbol VARCHAR(32) NOT NULL,
    source_time TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    next_funding_time TIMESTAMPTZ NOT NULL,
    funding_interval_hours INTEGER CHECK (funding_interval_hours > 0),
    payload_hash CHAR(64) NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    raw_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (attempt_id, symbol),
    CHECK (source_time <= received_at),
    CHECK (next_funding_time > source_time)
);

CREATE INDEX IF NOT EXISTS idx_ai_tuner_funding_schedule_runs_scheduled
ON public.ai_tuner_funding_schedule_runs(scheduled_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_tuner_funding_schedule_observations_lookup
ON public.ai_tuner_funding_schedule_observations(symbol, source_time DESC);

CREATE INDEX IF NOT EXISTS idx_ai_tuner_funding_schedule_observations_event
ON public.ai_tuner_funding_schedule_observations(symbol, next_funding_time);

-- The writer is not an owner, so its table privileges cannot bypass append-only access.
-- Its password is supplied by deployment configuration, never by this migration.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_tuner_funding_owner') THEN
        CREATE ROLE ai_tuner_funding_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_tuner_funding_writer') THEN
        CREATE ROLE ai_tuner_funding_writer LOGIN;
    END IF;
END
$$;

ALTER TABLE public.ai_tuner_funding_schedule_runs OWNER TO ai_tuner_funding_owner;
ALTER TABLE public.ai_tuner_funding_schedule_observations OWNER TO ai_tuner_funding_owner;

REVOKE ALL ON public.ai_tuner_funding_schedule_runs FROM PUBLIC, trading_user;
REVOKE ALL ON public.ai_tuner_funding_schedule_observations FROM PUBLIC, trading_user;
GRANT USAGE ON SCHEMA public TO ai_tuner_funding_writer;
GRANT SELECT, INSERT ON public.ai_tuner_funding_schedule_runs TO ai_tuner_funding_writer;
GRANT SELECT, INSERT ON public.ai_tuner_funding_schedule_observations TO ai_tuner_funding_writer;
GRANT SELECT ON public.ai_tuner_universe_scan_runs TO ai_tuner_funding_writer;
GRANT SELECT ON public.ai_tuner_universe_snapshots TO ai_tuner_funding_writer;
GRANT SELECT ON public.ai_tuner_funding_schedule_runs TO trading_user;
GRANT SELECT ON public.ai_tuner_funding_schedule_observations TO trading_user;
