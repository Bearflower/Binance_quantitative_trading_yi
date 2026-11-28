-- Apply before starting new equity segments. No historical changes are invented.
-- Source mutations are serialized until commit, so visible journal IDs also
-- preserve committed transaction order. Rollback gaps in IDs are legitimate.
BEGIN;
SET LOCAL lock_timeout='5s';
LOCK TABLE public.capital_allocation IN SHARE ROW EXCLUSIVE MODE;
CREATE TABLE IF NOT EXISTS trading.hrs_allocation_audit_control (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 version integer NOT NULL CHECK(version=1), installed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO trading.hrs_allocation_audit_control(singleton,version) VALUES(true,1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS trading.hrs_allocation_event (
 event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 transaction_id bigint NOT NULL, statement_at timestamptz NOT NULL,
 operation text NOT NULL CHECK(operation IN ('INSERT','UPDATE','DELETE')),
 old_row jsonb,new_row jsonb,
 CHECK(old_row IS NOT NULL OR new_row IS NOT NULL)
);
CREATE OR REPLACE FUNCTION trading.hrs_serialize_allocation_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,trading AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('hrs-capital-allocation-audit-v1',7184));
 RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION trading.hrs_capture_allocation_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,trading AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  INSERT INTO trading.hrs_allocation_event(transaction_id,statement_at,operation,old_row,new_row)
   VALUES(txid_current(),clock_timestamp(),TG_OP,NULL,to_jsonb(NEW));
 ELSIF TG_OP='DELETE' THEN
  INSERT INTO trading.hrs_allocation_event(transaction_id,statement_at,operation,old_row,new_row)
   VALUES(txid_current(),clock_timestamp(),TG_OP,to_jsonb(OLD),NULL);
 ELSE
  IF to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW) THEN
   INSERT INTO trading.hrs_allocation_event(transaction_id,statement_at,operation,old_row,new_row)
    VALUES(txid_current(),clock_timestamp(),TG_OP,to_jsonb(OLD),to_jsonb(NEW));
  END IF;
 END IF;
 RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION trading.hrs_reject_allocation_history_change() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,trading AS $$
BEGIN RAISE EXCEPTION 'Allocation audit history is immutable; reconciliation requires separate evidence'; END $$;
DROP TRIGGER IF EXISTS hrs_allocation_serialize ON public.capital_allocation;
CREATE TRIGGER hrs_allocation_serialize BEFORE INSERT OR UPDATE OR DELETE ON public.capital_allocation
 FOR EACH STATEMENT EXECUTE FUNCTION trading.hrs_serialize_allocation_change();
DROP TRIGGER IF EXISTS hrs_allocation_capture ON public.capital_allocation;
CREATE TRIGGER hrs_allocation_capture AFTER INSERT OR UPDATE OR DELETE ON public.capital_allocation
 FOR EACH ROW EXECUTE FUNCTION trading.hrs_capture_allocation_event();
DROP TRIGGER IF EXISTS hrs_allocation_no_truncate ON public.capital_allocation;
CREATE TRIGGER hrs_allocation_no_truncate BEFORE TRUNCATE ON public.capital_allocation
 FOR EACH STATEMENT EXECUTE FUNCTION trading.hrs_reject_allocation_history_change();
DROP TRIGGER IF EXISTS hrs_allocation_event_immutable ON trading.hrs_allocation_event;
CREATE TRIGGER hrs_allocation_event_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON trading.hrs_allocation_event
 FOR EACH STATEMENT EXECUTE FUNCTION trading.hrs_reject_allocation_history_change();
DROP TRIGGER IF EXISTS hrs_allocation_control_immutable ON trading.hrs_allocation_audit_control;
CREATE TRIGGER hrs_allocation_control_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON trading.hrs_allocation_audit_control
 FOR EACH STATEMENT EXECUTE FUNCTION trading.hrs_reject_allocation_history_change();
ALTER TABLE trading.hrs_equity_segment ADD COLUMN IF NOT EXISTS allocation_audit_event_id bigint;
ALTER TABLE trading.hrs_equity_segment ADD COLUMN IF NOT EXISTS allocation_audit_baseline jsonb;
ALTER TABLE trading.hrs_equity_segment ADD COLUMN IF NOT EXISTS allocation_audit_version integer;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trading_user') THEN
  GRANT SELECT ON trading.hrs_allocation_event,trading.hrs_allocation_audit_control TO trading_user;
 END IF;
END $$;
COMMIT;
