-- Forecast availability is independent of successful ledger advancement.
-- Retain actual observations even when later source checks deny the projection.
CREATE TABLE IF NOT EXISTS trading.hrs_funding_observation (
    account_id text NOT NULL,
    evidence_sha256 text NOT NULL,
    available_at timestamptz NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY(account_id,evidence_sha256)
);
CREATE OR REPLACE FUNCTION trading.hrs_funding_observation_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,trading AS $$ BEGIN RAISE EXCEPTION 'hrs_funding_observation is append-only'; END $$;
DROP TRIGGER IF EXISTS hrs_funding_observation_immutable ON trading.hrs_funding_observation;
CREATE TRIGGER hrs_funding_observation_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
ON trading.hrs_funding_observation FOR EACH STATEMENT EXECUTE FUNCTION trading.hrs_funding_observation_immutable();
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trading_user') THEN
  GRANT SELECT,INSERT ON trading.hrs_funding_observation TO trading_user;
 END IF;
END $$;
