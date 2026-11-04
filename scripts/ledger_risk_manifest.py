"""Strict immutable scope for the one ledger/risk release; no shell in manifests."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import PurePosixPath

MODE='ledger-risk-remediation'
RELEASE='ledger-risk-remediation-20261104'
MARKER=f'.github/deploy-modes/{RELEASE}'
MANIFEST=f'deployment/{RELEASE}.json'
IMAGES=['btc-eth','btc-eth-aggr','new-coin','hrs','ai-tuner','data-backend','dashboard-api']
SERVICE_IMAGES={'btc-eth-strategy':'btc-eth','btc-eth-aggressive-strategy':'btc-eth-aggr',
 'new-coin-strategy':'new-coin','hrs-strategy':'hrs','execution-facts-collector':'hrs',
 'data-backend':'data-backend','dashboard-api':'dashboard-api','ai-tuner':'ai-tuner'}
SERVICES=list(SERVICE_IMAGES)
MIGRATIONS=['database/postgres/migrations/'+name for name in (
 '2026-11-04-ledger-report-publication.sql','2026-11-04-entry-risk-policy.sql','2026-11-04-income-immutability.sql')]
COMPOSE=['docker-compose.yml','docker-compose.override.yml','docker-compose.logging.yml']
COLLECTOR_RESTORATION={
 'command':['/bin/sh','-c','rm -f /tmp/execution-facts-last-success; while python -m scripts.collect_execution_facts --days 2; do touch /tmp/execution-facts-last-success; sleep 3600; done; exit 1'],
 'healthcheck':{'test':['CMD','python','-c',"import os,time,sys; p='/tmp/execution-facts-last-success'; age=time.time()-os.path.getmtime(p) if os.path.isfile(p) else float('inf'); sys.exit(0 if 0 <= age < 5400 else 1)"],'interval':'1m0s','timeout':'5s','retries':2,'start_period':'3m0s'},
 'logging':{'driver':'journald','options':{}}}
HEX64=re.compile(r'[0-9a-f]{64}')
SHA=re.compile(r'[0-9a-f]{40}')
class ReleaseBlocked(ValueError):pass

def digest(value):
    data=value if isinstance(value,bytes) else json.dumps(value,sort_keys=True,separators=(',',':')).encode()
    return hashlib.sha256(data).hexdigest()

def template():
    return dict(schema_version=1,release_id=RELEASE,images=IMAGES,services=SERVICES,
        migrations=[{'path':p,'sha256':'0'*64} for p in MIGRATIONS],base_revision='0'*40,
        allowed_changes=[],source_checksums={},expected_production=None,policy=None,historical_reports=[],
        blocker='Refresh and independently review complete production evidence before activation')

def safe_path(path):
    return isinstance(path,str) and path and not PurePosixPath(path).is_absolute() and '..' not in PurePosixPath(path).parts and '\\' not in path and not any(c.isspace() for c in path)

def validate_manifest(m,*,activation=False):
    if set(m)!=set(template()) or m['schema_version']!=1 or m['release_id']!=RELEASE:
        raise ReleaseBlocked('invalid release manifest schema')
    if m['images']!=IMAGES or m['services']!=SERVICES:
        raise ReleaseBlocked('unexpected image or service scope')
    if [x.get('path') for x in m['migrations']]!=MIGRATIONS or any(not HEX64.fullmatch(x.get('sha256','')) for x in m['migrations']):
        raise ReleaseBlocked('invalid migration allowlist')
    if not SHA.fullmatch(m['base_revision']):raise ReleaseBlocked('invalid baseline')
    paths=[row.get('path') for row in m['allowed_changes']]
    if len(paths)!=len(set(paths)) or any(not safe_path(p) for p in paths):raise ReleaseBlocked('invalid changed paths')
    if any(row.get('status') not in ('A','M','D') for row in m['allowed_changes']):raise ReleaseBlocked('invalid change status')
    if any(not safe_path(p) or not HEX64.fullmatch(h) for p,h in m['source_checksums'].items()):raise ReleaseBlocked('invalid source hashes')
    if activation:
        p=m['expected_production'];policy=m['policy']
        if not p or not policy or m['blocker']:raise ReleaseBlocked('reviewed production evidence required')
        if set(p)!={'compose_sha256','containers','service_config_sha256','stacks','project','minimum_free_bytes','collector_restoration','service_compose_hash','service_runtime_sha256','compose_hash_matches'}:raise ReleaseBlocked('production evidence schema')
        if set(p['collector_restoration'])!={'before','after'}:raise ReleaseBlocked('collector restoration evidence required')
        validate_collector_restoration(p['collector_restoration']['after'])
        if p['project']!='trading_system' or set(p['compose_sha256'])!=set(COMPOSE):raise ReleaseBlocked('production project/config')
        if any(not HEX64.fullmatch(x) for x in p['compose_sha256'].values()):raise ReleaseBlocked('full config hashes required')
        if set(p['service_config_sha256'])!=set(SERVICES) or any(not HEX64.fullmatch(x) for x in p['service_config_sha256'].values()):raise ReleaseBlocked('effective service hashes required')
        if set(p['service_compose_hash'])!=set(SERVICES) or any(not HEX64.fullmatch(x) for x in p['service_compose_hash'].values()):raise ReleaseBlocked('running/effective Compose binding hashes required')
        if set(p['service_runtime_sha256'])!=set(SERVICES) or any(not HEX64.fullmatch(x) for x in p['service_runtime_sha256'].values()):raise ReleaseBlocked('actual runtime semantic evidence required')
        if set(p['compose_hash_matches'])!=set(SERVICES) or any(type(x) is not bool for x in p['compose_hash_matches'].values()):raise ReleaseBlocked('Compose label comparison evidence required')
        if set(p['stacks'])!=set(SERVICES):raise ReleaseBlocked('explicit stacks required')
        for stack in p['stacks'].values():
            if not isinstance(stack,list) or not stack or stack[0]!=COMPOSE[0] or len(stack)!=len(set(stack)) or not set(stack)<=set(COMPOSE):raise ReleaseBlocked('unsafe compose stack')
        if not set(SERVICES)<=set(p['containers']) or 'ai-tuner-funding-schedule' not in p['containers']:raise ReleaseBlocked('all target and excluded container evidence required')
        for service,row in p['containers'].items():
            if set(row)!={'name','id','image_id','started_at','source_sha256','config_sha256'} or not HEX64.fullmatch(row['config_sha256']) or not re.fullmatch(r'[A-Za-z0-9_.-]+',row['name']) or not HEX64.fullmatch(row['id']) or not re.fullmatch(r'sha256:[0-9a-f]{64}',row['image_id']) or not row['started_at']:raise ReleaseBlocked('full container identity required')
            if any(not safe_path(path) or not HEX64.fullmatch(h) for path,h in row['source_sha256'].items()):raise ReleaseBlocked('invalid production source fingerprints')
        if not isinstance(p['minimum_free_bytes'],int) or p['minimum_free_bytes']<5*1024**3:raise ReleaseBlocked('insufficient disk threshold')
        if set(policy)!={'account','verified_equity','expected_revision','valid_hours','reason','enable_reduced','candidates'}:raise ReleaseBlocked('policy evidence schema')
        from decimal import Decimal
        equity=Decimal(str(policy['verified_equity']))
        if not equity.is_finite() or equity<=0 or not re.fullmatch(r'[A-Za-z0-9_.-]+',policy['account']) or not policy['reason']:raise ReleaseBlocked('invalid policy evidence')
        if type(policy['expected_revision']) is not int or policy['expected_revision']<0 or type(policy['valid_hours']) is not int or not 1<=policy['valid_hours']<=72 or type(policy['enable_reduced']) is not bool:raise ReleaseBlocked('invalid policy revision/validity')
        if len(policy['candidates'])!=4:raise ReleaseBlocked('reviewed four-policy candidate required')
        from datetime import datetime,timezone,timedelta
        reports=m['historical_reports']
        if len(reports)!=3:raise ReleaseBlocked('three reviewed historical reports required')
        for row,month,net in zip(reports,(8,9,10),('-115.45319304','-6.65665904','5.80965338')):
            start=int(datetime(2026,month,1,tzinfo=timezone(timedelta(hours=8))).timestamp()*1000)
            end=int(datetime(2026,month+1,1,tzinfo=timezone(timedelta(hours=8))).timestamp()*1000)
            if set(row)!={'account_id','start_ms','end_ms','net_usdt','source_proof_sha256'} or row['account_id']!=policy['account'] or row['start_ms']!=start or row['end_ms']!=end or Decimal(row['net_usdt'])!=Decimal(net) or not HEX64.fullmatch(row['source_proof_sha256']):raise ReleaseBlocked('historical exchange evidence mismatch')
    return m

def require_image_only(before,after):
    if {k:v for k,v in before.items() if k!='image'}!={k:v for k,v in after.items() if k!='image'}:
        raise ReleaseBlocked('effective Compose drift beyond image')

def validate_tree(manifest,changes,read,base):
    validate_manifest(manifest)
    if manifest['base_revision']!=base:raise ReleaseBlocked('push base differs from reviewed baseline')
    actual=sorted((status,path) for status,path in changes)
    expected=sorted((row['status'],row['path']) for row in manifest['allowed_changes'])
    if actual!=expected:raise ReleaseBlocked('unreviewed mixed push changes')
    needed={path for status,path in changes if status!='D'}-{MANIFEST,MARKER}
    if not needed<=set(manifest['source_checksums']):raise ReleaseBlocked('missing changed source hashes')
    for path,value in manifest['source_checksums'].items():
        if digest(read(path))!=value:raise ReleaseBlocked('selected tree source mismatch: '+path)
    for row in manifest['migrations']:
        if digest(read(row['path']))!=row['sha256']:raise ReleaseBlocked('migration checksum mismatch')
    if read(MARKER).decode().strip()!=MANIFEST:raise ReleaseBlocked('invalid remediation marker content')


def validate_collector_restoration(config):
    normalized=json.loads(json.dumps(config))
    if isinstance(normalized.get('healthcheck'),dict):
        for key in ('interval','start_period'):
            value=normalized['healthcheck'].get(key)
            normalized['healthcheck'][key]={'60s':'1m0s','1m':'1m0s','180s':'3m0s','3m':'3m0s'}.get(value,value)
    if isinstance(normalized.get('logging'),dict):normalized['logging'].setdefault('options',{})
    if normalized!=COLLECTOR_RESTORATION:raise ReleaseBlocked('collector command/health/logging differs from approved restoration')


def effective_compose_hash(service,output):
    parts=output.strip().split()
    if len(parts)!=2 or parts[0]!=service or not HEX64.fullmatch(parts[1]):raise ReleaseBlocked('invalid effective Compose hash output: '+service)
    return parts[1]
