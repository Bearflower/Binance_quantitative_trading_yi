\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout='1s';
SET LOCAL statement_timeout='10s';
DO $guard$ BEGIN
 IF obj_description('trading.bootstrap_autovacuum_probe'::regclass,'pg_class') IS DISTINCT FROM
    'bootstrap-autovacuum-probe-v1; disposable synthetic rows; no business data'
 OR (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='trading.bootstrap_autovacuum_probe'::regclass)<>'trading_migration_admin'
 OR NOT EXISTS(SELECT 1 FROM pg_stat_user_tables WHERE relid='trading.bootstrap_autovacuum_probe'::regclass AND autovacuum_count>0 AND autoanalyze_count>0)
 THEN RAISE EXCEPTION 'probe identity or successful native-autovacuum evidence missing'; END IF;
END $guard$;
DROP TABLE trading.bootstrap_autovacuum_probe;
COMMIT;
