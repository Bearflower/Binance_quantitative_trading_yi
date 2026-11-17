-- Independent account-risk observation only. No entry-policy or order tables.
-- Account baseline is intentionally unique across budget renewals/calendar jobs.
BEGIN;
CREATE SCHEMA IF NOT EXISTS trading;

CREATE TABLE IF NOT EXISTS trading.account_risk_baseline (
    account_id text PRIMARY KEY,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (payload->>'account_id' = account_id),
    CHECK (length(payload->>'account_identity') > 0),
    CHECK (length(payload->>'credential_fingerprint') > 0)
);

CREATE TABLE IF NOT EXISTS trading.account_risk_equity_point (
    account_id text NOT NULL,
    sample_time_ms bigint NOT NULL CHECK (sample_time_ms > 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, sample_time_ms),
    CHECK (payload->>'account_id' = account_id),
    CHECK ((payload->>'exchange_time_ms')::bigint = sample_time_ms)
);

CREATE TABLE IF NOT EXISTS trading.account_risk_transfer (
    account_id text NOT NULL,
    transfer_type text NOT NULL CHECK (transfer_type IN ('MAIN_PORTFOLIO_MARGIN','PORTFOLIO_MARGIN_MAIN')),
    event_id text NOT NULL CHECK (length(event_id) > 0),
    exchange_time_ms bigint NOT NULL CHECK (exchange_time_ms > 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, transfer_type, event_id),
    CHECK (payload->>'account_id' = account_id),
    CHECK (payload->>'event_id' = event_id),
    CHECK (payload->>'transfer_type' = transfer_type),
    CHECK ((payload->>'exchange_time_ms')::bigint = exchange_time_ms)
);
CREATE INDEX IF NOT EXISTS account_risk_transfer_time_idx
    ON trading.account_risk_transfer (account_id, exchange_time_ms);

CREATE TABLE IF NOT EXISTS trading.account_risk_coverage (
    account_id text NOT NULL,
    coverage_id text NOT NULL,
    transfer_type text NOT NULL CHECK (transfer_type IN ('MAIN_PORTFOLIO_MARGIN','PORTFOLIO_MARGIN_MAIN')),
    start_ms bigint NOT NULL CHECK (start_ms > 0),
    end_ms bigint NOT NULL CHECK (end_ms >= start_ms),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, coverage_id),
    CHECK (payload->>'account_id' = account_id),
    CHECK (payload->>'transfer_type' = transfer_type),
    CHECK ((payload->>'start_ms')::bigint = start_ms),
    CHECK ((payload->>'end_ms')::bigint = end_ms)
);

-- A verified historical breach survives later API/storage coverage gaps. This
-- is an observation fact, never an order, a policy change or an execution latch.
CREATE TABLE IF NOT EXISTS trading.account_risk_breach (
    account_id text NOT NULL,
    breach_id text NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, breach_id),
    CHECK (payload->>'account_id' = account_id),
    CHECK (length(payload->>'account_identity') > 0),
    CHECK ((payload->>'max_drawdown_ratio')::numeric >= 0)
);

CREATE OR REPLACE FUNCTION trading.account_risk_forbid_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'account risk facts are append-only';
END;
$$;

DO $$
DECLARE tbl text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY['account_risk_baseline','account_risk_equity_point',
                              'account_risk_transfer','account_risk_coverage','account_risk_breach']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname=tbl || '_immutable'
                       AND tgrelid=('trading.' || tbl)::regclass) THEN
            EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON trading.%I '
                           'FOR EACH ROW EXECUTE FUNCTION trading.account_risk_forbid_mutation()',
                           tbl || '_immutable', tbl);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname=tbl || '_no_truncate'
                       AND tgrelid=('trading.' || tbl)::regclass) THEN
            EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON trading.%I '
                           'FOR EACH STATEMENT EXECUTE FUNCTION trading.account_risk_forbid_mutation()',
                           tbl || '_no_truncate', tbl);
        END IF;
    END LOOP;
    -- The existing application role receives only the permissions it needs.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='trading_user') THEN
        GRANT USAGE ON SCHEMA trading TO trading_user;
        GRANT SELECT, INSERT ON trading.account_risk_baseline,
            trading.account_risk_equity_point, trading.account_risk_transfer,
            trading.account_risk_coverage, trading.account_risk_breach TO trading_user;
    END IF;
END;
$$;
COMMIT;
