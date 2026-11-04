-- Income-only immutable observations and durable conflict publication gate.
-- Existing account_id/income_type/tran_id PK is intentionally unchanged.
BEGIN;
CREATE TABLE IF NOT EXISTS trading.income_ingestion_attempt (
    attempt_id text PRIMARY KEY,
    account_id text NOT NULL,
    observations jsonb NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','complete','conflicted')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS income_ingestion_unresolved ON trading.income_ingestion_attempt(account_id)
    WHERE state IN ('pending','conflicted');
CREATE TABLE IF NOT EXISTS trading.income_ingestion_conflict (
    attempt_id text PRIMARY KEY REFERENCES trading.income_ingestion_attempt(attempt_id),
    error_code text NOT NULL,
    error_message text NOT NULL,
    evidence jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS trading.income_ingestion_attempt_event (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_id text NOT NULL REFERENCES trading.income_ingestion_attempt(attempt_id),
    old_state text,
    new_state text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor text NOT NULL DEFAULT session_user
);

CREATE OR REPLACE FUNCTION trading.income_raw_semantic(payload jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE result jsonb := payload; key text; number numeric;
BEGIN
    FOREACH key IN ARRAY ARRAY['income','tranId','tradeId','time'] LOOP
        IF payload ? key AND payload->key <> 'null'::jsonb THEN
            IF key <> 'income' AND payload->>key = '' THEN
                result := jsonb_set(result,ARRAY[key],'null'::jsonb);
            ELSE
                number := (payload->>key)::numeric;
                IF number::text IN ('NaN','Infinity','-Infinity') OR
                   (key <> 'income' AND number <> trunc(number)) THEN
                    RAISE EXCEPTION 'Invalid numeric income field: %',key;
                END IF;
                result := jsonb_set(result,ARRAY[key],to_jsonb(number));
            END IF;
        END IF;
    END LOOP;
    RETURN result;
END $$;

CREATE OR REPLACE FUNCTION trading.guard_immutable_income() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,trading AS $$
DECLARE old_raw jsonb;
BEGIN
    IF TG_OP='UPDATE' AND
       (to_jsonb(OLD)-'fetched_at') IS NOT DISTINCT FROM (to_jsonb(NEW)-'fetched_at') THEN
        RETURN NULL; -- exact normalized retry: retain original fact and first fetch time
    END IF;
    SELECT to_jsonb(r) INTO old_raw FROM trading.execution_fact_raw r
      WHERE source='um/income' AND entity_key='income'
        AND entity_id=OLD.account_id||':'||OLD.income_type||':'||OLD.tran_id;
    RAISE EXCEPTION 'Contradictory immutable income observation'
      USING ERRCODE='P7501', DETAIL=jsonb_build_object('old',to_jsonb(OLD),'new',to_jsonb(NEW),'old_raw',old_raw)::text;
END $$;
DROP TRIGGER IF EXISTS immutable_income ON trading.execution_fact_income;
CREATE TRIGGER immutable_income BEFORE UPDATE OR DELETE ON trading.execution_fact_income
FOR EACH ROW EXECUTE FUNCTION trading.guard_immutable_income();

CREATE OR REPLACE FUNCTION trading.guard_immutable_income_raw() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,trading AS $$
BEGIN
    IF (OLD.source='um/income' AND OLD.entity_key='income') OR
       (TG_OP='UPDATE' AND NEW.source='um/income' AND NEW.entity_key='income') THEN
        IF TG_OP='UPDATE' AND (OLD.source,OLD.entity_key,OLD.entity_id)=(NEW.source,NEW.entity_key,NEW.entity_id)
           AND trading.income_raw_semantic(OLD.raw_payload)=trading.income_raw_semantic(NEW.raw_payload) THEN
            RETURN NULL; -- semantically identical wire retry preserves first raw/hash/time
        END IF;
        RAISE EXCEPTION 'Contradictory immutable income raw observation'
          USING ERRCODE='P7502',DETAIL=jsonb_build_object('old',to_jsonb(OLD),'new',to_jsonb(NEW))::text;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW; -- original fill raw behavior is deliberately unchanged
END $$;
DROP TRIGGER IF EXISTS immutable_income_raw ON trading.execution_fact_raw;
CREATE TRIGGER immutable_income_raw BEFORE UPDATE OR DELETE ON trading.execution_fact_raw
FOR EACH ROW EXECUTE FUNCTION trading.guard_immutable_income_raw();

CREATE OR REPLACE FUNCTION trading.guard_income_attempt() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,trading AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.state <> 'pending' OR NEW.finished_at IS NOT NULL THEN RAISE EXCEPTION 'Income attempt must start pending'; END IF;
    ELSIF TG_OP='UPDATE' THEN
        IF OLD.state<>'pending' OR NEW.state NOT IN ('complete','conflicted') OR NEW.finished_at IS NULL OR
           (to_jsonb(OLD)-ARRAY['state','finished_at']) IS DISTINCT FROM (to_jsonb(NEW)-ARRAY['state','finished_at']) THEN
            RAISE EXCEPTION 'Invalid income attempt transition';
        END IF;
        IF NEW.state='conflicted' AND NOT EXISTS(SELECT 1 FROM trading.income_ingestion_conflict WHERE attempt_id=NEW.attempt_id) THEN
            RAISE EXCEPTION 'Conflict transition requires durable evidence';
        END IF;
    ELSE
        RAISE EXCEPTION 'Income attempts cannot be deleted';
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION trading.audit_income_attempt() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,trading AS $$
BEGIN
    INSERT INTO trading.income_ingestion_attempt_event(attempt_id,old_state,new_state)
      VALUES(NEW.attempt_id,CASE WHEN TG_OP='UPDATE' THEN OLD.state ELSE NULL END,NEW.state);
    RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION trading.reject_income_proof_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Income evidence is append-only'; END $$;
DROP TRIGGER IF EXISTS income_attempt_guard ON trading.income_ingestion_attempt;
CREATE TRIGGER income_attempt_guard BEFORE INSERT OR UPDATE OR DELETE ON trading.income_ingestion_attempt
FOR EACH ROW EXECUTE FUNCTION trading.guard_income_attempt();
DROP TRIGGER IF EXISTS income_attempt_audit ON trading.income_ingestion_attempt;
CREATE TRIGGER income_attempt_audit AFTER INSERT OR UPDATE ON trading.income_ingestion_attempt
FOR EACH ROW EXECUTE FUNCTION trading.audit_income_attempt();
DROP TRIGGER IF EXISTS income_conflict_immutable ON trading.income_ingestion_conflict;
CREATE TRIGGER income_conflict_immutable BEFORE UPDATE OR DELETE ON trading.income_ingestion_conflict
FOR EACH ROW EXECUTE FUNCTION trading.reject_income_proof_mutation();
DROP TRIGGER IF EXISTS income_attempt_event_immutable ON trading.income_ingestion_attempt_event;
CREATE TRIGGER income_attempt_event_immutable BEFORE UPDATE OR DELETE ON trading.income_ingestion_attempt_event
FOR EACH ROW EXECUTE FUNCTION trading.reject_income_proof_mutation();
GRANT USAGE ON SCHEMA trading TO trading_user;
GRANT SELECT,INSERT ON trading.income_ingestion_attempt,trading.income_ingestion_conflict TO trading_user;
GRANT UPDATE(state,finished_at) ON trading.income_ingestion_attempt TO trading_user;
GRANT SELECT ON trading.income_ingestion_attempt_event TO trading_user;
REVOKE ALL ON FUNCTION trading.guard_income_attempt(),trading.audit_income_attempt() FROM PUBLIC;
COMMIT;
