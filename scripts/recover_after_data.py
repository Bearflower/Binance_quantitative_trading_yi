#!/usr/bin/env python3
"""Default-read-only recovery of the exact post-data-backend import-marker failure."""
import json
import re
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.recover_after_meli import AfterMeliRuntime
from scripts.recover_after_hrs import require,require_startup,log_events
from scripts.recover_ledger_risk_release import FAILED_SHA,MANIFEST_DIGEST
from scripts.scoped_ledger_risk_release import atomic_json
from scripts.ledger_risk_manifest import digest

COMPLETED=['execution-facts-collector','hrs-strategy','btc-eth-aggressive-strategy','btc-eth-strategy','new-coin-strategy','data-backend']
PLAIN_LOG=re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(debug|info|warning) *\] .+$')
ERROR_LOG=re.compile(r'\b(traceback|error|critical|fatal)\b',re.IGNORECASE)
CONSUMER_CODE="import sys;sys.path.insert(0,'/app');sys.path.insert(0,'/app/dashboard/backend');from services.data_service_docker import DataService;from services.exchange_ledger import ExchangeLedgerDashboardMixin;from shared.accounting.published_report import read_published_report;assert DataService._ledger_report is ExchangeLedgerDashboardMixin._ledger_report;assert DataService._ledger_report.__globals__['read_published_report'] is read_published_report;print('canonical-reader-import-ok')"

def require_consumer_import(output):
    lines=[line.strip() for line in output.splitlines() if line.strip()]
    require(bool(lines) and lines[-1]=='canonical-reader-import-ok','canonical consumer marker missing/followed by other output')
    for line in lines[:-1]:
        require(not ERROR_LOG.search(line),'consumer import error log')
        plain=PLAIN_LOG.fullmatch(line)
        if plain:
            datetime.strptime(plain.group(1),'%Y-%m-%d %H:%M:%S')
            continue
        events=list(log_events(line))
        require(bool(events),'unrecognized consumer import output')
        require(all(event.get('level','info') in ('debug','info','warning') for event in events),'consumer import failure level')

def validate_data_state(state):
    require(all(state.get(k)==v for k,v in dict(sha=FAILED_SHA,manifest_sha256=MANIFEST_DIGEST,status='failed',step='ReleaseBlocked',changed=COMPLETED,no_automatic_rollback=True).items()),'not the exact post-data-backend failure')

def execute_after_data(backend):
    backend.action('consumer_gate','data-backend')
    backend.action('up','dashboard-api');backend.action('consumer_gate','dashboard-api')
    backend.action('freeze_gate');backend.action('up','ai-tuner')
    backend.action('final_gate');backend.action('success')

class AfterDataRuntime(AfterMeliRuntime):
    AUDIT_DIRECTORY='after-data-recovery'

    @classmethod
    def load(cls,root,bundle,approval):
        require(isinstance(approval.get('reason'),str) and bool(approval['reason'].strip()),'consumer recovery reason required')
        require(digest(Path(__file__).read_bytes())==approval.get('recovery_code_sha256'),'consumer recovery code mismatch')
        self=super().load(root,bundle,approval['meli_approval'])
        previous=self.original/'after-meli-recovery'
        names=['state.json','approval.json','validated-live.json','strategy_gate-proof.json']+[s+'-startup.json' for s in COMPLETED if s.endswith('strategy')]
        require(set(approval.get('previous_artifacts',{}))==set(names),'complete failed-stage artifact hashes required')
        for name in names:
            path=previous/name
            require(not path.is_symlink() and digest(path.read_bytes())==approval['previous_artifacts'][name],'preceding-stage artifact drift')
        state=json.loads((previous/'state.json').read_text());validate_data_state(state)
        require(state.get('images')==self.images,'preceding images changed')
        require(set(approval.get('containers',{}))==set(self.before),'complete current container review required')
        self.approval=dict(self.approval,containers=approval['containers'])
        self.changed=list(COMPLETED);self.full_approval=approval
        return self

    def upgraded_services(self):return tuple(COMPLETED)

    def consumer_import(self,service):
        require(service in ('data-backend','dashboard-api'),'invalid consumer target')
        row=self.inspect(service)
        output=self.run(['docker','exec',row['Id'],'python','-c',CONSUMER_CODE],include_stderr=True)
        require_consumer_import(output)
        return dict(container_id=row['Id'],output=output,canonical_reader=True)

    def validate_live(self):
        # Inherited full inventory/Compose/image and signed MELI/economic checks.
        super().validate_live()
        for service in COMPLETED:
            if not service.endswith('strategy'):continue
            row=self.inspect(service)
            logs=self.run(['docker','logs','--since',row['State']['StartedAt'],row['Id']],include_stderr=True)
            require_startup(logs,service)
            self.fresh[service+'-startup']=dict(container_id=row['Id'],logs=logs)
        self.fresh['data-backend-import']=self.consumer_import('data-backend')

    def action(self,name,*args):
        if name=='consumer_gate':
            service=args[0];evidence=self.consumer_import(service)
            atomic_json(self.directory/(service+'-import.json'),evidence)
            self.journal(service,'consumer-startup');self.probe(name)
            self.record('in_progress',name);return
        return super().action(name,*args)

    def continue_release(self):
        self.directory.mkdir(mode=0o700)
        atomic_json(self.directory/'approval.json',self.full_approval);atomic_json(self.directory/'validated-live.json',self.fresh)
        self.validating=False
        try:execute_after_data(self)
        except Exception as exc:self.record('failed',type(exc).__name__);raise

def main():
    import argparse,fcntl
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('--bundle',required=True);p.add_argument('--approval',required=True);p.add_argument('--execute',action='store_true');a=p.parse_args()
    try:
        with open(Path(a.root)/'.deploy_tmp/compose_deploy.lock','r+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            runtime=AfterDataRuntime.load(a.root,a.bundle,json.loads(Path(a.approval).read_text()));runtime.validate_live()
            if a.execute:runtime.continue_release()
            print(json.dumps({'ok':True,'executed':a.execute,'sha':runtime.sha}))
        return 0
    except Exception as exc:print('Consumer recovery blocked: '+type(exc).__name__,file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
