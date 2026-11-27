-- Administrator-only policy acceptance; application observations are append-only.
BEGIN;
DO $$ BEGIN
    IF current_user <> 'trading_migration_admin' THEN
        RAISE EXCEPTION 'run as trading_migration_admin';
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS trading.historical_risk_acceptance (
    account_id text NOT NULL CHECK (btrim(account_id) <> ''),
    revision bigint NOT NULL CHECK (revision > 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    receipt_sha256 text NOT NULL CHECK (receipt_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (account_id,revision),
    UNIQUE (receipt_sha256),
    CHECK (payload->>'account_id' = account_id),
    CHECK ((payload->>'revision')::bigint = revision)
);
CREATE TABLE IF NOT EXISTS trading.classic_account_risk_point (
    account_id text NOT NULL CHECK (btrim(account_id) <> ''),
    policy_revision bigint NOT NULL CHECK (policy_revision > 0),
    sample_time_ms bigint NOT NULL CHECK (sample_time_ms > 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (account_id,policy_revision,sample_time_ms),
    FOREIGN KEY (account_id,policy_revision)
        REFERENCES trading.historical_risk_acceptance(account_id,revision)
);
CREATE OR REPLACE FUNCTION trading.reject_historical_risk_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'historical risk receipts and observations are immutable';
END $$;
DO $$ DECLARE tbl text; BEGIN
    FOREACH tbl IN ARRAY ARRAY['historical_risk_acceptance','classic_account_risk_point'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=('trading.'||tbl)::regclass AND tgname=tbl||'_immutable') THEN
            EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON trading.%I FOR EACH ROW EXECUTE FUNCTION trading.reject_historical_risk_mutation()',tbl||'_immutable',tbl);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=('trading.'||tbl)::regclass AND tgname=tbl||'_no_truncate') THEN
            EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON trading.%I FOR EACH STATEMENT EXECUTE FUNCTION trading.reject_historical_risk_mutation()',tbl||'_no_truncate',tbl);
        END IF;
    END LOOP;
END $$;
COMMENT ON TABLE trading.historical_risk_acceptance IS
    'Administrator-approved finite historical gaps; never a certificate of complete exchange evidence. Revocation is a new disabled revision.';
COMMENT ON TABLE trading.classic_account_risk_point IS
    'Append-only observed classic USDT risk. Writers serialize with advisory transaction key classic-account-risk-v1:<account>. Prior revisions retain high-watermarks and breaches.';
COMMIT;
-- Runtime privileges are applied exclusively by scripts/database_permissions.py.
