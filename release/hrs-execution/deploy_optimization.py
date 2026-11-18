"""Controlled HRS-only rollout. Run on prod with immutable CI digests, after review.

Default prepares a reviewable overlay and performs read-only checks. --apply
installs additive migrations and replaces only HRS/market-cap plus a DB-only
acceptance reporter. Credentials are generated/consumed only on the server.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from urllib.parse import quote, urlparse

READER_TABLES = ('hrs.hrs_decision_evidence', 'trading.hrs_equity_segment',
    'trading.hrs_loss_gate_state', 'trading.hrs_equity_evidence',
    'trading.execution_fact_fill', 'trading.execution_fact_income',
    'trading.execution_fact_cursor', 'trading.trade_records',
    'trading.reviewed_income_attribution', 'trading.income_ingestion_attempt')
MIGRATIONS = ('2026-11-17-hrs-risk-ledger-loss-gates.sql',
    '2026-11-17-hrs-equity-service.sql', '2026-11-17-hrs-allocation-event-audit.sql',
    '2026-11-17-hrs-equity-reconciliation.sql', '2026-11-17-hrs-funding-publication.sql',
    '2026-11-17-hrs-decision-evidence.sql', '2026-11-17-hrs-frozen-exit-plan.sql',
    '2026-11-17-hrs-condition-account-scope.sql')
PUBLIC_FLAGS = {'HRS_EVIDENCE_CAPTURE_ENABLED','HRS_EQUITY_GATE_ENABLED',
    'ENTRY_EQUITY_GATE_STRATEGIES','HRS_EQUITY_ALLOW_INCEPTION',
    'HRS_EQUITY_REFRESH_SECONDS','HRS_ACCEPTANCE_REPORT_ENABLED'}
REPORTER = 'trading_system-hrs-acceptance'
TARGETS = {'trading_system-hrs':'hrs-strategy',
           'trading_system-market-cap':'market-cap-service',REPORTER:'hrs-acceptance-reporter'}
SAFE_ERRORS = frozenset({'candidate_process_failed','candidate_initialization_timeout',
    'candidate_inspection_failed','candidate_image_mismatch','initialization_unproven',
    'unrelated_container_changed','collector_projection_missing','initialization_response_invalid',
    'database_permission_denied','database_authentication_failed','database_connection_failed',
    'database_relation_missing','configuration_file_missing','configuration_invalid',
    'initialization_probe_failed','command_failed','command_timeout','unclassified_error',
    'timeout_recovery_pending','unsettled_entry_reservation','local_exchange_position_disagreement',
    'hard_stop_not_proven','preflight_write_forbidden'})
SAFE_STAGES = frozenset({'collector_connect','collector_projection_read','writer_connect',
    'writer_evidence_read','loaded_configuration_read','complete'})
SAFE_GATE_REASONS = frozenset({'daily_origin_missing','rolling_origin_missing',
    'daily_loss_limit','rolling_loss_limit','inception_history_only','history_missing',
    'source_observation_stale','source_collection_incomplete','projection_publication_failed',
    'InsufficientPrivilegeError','UndefinedTableError','InvalidPasswordError','ConnectionDoesNotExistError',
    'funding_schedule_coverage_gap','funding_settlement_pending','funding_eligibility_window_incomplete',
    'funding_eligibility_inventory_ambiguous','inventory_owner_path_conflict',
    'allocation_event_history_unrecorded_flow','entry_submission_requires_equity_refresh'})


def safe_error(exc):
    """Never persist arbitrary exception text, SQL, subprocess stderr or argv."""
    types={'RuntimeError','ValueError','KeyError','TypeError','StopIteration',
           'TimeoutExpired','KeyboardInterrupt','SystemExit','OSError'}
    name=type(exc).__name__
    code='unclassified_error'
    if isinstance(exc,RuntimeError) and str(exc) in SAFE_ERRORS:code=str(exc)
    elif isinstance(exc,subprocess.TimeoutExpired):code='command_timeout'
    result=dict(error_type=name if name in types else 'Exception',error_code=code)
    if isinstance(exc,RuntimeError):
        match=re.fullmatch(r'(?:docker|psql) failed with exit (-?\d{1,5})',str(exc))
        if match:result.update(error_code='command_failed',exit_code=int(match[1]))
    return result


def safe_gate(gate):
    if not isinstance(gate,dict):return None
    reasons=gate.get('reasons',[])
    if not isinstance(reasons,list):reasons=[None]
    known=[reason for reason in reasons if isinstance(reason,str) and reason in SAFE_GATE_REASONS]
    omitted=gate.get('unknown_reason_count',0)
    if type(omitted) is not int or not 0<=omitted<=10000:omitted=0
    return dict(ready=gate.get('ready') if type(gate.get('ready')) is bool else None,
        allowed=gate.get('allowed') if type(gate.get('allowed')) is bool else None,
        reasons=known,unknown_reason_count=len(reasons)-len(known)+omitted)


def safe_container(container):
    state=container.get('State',{})
    status=state.get('Status');health=state.get('Health',{}).get('Status')
    result=dict(status=status if status in {'created','running','paused','restarting','removing','exited','dead'} else 'unknown',
        health=health if health in {'starting','healthy','unhealthy'} else 'unknown')
    if type(state.get('ExitCode')) is int:result['exit_code']=state['ExitCode']
    return result


def safe_initialization(data,started_at,release_sha,expected_config_hash):
    if not isinstance(data,dict):return dict(status='unproven',error_code='initialization_response_invalid')
    result=dict(status='error' if data.get('status')=='error' else 'observed')
    if isinstance(data.get('stage'),str) and data['stage'] in SAFE_STAGES:result['stage']=data['stage']
    if isinstance(data.get('error_code'),str) and data['error_code'] in SAFE_ERRORS:result['error_code']=data['error_code']
    gate=safe_gate(data.get('gate'))
    if gate is not None:result['gate']=gate
    result['release_matches']=data.get('release_sha')==release_sha
    loaded=data.get('loaded_config')
    result['configuration_matches']=isinstance(loaded,dict) and loaded.get('strategy')=='hrs' and loaded.get('config_hash')==expected_config_hash
    for key in ('configuration_fresh','collector_fresh','writer_fresh'):result[key]=False
    try:
        started=utc(started_at);checked=utc(data['checked_at'])
        result['configuration_fresh']=datetime.fromtimestamp(data['config_mtime'],timezone.utc)>=started
        for key,value in (('collector_fresh',data.get('gate',{}).get('updated_at')),('writer_fresh',data.get('writer_recorded_at'))):
            try:result[key]=started<=utc(value)<=checked and (checked-utc(value)).total_seconds()<=120
            except (ValueError,TypeError,OverflowError):pass
    except (KeyError,ValueError,TypeError,OverflowError,AttributeError):pass
    return result


def actual_volumes(overlay, mounts):
    volumes=[]
    for mount in mounts:
        if mount['Type'] not in ('bind','volume'):
            raise ValueError('unsupported_actual_mount_type')
        source=mount['Source']
        if mount['Type']=='volume':
            source=mount['Name']
            overlay.setdefault('volumes',{})[source]={'external':True,'name':source}
        volumes.append(dict(type=mount['Type'],source=source,target=mount['Destination'],read_only=not mount['RW']))
    return volumes


def release_overlay(previous, hrs_image, hub_image, mounts):
    overlay = deepcopy(previous)
    services = overlay.setdefault('services', {})
    service = services.setdefault('hrs-strategy', {})
    service['image'] = hrs_image
    service.setdefault('environment', {}).update(HRS_EVIDENCE_CAPTURE_ENABLED='1',
        HRS_EQUITY_GATE_ENABLED='1', ENTRY_EQUITY_GATE_STRATEGIES='hrs',
        HRS_EQUITY_ALLOW_INCEPTION='true', HRS_EQUITY_REFRESH_SECONDS='30',
        HRS_ACCEPTANCE_REPORT_ENABLED='0')
    service['volumes'] = actual_volumes(overlay,mounts)
    services.setdefault('market-cap-service', {})['image'] = hub_image
    return overlay


def reader_grants(database):
    identifier = '"'+database.replace('"','""')+'"'
    return (f'GRANT CONNECT ON DATABASE {identifier} TO hrs_acceptance_reader;\n'
        'GRANT USAGE ON SCHEMA hrs,trading TO hrs_acceptance_reader;\n'
        'GRANT SELECT ON '+','.join(READER_TABLES)+' TO hrs_acceptance_reader;\n'
        'ALTER ROLE hrs_acceptance_reader SET default_transaction_read_only=on;\n')


def migration_sql(folder, hashes, tail=''):
    """Hash the uploaded bytes, then keep one outer transaction for all eight.

    Only standalone transaction wrapper lines in these explicitly trusted files
    are normalized. PL/pgSQL BEGIN blocks and all other SQL remain untouched.
    """
    if not isinstance(hashes,dict) or set(hashes)!=set(MIGRATIONS):
        raise ValueError('migration_hash_inventory_mismatch')
    parts=[]
    for name in MIGRATIONS:
        raw=(Path(folder)/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=hashes[name]:
            raise ValueError('migration_hash_mismatch:'+name)
        parts.append('\n'.join(line for line in raw.decode('utf-8').splitlines()
            if not re.fullmatch(r'\s*(?:BEGIN|COMMIT)\s*;\s*',line,re.IGNORECASE)))
    return "BEGIN;\nSET LOCAL lock_timeout='5s';\nSET LOCAL statement_timeout='30s';\n"+'\n'.join(parts)+'\n'+tail+'\nCOMMIT;\n'


def environment_drift(actual, image_environment, compose_environment, release_sha):
    resolved=dict(image_environment)
    for key,value in compose_environment.items():
        if value is None:
            resolved.pop(key,None)
        else:
            resolved[key]=str(value)
    drift={key for key in set(actual)|set(resolved)
        if key not in PUBLIC_FLAGS|{'HRS_RELEASE_SHA'} and actual.get(key)!=resolved.get(key)}
    if resolved.get('HRS_RELEASE_SHA')!=release_sha:
        drift.add('HRS_RELEASE_SHA')
    return sorted(drift)


def utc(value):
    if isinstance(value,str):value=datetime.fromisoformat(value.replace('Z','+00:00'))
    if not isinstance(value,datetime) or value.tzinfo is None:
        raise ValueError('aware_timestamp_required')
    return value.astimezone(timezone.utc)


def initialized_proof(data, started_at, release_sha, expected_config_hash):
    try:
        started=utc(started_at);checked=utc(data['checked_at'])
        gate=data['gate'];updated=utc(gate['updated_at']);written=utc(data['writer_recorded_at'])
        if (data['release_sha']!=release_sha or data['loaded_config'].get('strategy')!='hrs'
            or data['loaded_config']['config_hash']!=expected_config_hash
            or datetime.fromtimestamp(data['config_mtime'],timezone.utc)<started
            or not started<=updated<=checked or not started<=written<=checked
            or (checked-updated).total_seconds()>120 or (checked-written).total_seconds()>120):
            raise ValueError('source_identity_or_freshness')
        return dict(release_sha=release_sha,config_hash=expected_config_hash,
            collector_updated_at=updated.isoformat(),writer_recorded_at=written.isoformat(),
            **safe_gate(gate))
    except (KeyError,TypeError,ValueError,OverflowError) as exc:
        raise RuntimeError('initialization_unproven') from exc


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+'.tmp')
    with open(temp, 'w') as handle:
        os.chmod(temp, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    os.replace(temp, path)


def run(args, *, input=None, timeout=180):
    result = subprocess.run([str(arg) for arg in args], input=input, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        # Docker/SQL stderr can contain rendered environment values. Do not emit it.
        raise RuntimeError(f'{args[0]} failed with exit {result.returncode}')
    return result.stdout.strip()


def inspect(name):
    return json.loads(run(['docker', 'inspect', name]))[0]


def environment(container):
    return dict(item.split('=',1) for item in container['Config'].get('Env',[]) if '=' in item)


def compose(root, files):
    result = ['docker','compose','--project-directory',str(root),'-p','trading_system']
    for file in files:
        result += ['-f',str(file)]
    return result


PREFLIGHT = '''import asyncio,os,json,asyncpg
from decimal import Decimal
from shared.binance_api import BinanceClient
async def main():
 c=await asyncpg.connect(host=os.environ['DATABASE_HOST'],port=int(os.getenv('DATABASE_PORT','5432')),database=os.environ['DATABASE_NAME'],user=os.environ['DATABASE_USER'],password=os.environ['DATABASE_PASSWORD'],server_settings={'default_transaction_read_only':'on'})
 account=os.getenv('TRADE_ACCOUNT_ID','default')
 local=await c.fetch('SELECT * FROM hrs.hrs_positions')
 timeout=await c.fetch("SELECT key FROM hrs.hrs_meta WHERE key LIKE 'hrs-timeout:%'")
 if timeout: raise RuntimeError('timeout_recovery_pending')
 rows=await c.fetch("SELECT * FROM trading.entry_risk_reservation WHERE account_id=$1 AND strategy_id='hrs' AND state<>'released'",account)
 api=BinanceClient(os.environ['BINANCE_API_KEY'],os.environ['BINANCE_API_SECRET'])
 original=api._request
 async def readonly(method,*args,**kwargs):
  if method.upper()!='GET': raise RuntimeError('preflight_write_forbidden')
  return await original(method,*args,**kwargs)
 api._request=readonly
 proofs=[]
 async with api:
  inventory=await api.get_position()
  independent=await api.get_um_account_positions()
  for row in rows:
   order=await api.get_order_by_client_id(row['symbol'],row['client_order_id'])
   if order.get('status')!='FILLED' or Decimal(str(order.get('executedQty')))!=Decimal(str(row['quantity'])): raise RuntimeError('unsettled_entry_reservation')
  for row in local:
   symbol=row['symbol'];qty=Decimal(str(row.get('remaining_quantity') or row['quantity']))
   amounts=[[Decimal(str(p['positionAmt'])) for p in data if p.get('symbol')==symbol and Decimal(str(p['positionAmt']))!=0] for data in (inventory,independent)]
   expected=-qty if row['direction']=='short' else qty
   if amounts!=[[expected],[expected]]:raise RuntimeError('local_exchange_position_disagreement')
   ids=json.loads(row['algo_ids']) if isinstance(row['algo_ids'],str) else row['algo_ids']
   orders=await api.get_open_algo_orders(symbol)
   hard=[o for o in orders if str(o.get('algoId'))==str(ids.get('sl')) and (o.get('orderType') or o.get('type')) in ('STOP','STOP_MARKET') and str(o.get('closePosition')).lower()=='true']
   if len(hard)!=1:raise RuntimeError('hard_stop_not_proven')
   proofs.append(dict(symbol=symbol,quantity=str(qty),hard_stop_id=str(hard[0]['algoId']),trigger_price=str(hard[0].get('triggerPrice'))))
 await c.close()
 print('HRS_RELEASE_PREFLIGHT='+json.dumps(dict(account_id=account,positions=proofs,filled_retained_reservations=len(rows))))
asyncio.run(main())'''


def preflight():
    output = run(['docker','exec','-i','trading_system-hrs','python','-'], input=PREFLIGHT)
    line = next(line for line in output.splitlines() if line.startswith('HRS_RELEASE_PREFLIGHT='))
    return json.loads(line.split('=',1)[1])


INITIALIZATION = '''import asyncio,os,json,re,asyncpg
from pathlib import Path
from datetime import datetime,timezone
async def main():
 stage='collector_connect';result={}
 try:
  host=os.environ['DATABASE_HOST'];port=int(os.getenv('DATABASE_PORT','5432'));database=os.environ['DATABASE_NAME']
  options=dict(host=host,port=port,database=database,server_settings={'default_transaction_read_only':'on'})
  c=await asyncpg.connect(user=os.environ['DATABASE_USER'],password=os.environ['DATABASE_PASSWORD'],**options)
  try:
   stage='collector_projection_read'
   gate=await c.fetchrow("SELECT updated_at,ready,allowed,reasons,evidence_sha256 FROM trading.strategy_equity_gate WHERE account_id=$1 AND strategy_id='hrs'",os.getenv('TRADE_ACCOUNT_ID','default'))
  finally: await c.close()
  if gate is None:
   result=dict(status='error',stage=stage,error_code='collector_projection_missing')
  else:
   result['gate']=dict(gate)
   if isinstance(result['gate']['reasons'],str):result['gate']['reasons']=json.loads(result['gate']['reasons'])
   stage='writer_connect'
   writer=await asyncpg.connect(user=os.environ['AI_TUNER_SNAPSHOT_DB_USER'],password=os.environ['AI_TUNER_SNAPSHOT_DB_PASSWORD'],**options)
   try:
    stage='writer_evidence_read'
    recorded=await writer.fetchval("SELECT max(recorded_at) FROM hrs.hrs_decision_evidence WHERE kind='equity_gate' AND payload->>'evidence_sha256'=$1",gate['evidence_sha256'])
   finally: await writer.close()
   stage='loaded_configuration_read';loaded=Path('/app/logs/tuning-loaded.json');configuration=json.loads(loaded.read_text())
   result.update(status='observed',stage='complete',release_sha=os.getenv('HRS_RELEASE_SHA'),
    loaded_config={key:configuration.get(key) for key in ('strategy','config_hash')},config_mtime=loaded.stat().st_mtime,
    checked_at=datetime.now(timezone.utc).isoformat(),writer_recorded_at=recorded)
 except Exception as error:
  code={'InsufficientPrivilegeError':'database_permission_denied','InvalidPasswordError':'database_authentication_failed',
   'InvalidAuthorizationSpecificationError':'database_authentication_failed','UndefinedTableError':'database_relation_missing',
   'CannotConnectNowError':'database_connection_failed','ConnectionDoesNotExistError':'database_connection_failed',
   'ConnectionRefusedError':'database_connection_failed','TimeoutError':'database_connection_failed',
   'FileNotFoundError':'configuration_file_missing','JSONDecodeError':'configuration_invalid'}.get(type(error).__name__,'initialization_probe_failed')
  result.update(status='error',stage=stage,error_code=code)
 if 'gate' in result:
  gate=result['gate'];reasons=gate.get('reasons',[])
  if not isinstance(reasons,list):reasons=[None]
  known=[reason for reason in reasons if isinstance(reason,str) and reason in __SAFE_GATE_REASONS__]
  result['gate']={key:gate.get(key) for key in ('updated_at','ready','allowed')}
  result['gate'].update(reasons=known,unknown_reason_count=len(reasons)-len(known))
 if result.get('release_sha') is not None and not re.fullmatch('[0-9a-f]{40}',str(result['release_sha'])):result['release_sha']=None
 if 'loaded_config' in result:
  config=result['loaded_config']
  if config.get('strategy')!='hrs':config['strategy']=None
  if not re.fullmatch('[0-9a-f]{64}',str(config.get('config_hash'))):config['config_hash']=None
 print('HRS_INITIALIZATION='+json.dumps(result,default=lambda value:value.isoformat()))
asyncio.run(main())'''.replace('__SAFE_GATE_REASONS__',repr(sorted(SAFE_GATE_REASONS)))


def read_initialization():
    output=run(['docker','exec','-i','trading_system-hrs','python','-'],input=INITIALIZATION)
    try:
        line=next(line for line in output.splitlines() if line.startswith('HRS_INITIALIZATION='))
        data=json.loads(line.split('=',1)[1])
        if not isinstance(data,dict):raise ValueError()
        return data
    except (StopIteration,ValueError):
        raise RuntimeError('initialization_response_invalid') from None


def container_ids():
    identifiers=run(['docker','ps','-aq']).split()
    if not identifiers:return {}
    return {item['Name'].lstrip('/'):item['Id'] for item in json.loads(run(['docker','inspect',*identifiers]))}


def unchanged_others(before,after):
    return all(after.get(name)==identity for name,identity in before.items() if name not in TARGETS)


def mount_identity(mounts):
    return sorted((m['Type'],m.get('Name') if m['Type']=='volume' else m['Source'],m['Destination'],m['RW']) for m in mounts)


def pin_snapshots(root):
    result={}
    for name in ('ledger-risk-active.json','hrs-market-cap-active.json','hrs-execution-active.json'):
        path=root/'.deploy_tmp'/name
        result[name]=json.loads(path.read_text()) if path.exists() else None
    return result


def restore_pins(root,snapshots):
    for name,value in snapshots.items():
        path=root/'.deploy_tmp'/name
        if value is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path,value)


def rollback(root,folder,plan,candidate_image):
    """Restore only the originally observed target geometry; never project down."""
    errors=[];restored=[]
    old=compose(root,plan['compose_files']+[folder/'rollback-images.json'])
    for name,original in plan['targets'].items():
        try:
            try: current=inspect(name)
            except RuntimeError: current=None
            if (current is None or current['Config']['Image']!=original['image']
                    or mount_identity(current.get('Mounts',[]))!=mount_identity(original['mounts'])):
                run(old+['up','-d','--no-deps','--pull','never',TARGETS[name]])
            restored.append(name)
        except Exception:
            errors.append(name)
    if REPORTER not in plan['targets']:
        try:
            try: current=inspect(REPORTER)
            except RuntimeError: current=None
            if current is not None:
                labels=current['Config'].get('Labels',{})
                if (current['Config']['Image']!=candidate_image
                    or labels.get('com.docker.compose.project')!='trading_system'
                    or labels.get('com.docker.compose.service')!='hrs-acceptance-reporter'):
                    raise RuntimeError('new_reporter_identity_unproven')
                run(['docker','rm','-f',REPORTER])
        except Exception:
            errors.append(REPORTER)
    try:
        for name,original in plan['targets'].items():
            current=inspect(name)
            if (current['Config']['Image']!=original['image']
                    or mount_identity(current.get('Mounts',[]))!=mount_identity(original['mounts'])):
                errors.append(name)
        if not unchanged_others(plan['containers'],container_ids()):errors.append('unrelated_container_changed')
        if not errors:restore_pins(root,plan['pins'])
    except Exception:
        errors.append('rollback_verification_or_pin_restore')
    return dict(status='complete' if not errors else 'incomplete',restored_targets=restored,
                failures=sorted(set(errors)),database_changes='additive_migrations_retained')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--sha',required=True)
    parser.add_argument('--hrs-image',required=True)
    parser.add_argument('--hub-image',required=True)
    parser.add_argument('--root',default='/root/trading_system')
    parser.add_argument('--manifest',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}',args.sha): raise ValueError('invalid_release_sha')
    for image in (args.hrs_image,args.hub_image):
        if not re.fullmatch(r'ghcr\.io/bearflower/trading-hrs@sha256:[0-9a-f]{64}',image): raise ValueError('mutable_or_unexpected_image')
    root=Path(args.root).resolve();folder=root/'.releases/hrs-optimization'/args.sha
    folder.mkdir(parents=True,exist_ok=True)
    if (folder/'state.json').exists() and json.loads((folder/'state.json').read_text()).get('status')=='complete':
        raise RuntimeError('release_already_completed')
    manifest=json.loads(Path(args.manifest).read_text())
    expected=manifest['source_hashes']
    if not isinstance(expected,dict) or not expected:
        raise ValueError('source_hash_inventory_missing')
    for path,digest in expected.items():
        if Path(path).is_absolute() or '..' in Path(path).parts or not re.fullmatch('[0-9a-f]{64}',digest):
            raise ValueError('unsafe_manifest_path_or_hash')
    if not re.fullmatch('[0-9a-f]{64}',manifest['expected_config_hash']):
        raise ValueError('expected_configuration_hash_missing')
    migration_sql(folder/'migrations',manifest['migration_hashes'])
    before={name:inspect(name) for name in ('trading_system-hrs','trading_system-market-cap')}
    all_before=container_ids()
    if REPORTER in all_before:before[REPORTER]=inspect(REPORTER)
    env=environment(before['trading_system-hrs'])
    if not env.get('AI_TUNER_SNAPSHOT_DB_USER') or not env.get('AI_TUNER_SNAPSHOT_DB_PASSWORD'):
        raise RuntimeError('independent_snapshot_credentials_missing')
    facts=preflight();atomic(folder/'preflight.json',facts)
    pinpath=root/'.deploy_tmp/ledger-risk-active.json'
    planpath=folder/'rollback-plan.json'
    if planpath.exists():
        plan=json.loads(planpath.read_text());pin=plan['pins']['ledger-risk-active.json']
        if not unchanged_others(plan['containers'],all_before):raise RuntimeError('unrelated_container_changed_since_prepare')
    else:
        pin=json.loads(pinpath.read_text())
        previous=json.loads(Path(pin['overlay']).read_text())
        if REPORTER in before and 'hrs-acceptance-reporter' not in previous.get('services',{}):
            raise RuntimeError('existing_reporter_definition_missing')
        if (folder/'before.json').exists():
            first=json.loads((folder/'before.json').read_text())
            if any(first['containers'].get(name)!=all_before.get(name) for name in before):
                raise RuntimeError('first_prepare_targets_changed_without_rollback_plan')
        else:
            atomic(folder/'before.json',dict(pin=pin,containers=all_before,
                images={name:c['Config']['Image'] for name,c in before.items()}))
        old_overlay=deepcopy(previous)
        for name,container in before.items():
            service=old_overlay.setdefault('services',{}).setdefault(TARGETS[name],{})
            service['image']=container['Config']['Image']
            service['volumes']=actual_volumes(old_overlay,container.get('Mounts',[]))
        old_env=old_overlay['services']['hrs-strategy'].setdefault('environment',{})
        old_env.update({key:env.get(key) for key in PUBLIC_FLAGS|{'HRS_RELEASE_SHA'}})
        atomic(folder/'rollback-images.json',old_overlay)
        plan=dict(containers=all_before,pins=pin_snapshots(root),
            compose_files=before['trading_system-hrs']['Config']['Labels']['com.docker.compose.project.config_files'].split(','),
            targets={name:dict(image=c['Config']['Image'],mounts=c.get('Mounts',[])) for name,c in before.items()})
        atomic(planpath,plan)
    previous=json.loads(Path(pin['overlay']).read_text())
    overlay=release_overlay(previous,args.hrs_image,args.hub_image,plan['targets']['trading_system-hrs']['mounts'])
    actual_networks=set(before['trading_system-hrs']['NetworkSettings']['Networks']) & set(inspect('trading_system-postgres')['NetworkSettings']['Networks'])
    if not actual_networks:raise RuntimeError('shared_database_network_missing')
    network=sorted(actual_networks)[0]
    overlay.setdefault('networks',{})['hrs-acceptance-network']={'external':True,'name':network}
    report_folder=root/'logs/hrs-acceptance';report_folder.mkdir(parents=True,exist_ok=True)
    secret_file=root/'.secrets/hrs-acceptance.env'
    overlay['services']['hrs-acceptance-reporter']=dict(image=args.hrs_image,container_name='trading_system-hrs-acceptance',restart='unless-stopped',
        command=['python','-m','strategies.hrs.acceptance_report','--interval','900'],env_file=[str(secret_file)],
        volumes=[str(report_folder)+':/app/logs'],networks=['hrs-acceptance-network'],read_only=True,tmpfs=['/tmp'],mem_limit='256m',cpus=.3,
        healthcheck={'test':['CMD','python','-c',"import json,datetime,pathlib;d=json.loads(pathlib.Path('/app/logs/hrs-acceptance.json').read_text());assert(datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(d['generated_at'])).total_seconds()<1800"],
                     'interval':'60s','timeout':'10s','retries':3,'start_period':'120s'},
        logging={'driver':'json-file','options':{'max-size':'10m','max-file':'3'}})
    newfile=folder/'images.json';atomic(newfile,overlay)
    if not args.apply:
        print(json.dumps(dict(status='prepared',overlay=str(newfile),preflight=facts)))
        return
    for image in (args.hrs_image,args.hub_image):run(['docker','pull',image],timeout=300)
    probe="import hashlib;from pathlib import Path;expected="+repr(expected)+";assert all(hashlib.sha256((Path('/app')/p).read_bytes()).hexdigest()==h for p,h in expected.items());from strategies.hrs.main import main;print('CANDIDATE_SOURCES_OK')"
    run(['docker','run','--rm','--network','none','--entrypoint','python',args.hrs_image,'-c',probe])
    files=plan['compose_files']
    # Only public new flags and actual mount restoration can change HRS geometry.
    check=deepcopy(overlay);check['services'].pop('hrs-acceptance-reporter')
    checkfile=folder/'checked-images.json';atomic(checkfile,check)
    rendered=json.loads(run(compose(root,files+[checkfile])+['config','--format','json']))['services']['hrs-strategy']
    proposed=rendered.get('environment',{})
    candidate_image=json.loads(run(['docker','image','inspect',args.hrs_image]))[0]
    drift=environment_drift(env,environment(candidate_image),proposed,args.sha)
    if drift:raise RuntimeError('existing_environment_drift_keys:'+','.join(sorted(drift)))
    database=env['DATABASE_NAME']
    password=None
    if secret_file.exists():
        values=dict(line.split('=',1) for line in secret_file.read_text().splitlines() if '=' in line)
        password=urlparse(values['DATABASE_URL']).password
    password=password or secrets.token_urlsafe(36)
    secret_file.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(secret_file,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as handle:
        os.fchmod(handle.fileno(),0o600)
        handle.write('DATABASE_URL=postgresql://hrs_acceptance_reader:'+quote(password,safe='')+'@'+env['DATABASE_HOST']+':'+env.get('DATABASE_PORT','5432')+'/'+quote(database,safe='')+'\n')
        handle.write('TRADE_ACCOUNT_ID='+facts['account_id']+'\n')
    tail="DO $$BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='hrs_acceptance_reader') THEN CREATE ROLE hrs_acceptance_reader LOGIN; END IF; END$$;\n"
    tail+="ALTER ROLE hrs_acceptance_reader PASSWORD '"+password.replace("'","''")+"';\n"+reader_grants(database)
    sql=migration_sql(folder/'migrations',manifest['migration_hashes'],tail)
    candidate=compose(root,files+[newfile])
    wanted={'trading_system-hrs':args.hrs_image,'trading_system-market-cap':args.hub_image,REPORTER:args.hrs_image}
    steps=[];observations=[];phase='migrations';replacement_attempted=False
    def audit(status,**extra):
        state=dict(status=status,sha=args.sha,phase=phase,completed_steps=list(steps),
            images=wanted,source_hashes=expected,migration_hashes=manifest['migration_hashes'],
            observations=list(observations),
            economic_parameters_changed=False,performance_qualified=False,
            recorded_at=datetime.now(timezone.utc).isoformat(),**extra)
        atomic(folder/'state.json',state)
        return state
    audit('applying')
    try:
        run(['docker','exec','-i','trading_system-postgres','psql','-v','ON_ERROR_STOP=1','-U','trading_migration_admin','-d',database],input=sql)
        steps.append('migrations_applied');audit('applying')
        preflight()  # No unresolved live entry may appear during preparation.
        for service in ('market-cap-service','hrs-strategy','hrs-acceptance-reporter'):
            phase='replace_'+service;replacement_attempted=True;audit('applying')
            run(candidate+['up','-d','--no-deps','--pull','never',service])
            steps.append(phase);audit('applying')
        phase='health_and_initialization';audit('applying')
        for attempt in range(90):
            observation=dict(attempt=attempt+1,checked_at=datetime.now(timezone.utc).isoformat(),containers={},
                initialization={'status':'not_checked'})
            observations.append(observation);states={}
            for name in wanted:
                try:
                    states[name]=inspect(name)
                    observation['containers'][name]=safe_container(states[name])
                except Exception as error:
                    observation['containers'][name]=dict(status='inspection_failed',**safe_error(error))
            audit('applying')
            if len(states)!=len(wanted):raise RuntimeError('candidate_inspection_failed')
            if any(c['State']['Status'] in ('exited','dead') for c in states.values()):
                raise RuntimeError('candidate_process_failed')
            healthy=all(c['State']['Status']=='running' and c['State'].get('Health',{}).get('Status')=='healthy' for c in states.values())
            if healthy:
                try:
                    data=read_initialization()
                    observation['initialization']=safe_initialization(data,
                        states['trading_system-hrs']['State']['StartedAt'],args.sha,manifest['expected_config_hash'])
                    initialized=initialized_proof(data,
                        states['trading_system-hrs']['State']['StartedAt'],args.sha,manifest['expected_config_hash'])
                except (RuntimeError,ValueError,KeyError,TypeError,StopIteration) as error:
                    diagnostic=observation['initialization']
                    if diagnostic['status']=='not_checked':diagnostic.update(status='error',**safe_error(error))
                    elif diagnostic['status']!='error':diagnostic.update(status='unproven',**safe_error(error))
                else:
                    observation['initialization']['status']='proven';audit('applying')
                    break
            audit('applying')
            time.sleep(2)
        else:raise RuntimeError('candidate_initialization_timeout')
        for name,image in wanted.items():
            if states[name]['Config']['Image']!=image:raise RuntimeError('candidate_image_mismatch')
        run(['docker','exec','trading_system-hrs','python','-c',probe])
        if not unchanged_others(plan['containers'],container_ids()):raise RuntimeError('unrelated_container_changed')
        steps.append('application_initialized');phase='publish_pins';audit('applying')
        for filename,old in plan['pins'].items():
            old=old or {}
            atomic(root/'.deploy_tmp'/filename,dict(old,sha=args.sha,predecessor_sha=old.get('sha'),
                overlay=str(newfile),state=str(folder/'state.json'),status='active'))
        phase='complete'
        state=audit('complete',containers={name:c['Id'] for name,c in states.items()},
            migrated=list(MIGRATIONS),initialization=initialized,other_containers_unchanged=True)
        print(json.dumps(state))
    except BaseException as exc:
        failure=safe_error(exc)
        persisted=True
        try:
            audit('failing',**failure)  # Preserve observations BEFORE rollback changes them.
        except OSError:
            persisted=False  # A full/unwritable disk must never prevent target recovery.
        recovery=(rollback(root,folder,plan,args.hrs_image) if replacement_attempted else
            dict(status='not_needed',database_changes='additive_migrations_retained'))
        if recovery['status']=='not_needed':restore_pins(root,plan['pins'])
        if recovery['status']=='incomplete':
            # Do not leave a misleading active pointer after failed recovery.
            for filename,old in plan['pins'].items():
                atomic(root/'.deploy_tmp'/filename,dict(old or {},status='rollback_failed',state=str(folder/'state.json')))
        try:
            audit('failed',**failure,rollback=recovery,pre_rollback_audit_persisted=persisted)
        except OSError:
            raise RuntimeError('rollout_failed_rollback_'+recovery['status']+'_audit_unavailable') from None
        raise RuntimeError('rollout_failed_rollback_'+recovery['status']) from None


if __name__=='__main__':
    main()
