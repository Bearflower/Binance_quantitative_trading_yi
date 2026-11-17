-- Default PM allocation observations. Run as a dedicated schema owner, never
-- trading_user. No capital before this installation's BASELINE is inferred.
BEGIN;
SET LOCAL TIME ZONE 'UTC';
CREATE SCHEMA IF NOT EXISTS trading;
-- Blocks concurrent source writes throughout trigger installation and BASELINE.
LOCK TABLE public.capital_allocation IN ACCESS EXCLUSIVE MODE;

DO $$ BEGIN
    IF current_user = 'trading_user' THEN
        RAISE EXCEPTION 'Install capital history as the schema owner, not trading_user';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS trading.capital_allocation_history (
    id bigserial PRIMARY KEY,
    account_id text NOT NULL DEFAULT 'default' CHECK (account_id = 'default'),
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    operation text NOT NULL CHECK (operation IN ('BASELINE', 'INSERT', 'UPDATE', 'DELETE')),
    allocations jsonb NOT NULL CHECK (jsonb_typeof(allocations) = 'array')
);
CREATE UNIQUE INDEX IF NOT EXISTS capital_history_one_baseline
    ON trading.capital_allocation_history(account_id) WHERE operation = 'BASELINE';
CREATE INDEX IF NOT EXISTS capital_history_account_observed
    ON trading.capital_allocation_history(account_id, observed_at DESC, id DESC);

-- Reinstalling healthy audit is safe under the source lock. A damaged existing
-- audit cannot be silently repaired: a new BASELINE would hide an unknown gap.
DO $$ DECLARE healthy boolean;
BEGIN
    IF (SELECT pg_get_userbyid(relowner) = 'trading_user'
        FROM pg_catalog.pg_class WHERE oid = 'trading.capital_allocation_history'::regclass) THEN
        RAISE EXCEPTION 'Runtime trading_user must not own capital history';
    END IF;
    IF EXISTS (SELECT FROM trading.capital_allocation_history) THEN
        SELECT count(*) = 4 INTO healthy
        FROM pg_catalog.pg_trigger AS t
        JOIN pg_catalog.pg_proc AS p ON p.oid = t.tgfoid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
        WHERE NOT t.tgisinternal AND t.tgenabled = 'A' AND n.nspname = 'trading'
          AND (t.tgrelid, t.tgname, t.tgtype::int, p.proname) IN (
            ('public.capital_allocation'::regclass, 'capital_history_serialize', 30, 'serialize_capital_allocation'),
            ('public.capital_allocation'::regclass, 'capital_history_capture', 28, 'capture_capital_allocation'),
            ('public.capital_allocation'::regclass, 'capital_history_no_truncate', 34, 'reject_capital_history_mutation'),
            ('trading.capital_allocation_history'::regclass, 'capital_history_immutable', 58, 'reject_capital_history_mutation')
          );
        IF NOT healthy OR NOT EXISTS (
            SELECT FROM trading.capital_allocation_history WHERE operation = 'BASELINE'
        ) THEN
            RAISE EXCEPTION 'Capital audit continuity unavailable; investigate gap before reinstalling';
        END IF;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION trading.reject_capital_history_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$ BEGIN
    RAISE EXCEPTION 'Capital history is append-only; source/history TRUNCATE is prohibited';
END $$;

-- A full-table snapshot must include every previously committed source write.
-- Serialize writers until transaction end. READ COMMITTED gives the volatile
-- AFTER trigger's SELECT a fresh snapshot after waiting for a previous writer.
-- Higher-isolation snapshots cannot prove this and therefore reject the write.
CREATE OR REPLACE FUNCTION trading.serialize_capital_allocation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$ BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'Capital allocation auditing requires read committed isolation';
    END IF;
    PERFORM pg_advisory_xact_lock(20261117, 17001);
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION trading.capture_capital_allocation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog SET timezone = 'UTC' AS $$
DECLARE observed timestamptz;
BEGIN
    -- Preserve observation order even if the host clock steps backwards.
    SELECT greatest(clock_timestamp(), max(observed_at) + interval '1 microsecond')
        INTO observed FROM trading.capital_allocation_history;
    INSERT INTO trading.capital_allocation_history(account_id, observed_at, operation, allocations)
    SELECT 'default', observed, TG_OP,
           coalesce(jsonb_agg(to_jsonb(allocation) ORDER BY allocation.id), '[]'::jsonb)
    FROM public.capital_allocation AS allocation;
    RETURN NULL;
END $$;

-- CREATE OR REPLACE keeps the prior function owner; explicitly restore the
-- migration owner's authority before revoking runtime direct access.
DO $$ BEGIN
    EXECUTE format('ALTER FUNCTION trading.serialize_capital_allocation() OWNER TO %I', current_user);
    EXECUTE format('ALTER FUNCTION trading.capture_capital_allocation() OWNER TO %I', current_user);
    EXECUTE format('ALTER FUNCTION trading.reject_capital_history_mutation() OWNER TO %I', current_user);
END $$;
REVOKE ALL ON FUNCTION trading.serialize_capital_allocation() FROM PUBLIC, trading_user;
REVOKE ALL ON FUNCTION trading.capture_capital_allocation() FROM PUBLIC, trading_user;
REVOKE ALL ON FUNCTION trading.reject_capital_history_mutation() FROM PUBLIC, trading_user;

DROP TRIGGER IF EXISTS capital_history_serialize ON public.capital_allocation;
CREATE TRIGGER capital_history_serialize BEFORE INSERT OR UPDATE OR DELETE
    ON public.capital_allocation FOR EACH STATEMENT
    EXECUTE FUNCTION trading.serialize_capital_allocation();
ALTER TABLE public.capital_allocation ENABLE ALWAYS TRIGGER capital_history_serialize;
DROP TRIGGER IF EXISTS capital_history_capture ON public.capital_allocation;
CREATE TRIGGER capital_history_capture AFTER INSERT OR UPDATE OR DELETE
    ON public.capital_allocation FOR EACH STATEMENT
    EXECUTE FUNCTION trading.capture_capital_allocation();
ALTER TABLE public.capital_allocation ENABLE ALWAYS TRIGGER capital_history_capture;
DROP TRIGGER IF EXISTS capital_history_no_truncate ON public.capital_allocation;
CREATE TRIGGER capital_history_no_truncate BEFORE TRUNCATE ON public.capital_allocation
    FOR EACH STATEMENT EXECUTE FUNCTION trading.reject_capital_history_mutation();
ALTER TABLE public.capital_allocation ENABLE ALWAYS TRIGGER capital_history_no_truncate;
DROP TRIGGER IF EXISTS capital_history_immutable ON trading.capital_allocation_history;
CREATE TRIGGER capital_history_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON trading.capital_allocation_history FOR EACH STATEMENT
    EXECUTE FUNCTION trading.reject_capital_history_mutation();
ALTER TABLE trading.capital_allocation_history ENABLE ALWAYS TRIGGER capital_history_immutable;

INSERT INTO trading.capital_allocation_history(account_id, operation, allocations)
SELECT 'default', 'BASELINE',
       coalesce(jsonb_agg(to_jsonb(allocation) ORDER BY allocation.id), '[]'::jsonb)
FROM public.capital_allocation AS allocation
HAVING NOT EXISTS (SELECT FROM trading.capital_allocation_history WHERE operation = 'BASELINE');

REVOKE ALL ON trading.capital_allocation_history FROM PUBLIC, trading_user;
REVOKE ALL ON SEQUENCE trading.capital_allocation_history_id_seq FROM PUBLIC, trading_user;
GRANT USAGE ON SCHEMA trading TO trading_user;
GRANT SELECT ON trading.capital_allocation_history TO trading_user;
COMMIT;
