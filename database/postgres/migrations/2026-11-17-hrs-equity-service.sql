-- Runtime projection consumed by shared.equity_entry_gate, and immutable source proof.
CREATE TABLE IF NOT EXISTS trading.strategy_equity_gate (
 account_id text NOT NULL, strategy_id text NOT NULL, policy_revision bigint NOT NULL,
 ready boolean NOT NULL, allowed boolean NOT NULL, as_of timestamptz NOT NULL,
 valid_until timestamptz NOT NULL, reasons jsonb NOT NULL, evidence_sha256 text NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(account_id,strategy_id),
 CHECK (NOT allowed OR ready), CHECK (valid_until <= as_of + interval '120 seconds'),
 CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE TABLE IF NOT EXISTS trading.hrs_equity_segment (
 account_id text PRIMARY KEY, segment_id text NOT NULL UNIQUE,
 inception timestamptz NOT NULL, opening_cash numeric NOT NULL CHECK (opening_cash>0 AND opening_cash<'Infinity'::numeric),
 opening_allocation_sha256 text NOT NULL, opening_evidence jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS trading.hrs_equity_evidence (
 account_id text NOT NULL, evidence_sha256 text NOT NULL, segment_id text,
 as_of timestamptz NOT NULL, available_at timestamptz NOT NULL,
 payload jsonb NOT NULL, PRIMARY KEY(account_id,evidence_sha256), CHECK (available_at >= as_of)
);
-- SELECT ... FOR UPDATE requires UPDATE on at least one column of the locked
-- table, even for an empty result. Grant only the lock-enabling identity
-- column; the statement trigger rejects every data mutation, including no-ops.
CREATE OR REPLACE FUNCTION trading.hrs_reject_equity_segment_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,trading AS $$
BEGIN RAISE EXCEPTION 'HRS equity segments are immutable'; END $$;
DROP TRIGGER IF EXISTS hrs_equity_segment_immutable ON trading.hrs_equity_segment;
CREATE TRIGGER hrs_equity_segment_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
 ON trading.hrs_equity_segment FOR EACH STATEMENT
 EXECUTE FUNCTION trading.hrs_reject_equity_segment_mutation();
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='trading_user') THEN
  GRANT SELECT,INSERT,UPDATE ON trading.strategy_equity_gate TO trading_user;
  GRANT SELECT,INSERT ON trading.hrs_equity_segment,trading.hrs_equity_evidence TO trading_user;
  GRANT UPDATE(account_id) ON trading.hrs_equity_segment TO trading_user;
 END IF;
END $$;
