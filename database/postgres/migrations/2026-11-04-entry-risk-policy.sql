-- Apply as migration administrator. Runtime trading_user requires no DDL privileges.
BEGIN;
CREATE TABLE IF NOT EXISTS trading.strategy_risk_policy (
 account_id text NOT NULL, strategy_id text NOT NULL, revision bigint NOT NULL CHECK(revision>0),
 effective_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
 mode text NOT NULL CHECK(mode IN ('monitor_only','reduced','capped')),
 allow_add boolean NOT NULL DEFAULT false, multiplier numeric NOT NULL CHECK(multiplier>=0 AND multiplier<=1),
 ratio_cap numeric NOT NULL CHECK(ratio_cap>=0 AND ratio_cap<=1),
 entry_ceiling numeric NOT NULL CHECK(entry_ceiling>=0 AND entry_ceiling< 'Infinity'::numeric),
 freeze_allocation boolean NOT NULL DEFAULT true, reason text NOT NULL CHECK(length(reason)>0),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(account_id,strategy_id,revision), CHECK(expires_at>effective_at),
 CHECK(mode<>'reduced' OR (multiplier<=.5 AND NOT allow_add AND ratio_cap<=.15))
);
CREATE TABLE IF NOT EXISTS trading.strategy_risk_active (
 account_id text NOT NULL,strategy_id text NOT NULL,revision bigint NOT NULL,
 PRIMARY KEY(account_id,strategy_id),
 FOREIGN KEY(account_id,strategy_id,revision) REFERENCES trading.strategy_risk_policy
);
CREATE TABLE IF NOT EXISTS trading.strategy_risk_audit (
 id bigserial PRIMARY KEY,account_id text NOT NULL,strategy_id text NOT NULL,
 previous_revision bigint NOT NULL,revision bigint NOT NULL,reason text NOT NULL,
 activated_by text NOT NULL DEFAULT current_user, activated_at timestamptz NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION trading.reject_risk_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Risk policy/audit history is immutable; activate a new revision'; END $$;
DROP TRIGGER IF EXISTS immutable_risk_policy ON trading.strategy_risk_policy;
CREATE TRIGGER immutable_risk_policy BEFORE UPDATE OR DELETE ON trading.strategy_risk_policy
FOR EACH ROW EXECUTE FUNCTION trading.reject_risk_history_mutation();
DROP TRIGGER IF EXISTS immutable_risk_audit ON trading.strategy_risk_audit;
CREATE TRIGGER immutable_risk_audit BEFORE UPDATE OR DELETE ON trading.strategy_risk_audit
FOR EACH ROW EXECUTE FUNCTION trading.reject_risk_history_mutation();
CREATE TABLE IF NOT EXISTS trading.entry_risk_reservation (
 client_order_id text PRIMARY KEY,account_id text NOT NULL,strategy_id text NOT NULL,revision bigint NOT NULL,
 symbol text NOT NULL,quantity numeric NOT NULL CHECK(quantity>0 AND quantity<'Infinity'::numeric),
 original_quantity numeric NOT NULL CHECK(original_quantity>=quantity AND original_quantity<'Infinity'::numeric),
 price numeric NOT NULL CHECK(price>0 AND price<'Infinity'::numeric),
 leverage numeric NOT NULL CHECK(leverage>=1 AND leverage<'Infinity'::numeric),
 margin numeric NOT NULL CHECK(margin>0 AND margin<'Infinity'::numeric),
 state text NOT NULL CHECK(state IN ('reserved','submitting','released')),
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(account_id,strategy_id,revision) REFERENCES trading.strategy_risk_policy
);
ALTER TABLE trading.entry_risk_reservation ADD COLUMN IF NOT EXISTS geometry jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE trading.entry_risk_reservation ADD COLUMN IF NOT EXISTS pressure_risk numeric;
ALTER TABLE trading.entry_risk_reservation ADD COLUMN IF NOT EXISTS original_pressure_risk numeric;
CREATE INDEX IF NOT EXISTS entry_risk_pending ON trading.entry_risk_reservation(account_id,strategy_id) WHERE state<>'released';
-- Protect all legacy claim-release/expiry writers, including another strategy's cleanup.
-- Returning OLD prevents a cleanup from clearing the exclusive claim after ambiguity.
CREATE OR REPLACE FUNCTION trading.protect_reserved_entry_claim() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM trading.entry_risk_reservation r
           WHERE r.symbol=OLD.symbol AND r.strategy_id=OLD.strategy AND r.state<>'released') THEN
   IF TG_OP='DELETE' THEN RETURN NULL; END IF;
   IF NEW.claim_state='RELEASED' OR NEW.expires_at<'infinity'::timestamptz THEN RETURN OLD; END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS protect_reserved_entry_claim ON trading.position_claims;
CREATE TRIGGER protect_reserved_entry_claim BEFORE UPDATE OR DELETE ON trading.position_claims
FOR EACH ROW EXECUTE FUNCTION trading.protect_reserved_entry_claim();
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trading_user') THEN
  GRANT USAGE ON SCHEMA trading TO trading_user;
  GRANT SELECT ON trading.strategy_risk_policy,trading.strategy_risk_active,trading.strategy_risk_audit TO trading_user;
  GRANT SELECT,INSERT,UPDATE ON trading.entry_risk_reservation TO trading_user;
 END IF;
END $$;
COMMIT;
