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
            ready=gate['ready'],allowed=gate['allowed'],reasons=gate['reasons'])
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


INITIALIZATION = '''import asyncio,os,json,asyncpg
from pathlib import Path
from datetime import datetime,timezone
async def main():
 host=os.environ['DATABASE_HOST'];port=int(os.getenv('DATABASE_PORT','5432'));database=os.environ['DATABASE_NAME']
 options=dict(host=host,port=port,database=database,server_settings={'default_transaction_read_only':'on'})
 c=await asyncpg.connect(user=os.environ['DATABASE_USER'],password=os.environ['DATABASE_PASSWORD'],**options)
 try: gate=await c.fetchrow("SELECT updated_at,ready,allowed,reasons,evidence_sha256 FROM trading.strategy_equity_gate WHERE account_id=$1 AND strategy_id='hrs'",os.getenv('TRADE_ACCOUNT_ID','default'))
 finally: await c.close()
 if gate is None: raise RuntimeError('collector_projection_missing')
 writer=await asyncpg.connect(user=os.environ['AI_TUNER_SNAPSHOT_DB_USER'],password=os.environ['AI_TUNER_SNAPSHOT_DB_PASSWORD'],**options)
 try: recorded=await writer.fetchval("SELECT max(recorded_at) FROM hrs.hrs_decision_evidence WHERE kind='equity_gate' AND payload->>'evidence_sha256'=$1",gate['evidence_sha256'])
 finally: await writer.close()
 loaded=Path('/app/logs/tuning-loaded.json')
 result=dict(release_sha=os.getenv('HRS_RELEASE_SHA'),loaded_config=json.loads(loaded.read_text()),config_mtime=loaded.stat().st_mtime,checked_at=datetime.now(timezone.utc).isoformat(),gate=dict(gate),writer_recorded_at=recorded)
 if isinstance(result['gate']['reasons'],str): result['gate']['reasons']=json.loads(result['gate']['reasons'])
 print('HRS_INITIALIZATION='+json.dumps(result,default=lambda value:value.isoformat()))
asyncio.run(main())'''


def read_initialization():
    output=run(['docker','exec','-i','trading_system-hrs','python','-'],input=INITIALIZATION)
    line=next(line for line in output.splitlines() if line.startswith('HRS_INITIALIZATION='))
    return json.loads(line.split('=',1)[1])


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
    steps=[];phase='migrations';replacement_attempted=False
    def audit(status,**extra):
        state=dict(status=status,sha=args.sha,phase=phase,completed_steps=list(steps),
            images=wanted,source_hashes=expected,migration_hashes=manifest['migration_hashes'],
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
            states={name:inspect(name) for name in wanted}
            if any(c['State']['Status'] in ('exited','dead') for c in states.values()):
                raise RuntimeError('candidate_process_failed')
            healthy=all(c['State']['Status']=='running' and c['State'].get('Health',{}).get('Status')=='healthy' for c in states.values())
            if healthy:
                try:
                    initialized=initialized_proof(read_initialization(),
                        states['trading_system-hrs']['State']['StartedAt'],args.sha,manifest['expected_config_hash'])
                except (RuntimeError,ValueError,KeyError,StopIteration):
                    pass  # A successful no-op Docker healthcheck is insufficient.
                else:
                    break
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
        recovery=(rollback(root,folder,plan,args.hrs_image) if replacement_attempted else
            dict(status='not_needed',database_changes='additive_migrations_retained'))
        if recovery['status']=='not_needed':restore_pins(root,plan['pins'])
        if recovery['status']=='incomplete':
            # Do not leave a misleading active pointer after failed recovery.
            for filename,old in plan['pins'].items():
                atomic(root/'.deploy_tmp'/filename,dict(old or {},status='rollback_failed',state=str(folder/'state.json')))
        audit('failed',error_type=type(exc).__name__,rollback=recovery)
        raise RuntimeError('rollout_failed_rollback_'+recovery['status']) from None


if __name__=='__main__':
    main()
