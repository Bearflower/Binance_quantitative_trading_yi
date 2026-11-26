CREATE TABLE IF NOT EXISTS trading.hrs_equity_reconciliation (
 account_id text NOT NULL,segment_id text NOT NULL,
 before_state_version bigint NOT NULL,after_state_version bigint NOT NULL,
 as_of timestamptz NOT NULL,available_at timestamptz NOT NULL,
 receipt_sha256 text NOT NULL,before_state jsonb NOT NULL,after_state jsonb NOT NULL,
 checkpoint_series jsonb NOT NULL,receipt jsonb NOT NULL,
 PRIMARY KEY(account_id,after_state_version),UNIQUE(account_id,receipt_sha256),
 CHECK(after_state_version=before_state_version+1),CHECK(available_at>=as_of),
 CHECK(receipt_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE OR REPLACE FUNCTION trading.hrs_reject_equity_revision_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,trading AS $$
BEGIN RAISE EXCEPTION 'Equity reconciliation revisions are immutable'; END $$;
DROP TRIGGER IF EXISTS hrs_equity_reconciliation_immutable ON trading.hrs_equity_reconciliation;
CREATE TRIGGER hrs_equity_reconciliation_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON trading.hrs_equity_reconciliation
 FOR EACH STATEMENT EXECUTE FUNCTION trading.hrs_reject_equity_revision_mutation();
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trading_user') THEN
  GRANT SELECT,INSERT ON trading.hrs_equity_reconciliation TO trading_user;
 END IF;
END $$;
