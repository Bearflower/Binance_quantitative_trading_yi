-- Manual, administrator-applied migration. Installs capture NOW; never backdates
-- an all-entry-protocol cutover. Current released rows become legacy facts only.
BEGIN;
CREATE SCHEMA IF NOT EXISTS trading;
LOCK TABLE trading.entry_risk_reservation IN SHARE ROW EXCLUSIVE MODE;
CREATE TABLE IF NOT EXISTS trading.entry_activity_source_fact (
 id bigserial PRIMARY KEY,
 account_id text NOT NULL CHECK(account_id<>''),
 kind text NOT NULL CHECK(kind IN ('capture_boundary','legacy_intent','intent','intent_state',
   'collection_boundary','collection_batch','protocol_window','flat_position')),
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
 observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 created_by text NOT NULL DEFAULT current_user
);
CREATE INDEX IF NOT EXISTS entry_activity_source_account ON trading.entry_activity_source_fact(account_id,id);
CREATE TABLE IF NOT EXISTS trading.entry_order_observation (
 id bigserial PRIMARY KEY,
 account_id text NOT NULL CHECK(account_id<>''),
 client_order_id text NOT NULL CHECK(client_order_id<>''),
 symbol text NOT NULL CHECK(symbol<>''),
 order_id text, side text, status text, orig_qty numeric, executed_qty numeric,
 outcome text NOT NULL CHECK(outcome IN ('ok','order_not_found','order_lookup_timeout',
  'order_lookup_unknown','invalid_order_response','order_identity_conflict')),
 raw_payload jsonb NOT NULL CHECK(jsonb_typeof(raw_payload)='object'),
 payload_sha256 text NOT NULL CHECK(payload_sha256 ~ '^[0-9a-f]{64}$'),
 credential_fingerprint text NOT NULL CHECK(credential_fingerprint ~ '^[0-9a-f]{64}$'),
 source text NOT NULL CHECK(source='signed_pm_get_order_v1'),
 batch_id text NOT NULL,
 observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK ((outcome='ok' AND order_id IS NOT NULL AND side IS NOT NULL
   AND orig_qty IS NOT NULL AND executed_qty IS NOT NULL
   AND order_id ~ '^[1-9][0-9]*$' AND side IN ('BUY','SELL')
   AND status IN ('NEW','PARTIALLY_FILLED','PENDING_CANCEL','FILLED','CANCELED','EXPIRED','EXPIRED_IN_MATCH','REJECTED')
   AND orig_qty>0 AND orig_qty<'Infinity'::numeric
   AND executed_qty>=0 AND executed_qty<=orig_qty
   AND client_order_id=raw_payload->>'clientOrderId'
   AND order_id=raw_payload->>'orderId' AND symbol=raw_payload->>'symbol'
   AND side=raw_payload->>'side' AND status=raw_payload->>'status'
   AND orig_qty=(raw_payload->>'origQty')::numeric
   AND executed_qty=(raw_payload->>'executedQty')::numeric)
  OR (outcome<>'ok' AND order_id IS NULL AND side IS NULL AND status IS NULL
      AND orig_qty IS NULL AND executed_qty IS NULL AND raw_payload='{}'::jsonb)),
 CHECK(raw_payload - ARRAY['clientOrderId','orderId','symbol','side','status','origQty',
   'executedQty','reduceOnly','positionSide','time','updateTime','type','origType','closePosition']='{}'::jsonb)
);
CREATE INDEX IF NOT EXISTS entry_order_observation_client ON trading.entry_order_observation(account_id,client_order_id,id);
CREATE INDEX IF NOT EXISTS entry_order_observation_order ON trading.entry_order_observation(account_id,symbol,order_id);
CREATE OR REPLACE FUNCTION trading.reject_entry_activity_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Entry activity evidence is append-only'; END $$;
CREATE OR REPLACE FUNCTION trading.guard_entry_order_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 IF current_setting('transaction_isolation') <> 'read committed' THEN
  RAISE EXCEPTION 'Entry order identity capture requires read committed';
 END IF;
 IF NEW.outcome='ok' THEN
  IF NOT (NEW.raw_payload ?& ARRAY['clientOrderId','orderId','symbol','side','status','origQty','executedQty']) THEN
   RAISE EXCEPTION 'Signed order response identity fields required';
  END IF;
  -- Serialize both identity keys across concurrent collectors.
  PERFORM pg_advisory_xact_lock(hashtextextended('entry-client:'||NEW.account_id||':'||NEW.client_order_id,0));
  PERFORM pg_advisory_xact_lock(hashtextextended('entry-order:'||NEW.account_id||':'||NEW.symbol||':'||NEW.order_id,0));
  IF EXISTS(SELECT FROM trading.entry_order_observation o WHERE o.account_id=NEW.account_id
    AND o.outcome='ok' AND
     ((o.client_order_id=NEW.client_order_id AND
       (o.symbol,o.order_id,o.side,o.orig_qty) IS DISTINCT FROM
       (NEW.symbol,NEW.order_id,NEW.side,NEW.orig_qty))
      OR (o.symbol=NEW.symbol AND o.order_id=NEW.order_id AND o.client_order_id<>NEW.client_order_id))) THEN
    RAISE EXCEPTION 'Entry order identity conflict';
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION trading.guard_entry_activity_source() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 NEW.created_by := current_user;
 -- Runtime collectors cannot attest protocol/capture installation or impersonate
 -- a source trigger. Administrator protocol review still needs signed orders,
 -- complete batches and fills in the reader; a declaration alone proves nothing.
 IF current_user='trading_user' AND NEW.kind IN ('protocol_window','capture_boundary','intent','legacy_intent','intent_state') THEN
  RAISE EXCEPTION 'Administrator/capture source required';
 END IF;
 RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION trading.capture_entry_activity_intent() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,trading AS $$
BEGIN
 IF TG_OP='UPDATE' AND (NEW.client_order_id,NEW.account_id,NEW.strategy_id,NEW.symbol,NEW.quantity,NEW.geometry)
       IS DISTINCT FROM (OLD.client_order_id,OLD.account_id,OLD.strategy_id,OLD.symbol,OLD.quantity,OLD.geometry) THEN
  RAISE EXCEPTION 'Captured entry intent identity is immutable';
 END IF;
 IF TG_OP='INSERT' THEN
  INSERT INTO trading.entry_activity_source_fact(account_id,kind,payload)
  VALUES(NEW.account_id,'intent',to_jsonb(NEW));
 ELSIF TG_OP='UPDATE' THEN
  INSERT INTO trading.entry_activity_source_fact(account_id,kind,payload)
  VALUES(NEW.account_id,'intent_state',to_jsonb(NEW));
 ELSE
  INSERT INTO trading.entry_activity_source_fact(account_id,kind,payload)
  VALUES(OLD.account_id,'intent_state',to_jsonb(OLD));
 END IF;
 RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
END $$;

DO $$ DECLARE tbl text;
BEGIN
 FOREACH tbl IN ARRAY ARRAY['entry_order_observation','entry_activity_source_fact'] LOOP
  IF NOT EXISTS(SELECT FROM pg_trigger WHERE tgrelid=('trading.'||tbl)::regclass AND tgname=tbl||'_immutable') THEN
   EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON trading.%I FOR EACH ROW EXECUTE FUNCTION trading.reject_entry_activity_mutation()',tbl||'_immutable',tbl);
   EXECUTE format('ALTER TABLE trading.%I ENABLE ALWAYS TRIGGER %I',tbl,tbl||'_immutable');
  END IF;
  IF NOT EXISTS(SELECT FROM pg_trigger WHERE tgrelid=('trading.'||tbl)::regclass AND tgname=tbl||'_no_truncate') THEN
   EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON trading.%I FOR EACH STATEMENT EXECUTE FUNCTION trading.reject_entry_activity_mutation()',tbl||'_no_truncate',tbl);
   EXECUTE format('ALTER TABLE trading.%I ENABLE ALWAYS TRIGGER %I',tbl,tbl||'_no_truncate');
  END IF;
 END LOOP;
 IF NOT EXISTS(SELECT FROM pg_trigger WHERE tgrelid='trading.entry_order_observation'::regclass AND tgname='entry_order_identity_guard') THEN
  CREATE TRIGGER entry_order_identity_guard BEFORE INSERT ON trading.entry_order_observation FOR EACH ROW EXECUTE FUNCTION trading.guard_entry_order_identity();
  ALTER TABLE trading.entry_order_observation ENABLE ALWAYS TRIGGER entry_order_identity_guard;
 END IF;
 IF NOT EXISTS(SELECT FROM pg_trigger WHERE tgrelid='trading.entry_activity_source_fact'::regclass AND tgname='entry_activity_source_guard') THEN
  CREATE TRIGGER entry_activity_source_guard BEFORE INSERT ON trading.entry_activity_source_fact FOR EACH ROW EXECUTE FUNCTION trading.guard_entry_activity_source();
  ALTER TABLE trading.entry_activity_source_fact ENABLE ALWAYS TRIGGER entry_activity_source_guard;
 END IF;
 IF EXISTS(SELECT FROM trading.entry_activity_source_fact WHERE kind='capture_boundary') THEN
  IF NOT EXISTS(SELECT FROM pg_trigger WHERE tgrelid='trading.entry_risk_reservation'::regclass
       AND tgname='entry_activity_capture_intent' AND tgenabled='A') THEN
   RAISE EXCEPTION 'Entry activity capture continuity was lost; cannot backfill a cutover';
  END IF;
 ELSE
  INSERT INTO trading.entry_activity_source_fact(account_id,kind,payload)
   SELECT account_id,'capture_boundary',jsonb_build_object('source_version','reservation_capture_v1')
   FROM (SELECT DISTINCT account_id FROM trading.entry_risk_reservation UNION SELECT 'default') a;
  INSERT INTO trading.entry_activity_source_fact(account_id,kind,payload)
   SELECT account_id,'legacy_intent',to_jsonb(r) FROM trading.entry_risk_reservation r;
  CREATE TRIGGER entry_activity_capture_intent BEFORE INSERT OR UPDATE OR DELETE
    ON trading.entry_risk_reservation FOR EACH ROW EXECUTE FUNCTION trading.capture_entry_activity_intent();
  ALTER TABLE trading.entry_risk_reservation ENABLE ALWAYS TRIGGER entry_activity_capture_intent;
 END IF;
 IF EXISTS(SELECT FROM pg_roles WHERE rolname='trading_user') THEN
  GRANT USAGE ON SCHEMA trading TO trading_user;
  REVOKE UPDATE,DELETE,TRUNCATE ON trading.entry_order_observation,trading.entry_activity_source_fact FROM trading_user;
  GRANT SELECT,INSERT ON trading.entry_order_observation,trading.entry_activity_source_fact TO trading_user;
  GRANT USAGE,SELECT ON SEQUENCE trading.entry_order_observation_id_seq,trading.entry_activity_source_fact_id_seq TO trading_user;
 END IF;
END $$;
COMMIT;
