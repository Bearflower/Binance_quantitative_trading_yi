CREATE OR REPLACE FUNCTION trading.capture_entry_activity_intent()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'trading'
AS $function$
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
END $function$
;
CREATE TRIGGER entry_activity_capture_intent BEFORE INSERT OR DELETE OR UPDATE ON trading.entry_risk_reservation FOR EACH ROW EXECUTE FUNCTION trading.capture_entry_activity_intent();
