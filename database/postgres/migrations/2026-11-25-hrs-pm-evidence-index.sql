-- Run as trading_migration_admin with psql autocommit, never inside BEGIN.
-- Preserve the exact _last_pm_evidence predicate and ordering. Only index keys
-- are stored here, never the large immutable JSON evidence payload.
-- A failed concurrent build can leave an invalid index: verify indisvalid and
-- indisready before accepting or retrying. IF NOT EXISTS alone is not proof.
SET lock_timeout = '5s';
SET statement_timeout = '10min';
CREATE INDEX CONCURRENTLY IF NOT EXISTS hrs_equity_pm_lookup_idx
 ON trading.hrs_equity_evidence (account_id, as_of DESC, available_at DESC)
 WHERE (payload->'result' ? 'ledger_version');
DO $guard$
BEGIN
 IF NOT EXISTS (
   SELECT 1 FROM pg_index
   WHERE indexrelid='trading.hrs_equity_pm_lookup_idx'::regclass
     AND indisvalid AND indisready AND indislive
     AND pg_get_indexdef(indexrelid) =
       'CREATE INDEX hrs_equity_pm_lookup_idx ON trading.hrs_equity_evidence USING btree (account_id, as_of DESC, available_at DESC) WHERE ((payload -> ''result''::text) ? ''ledger_version''::text)'
 ) THEN
   RAISE EXCEPTION 'hrs_equity_pm_lookup_idx is invalid or differs from the reviewed definition';
 END IF;
END $guard$;
RESET statement_timeout;
RESET lock_timeout;
