#!/usr/bin/env python3
"""Audit effective PostgreSQL ACLs; explicitly apply only declared missing grants.

Standard library + psql only. No credential arguments are accepted or printed.
Local mode uses libpq environment; default mode uses the database container.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / 'database/postgres/permissions.json'
TABLE_PRIVS = {'SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'}
SEQ_PRIVS = {'SELECT','UPDATE','USAGE'}
COLUMN_PRIVS = {'SELECT','INSERT','UPDATE','REFERENCES'}
FLAGS = {'rolsuper','rolcreatedb','rolcreaterole','rolreplication','rolbypassrls'}


def identifier(value):
    # PostgreSQL supports Unicode asset names; format(%I) still quotes every name.
    if not isinstance(value, str) or not value.isidentifier() or value != value.lower():
        raise ValueError(f'invalid identifier: {value!r}')


def validate(manifest):
    if manifest.get('version') != 1:
        raise ValueError('unsupported manifest version')
    schemas, roles, profiles = manifest['schemas'], manifest['roles'], manifest['profiles']
    if (not schemas or not roles or not manifest['objects']
            or profiles.get('none') not in ([], {'required': [], 'allowed': []})):
        raise ValueError('empty contract or missing none profile')
    if len(schemas) != len(set(schemas)):
        raise ValueError('duplicate schemas')
    for schema in schemas:
        identifier(schema)
    for name, privs in profiles.items():
        identifier(name)
        if isinstance(privs, list):
            profiles[name] = {'required': privs, 'allowed': privs}
            privs = profiles[name]
        if (not isinstance(privs, dict) or set(privs) != {'required', 'allowed'}
                or not isinstance(privs['required'], list) or not isinstance(privs['allowed'], list)
                or not set(privs['required']) <= set(privs['allowed'])
                or not set(privs['allowed']) <= TABLE_PRIVS | SEQ_PRIVS):
            raise ValueError('invalid privilege profile')
    for role, config in roles.items():
        identifier(role)
        if not set(config.get('allowed_flags', [])) <= FLAGS:
            raise ValueError('invalid role flags')
        if 'database' in config:
            database=config['database']
            if (set(database) != {'required','allowed'}
                    or not set(database['required']) <= set(database['allowed'])
                    or not set(database['allowed']) <= {'CONNECT','TEMPORARY'}):
                raise ValueError('invalid database privileges')
        for schema, privs in config['schemas'].items():
            if schema not in schemas or not set(privs) <= {'USAGE','CREATE'}:
                raise ValueError('invalid schema permission')
    keys = set()
    for rule in manifest['objects']:
        if rule['schema'] not in schemas or rule['kind'] not in ('table','sequence'):
            raise ValueError('invalid object scope')
        if ('name' in rule) == ('pattern' in rule):
            raise ValueError('object must have exactly one name or pattern')
        if 'name' in rule:
            identifier(rule['name'])
        else:
            # Narrow allowlist: anchored, lower-case object names, no SQL regex dialect ambiguity.
            if not re.fullmatch(r'\^[a-z_]+\[a-z0-9_\]\+\$', rule['pattern']):
                raise ValueError('patterns must be anchored prefix + [a-z0-9_]+')
            if rule.get('required'):
                raise ValueError('patterns cannot be required')
        key = (rule['schema'],rule.get('name'),rule.get('pattern'),rule['kind'])
        if key in keys:
            raise ValueError('duplicate rule')
        keys.add(key)
        if 'owner' in rule:
            identifier(rule['owner'])
        valid = SEQ_PRIVS if rule['kind']=='sequence' else TABLE_PRIVS
        for role, profile in rule.get('grants',{}).items():
            if role not in roles or profile not in profiles or not set(profiles[profile]['allowed']) <= valid:
                raise ValueError('invalid grant')
        for role, columns in rule.get('columns',{}).items():
            if role not in roles or rule['kind'] != 'table':
                raise ValueError('invalid column role')
            for column, privs in columns.items():
                identifier(column)
                if not set(privs) <= COLUMN_PRIVS:
                    raise ValueError('invalid column privilege')
        if 'lock' in rule:
            lock=rule['lock']
            if lock['role'] not in roles or 'UPDATE' not in rule.get('columns',{}).get(lock['role'],{}).get(lock['column'],[]):
                raise ValueError('lock requires declared UPDATE column')
            identifier(lock['trigger']); identifier(lock['column'])
            if len(lock['function'].split('.'))!=2:
                raise ValueError('trigger function must be schema qualified')
            for part in lock['function'].split('.'):
                identifier(part)
    if 'bootstrap_identity' in manifest:
        bootstrap=manifest['bootstrap_identity']
        if (bootstrap.get('bootstrap_role') != 'trading_bootstrap_admin'
                or bootstrap.get('application_role') != 'trading_user'
                or bootstrap.get('oid') != 10
                or not isinstance(bootstrap.get('allow_legacy_demoted'),bool)):
            raise ValueError('invalid bootstrap identity contract')
        if bootstrap['bootstrap_role'] in roles:
            raise ValueError('bootstrap administrator must not be a runtime role')
    return manifest


# The function inspects effective ACLs (PUBLIC and inherited roles included).
# Checks are in the same transaction as grants; any postflight issue rolls back.
CHECK_FUNCTION = r'''
CREATE FUNCTION pg_temp.permission_issues(doc jsonb, repair boolean) RETURNS jsonb
LANGUAGE plpgsql AS $body$
DECLARE
 issues jsonb := '[]'; role_name text; role_config jsonb; schema_name text;
 priv text; permitted jsonb; required_privileges jsonb; rule jsonb; matched jsonb; object_row record;
 column_row record; role_row record; column_name text; column_permissions jsonb;
 object_kind text; object_name text; actual boolean; wanted boolean; lock_rule jsonb;
 mutation_rejected boolean;
BEGIN
 IF doc ? 'bootstrap_identity' THEN
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=doc->'bootstrap_identity'->>'bootstrap_role') THEN
   IF NOT EXISTS(SELECT 1 FROM pg_authid WHERE oid=10 AND rolname=doc->'bootstrap_identity'->>'bootstrap_role' AND rolsuper AND NOT rolcanlogin AND rolpassword IS NULL)
      OR EXISTS(SELECT 1 FROM pg_auth_members WHERE roleid=10 OR member=10)
      OR NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=doc->'bootstrap_identity'->>'application_role' AND oid<>10) THEN
    issues := issues || jsonb_build_array(jsonb_build_object('code','bootstrap_identity_mismatch'));
   END IF;
  ELSIF NOT (coalesce((doc->'bootstrap_identity'->>'allow_legacy_demoted')::boolean,false)
        AND EXISTS(SELECT 1 FROM pg_roles WHERE oid=10 AND rolname=doc->'bootstrap_identity'->>'application_role' AND NOT rolsuper)) THEN
   issues := issues || jsonb_build_array(jsonb_build_object('code','bootstrap_identity_missing'));
  END IF;
 END IF;
 FOR role_name,role_config IN SELECT * FROM jsonb_each(doc->'roles') LOOP
  SELECT * INTO role_row FROM pg_roles WHERE rolname=role_name;
  IF NOT FOUND THEN
   IF coalesce((role_config->>'required')::boolean,false) THEN
    issues := issues || jsonb_build_array(jsonb_build_object('code','missing_role','role',role_name));
   END IF;
   CONTINUE;
  END IF;
  FOREACH priv IN ARRAY ARRAY['rolsuper','rolcreatedb','rolcreaterole','rolreplication','rolbypassrls'] LOOP
   IF (to_jsonb(role_row)->>priv)::boolean AND NOT coalesce(role_config->'allowed_flags','[]') ? priv THEN
    issues := issues || jsonb_build_array(jsonb_build_object('code','elevated_role','role',role_name,'privilege',priv));
   END IF;
  END LOOP;
  -- NOINHERIT still permits SET ROLE; managed runtime accounts have no memberships.
  IF EXISTS(SELECT 1 FROM pg_auth_members WHERE member=role_row.oid) THEN
   issues := issues || jsonb_build_array(jsonb_build_object('code','unexpected_membership','role',role_name));
  END IF;
  IF EXISTS(SELECT 1 FROM pg_roles p WHERE p.rolname<>role_name AND (p.rolsuper OR p.rolcreaterole OR p.rolcreatedb OR p.rolreplication OR p.rolbypassrls) AND pg_has_role(role_name,p.oid,'MEMBER')) THEN
   issues := issues || jsonb_build_array(jsonb_build_object('code','elevated_membership','role',role_name));
  END IF;
  IF EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database() AND (datdba=role_row.oid OR has_database_privilege(role_name,oid,'CREATE'))) THEN
   issues := issues || jsonb_build_array(jsonb_build_object('code','unexpected_database_control','role',role_name));
  END IF;
  IF EXISTS(SELECT 1 FROM jsonb_array_elements(doc->'objects') r JOIN pg_roles p ON p.rolname=r->>'owner' WHERE p.rolname<>role_name AND NOT doc->'roles' ? p.rolname AND pg_has_role(role_name,p.oid,'MEMBER')) THEN
   issues := issues || jsonb_build_array(jsonb_build_object('code','owner_role_membership','role',role_name));
  END IF;
  IF role_config ? 'database' THEN
   FOREACH priv IN ARRAY ARRAY['CONNECT','TEMPORARY'] LOOP
    actual := has_database_privilege(role_name,current_database(),priv);
    wanted := role_config->'database'->'required' ? priv;
    permitted := role_config->'database'->'allowed';
    IF actual AND NOT permitted ? priv THEN
     issues := issues || jsonb_build_array(jsonb_build_object('code','unexpected_database_privilege','role',role_name,'privilege',priv));
    END IF;
    IF has_database_privilege(role_name,current_database(),priv||' WITH GRANT OPTION') THEN
     issues := issues || jsonb_build_array(jsonb_build_object('code','unexpected_database_grant_option','role',role_name,'privilege',priv));
    END IF;
    IF wanted AND NOT actual THEN
     IF repair THEN EXECUTE format('GRANT %s ON DATABASE %I TO %I',priv,current_database(),role_name);
     ELSE issues := issues || jsonb_build_array(jsonb_build_object('code','missing_privilege','role',role_name,'object',current_database(),'privilege',priv)); END IF;
    END IF;
   END LOOP;
  END IF;
  FOR schema_name IN SELECT jsonb_array_elements_text(doc->'schemas') LOOP
   permitted := coalesce(role_config->'schemas'->schema_name,'[]');
   IF NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=schema_name) THEN
    IF jsonb_array_length(permitted)>0 THEN
     issues := issues || jsonb_build_array(jsonb_build_object('code','missing_schema','role',role_name,'object',schema_name));
    END IF;
    CONTINUE;
   END IF;
   FOREACH priv IN ARRAY ARRAY['USAGE','CREATE'] LOOP
    actual := has_schema_privilege(role_name,schema_name,priv); wanted := permitted ? priv;
    IF wanted AND NOT actual AND repair THEN
     EXECUTE format('GRANT %s ON SCHEMA %I TO %I',priv,schema_name,role_name); actual:=true;
    END IF;
    IF actual IS DISTINCT FROM wanted THEN
     issues:=issues || jsonb_build_array(jsonb_build_object('code',CASE WHEN wanted THEN 'missing_privilege' ELSE 'unexpected_privilege' END,'role',role_name,'object',schema_name,'privilege',priv));
    END IF;
    IF has_schema_privilege(role_name,schema_name,priv||' WITH GRANT OPTION') AND NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=schema_name AND nspowner=role_row.oid) THEN
     issues:=issues || jsonb_build_array(jsonb_build_object('code','unexpected_grant_option','role',role_name,'object',schema_name,'privilege',priv));
    END IF;
   END LOOP;
  END LOOP;

 END LOOP;
 FOR rule IN SELECT * FROM jsonb_array_elements(doc->'objects') LOOP
  IF coalesce((rule->>'required')::boolean,false) AND NOT EXISTS (
   SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname=rule->>'schema' AND c.relname=rule->>'name'
   AND ((rule->>'kind'='sequence' AND c.relkind='S') OR (rule->>'kind'='table' AND c.relkind IN ('r','p','v','m','f')))
  ) THEN
   issues:=issues || jsonb_build_array(jsonb_build_object('code','missing_object','object',(rule->>'schema')||'.'||(rule->>'name')));
  END IF;
 END LOOP;
 FOR object_row IN
  SELECT c.oid,c.relname,c.relkind,c.relowner,n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE doc->'schemas' ? n.nspname AND c.relkind IN ('r','p','v','m','f','S')
  AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e')
  ORDER BY n.nspname,c.relname
 LOOP
  object_kind:=CASE WHEN object_row.relkind='S' THEN 'sequence' ELSE 'table' END;
  object_name:=object_row.nspname||'.'||object_row.relname;
  SELECT jsonb_agg(r) INTO matched FROM jsonb_array_elements(doc->'objects') r
  WHERE r->>'schema'=object_row.nspname AND r->>'kind'=object_kind
    AND (r->>'name'=object_row.relname OR (r ? 'pattern' AND object_row.relname ~ (r->>'pattern') AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(doc->'objects') exact_rule WHERE exact_rule->>'schema'=object_row.nspname AND exact_rule->>'kind'=object_kind AND exact_rule->>'name'=object_row.relname)));
  IF matched IS NULL OR jsonb_array_length(matched)<>1 THEN
   issues:=issues || jsonb_build_array(jsonb_build_object('code',CASE WHEN matched IS NULL THEN 'unregistered_object' ELSE 'ambiguous_rule' END,'object',object_name));
   CONTINUE;
  END IF;
  rule:=matched->0;
  IF rule ? 'owner' AND rule->>'owner'<>pg_get_userbyid(object_row.relowner) THEN
   issues:=issues || jsonb_build_array(jsonb_build_object('code','owner_drift','object',object_name,'expected',rule->>'owner','actual',pg_get_userbyid(object_row.relowner)));
  END IF;
  lock_rule:=rule->'lock';
  IF lock_rule IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace n ON n.oid=p.pronamespace
   WHERE t.tgrelid=object_row.oid AND t.tgname=lock_rule->>'trigger' AND NOT t.tgisinternal
    AND t.tgenabled IN ('O','A') AND (t.tgtype::int & 58)=58 AND (t.tgtype::int & 1)=0
    AND n.nspname||'.'||p.proname=lock_rule->>'function'
  ) THEN
   issues:=issues || jsonb_build_array(jsonb_build_object('code','missing_immutable_trigger','object',object_name));
  END IF;
  FOR role_name IN SELECT jsonb_object_keys(doc->'roles') LOOP
   IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN CONTINUE; END IF;
   permitted:=doc->'profiles'->coalesce(rule->'grants'->>role_name,'none')->'allowed';
   required_privileges:=doc->'profiles'->coalesce(rule->'grants'->>role_name,'none')->'required';
   FOR priv IN SELECT unnest(CASE WHEN object_kind='sequence' THEN ARRAY['SELECT','UPDATE','USAGE'] ELSE ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'] END) LOOP
    actual:=CASE WHEN object_kind='sequence' THEN has_sequence_privilege(role_name,object_row.oid,priv) ELSE has_table_privilege(role_name,object_row.oid,priv) END;
    wanted:=required_privileges ? priv;
    IF wanted AND NOT actual AND repair THEN
     EXECUTE format('GRANT %s ON %s %I.%I TO %I',priv,CASE WHEN object_kind='sequence' THEN 'SEQUENCE' ELSE 'TABLE' END,object_row.nspname,object_row.relname,role_name);actual:=true;
    END IF;
    IF (actual AND NOT permitted ? priv) OR (wanted AND NOT actual) THEN
     issues:=issues || jsonb_build_array(jsonb_build_object('code',CASE WHEN wanted THEN 'missing_privilege' ELSE 'unexpected_privilege' END,'role',role_name,'object',object_name,'privilege',priv));
    END IF;
    IF object_row.relowner<>(SELECT oid FROM pg_roles WHERE rolname=role_name) AND (CASE WHEN object_kind='sequence' THEN has_sequence_privilege(role_name,object_row.oid,priv||' WITH GRANT OPTION') ELSE has_table_privilege(role_name,object_row.oid,priv||' WITH GRANT OPTION') END) THEN
     issues:=issues || jsonb_build_array(jsonb_build_object('code','unexpected_grant_option','role',role_name,'object',object_name,'privilege',priv));
    END IF;
   END LOOP;
   IF object_kind='sequence' THEN CONTINUE; END IF;
   FOR column_name,column_permissions IN SELECT * FROM jsonb_each(coalesce(rule->'columns'->role_name,'{}')) LOOP
    IF NOT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=object_row.oid AND attname=column_name AND attnum>0 AND NOT attisdropped) THEN
     issues:=issues || jsonb_build_array(jsonb_build_object('code','missing_column','role',role_name,'object',object_name,'column',column_name));
    END IF;
   END LOOP;
   FOR column_row IN SELECT attname,attnum FROM pg_attribute WHERE attrelid=object_row.oid AND attnum>0 AND NOT attisdropped LOOP
    FOREACH priv IN ARRAY ARRAY['SELECT','INSERT','UPDATE','REFERENCES'] LOOP
     -- A permitted table privilege does not permit delegating a single column.
     IF has_column_privilege(role_name,object_row.oid,column_row.attnum,priv||' WITH GRANT OPTION') AND NOT has_table_privilege(role_name,object_row.oid,priv||' WITH GRANT OPTION') THEN
      issues:=issues || jsonb_build_array(jsonb_build_object('code','unexpected_grant_option','role',role_name,'object',object_name,'column',column_row.attname,'privilege',priv));
     END IF;
     IF permitted ? priv OR has_table_privilege(role_name,object_row.oid,priv) THEN CONTINUE; END IF;
     wanted:=coalesce(rule->'columns'->role_name->column_row.attname,'[]') ? priv;
     actual:=has_column_privilege(role_name,object_row.oid,column_row.attnum,priv);
     IF wanted AND NOT actual AND repair THEN
      EXECUTE format('GRANT %s (%I) ON TABLE %I.%I TO %I',priv,column_row.attname,object_row.nspname,object_row.relname,role_name); actual:=true;
     END IF;
     IF actual IS DISTINCT FROM wanted THEN
      issues:=issues || jsonb_build_array(jsonb_build_object('code',CASE WHEN wanted THEN 'missing_privilege' ELSE 'unexpected_column_privilege' END,'role',role_name,'object',object_name,'column',column_row.attname,'privilege',priv));
     END IF;
    END LOOP;
   END LOOP;
  END LOOP;
  -- Probe the actual locking query and statement-level immutability as the
  -- runtime role. WHERE false ensures that no business row is touched.
  IF lock_rule IS NOT NULL AND jsonb_array_length(issues)=0 THEN
   BEGIN
    EXECUTE format('SET LOCAL ROLE %I',lock_rule->>'role');
    EXECUTE format('EXPLAIN SELECT * FROM %I.%I WHERE false FOR UPDATE',object_row.nspname,object_row.relname);
    mutation_rejected:=false;
    BEGIN
     EXECUTE format('UPDATE %I.%I SET %I=%I WHERE false',object_row.nspname,object_row.relname,lock_rule->>'column',lock_rule->>'column');
    EXCEPTION WHEN raise_exception THEN
     mutation_rejected:=true;
    END;
    RESET ROLE;
    IF NOT mutation_rejected THEN
     issues:=issues || jsonb_build_array(jsonb_build_object('code','immutable_probe_failed','object',object_name));
    END IF;
   EXCEPTION WHEN OTHERS THEN
    RESET ROLE;
    issues:=issues || jsonb_build_array(jsonb_build_object('code','lock_probe_failed','object',object_name,'sqlstate',SQLSTATE));
   END;
  END IF;
 END LOOP;
 RETURN issues;
END $body$;
'''


def render_sql(manifest, mode):
    validate(manifest)
    literal = "'" + json.dumps(manifest, ensure_ascii=True).replace("'", "''") + "'::jsonb"
    # Constant advisory key shared by this tool. No broad ownership/default ACL changes.
    sql = "BEGIN; SET LOCAL standard_conforming_strings=on; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='60s';\n"
    if mode=='apply':
        sql += 'SELECT pg_advisory_xact_lock(7183111901);\n'
    sql += CHECK_FUNCTION
    sql += f'CREATE TEMP TABLE acl_result ON COMMIT DROP AS SELECT {literal} AS doc, pg_temp.permission_issues({literal},false) AS issues;\n'
    if mode=='apply':
        sql += '''DO $apply$ DECLARE found_issues jsonb; BEGIN
 SELECT issues INTO found_issues FROM acl_result;
 IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(found_issues) i WHERE i->>'code'<>'missing_privilege') THEN
  PERFORM pg_temp.permission_issues(doc,true) FROM acl_result;
  UPDATE acl_result SET issues=pg_temp.permission_issues(doc,false);
  IF EXISTS(SELECT 1 FROM acl_result WHERE jsonb_array_length(issues)>0) THEN
   RAISE EXCEPTION 'permission postflight failed; all grants rolled back';
  END IF;
 END IF;
END $apply$;
'''
    sql += "SELECT jsonb_build_object('ok',jsonb_array_length(issues)=0,'mode','" + mode + "','issues',issues,'objects_checked',(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE doc->'schemas' ? n.nspname AND c.relkind IN ('r','p','v','m','f','S') AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e'))) FROM acl_result;\n"
    sql += 'COMMIT;\n' if mode=='apply' else 'ROLLBACK;\n'
    return sql


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['audit','apply','sql'])
    parser.add_argument('--manifest',type=Path,default=DEFAULT_MANIFEST)
    parser.add_argument('--local',action='store_true',help='psql via libpq environment; otherwise docker exec')
    parser.add_argument('--container',default='trading_system-postgres')
    parser.add_argument('--database',default='trading_platform')
    parser.add_argument('--admin',default='trading_migration_admin')
    args=parser.parse_args(argv)
    try:
        manifest=validate(json.loads(args.manifest.read_text()))
        if args.mode=='sql':
            print(render_sql(manifest,'apply'));return 0
        command=['psql','-X','-qAt','-v','ON_ERROR_STOP=1']
        if not args.local:
            command=['docker','exec','-i',args.container]+command+['-U',args.admin,'-d',args.database]
        result=subprocess.run(command,input=render_sql(manifest,args.mode),text=True,capture_output=True,timeout=180)
        if result.returncode:
            print('PostgreSQL permission check failed: '+result.stderr.strip(),file=sys.stderr);return 2
        # apply advisory-lock SELECT emits one empty line; JSON is the only nonempty row.
        report=json.loads(result.stdout.strip())
        report['checked_at']=datetime.now(timezone.utc).isoformat()
        report['manifest_sha256']=hashlib.sha256(args.manifest.read_bytes()).hexdigest()
        print(json.dumps(report,ensure_ascii=False,sort_keys=True))
        return 0 if report['ok'] else 1
    except (ValueError,KeyError,TypeError,OSError,subprocess.TimeoutExpired) as exc:
        print(f'Permission contract error: {exc}',file=sys.stderr);return 2


if __name__=='__main__':
    sys.exit(main())
