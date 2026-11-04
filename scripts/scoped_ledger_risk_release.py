#!/usr/bin/env python3
"""Scoped release executor. Must be called under the existing compose_deploy.lock.

No permissive rollback. A partial run requires reviewed recovery; it cannot replay
activation or restart legacy collectors. All external commands are fixed argv lists.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.ledger_risk_manifest import (MODE,SERVICES,SERVICE_IMAGES,IMAGES,MIGRATIONS,COMPOSE,
    MANIFEST,RELEASE,SHA,digest,validate_manifest,require_image_only,validate_collector_restoration,ReleaseBlocked)

STRATEGIES=['hrs-strategy','btc-eth-aggressive-strategy','btc-eth-strategy','new-coin-strategy']

def execute_release(backend):
    """One mutually exclusive path; an exception prevents every later operation."""
    backend.action('preflight')
    backend.action('pull_and_verify')
    backend.action('capture')
    backend.action('stop','ai-tuner')
    backend.action('stop','execution-facts-collector')
    backend.action('archive_collector')
    for path in MIGRATIONS:backend.action('migrate',path)
    backend.action('schema_gate')
    backend.action('activate_policy')
    backend.action('up','execution-facts-collector')
    backend.action('collector_gate')
    for service in STRATEGIES:
        backend.action('up',service)
        backend.action('strategy_gate',service)
    for service in ['data-backend','dashboard-api']:
        backend.action('up',service)
        backend.action('consumer_gate',service)
    backend.action('freeze_gate')
    backend.action('up','ai-tuner')
    backend.action('final_gate')
    backend.action('success')


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    with open(tmp,'w') as stream:
        os.chmod(tmp,0o600);json.dump(value,stream,sort_keys=True,indent=2);stream.flush();os.fsync(stream.fileno())
    os.replace(tmp,path)


def verify_dashboard_reports(response,expected):
    from decimal import Decimal
    if response.get('code')!=0:raise ReleaseBlocked('dashboard HTTP report unavailable')
    rows=response.get('data',{}).get('trends',[])
    for proof in expected:
        matches=[row for row in rows if row.get('start_ms')==proof['start_ms']]
        if len(matches)!=1:raise ReleaseBlocked('dashboard monthly window missing/duplicated')
        row=matches[0]
        if any(row.get(key)!=proof[key] for key in ('account_id','start_ms','end_ms','report_id','report_version','publication_status','strategy_attribution_complete')) or row.get('account_coverage_complete') is not True or row.get('refresh_error') is not None or Decimal(str(row.get('total_pnl')))!=Decimal(proof['net_usdt']):raise ReleaseBlocked('dashboard report identity/net/coverage mismatch')


def private_admin_environment(root):
    """Use the existing private secret; never persist or print its derived DSN."""
    from urllib.parse import quote
    import stat
    secret=Path(root)/'.secrets/trading_migration_admin_password'
    if secret.is_symlink() or not secret.is_file() or stat.S_IMODE(secret.stat().st_mode)!=0o600 or secret.stat().st_uid!=os.geteuid():
        raise ReleaseBlocked('private migration administrator secret required')
    password=secret.read_text().rstrip('\n')
    if not password or '\n' in password or '\r' in password:
        raise ReleaseBlocked('invalid migration administrator secret')
    return {'RISK_POLICY_ADMIN_DSN':'postgresql://trading_migration_admin:'+quote(password,safe='')+'@postgres:5432/trading_platform'}


def image_probe_code(root='/app'):
    # Docker exec/run inherits each image WORKDIR (dashboard uses /app/dashboard/backend).
    # Establish the application root explicitly instead of relying on caller PYTHONPATH.
    return "import sys,json,hashlib,pathlib; p=pathlib.Path("+repr(root)+"); sys.path.insert(0,str(p)); import shared.accounting.published_report,shared.entry_risk_policy; print(json.dumps({'version':(p/'VERSION').read_text(),'hashes':{str(x.relative_to(p)):hashlib.sha256(x.read_bytes()).hexdigest() for x in p.rglob('*.py') if x.is_file()}}))"


class Runtime:
    def __init__(self,root,bundle,sha,manifest,digests):
        self.root=Path(root).resolve();self.bundle=Path(bundle).resolve();self.sha=sha
        self.manifest=validate_manifest(manifest,activation=True)
        if not SHA.fullmatch(sha):raise ReleaseBlocked('invalid release SHA')
        if (self.bundle/'release-sha').read_text().strip()!=sha:raise ReleaseBlocked('bundle SHA mismatch')
        for path in ['scripts/scoped_ledger_risk_release.py','scripts/ledger_risk_manifest.py']:
            if digest((self.bundle/path).read_bytes())!=manifest['source_checksums'].get(path):raise ReleaseBlocked('runtime bundle checksum mismatch')
        self.production=manifest['expected_production'];self.images={}
        if set(digests)!=set(IMAGES):raise ReleaseBlocked('all seven CI image digests required')
        import re
        for name,value in digests.items():
            expected=f'ghcr.io/bearflower/trading-{name}@sha256:'
            if not isinstance(value,str) or not value.startswith(expected) or not re.fullmatch('[0-9a-f]{64}',value[len(expected):]):raise ReleaseBlocked('invalid immutable registry digest')
            self.images[name]=value
        self.directory=self.root/'.releases'/RELEASE/sha
        self.state_file=self.directory/'state.json'
        self.pin_file=self.root/'.deploy_tmp/ledger-risk-active.json'
        self.overlay=self.directory/'images.json';self.changed=[];self.before=None
        if self.state_file.exists():raise ReleaseBlocked('existing completed/partial run: verify pins and review recovery, never blindly replay')
        if self.pin_file.exists():raise ReleaseBlocked('active release already pinned; explicit reviewed successor required')

    def run(self,args,*,input=None,timeout=120,extra_env=None,include_stderr=False):
        env=dict(os.environ);env.pop('COMPOSE_FILE',None);env['COMPOSE_PROFILES']=''
        if extra_env:env.update(extra_env)
        result=subprocess.run(args,cwd=self.root,env=env,input=input,text=True,capture_output=True,timeout=timeout)
        if result.returncode:raise ReleaseBlocked('command failed: '+args[0]+' '+args[1]+' (output suppressed to protect credentials)')
        return (result.stdout+result.stderr if include_stderr else result.stdout).strip()

    def compose(self,service,*,release=True):
        argv=['docker','compose','--project-directory',str(self.root),'-p',self.production['project']]
        for name in self.production['stacks'][service]:argv+=['-f',str(self.root/name)]
        if release:argv+=['-f',str(self.overlay)]
        return argv

    def inspect(self,service):
        name=self.production['containers'][service]['name']
        rows=json.loads(self.run(['docker','inspect',name]))
        if len(rows)!=1:raise ReleaseBlocked('ambiguous container')
        return rows[0]

    def journal(self,service,suffix):
        row=self.inspect(service)
        # docker logs works for journald and historical json-file drivers; never erase either.
        text=self.run(['docker','logs','--timestamps','--since','24h',row['Id']],include_stderr=True)
        path=self.directory/(service+'-'+suffix+'.log');path.write_text(text);os.chmod(path,0o600)

    def sql(self,sql):
        return self.run(['docker','exec','-i','trading_system-postgres','psql','-X','--set','ON_ERROR_STOP=on',
                         '-U','trading_migration_admin','-d','trading_platform','-At'],input=sql)

    def record(self,status,step):
        atomic_json(self.state_file,dict(sha=self.sha,status=status,step=step,changed=self.changed,
            images=self.images,stacks=self.production['stacks'],manifest_sha256=digest(self.manifest),
            no_automatic_rollback=True,updated_at=time.time()))

    def health(self,service):
        deadline=time.monotonic()+(1800 if service=='execution-facts-collector' else 300)
        while time.monotonic()<deadline:
            state=self.inspect(service)['State']
            if state.get('Status')=='running' and state.get('Health',{}).get('Status')=='healthy':return
            time.sleep(3)
        raise ReleaseBlocked('health gate failed: '+service)

    def image_proof(self,image,container=None):
        # All selected Dockerfiles package shared accounting. Never import startup/main modules.
        code=image_probe_code()
        command=['docker','exec',container,'python','-c',code] if container else ['docker','run','--rm','--network','none','--entrypoint','python',image,'-c',code]
        proof=json.loads(self.run(command))
        if 'GIT_SHA='+self.sha not in proof['version'].splitlines():raise ReleaseBlocked('image VERSION mismatch')
        required=['shared/entry_risk_policy.py','shared/accounting/published_report.py','shared/accounting/income_integrity.py','shared/income_ingestion.py','shared/capital_manager.py']
        if image==self.images['hrs']:
            required+=['strategies/hrs/strategy.py','strategies/hrs/position_manager.py','strategies/hrs/risk_budget.py','shared/binance_api.py','shared/market_cap.py','scripts/verify_ledger_risk_release.py','scripts/activate_entry_risk_policy.py','scripts/collect_execution_facts.py','shared/execution_facts_sink.py']
        if image in (self.images['dashboard-api'],self.images['data-backend']):required+=['dashboard/backend/services/exchange_ledger.py','dashboard/backend/services/data_service_docker.py']
        for key,directory in [('btc-eth','btc_eth'),('btc-eth-aggr','btc_eth_aggressive'),('new-coin','new_coin')]:
            if image==self.images[key]:required+=['strategies/'+directory+'/strategy.py','strategies/'+directory+'/main.py']
        if image==self.images['new-coin']:required+=['strategies/new_coin/executor.py']
        if image==self.images['ai-tuner']:required+=['ai_tuner/allocation/pnl_collector.py','ai_tuner/allocation/monthly_job.py','ai_tuner/allocation/daily_refresher.py','ai_tuner/allocation/config_updater.py']
        if image==self.images['data-backend']:
            path='services/data_backend/src/main.py'
            if proof['hashes'].get('src/main.py')!=self.manifest['source_checksums'].get(path):raise ReleaseBlocked('data backend entrypoint mismatch')
        for path in required:
            if not self.manifest['source_checksums'].get(path) or proof['hashes'].get(path)!=self.manifest['source_checksums'][path]:raise ReleaseBlocked('image source mismatch: '+path)
        return proof

    def probe(self,stage):
        policy=self.manifest['policy']
        args=self.compose('hrs-strategy')+['run','--rm','--no-deps','--entrypoint','python','hrs-strategy',
            '/app/scripts/verify_ledger_risk_release.py','--stage',stage,'--account',policy['account'],
            '--revision',str(policy['expected_revision']+1)]
        if policy['enable_reduced']:args+=['--require-reduced']
        if stage=='final':args+=['--report-expectations',json.dumps(self.manifest['historical_reports'])]
        output=self.run(args,timeout=180)
        # Only the final line is the machine-readable result; library logs precede it.
        result=json.loads(output.splitlines()[-1])
        if result.get('ok') is not True:raise ReleaseBlocked('read-only gate failed: '+stage)
        atomic_json(self.directory/(stage+'-proof.json'),result)
        return result

    def action(self,name,*args):
        if name=='preflight':
            for path,value in self.production['compose_sha256'].items():
                if digest((self.root/path).read_bytes())!=value:raise ReleaseBlocked('production compose drift: '+path)
            if shutil.disk_usage(self.root).free<self.production['minimum_free_bytes']:raise ReleaseBlocked('disk headroom')
            self.before={}
            for service,expected in self.production['containers'].items():
                row=self.inspect(service)
                if (row['Id'],row['Image'],row['State']['StartedAt'])!=(expected['id'],expected['image_id'],expected['started_at']):raise ReleaseBlocked('container drift: '+service)
                if digest({'Config':row['Config'],'HostConfig':row['HostConfig']})!=expected['config_sha256']:raise ReleaseBlocked('running container config drift: '+service)
                for path,value in expected['source_sha256'].items():
                    actual=self.run(['docker','exec',expected['name'],'sha256sum','/app/'+path]).split()[0]
                    if actual!=value:raise ReleaseBlocked('production source drift: '+service+'/'+path)
                self.before[service]=row
            actual_ids=set(self.run(['docker','ps','-a','--no-trunc','--filter','label=com.docker.compose.project='+self.production['project'],'--format','{{.ID}}']).splitlines())
            if actual_ids!={row['id'] for row in self.production['containers'].values()}:raise ReleaseBlocked('project inventory incomplete or drifted')
            # No mutation before complete expected production identities are checked.
            self.directory.mkdir(parents=True,exist_ok=False);os.chmod(self.directory,0o700)
            atomic_json(self.directory/'preflight-containers.json',self.before)
            atomic_json(self.directory/'manifest.json',self.manifest)
            atomic_json(self.overlay,{'services':{s:{'image':self.images[SERVICE_IMAGES[s]]} for s in SERVICES}})
            for service in SERVICES:
                before=json.loads(self.run(self.compose(service,release=False)+['config','--format','json']))['services'][service]
                after=json.loads(self.run(self.compose(service)+['config','--format','json']))['services'][service]
                if digest(before)!=self.production['service_config_sha256'][service]:raise ReleaseBlocked('effective production config drift: '+service)
                require_image_only(before,after)
                if service=='execution-facts-collector':
                    validate_collector_restoration({key:after.get(key) for key in ('command','healthcheck','logging')})
                    row=self.before[service]
                    actual={'command':row['Config'].get('Cmd'),'healthcheck':row['Config'].get('Healthcheck'),'logging':row['HostConfig'].get('LogConfig')}
                    if actual!=self.production['collector_restoration']['before']:raise ReleaseBlocked('collector restoration baseline drift')
                atomic_json(self.directory/(service+'-effective.json'),after)
            if self.sql("SELECT (NOT rolsuper)::int FROM pg_roles WHERE rolname='trading_user';")!='1':raise ReleaseBlocked('application role must be NOSUPERUSER')
            if self.sql("SELECT (rolsuper AND rolcanlogin)::int FROM pg_roles WHERE rolname=current_user;")!='1':raise ReleaseBlocked('migration administrator unavailable')
            private_admin_environment(self.root)
        elif name=='pull_and_verify':
            for image in self.images.values():self.run(['docker','pull',image],timeout=600)
            for image in self.images.values():self.image_proof(image)
        elif name=='capture':
            backup=self.directory/'database-before.dump'
            with open(backup,'xb') as stream:
                os.chmod(backup,0o600)
                completed=subprocess.run(['docker','exec','trading_system-postgres','pg_dump','-U','trading_migration_admin','-d','trading_platform','-Fc'],stdout=stream,stderr=subprocess.PIPE,timeout=900)
                stream.flush();os.fsync(stream.fileno())
            if completed.returncode or not backup.stat().st_size:raise ReleaseBlocked('database backup failed; no services stopped')
            for service in SERVICES:self.journal(service,'before')
            # Keep previous images with local tags; never prune them.
            for service,row in self.before.items():
                self.run(['docker','tag',row['Image'],f'ledger-risk-rollback/{service}:{self.sha}'])
            self.capture=self.probe('preflight')
            atomic_json(self.pin_file,dict(sha=self.sha,status='activating',overlay=str(self.overlay),state=str(self.state_file)))
        elif name=='stop':self.run(self.compose(args[0],release=False)+['stop',args[0]])
        elif name=='archive_collector':self.journal('execution-facts-collector','stopped')
        elif name=='migrate':
            path=args[0];row=next(r for r in self.manifest['migrations'] if r['path']==path)
            text=(self.bundle/path).read_bytes()
            if digest(text)!=row['sha256']:raise ReleaseBlocked('migration checksum mismatch')
            self.sql(text.decode())
        elif name=='schema_gate':self.probe('schema')
        elif name=='activate_policy':
            from decimal import Decimal
            p=self.manifest['policy']
            live=self.probe('pre_activation')
            equity=min(Decimal(str(p['verified_equity'])),Decimal(live['equity']))
            ceilings={row['strategy_id']:row['entry_ceiling'] for row in p['candidates']}
            args=self.compose('hrs-strategy')+['run','--rm','--no-deps','-e','RISK_POLICY_ADMIN_DSN','--entrypoint','python','hrs-strategy','/app/scripts/activate_entry_risk_policy.py','--account',p['account'],'--verified-equity',str(equity),'--approved-ceilings',json.dumps(ceilings),'--expected-revision',str(p['expected_revision']),'--valid-hours',str(p['valid_hours']),'--reason',p['reason']]
            if p['enable_reduced']:args+=['--enable-reduced']
            private_env=private_admin_environment(self.root)
            dry=json.loads(self.run(args,extra_env=private_env))
            approved={row['strategy_id']:row for row in p['candidates']}
            if len(dry['candidates'])!=4 or dry['holdings_allocation_mutated'] or dry['close_callbacks']:raise ReleaseBlocked('policy candidate drift')
            for row in dry['candidates']:
                old=approved.get(row['strategy_id'])
                if old is None or any(row.get(key)!=old.get(key) for key in old if key not in ('entry_ceiling','allocation_amount')) or Decimal(row['entry_ceiling'])>Decimal(old['entry_ceiling']):raise ReleaseBlocked('policy candidate increase/drift')
            active=json.loads(self.run(args+['--activate'],extra_env=private_env))
            if active['candidates']!=dry['candidates']:raise ReleaseBlocked('policy allocation changed between dry run and activation')
            if active['active_revision']!=p['expected_revision']+1:raise ReleaseBlocked('policy CAS failed')
            atomic_json(self.directory/'policy-activation.json',active)
            self.probe('policy')
        elif name=='up':
            service=args[0];self.run(self.compose(service)+['up','-d','--no-deps',service],timeout=180)
            self.changed.append(service);self.health(service)
            row=self.inspect(service);expected=self.run(['docker','image','inspect','--format','{{.Id}}',self.images[SERVICE_IMAGES[service]]])
            if row['Image']!=expected:raise ReleaseBlocked('wrong running image')
            self.image_proof(self.images[SERVICE_IMAGES[service]],container=row['Id'])
        elif name=='collector_gate':
            # Heartbeat must be newer than this container start, not just an old mounted file.
            row=self.inspect('execution-facts-collector')
            proof=self.run(['docker','exec',row['Id'],'python','-c',"import json,os; print(json.dumps({'mtime':os.stat('/tmp/execution-facts-last-success').st_mtime}))"])
            from datetime import datetime
            started=datetime.fromisoformat(row['State']['StartedAt'].replace('Z','+00:00')).timestamp()
            if json.loads(proof)['mtime']<started:raise ReleaseBlocked('collector has not completed a fresh collection')
            self.probe('collector')
        elif name=='strategy_gate':
            service=args[0];row=self.inspect(service)
            marker='策略初始化完成，开始运行' if service in ('hrs-strategy','new-coin-strategy') else '调度器已启动'
            deadline=time.monotonic()+300
            while True:
                logs=self.run(['docker','logs','--since',row['State']['StartedAt'],row['Id']],include_stderr=True)
                if 'Traceback (most recent call last)' in logs or '策略执行失败' in logs:raise ReleaseBlocked('strategy startup failure: '+service)
                if marker in logs:break
                if time.monotonic()>=deadline:raise ReleaseBlocked('application startup evidence missing: '+service)
                time.sleep(3)
            path=self.directory/(service+'-startup.log');path.write_text(logs);os.chmod(path,0o600)
            self.probe(name)
        elif name=='consumer_gate':
            service=args[0];row=self.inspect(service)
            code="import sys;sys.path.insert(0,'/app');sys.path.insert(0,'/app/dashboard/backend');from services.data_service_docker import DataService;from services.exchange_ledger import ExchangeLedgerDashboardMixin;from shared.accounting.published_report import read_published_report;assert DataService._ledger_report is ExchangeLedgerDashboardMixin._ledger_report;assert DataService._ledger_report.__globals__['read_published_report'] is read_published_report;print('canonical-reader-import-ok')"
            if self.run(['docker','exec',row['Id'],'python','-c',code])!='canonical-reader-import-ok':raise ReleaseBlocked('running consumer reader mismatch')
            self.journal(service,'consumer-startup');self.probe(name)
        elif name=='freeze_gate':self.probe(name)
        elif name=='final_gate':
            result=self.probe('final')
            from datetime import datetime,timezone,timedelta
            now=datetime.now(timezone(timedelta(hours=8)))
            months=(now.year-2026)*12+now.month-8+1
            if not 4<=months<=30:raise ReleaseBlocked('release report HTTP window requires renewed review')
            row=self.inspect('dashboard-api')
            code="import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8767/api/trend?type=monthly&days="+str(months)+"',timeout=120).read().decode())"
            response=json.loads(self.run(['docker','exec',row['Id'],'python','-c',code],timeout=150))
            verify_dashboard_reports(response,result['publication_evidence']['reports'])
            atomic_json(self.directory/'dashboard-monthly-http.json',response)
            atomic_json(self.directory/'position-transition.json',{'before':self.capture['position_exposure'],'after':result['position_exposure'],'order_hashes_before':[self.capture['ordinary_orders_sha256'],self.capture['algo_orders_sha256']],'order_hashes_after':[result['ordinary_orders_sha256'],result['algo_orders_sha256']]})
            if result['position_exposure']!=self.capture['position_exposure']:raise ReleaseBlocked('position change requires independent exchange event review; no automatic rollback')
            if result['ordinary_orders_sha256']!=self.capture['ordinary_orders_sha256'] or result['algo_orders_sha256']!=self.capture['algo_orders_sha256']:raise ReleaseBlocked('protective order change requires independent restoration/event review; no automatic rollback')
            if result['allocation_sha256']!=self.capture['allocation_sha256']:raise ReleaseBlocked('holdings allocation changed')
            for service,old in self.before.items():
                if service in SERVICES:continue
                current=self.inspect(service)
                if (old['Id'],old['Image'],old['State']['StartedAt'])!=(current['Id'],current['Image'],current['State']['StartedAt']):raise ReleaseBlocked('excluded container changed')
            for service in SERVICES:self.journal(service,'after')
        elif name=='success':
            atomic_json(self.pin_file,dict(sha=self.sha,status='active',overlay=str(self.overlay),state=str(self.state_file),stacks=self.production['stacks']))
        else:raise ReleaseBlocked('unknown release action')
        self.record('complete' if name=='success' else 'in_progress',name)


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',default='/root/trading_system');p.add_argument('--bundle',required=True);p.add_argument('--sha',required=True)
    a=p.parse_args();runtime=None
    try:
        bundle=Path(a.bundle)
        runtime=Runtime(a.root,bundle,a.sha,json.loads((bundle/MANIFEST).read_text()),json.loads((bundle/'image-digests.json').read_text()))
        execute_release(runtime)
    except Exception as exc:
        if runtime and runtime.directory.exists():runtime.record('failed',type(exc).__name__)
        print('Scoped release blocked/failed: '+str(exc),file=sys.stderr);return 2
    return 0
if __name__=='__main__':raise SystemExit(main())
