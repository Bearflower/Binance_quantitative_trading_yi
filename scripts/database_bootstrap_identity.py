#!/usr/bin/env python3
"""Prepare guarded PG15 bootstrap identity split/rollback SQL; never execute it.

Only read-only catalog inventory and the existing audit run during preparation.
Passwords stay inside a server-side DO variable during later authorized SQL.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
import database_permissions as permissions

APPLICATION='trading_user'
BOOTSTRAP='trading_bootstrap_admin'
ROOT=Path(__file__).resolve().parents[1]

FUNCTIONS_SQL=r'''
CREATE FUNCTION pg_temp.identity_owned(role_oid oid) RETURNS jsonb LANGUAGE sql AS $owned$
 WITH objects AS (
 SELECT 'SCHEMA' kind,nspname schema,nspname name,'' arguments,false security_definer
 FROM pg_namespace WHERE nspowner=role_oid AND nspname !~ '^pg_' AND nspname<>'information_schema'
 UNION ALL
 SELECT CASE c.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,n.nspname,c.relname,'',false
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE c.relowner=role_oid AND c.relkind IN ('r','p','S','v','m','f') AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
 AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e')
 UNION ALL
 SELECT CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,n.nspname,p.proname,pg_get_function_identity_arguments(p.oid),p.prosecdef
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE p.proowner=role_oid AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
 AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e')
 UNION ALL
 SELECT CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END,n.nspname,t.typname,'',false
 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
 WHERE t.typowner=role_oid AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND t.typelem=0
 AND (t.typrelid=0 OR EXISTS(SELECT 1 FROM pg_class c WHERE c.oid=t.typrelid AND c.relkind='c'))
 AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_type'::regclass AND d.objid=t.oid AND d.deptype='e')
 ) SELECT coalesce(jsonb_agg(to_jsonb(objects) ORDER BY kind,schema,name,arguments),'[]'::jsonb) FROM objects;
$owned$;
CREATE FUNCTION pg_temp.identity_defaults(role_oid oid) RETURNS jsonb LANGUAGE sql AS $defaults$
 WITH a AS (
 SELECT pg_get_userbyid(d.defaclrole) creator,n.nspname schema,
 CASE d.defaclobjtype WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES' ELSE d.defaclobjtype::text END kind,
 CASE x.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END grantee,
 pg_get_userbyid(x.grantor) grantor,x.is_grantable grantable,array_agg(x.privilege_type ORDER BY x.privilege_type) privileges
 FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace
 CROSS JOIN LATERAL aclexplode(d.defaclacl) x
 WHERE d.defaclrole=role_oid OR x.grantee=role_oid
 GROUP BY d.defaclrole,n.nspname,d.defaclobjtype,x.grantee,x.grantor,x.is_grantable
 ) SELECT coalesce(jsonb_agg(to_jsonb(a) ORDER BY creator,schema,kind,grantee),'[]'::jsonb) FROM a;
$defaults$;
CREATE FUNCTION pg_temp.identity_unsupported(role_oid oid) RETURNS int LANGUAGE sql AS $unsupported$
 SELECT
 (SELECT count(*) FROM pg_policy WHERE role_oid=ANY(polroles))+
 (SELECT count(*) FROM pg_auth_members WHERE member=role_oid OR roleid=role_oid)+
 (SELECT count(*) FROM pg_extension WHERE extowner=role_oid AND extnamespace<>'pg_catalog'::regnamespace)+
 (SELECT count(*) FROM pg_foreign_data_wrapper WHERE fdwowner=role_oid)+
 (SELECT count(*) FROM pg_foreign_server WHERE srvowner=role_oid)+
 (SELECT count(*) FROM pg_event_trigger WHERE evtowner=role_oid)+
 (SELECT count(*) FROM pg_publication WHERE pubowner=role_oid)+
 (SELECT count(*) FROM pg_subscription WHERE subowner=role_oid)+
 (SELECT count(*) FROM pg_largeobject_metadata WHERE lomowner=role_oid)+
 (SELECT count(*) FROM pg_operator WHERE oprowner=role_oid AND oprnamespace<>'pg_catalog'::regnamespace)+
 (SELECT count(*) FROM pg_opclass WHERE opcowner=role_oid AND opcnamespace<>'pg_catalog'::regnamespace)+
 (SELECT count(*) FROM pg_opfamily WHERE opfowner=role_oid AND opfnamespace<>'pg_catalog'::regnamespace)+
 (SELECT count(*) FROM pg_collation WHERE collowner=role_oid AND collnamespace<>'pg_catalog'::regnamespace)+
 (SELECT count(*) FROM pg_conversion WHERE conowner=role_oid AND connamespace<>'pg_catalog'::regnamespace)+
 (SELECT count(*) FROM pg_ts_config WHERE cfgowner=role_oid AND cfgnamespace<>'pg_catalog'::regnamespace)+
 (SELECT count(*) FROM pg_ts_dict WHERE dictowner=role_oid AND dictnamespace<>'pg_catalog'::regnamespace)+
 (SELECT count(*) FROM pg_statistic_ext WHERE stxowner=role_oid)+
 (SELECT count(*) FROM pg_user_mapping WHERE umuser=role_oid)+
 (SELECT count(*) FROM pg_parameter_acl a CROSS JOIN LATERAL aclexplode(a.paracl) x WHERE x.grantee=role_oid)+
 (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relowner=role_oid AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND c.reltablespace<>0)+
 (SELECT count(*) FROM pg_shseclabel WHERE classoid='pg_authid'::regclass AND objoid=role_oid)+
 (SELECT count(*) FROM pg_shdescription WHERE classoid='pg_authid'::regclass AND objoid=role_oid);
$unsupported$;
'''


def literal(value):
    return "'"+str(value).replace("'","''")+"'"


def json_literal(value):
    return literal(json.dumps(value,sort_keys=True))+'::jsonb'


def inventory_sql():
    return FUNCTIONS_SQL+r'''
SELECT jsonb_build_object('database',current_database(),'version',current_setting('server_version_num')::int,
 'owned',pg_temp.identity_owned(10),'defaults',pg_temp.identity_defaults(10),'unsupported',pg_temp.identity_unsupported(10),
 'role',(SELECT jsonb_build_object('oid',oid,'name',rolname,'superuser',rolsuper,'login',rolcanlogin,'bypass_rls',rolbypassrls,'create_db',rolcreatedb,'create_role',rolcreaterole,'replication',rolreplication,'inherit',rolinherit,'connection_limit',rolconnlimit,'password_format',CASE WHEN rolpassword LIKE 'SCRAM-SHA-256$%' THEN 'scram' WHEN rolpassword LIKE 'md5%' THEN 'md5' WHEN rolpassword IS NULL THEN 'none' ELSE 'other' END) FROM pg_authid WHERE oid=10),
 'setting_hash',coalesce((SELECT md5(string_agg(setdatabase::text||':'||setconfig::text,',' ORDER BY setdatabase)) FROM pg_db_role_setting WHERE setrole=10),''),
 'active_clients',(SELECT count(*) FROM pg_stat_activity WHERE usesysid=10 AND backend_type NOT IN ('logical replication launcher','autovacuum launcher','autovacuum worker')),
 'prepared_transactions',(SELECT count(*) FROM pg_prepared_xacts),'preload_libraries',current_setting('shared_preload_libraries'));
'''


def checked_contract(manifest,plan):
    permissions.validate(manifest)
    config=manifest['bootstrap_identity']
    if (config['bootstrap_role'],config['application_role'],config['oid'])!=(BOOTSTRAP,APPLICATION,10):
        raise ValueError('unsupported role identity contract')
    if not 150000<=plan['version']<160000 or plan['database']!=config['database']:
        raise ValueError('requires the reviewed PostgreSQL15 database')
    if plan['role']['name']!=APPLICATION or plan['role']['superuser'] or plan['unsupported'] or plan['prepared_transactions'] or plan['preload_libraries']:
        raise ValueError('unsupported role/dependency/preload/prepared-transaction state')
    expected=[]
    for item in config['default_privileges']:
        if item['kind'] not in ('TABLES','SEQUENCES') or item['grantee']!=APPLICATION or not item['privileges']:
            raise ValueError('unreviewed default privilege class')
        allowed={'SELECT','INSERT','UPDATE','DELETE'} if item['kind']=='TABLES' else {'SELECT','USAGE'}
        if not set(item['privileges'])<=allowed:raise ValueError('default privilege outside reviewed bounds')
        expected.append(dict(item,privileges=sorted(item['privileges']),grantor=item['creator'],grantable=False))
    expected.sort(key=lambda r:(r['creator'],r['schema'],r['kind'],r['grantee']))
    if expected!=plan['defaults']:raise ValueError('default privilege inventory differs from sole contract')
    schemas=sorted(o['name'] for o in plan['owned'] if o['kind']=='SCHEMA')
    routines=sorted((o['schema'],o['name'],o['arguments']) for o in plan['owned'] if o['kind'] in ('FUNCTION','PROCEDURE'))
    types=sorted((o['schema'],o['name']) for o in plan['owned'] if o['kind'] in ('TYPE','DOMAIN'))
    if schemas!=sorted(config['owned_schemas']) or routines!=sorted((r['schema'],r['name'],r['arguments']) for r in config['owned_routines']) or types!=sorted((r['schema'],r['name']) for r in config['owned_types']):
        raise ValueError('non-relation ownership requires explicit contract classification')
    for obj in plan['owned']:
        permissions.identifier(obj['schema']);permissions.identifier(obj['name'])
        if obj['kind'] not in ('SCHEMA','TABLE','SEQUENCE','VIEW','MATERIALIZED VIEW','FOREIGN TABLE','FUNCTION','PROCEDURE','TYPE','DOMAIN'):
            raise ValueError('unreviewed ownership class')
    if any(o['schema'] not in manifest['schemas'] for o in plan['owned']):raise ValueError('owned object outside managed schemas')
    return config


TRANSFORM_SQL=r'''
DO $identity$
DECLARE source_role record; source_oid oid; target_oid oid; target_role text; obj jsonb; settings record; entry text;
 old_creator text; new_creator text; grants text; suffix text; before_setting text; old_password text;
BEGIN
 SELECT * INTO STRICT source_role FROM pg_authid WHERE rolname='trading_user'; source_oid:=source_role.oid; old_password:=source_role.rolpassword;
 IF source_role.rolcanlogin THEN RAISE EXCEPTION 'application login gate must be committed first'; END IF;
 IF source_role.rolsuper OR source_role.rolcreatedb OR source_role.rolcreaterole OR source_role.rolreplication THEN RAISE EXCEPTION 'application attributes are outside the contract'; END IF;
 IF old_password IS NULL OR NOT (old_password LIKE 'SCRAM-SHA-256$%' OR old_password LIKE 'md5%') THEN RAISE EXCEPTION 'unsupported password format'; END IF;
 IF current_setting('server_version_num')::int NOT BETWEEN 150000 AND 159999 THEN RAISE EXCEPTION 'reviewed only on PostgreSQL15'; END IF;
 IF current_database()<>__DATABASE__ OR pg_temp.identity_unsupported(source_oid)<>0 OR EXISTS(SELECT 1 FROM pg_prepared_xacts) THEN RAISE EXCEPTION 'unsupported dependency or database'; END IF;
 IF coalesce((SELECT md5(string_agg(setdatabase::text||':'||setconfig::text,',' ORDER BY setdatabase)) FROM pg_db_role_setting WHERE setrole=source_oid),'')<>__SETTING_HASH__ THEN RAISE EXCEPTION 'reviewed role settings drifted'; END IF;
 IF pg_temp.identity_owned(source_oid)<>__OWNED__ OR pg_temp.identity_defaults(source_oid)<>__DEFAULTS__ THEN RAISE EXCEPTION 'reviewed inventory drifted'; END IF;
 IF EXISTS(SELECT 1 FROM pg_stat_activity WHERE usesysid IN (source_oid,10) AND backend_type NOT IN ('logical replication launcher','autovacuum launcher','autovacuum worker')) THEN RAISE EXCEPTION 'application sessions were not drained'; END IF;
 IF current_setting('shared_preload_libraries')<>'' THEN RAISE EXCEPTION 'unreviewed audit/background extension'; END IF;
 IF __FORWARD__ THEN
  IF source_oid<>10 OR to_regrole('trading_bootstrap_admin') IS NOT NULL THEN RAISE EXCEPTION 'unexpected initial identity'; END IF;
  ALTER ROLE trading_user RENAME TO trading_bootstrap_admin;
  suffix:=CASE WHEN source_role.rolvaliduntil IS NULL THEN '' ELSE format(' VALID UNTIL %L',source_role.rolvaliduntil::text) END;
  EXECUTE format('CREATE ROLE trading_user NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION %s %s CONNECTION LIMIT %s PASSWORD %L%s',CASE WHEN source_role.rolinherit THEN 'INHERIT' ELSE 'NOINHERIT' END,CASE WHEN source_role.rolbypassrls THEN 'BYPASSRLS' ELSE 'NOBYPASSRLS' END,source_role.rolconnlimit,old_password,suffix);
  target_role:='trading_user';
 ELSE
  IF source_oid=10 OR NOT EXISTS(SELECT 1 FROM pg_authid WHERE oid=10 AND rolname='trading_bootstrap_admin' AND rolsuper AND NOT rolcanlogin AND rolpassword IS NULL) THEN RAISE EXCEPTION 'unexpected separated identity'; END IF;
  IF EXISTS(SELECT 1 FROM pg_shdepend WHERE refclassid='pg_authid'::regclass AND refobjid=source_oid AND dbid<>0 AND dbid<>(SELECT oid FROM pg_database WHERE datname=current_database())) THEN RAISE EXCEPTION 'new role acquired cross-database dependencies'; END IF;
  target_role:='trading_bootstrap_admin';
 END IF;
 SELECT oid INTO STRICT target_oid FROM pg_authid WHERE rolname=target_role;
 -- SQL ownership changes update dependent row types, indexes and owned sequences.
 FOR obj IN SELECT value FROM jsonb_array_elements(__OWNED__) ORDER BY CASE value->>'kind' WHEN 'SCHEMA' THEN 0 WHEN 'SEQUENCE' THEN 2 ELSE 1 END LOOP
  IF obj->>'kind'='SCHEMA' THEN EXECUTE format('ALTER SCHEMA %I OWNER TO %I',obj->>'name',target_role);
  ELSIF obj->>'kind' IN ('FUNCTION','PROCEDURE') THEN EXECUTE format('ALTER %s %I.%I(%s) OWNER TO %I',obj->>'kind',obj->>'schema',obj->>'name',obj->>'arguments',target_role);
  ELSE EXECUTE format('ALTER %s %I.%I OWNER TO %I',obj->>'kind',obj->>'schema',obj->>'name',target_role);
  END IF;
 END LOOP;
 -- Clone settings without returning them or their values to the client.
 FOR settings IN SELECT setdatabase,setconfig FROM pg_db_role_setting WHERE setrole=source_oid LOOP
  suffix:=CASE WHEN settings.setdatabase=0 THEN '' ELSE format(' IN DATABASE %I',(SELECT datname FROM pg_database WHERE oid=settings.setdatabase)) END;
  FOREACH entry IN ARRAY settings.setconfig LOOP
   before_setting:=current_setting(split_part(entry,'=',1),true);
   PERFORM set_config(split_part(entry,'=',1),substring(entry FROM position('=' IN entry)+1),true);
   EXECUTE format('ALTER ROLE %I%s SET %I FROM CURRENT',target_role,suffix,split_part(entry,'=',1));
   IF before_setting IS NOT NULL THEN PERFORM set_config(split_part(entry,'=',1),before_setting,true); END IF;
  END LOOP;
 END LOOP;
 IF coalesce((SELECT md5(string_agg(setdatabase::text||':'||setconfig::text,',' ORDER BY setdatabase)) FROM pg_db_role_setting WHERE setrole=target_oid),'')<>__SETTING_HASH__ THEN RAISE EXCEPTION 'role settings were not preserved'; END IF;
 FOR obj IN SELECT * FROM jsonb_array_elements(__DEFAULTS__) LOOP
  old_creator:=CASE WHEN obj->>'creator'='trading_user' AND __FORWARD__ THEN 'trading_bootstrap_admin' ELSE obj->>'creator' END;
  new_creator:=CASE WHEN obj->>'creator'='trading_user' THEN target_role ELSE obj->>'creator' END;
  SELECT string_agg(value,',') INTO grants FROM jsonb_array_elements_text(obj->'privileges');
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE %s ON %s FROM %I',old_creator,obj->>'schema',grants,obj->>'kind',CASE WHEN __FORWARD__ THEN 'trading_bootstrap_admin' ELSE 'trading_user' END);
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT %s ON %s TO %I',new_creator,obj->>'schema',grants,obj->>'kind',target_role);
 END LOOP;
 IF __FORWARD__ THEN
  IF (SELECT rolpassword FROM pg_authid WHERE oid=target_oid) IS DISTINCT FROM old_password THEN RAISE EXCEPTION 'credential preservation failed'; END IF;
  IF EXISTS(SELECT 1 FROM pg_stat_activity WHERE usesysid=10 AND backend_type NOT IN ('logical replication launcher','autovacuum launcher','autovacuum worker')) THEN RAISE EXCEPTION 'old application session appeared before promotion'; END IF;
  FOR settings IN SELECT setdatabase FROM pg_db_role_setting WHERE setrole=10 LOOP
   suffix:=CASE WHEN settings.setdatabase=0 THEN '' ELSE format(' IN DATABASE %I',(SELECT datname FROM pg_database WHERE oid=settings.setdatabase)) END;
   EXECUTE format('ALTER ROLE trading_bootstrap_admin%s RESET ALL',suffix);
  END LOOP;
  ALTER ROLE trading_bootstrap_admin SUPERUSER NOLOGIN NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 0 PASSWORD NULL;
 ELSE
  IF pg_temp.identity_owned(source_oid)<>'[]'::jsonb OR EXISTS(SELECT 1 FROM pg_database WHERE datdba=source_oid) OR EXISTS(SELECT 1 FROM pg_tablespace WHERE spcowner=source_oid) THEN RAISE EXCEPTION 'new role still owns objects; refuse cleanup'; END IF;
  -- All reviewed objects have already been explicitly moved back. This only
  -- removes the new identity's remaining grants; it cannot drop owned data.
  DROP OWNED BY trading_user;
  DROP ROLE trading_user;
  ALTER ROLE trading_bootstrap_admin RENAME TO trading_user;
  suffix:=CASE WHEN source_role.rolvaliduntil IS NULL THEN '' ELSE format(' VALID UNTIL %L',source_role.rolvaliduntil::text) END;
  EXECUTE format('ALTER ROLE trading_user NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION %s %s CONNECTION LIMIT %s PASSWORD %L%s',CASE WHEN source_role.rolinherit THEN 'INHERIT' ELSE 'NOINHERIT' END,CASE WHEN source_role.rolbypassrls THEN 'BYPASSRLS' ELSE 'NOBYPASSRLS' END,source_role.rolconnlimit,old_password,suffix);
  IF (SELECT rolpassword FROM pg_authid WHERE oid=10) IS DISTINCT FROM old_password THEN RAISE EXCEPTION 'rollback credential preservation failed'; END IF;
 END IF;
END $identity$;
'''


def render(manifest,plan,direction):
    config=checked_contract(manifest,plan)
    if direction not in ('forward','rollback'):raise ValueError('invalid direction')
    transform=TRANSFORM_SQL.replace('__DATABASE__',literal(config['database'])).replace('__OWNED__',json_literal(plan['owned'])).replace('__DEFAULTS__',json_literal(plan['defaults'])).replace('__FORWARD__','true' if direction=='forward' else 'false').replace('__SETTING_HASH__',literal(plan['setting_hash']))
    preaudit=permissions.CHECK_FUNCTION.replace('pg_temp.permission_issues','pg_temp.identity_preflight_issues')
    preaudit+='\nDO $pre$ BEGIN IF jsonb_array_length(pg_temp.identity_preflight_issues('+json_literal(manifest)+",false))<>0 THEN RAISE EXCEPTION 'permission preflight failed'; END IF; END $pre$;\n"
    apply=permissions.render_sql(manifest,'apply')
    assert apply.startswith('BEGIN;') and apply.rstrip().endswith('COMMIT;')
    apply=apply[len('BEGIN;'):].rsplit('COMMIT;',1)[0]
    # The existing tool reports non-missing issues without raising. Here any
    # issue must abort the whole identity transaction, not commit a partial split.
    guard="DO $post$ BEGIN IF EXISTS(SELECT 1 FROM acl_result WHERE jsonb_array_length(issues)<>0) THEN RAISE EXCEPTION 'identity postflight failed'; END IF; END $post$;\n"
    return "\\set ON_ERROR_STOP on\n\\set VERBOSITY sqlstate\nDO $gate$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trading_user' AND NOT rolcanlogin AND NOT rolsuper) THEN RAISE EXCEPTION 'committed NOLOGIN gate required'; END IF; END $gate$;\nSELECT pg_sleep(extract(epoch FROM current_setting('authentication_timeout')::interval)::double precision);\nBEGIN; SET LOCAL standard_conforming_strings=on; SET LOCAL search_path=pg_catalog; SET LOCAL lock_timeout='1s'; SET LOCAL statement_timeout='120s'; SET LOCAL log_statement='none'; SET LOCAL log_min_error_statement='panic'; SET LOCAL log_error_verbosity='terse'; SET LOCAL client_min_messages='warning';\nSELECT pg_advisory_xact_lock(7183111925);\n"+FUNCTIONS_SQL+preaudit+transform+apply+guard+'COMMIT;\n'


GATE_SQL="""\\set ON_ERROR_STOP on
BEGIN;
SELECT pg_advisory_xact_lock(7183111925);
DO $guard$ BEGIN
 IF current_user='trading_user' OR NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=current_user AND rolsuper)
 OR NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trading_user' AND NOT rolsuper)
 THEN RAISE EXCEPTION 'independent migration administrator and restricted application role required'; END IF;
END $guard$;
ALTER ROLE trading_user NOLOGIN;
COMMIT;
SELECT clock_timestamp() AS gate_committed_at;
"""
DRAIN_SQL="""\\set ON_ERROR_STOP on
DO $guard$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trading_user' AND NOT rolcanlogin AND NOT rolsuper) THEN RAISE EXCEPTION 'committed login gate required'; END IF; END $guard$;
-- Quiesce writers before this step. Never cancel an active business transaction.
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE usesysid=(SELECT oid FROM pg_roles WHERE rolname='trading_user')
 AND backend_type='client backend' AND state='idle';
SELECT pg_sleep(0.2);
SELECT pid,datname,application_name,backend_type,state,clock_timestamp()-xact_start AS transaction_age
 FROM pg_stat_activity WHERE usesysid=(SELECT oid FROM pg_roles WHERE rolname='trading_user')
 AND backend_type NOT IN ('logical replication launcher','autovacuum launcher','autovacuum worker');
DO $drain$ BEGIN IF EXISTS(SELECT 1 FROM pg_stat_activity WHERE usesysid=(SELECT oid FROM pg_roles WHERE rolname='trading_user') AND backend_type NOT IN ('logical replication launcher','autovacuum launcher','autovacuum worker')) THEN RAISE EXCEPTION 'active application sessions remain; wait for safe transaction completion and repeat drain'; END IF; END $drain$;
"""


def reopen_sql(manifest):
    body=permissions.render_sql(manifest,'audit')
    assert body.rstrip().endswith('ROLLBACK;')
    return "\\set ON_ERROR_STOP on\n"+body.rsplit('ROLLBACK;',1)[0]+"DO $guard$ BEGIN IF EXISTS(SELECT 1 FROM acl_result WHERE jsonb_array_length(issues)<>0) THEN RAISE EXCEPTION 'cannot reopen login before permission audit passes'; END IF; END $guard$;\nALTER ROLE trading_user LOGIN;\nCOMMIT;\n"


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,default=ROOT/'database/postgres/permissions.json')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--local',action='store_true')
    parser.add_argument('--database',default='trading_platform')
    parser.add_argument('--container',default='trading_system-postgres')
    args=parser.parse_args()
    manifest=json.loads(args.manifest.read_text())
    command=['psql','-XqAt','-v','ON_ERROR_STOP=1','-v','VERBOSITY=sqlstate','-U','trading_migration_admin','-d',args.database]
    if not args.local:command=['docker','exec','-i',args.container]+command
    audit_command=[sys.executable,str(Path(__file__).with_name('database_permissions.py')),'audit','--manifest',str(args.manifest),'--database',args.database]
    audit_command+=['--local'] if args.local else ['--container',args.container]
    audit_env=dict(os.environ,PGDATABASE=args.database,PGUSER='trading_migration_admin') if args.local else None
    audited=subprocess.run(audit_command,text=True,capture_output=True,timeout=185,env=audit_env)
    if audited.returncode or not json.loads(audited.stdout).get('ok'):
        raise ValueError('permission audit must pass before preparing migration artifacts')
    result=subprocess.run(command,input=inventory_sql(),text=True,capture_output=True,timeout=60)
    if result.returncode:raise RuntimeError('catalog inventory failed (server output withheld)')
    plan=json.loads(result.stdout)
    databases=subprocess.run(command,input="SELECT jsonb_agg(jsonb_build_object('name',datname,'allow_connections',datallowconn) ORDER BY datname) FROM pg_database;",text=True,capture_output=True,timeout=30)
    if databases.returncode:raise RuntimeError('database inventory failed')
    plan['other_databases']=[]
    for database in json.loads(databases.stdout):
        if database['name']==args.database:continue
        entry=dict(database)
        if database['allow_connections']:
            other_command=command[:-1]+[database['name']]
            other=subprocess.run(other_command,input=inventory_sql(),text=True,capture_output=True,timeout=60)
            if other.returncode:raise RuntimeError('other database catalog inventory failed')
            state=json.loads(other.stdout)
            business=subprocess.run(other_command,input="SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema';",text=True,capture_output=True,timeout=30)
            if business.returncode or int(business.stdout.strip())!=0:
                raise ValueError('other database contains non-system relations; direct ACLs require separate review')
            entry.update(owned_objects=len(state['owned']),default_acl_groups=len(state['defaults']),unsupported=state['unsupported'],non_system_relations=0)
            if state['owned'] or state['defaults'] or state['unsupported']:
                raise ValueError('other database has application dependencies; multi-database repair requires separate review')
        else:
            if database['name']!='template0':raise ValueError('uninspected non-template database')
            entry['note']='template0 remains closed and entirely owned by the unchanged bootstrap OID'
        plan['other_databases'].append(entry)
    checked_contract(manifest,plan)
    args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name,content in [('plan.json',json.dumps(plan,indent=2)+'\n'),('forward.sql',render(manifest,plan,'forward')),('rollback.sql',render(manifest,plan,'rollback')),('gate.sql',GATE_SQL),('drain-idle.sql',DRAIN_SQL),('reopen.sql',reopen_sql(manifest))]:
        path=args.output/name
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f:f.write(content)
    print(json.dumps({'prepared':True,'owned_objects':len(plan['owned']),'default_acl_groups':len(plan['defaults']),'active_clients':plan['active_clients'],'manifest_sha256':hashlib.sha256(args.manifest.read_bytes()).hexdigest(),'sql_executed':False}))


if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,TypeError,RuntimeError,OSError,subprocess.TimeoutExpired) as exc:
        print(json.dumps({'prepared':False,'error_type':type(exc).__name__,'message':str(exc) if isinstance(exc,ValueError) else 'preparation failed; raw connection output withheld'}));sys.exit(1)
