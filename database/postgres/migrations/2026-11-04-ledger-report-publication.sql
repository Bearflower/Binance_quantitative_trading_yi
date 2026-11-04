-- Immutable economic reports; deploy before ledger dashboard consumers.
BEGIN;
CREATE TABLE IF NOT EXISTS trading.ledger_report_run (
    report_id text PRIMARY KEY,
    account_id text NOT NULL,
    asset text NOT NULL CHECK (asset = 'USDT'),
    window_key text NOT NULL,
    report_version text NOT NULL,
    start_ms bigint NOT NULL,
    end_ms bigint NOT NULL CHECK (end_ms > start_ms),
    report jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (account_id, asset, window_key, report_version, report_id)
);
CREATE TABLE IF NOT EXISTS trading.ledger_report_active (
    account_id text NOT NULL,
    asset text NOT NULL,
    window_key text NOT NULL,
    report_version text NOT NULL,
    report_id text NOT NULL,
    end_ms bigint NOT NULL,
    PRIMARY KEY (account_id, asset, window_key, report_version),
    FOREIGN KEY (account_id, asset, window_key, report_version, report_id)
        REFERENCES trading.ledger_report_run(account_id, asset, window_key, report_version, report_id)
);
CREATE OR REPLACE FUNCTION trading.reject_ledger_report_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'Published ledger report runs are immutable';
END $$;
DROP TRIGGER IF EXISTS ledger_report_immutable ON trading.ledger_report_run;
CREATE TRIGGER ledger_report_immutable BEFORE UPDATE OR DELETE ON trading.ledger_report_run
FOR EACH ROW EXECUTE FUNCTION trading.reject_ledger_report_mutation();
-- Runtime app may append a run and atomically switch pointers, never mutate a run.
GRANT USAGE ON SCHEMA trading TO trading_user;
GRANT SELECT, INSERT ON trading.ledger_report_run TO trading_user;
GRANT SELECT, INSERT, UPDATE ON trading.ledger_report_active TO trading_user;
COMMIT;
