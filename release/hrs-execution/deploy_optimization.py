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


def release_overlay(previous, hrs_image, hub_image, mounts):
    overlay = deepcopy(previous)
    services = overlay.setdefault('services', {})
    service = services.setdefault('hrs-strategy', {})
    service['image'] = hrs_image
    service.setdefault('environment', {}).update(HRS_EVIDENCE_CAPTURE_ENABLED='1',
        HRS_EQUITY_GATE_ENABLED='1', ENTRY_EQUITY_GATE_STRATEGIES='hrs',
        HRS_EQUITY_ALLOW_INCEPTION='true', HRS_EQUITY_REFRESH_SECONDS='30',
        HRS_ACCEPTANCE_REPORT_ENABLED='0')
    volumes = []
    for mount in mounts:
        if mount['Type'] not in ('bind', 'volume'):
            raise ValueError('unsupported_actual_mount_type')
        volumes.append(dict(type=mount['Type'], source=mount.get('Name') if mount['Type']=='volume' else mount['Source'],
                            target=mount['Destination'], read_only=not mount['RW']))
    service['volumes'] = volumes
    services.setdefault('market-cap-service', {})['image'] = hub_image
    return overlay


def reader_grants(database):
    identifier = '"'+database.replace('"','""')+'"'
    return (f'GRANT CONNECT ON DATABASE {identifier} TO hrs_acceptance_reader;\n'
        'GRANT USAGE ON SCHEMA hrs,trading TO hrs_acceptance_reader;\n'
        'GRANT SELECT ON '+','.join(READER_TABLES)+' TO hrs_acceptance_reader;\n'
        'ALTER ROLE hrs_acceptance_reader SET default_transaction_read_only=on;\n')


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
    if (folder/'state.json').exists(): raise RuntimeError('release_already_completed')
    before={name:inspect(name) for name in ('trading_system-hrs','trading_system-market-cap')}
    all_before={item['Name'].lstrip('/'):item['Id'] for item in json.loads(run(['docker','inspect',*run(['docker','ps','-q']).split()]))}
    env=environment(before['trading_system-hrs'])
    if not env.get('AI_TUNER_SNAPSHOT_DB_USER') or not env.get('AI_TUNER_SNAPSHOT_DB_PASSWORD'):
        raise RuntimeError('independent_snapshot_credentials_missing')
    facts=preflight();atomic(folder/'preflight.json',facts)
    pinpath=root/'.deploy_tmp/ledger-risk-active.json';pin=json.loads(pinpath.read_text())
    overlay=release_overlay(json.loads(Path(pin['overlay']).read_text()),args.hrs_image,args.hub_image,before['trading_system-hrs']['Mounts'])
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
    if not (folder/'before.json').exists():
        atomic(folder/'before.json',dict(pin=pin,containers=all_before,images={name:c['Config']['Image'] for name,c in before.items()}))
    if not args.apply:
        print(json.dumps(dict(status='prepared',overlay=str(newfile),preflight=facts)))
        return
    for image in (args.hrs_image,args.hub_image):run(['docker','pull',image],timeout=300)
    manifest=json.loads(Path(args.manifest).read_text())
    expected=manifest['source_hashes']
    for path in expected:
        if Path(path).is_absolute() or '..' in Path(path).parts:raise ValueError('unsafe_manifest_path')
    probe="import hashlib;from pathlib import Path;expected="+repr(expected)+";assert all(hashlib.sha256((Path('/app')/p).read_bytes()).hexdigest()==h for p,h in expected.items());from strategies.hrs.main import main;print('CANDIDATE_SOURCES_OK')"
    run(['docker','run','--rm','--network','none','--entrypoint','python',args.hrs_image,'-c',probe])
    files=before['trading_system-hrs']['Config']['Labels']['com.docker.compose.project.config_files'].split(',')
    # Only public new flags and actual mount restoration can change HRS geometry.
    check=deepcopy(overlay);check['services'].pop('hrs-acceptance-reporter')
    checkfile=folder/'checked-images.json';atomic(checkfile,check)
    rendered=json.loads(run(compose(root,files+[checkfile])+['config','--format','json']))['services']['hrs-strategy']
    proposed=rendered.get('environment',{})
    allowed={'HRS_EVIDENCE_CAPTURE_ENABLED','HRS_EQUITY_GATE_ENABLED','ENTRY_EQUITY_GATE_STRATEGIES','HRS_EQUITY_ALLOW_INCEPTION','HRS_EQUITY_REFRESH_SECONDS','HRS_ACCEPTANCE_REPORT_ENABLED'}
    drift=[key for key,value in proposed.items() if key not in allowed and str(value)!=env.get(key)]
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
    sql="BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='30s';\n"+''.join((folder/'migrations'/name).read_text()+'\n' for name in MIGRATIONS)
    sql += "DO $$BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='hrs_acceptance_reader') THEN CREATE ROLE hrs_acceptance_reader LOGIN; END IF; END$$;\n"
    sql += "ALTER ROLE hrs_acceptance_reader PASSWORD '"+password.replace("'","''")+"';\n"+reader_grants(database)+'COMMIT;\n'
    run(['docker','exec','-i','trading_system-postgres','psql','-v','ON_ERROR_STOP=1','-U','trading_migration_admin','-d',database],input=sql)
    preflight()  # No unresolved live entry may appear during image preparation.
    candidate=compose(root,files+[newfile])
    run(candidate+['up','-d','--no-deps','--pull','never','market-cap-service'])
    run(candidate+['up','-d','--no-deps','--pull','never','hrs-strategy'])
    run(candidate+['up','-d','--no-deps','--pull','never','hrs-acceptance-reporter'])
    wanted={'trading_system-hrs':args.hrs_image,'trading_system-market-cap':args.hub_image,'trading_system-hrs-acceptance':args.hrs_image}
    for attempt in range(90):
        states={name:inspect(name) for name in wanted}
        if all(c['State']['Status']=='running' and c['State'].get('Health',{}).get('Status')=='healthy' for c in states.values()):break
        if any(c['State']['Status'] in ('exited','dead') for c in states.values()):raise RuntimeError('candidate_process_failed')
        time.sleep(2)
    else:raise RuntimeError('candidate_health_timeout')
    for name,image in wanted.items():
        if states[name]['Config']['Image']!=image:raise RuntimeError('candidate_image_mismatch')
    run(['docker','exec','trading_system-hrs','python','-c',probe])
    after={item['Name'].lstrip('/'):item['Id'] for item in json.loads(run(['docker','inspect',*run(['docker','ps','-q']).split()]))}
    if any(after.get(name)!=identity for name,identity in all_before.items() if name not in wanted):raise RuntimeError('unrelated_container_changed')
    state=dict(status='complete',sha=args.sha,images=wanted,containers={name:c['Id'] for name,c in states.items()},
        migrated=list(MIGRATIONS),source_hashes=expected,other_containers_unchanged=True,
        completed_at=datetime.now(timezone.utc).isoformat(),economic_parameters_changed=False,performance_qualified=False)
    atomic(folder/'state.json',state)
    updated=dict(pin,sha=args.sha,predecessor_sha=pin.get('sha'),overlay=str(newfile),state=str(folder/'state.json'),status='active')
    atomic(pinpath,updated)
    for filename in ('hrs-market-cap-active.json','hrs-execution-active.json'):
        path=root/'.deploy_tmp'/filename
        old=json.loads(path.read_text()) if path.exists() else {}
        atomic(path,dict(old,sha=args.sha,predecessor_sha=old.get('sha'),overlay=str(newfile),state=str(folder/'state.json'),status='active'))
    print(json.dumps(state))


if __name__=='__main__':
    main()
