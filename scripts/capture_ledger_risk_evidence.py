#!/usr/bin/env python3
"""Read-only, redacted expected_production capture; never activate or write files.

Supply reviewed explicit stacks. Output is evidence, not approval. Raw inspect,
Compose JSON, environment values and subprocess errors never reach stdout/stderr.
Requires this script and scripts/ledger_risk_manifest.py under the same root.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.ledger_risk_manifest import (COMPOSE,SERVICES,COLLECTOR_RESTORATION,
    HEX64,digest,ReleaseBlocked,validate_collector_restoration,effective_compose_hash)
from scripts.ledger_risk_runtime_config import verify_runtime_config

PROJECT='trading_system'
SOURCES={
 'hrs-strategy':['strategies/hrs/strategy.py','strategies/hrs/position_manager.py','strategies/hrs/risk_budget.py','shared/binance_api.py','shared/market_cap.py'],
 'execution-facts-collector':['scripts/collect_execution_facts.py','scripts/export_execution_facts.py','shared/execution_facts_sink.py','shared/database.py'],
 'btc-eth-strategy':['strategies/btc_eth/strategy.py','strategies/btc_eth/main.py','shared/binance_api.py','shared/capital_manager.py'],
 'btc-eth-aggressive-strategy':['strategies/btc_eth_aggressive/strategy.py','strategies/btc_eth_aggressive/main.py','shared/protection_retry.py','shared/binance_api.py'],
 'new-coin-strategy':['strategies/new_coin/strategy.py','strategies/new_coin/executor.py','shared/capital_manager.py'],
 'ai-tuner':['ai_tuner/allocation/pnl_collector.py','ai_tuner/allocation/monthly_job.py','ai_tuner/allocation/daily_refresher.py','ai_tuner/allocation/config_updater.py'],
 'data-backend':['src/main.py','dashboard/backend/services/data_service_docker.py'],
 'dashboard-api':['dashboard/backend/main_docker.py','dashboard/backend/api/routes_docker.py','dashboard/backend/services/data_service_docker.py'],
}

def read_command(args):
    env=dict(os.environ);env.pop('COMPOSE_FILE',None);env['COMPOSE_PROFILES']=''
    try:result=subprocess.run(args,text=True,capture_output=True,timeout=120,env=env)
    except Exception:raise ReleaseBlocked('read-only command unavailable') from None
    if result.returncode:raise ReleaseBlocked('read-only command failed; output withheld')
    return result.stdout.strip()

def collector_before(row):
    result={'command':row['Config'].get('Cmd'),'healthcheck':row['Config'].get('Healthcheck'),'logging':row['HostConfig'].get('LogConfig')}
    # Only known non-secret literals may leave the process. Unknown values are not echoed.
    legacy=['/bin/sh','-c','while true; do python -m scripts.collect_execution_facts --days 2; sleep 3600; done']
    if result['command'] not in (COLLECTOR_RESTORATION['command'],legacy):raise ReleaseBlocked('collector command is outside redaction allowlist')
    expected={'Test':COLLECTOR_RESTORATION['healthcheck']['test'],'Interval':60000000000,'Timeout':5000000000,'Retries':2,'StartPeriod':180000000000}
    if result['healthcheck'] not in (None,expected):raise ReleaseBlocked('collector healthcheck is outside redaction allowlist')
    if result['logging'] not in ({'Type':'journald','Config':{}},{'Type':'json-file','Config':{'max-size':'10m','max-file':'3'}}):raise ReleaseBlocked('collector logging is outside redaction allowlist')
    return result

def capture(root,stacks,*,run=read_command,minimum_free_bytes=5*1024**3,free_bytes=None):
    root=Path(root).resolve()
    if type(minimum_free_bytes) is not int or minimum_free_bytes<5*1024**3:raise ReleaseBlocked('invalid disk threshold')
    if set(stacks)!=set(SERVICES):raise ReleaseBlocked('explicit eight service stacks required')
    for stack in stacks.values():
        if not isinstance(stack,list) or not stack or stack[0]!=COMPOSE[0] or len(set(stack))!=len(stack) or not set(stack)<=set(COMPOSE):raise ReleaseBlocked('unsafe explicit stack')
    available=shutil.disk_usage(root).free if free_bytes is None else free_bytes
    if available<minimum_free_bytes:raise ReleaseBlocked('insufficient disk headroom')
    def snapshot():
        hashes={name:digest((root/name).read_bytes()) for name in COMPOSE}
        ids=run(['docker','ps','-a','--no-trunc','--filter','label=com.docker.compose.project='+PROJECT,'--format','{{.ID}}']).splitlines()
        if not ids or len(set(ids))!=len(ids) or any(not HEX64.fullmatch(cid) for cid in ids):raise ReleaseBlocked('invalid complete project inventory')
        containers={};before=None;running_hashes={};raw_rows={}
        for cid in sorted(ids):
            rows=json.loads(run(['docker','inspect',cid]))
            if not isinstance(rows,list) or len(rows)!=1:raise ReleaseBlocked('ambiguous inspect result')
            row=rows[0];labels=row['Config'].get('Labels') or {};service=labels.get('com.docker.compose.service','');name=row['Name'].removeprefix('/') if hasattr(str,'removeprefix') else row['Name'].lstrip('/')
            if row['Id']!=cid or labels.get('com.docker.compose.project')!=PROJECT or not re.fullmatch(r'[A-Za-z0-9_.-]+',service) or not re.fullmatch(r'[A-Za-z0-9_.-]+',name) or service in containers:raise ReleaseBlocked('invalid/duplicate project service identity')
            if not re.fullmatch(r'sha256:[0-9a-f]{64}',row['Image']) or not row['State'].get('StartedAt'):raise ReleaseBlocked('missing full runtime identity')
            if service in SERVICES:
                running_hashes[service]=labels.get('com.docker.compose.config-hash');raw_rows[service]=row
            source={}
            if service in SOURCES:
                paths=['VERSION']+SOURCES[service]
                lines=run(['docker','exec',cid,'sha256sum']+['/app/'+path for path in paths]).splitlines()
                if len(lines)!=len(paths):raise ReleaseBlocked('incomplete target source hashes')
                for line,path in zip(lines,paths):
                    parts=line.split()
                    if len(parts)!=2 or not HEX64.fullmatch(parts[0]) or parts[1]!='/app/'+path:raise ReleaseBlocked('invalid target source hash evidence')
                    source[path]=parts[0]
            containers[service]=dict(name=name,id=cid,image_id=row['Image'],started_at=row['State']['StartedAt'],config_sha256=digest({'Config':row['Config'],'HostConfig':row['HostConfig']}),source_sha256=source)
            if service=='execution-facts-collector':before=collector_before(row)
        if not set(SERVICES+['ai-tuner-funding-schedule'])<=set(containers):raise ReleaseBlocked('required targets/funding missing')
        configs={};binding={};runtime_hashes={};matches={}
        for service in SERVICES:
            args=['docker','compose','--project-directory',str(root),'-p',PROJECT]
            for name in stacks[service]:args+=['-f',str(root/name)]
            binding[service]=effective_compose_hash(service,run(args+['config','--hash',service]))
            matches[service]=binding[service]==running_hashes[service]
            full=json.loads(run(args+['config','--format','json']));config=full['services'][service]
            images=json.loads(run(['docker','image','inspect',config['image']]))
            if not isinstance(images,list) or len(images)!=1:raise ReleaseBlocked('ambiguous selected image')
            runtime_hashes[service]=verify_runtime_config(config,raw_rows[service],images[0],full.get('networks',{}),allow_collector_restoration=service=='execution-facts-collector')
            configs[service]=digest(config)
            if service=='execution-facts-collector':validate_collector_restoration({key:config.get(key) for key in ('command','healthcheck','logging')})
        return dict(compose_sha256=hashes,containers=containers,service_config_sha256=configs,service_compose_hash=binding,service_runtime_sha256=runtime_hashes,compose_hash_matches=matches,stacks=copy.deepcopy(stacks),project=PROJECT,minimum_free_bytes=minimum_free_bytes,collector_restoration={'before':before,'after':copy.deepcopy(COLLECTOR_RESTORATION)})
    first=snapshot();second=snapshot()
    if first!=second:raise ReleaseBlocked('production changed during evidence capture')
    return first

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',default='/root/trading_system');p.add_argument('--stacks-file',required=True);p.add_argument('--minimum-free-bytes',type=int,default=5*1024**3)
    args=p.parse_args(argv)
    try:
        stacks=json.loads(Path(args.stacks_file).read_text())
        evidence=capture(args.root,stacks,minimum_free_bytes=args.minimum_free_bytes)
    except ReleaseBlocked as exc:
        # These exceptions contain only verifier-authored categories/service names.
        print(json.dumps({'error':'evidence_capture_blocked','reason':str(exc)}),file=sys.stderr)
        return 2
    except Exception:
        # No raw exception may contain credentials from config, inspect or command output.
        print(json.dumps({'error':'evidence_capture_blocked','details':'No evidence emitted; inspect inputs privately.'}),file=sys.stderr)
        return 2
    print(json.dumps(evidence,sort_keys=True,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
