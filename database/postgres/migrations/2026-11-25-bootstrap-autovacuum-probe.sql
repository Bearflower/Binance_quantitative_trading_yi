-- Run only as the migration administrator in the reviewed repair window.
-- Deliberately fail if an object already exists with this reserved name.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout='1s';
SET LOCAL statement_timeout='10s';
CREATE TABLE trading.bootstrap_autovacuum_probe(id integer PRIMARY KEY,payload text NOT NULL)
 WITH (autovacuum_vacuum_threshold=0,autovacuum_vacuum_scale_factor=0,
       autovacuum_vacuum_insert_threshold=0,autovacuum_vacuum_insert_scale_factor=0,
       autovacuum_analyze_threshold=0,autovacuum_analyze_scale_factor=0);
ALTER TABLE trading.bootstrap_autovacuum_probe OWNER TO trading_migration_admin;
COMMENT ON TABLE trading.bootstrap_autovacuum_probe IS 'bootstrap-autovacuum-probe-v1; disposable synthetic rows; no business data';
INSERT INTO trading.bootstrap_autovacuum_probe SELECT n,'before' FROM generate_series(1,200) n;
UPDATE trading.bootstrap_autovacuum_probe SET payload='after';
COMMIT;
SELECT clock_timestamp(),relid::regclass,n_live_tup,n_dead_tup,last_autovacuum,last_autoanalyze,autovacuum_count,autoanalyze_count
 FROM pg_stat_user_tables WHERE relid='trading.bootstrap_autovacuum_probe'::regclass;
