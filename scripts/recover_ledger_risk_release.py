#!/usr/bin/env python3
"""Manual, single-failure recovery. Default validates only; never replays migrations."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.scoped_ledger_risk_release import Runtime, STRATEGIES, atomic_json
from scripts.ledger_risk_manifest import (RELEASE,MANIFEST,SERVICES,SERVICE_IMAGES,MIGRATIONS,
    digest,validate_manifest,ReleaseBlocked)

FAILED_SHA='d77c232881d75f65ae13ee4fcad33946b6c8413a'
MANIFEST_DIGEST='560e60bce57b5b0f6809494444f1a6036527890be929dea6e9b3327aa0f7dc5b'
FAILED_AT=1793800425.2689555
BACKUP_SIZE=28921034
BASELINE_KEYS=('position_exposure','ordinary_orders_sha256','algo_orders_sha256','allocation_sha256')

def execute_remaining(backend):
    backend.action('activate_policy')
    backend.action('up','execution-facts-collector');backend.action('collector_gate')
    for service in STRATEGIES:
        backend.action('up',service);backend.action('strategy_gate',service)
    for service in ('data-backend','dashboard-api'):
        backend.action('up',service);backend.action('consumer_gate',service)
    backend.action('freeze_gate');backend.action('up','ai-tuner')
    backend.action('final_gate');backend.action('success')

def require(condition,reason):
    if not condition:raise ReleaseBlocked(reason)

def same_baseline(before,after):
    for key in BASELINE_KEYS:
        require(key in before and key in after and before[key]==after[key],'recovery live baseline drift: '+key)

def validate_failure(state,pin,manifest,original):
    require(digest(manifest)==MANIFEST_DIGEST,'wrong original manifest')
    require(all(state.get(k)==v for k,v in dict(sha=FAILED_SHA,status='failed',step='ReleaseBlocked',changed=[],manifest_sha256=MANIFEST_DIGEST,updated_at=FAILED_AT,no_automatic_rollback=True).items()),'not the reviewed pre-activation failure')
    require(pin.get('sha')==FAILED_SHA and pin.get('status')=='activating' and pin.get('overlay')==str(original/'images.json') and pin.get('state')==str(original/'state.json'),'original pin mismatch')
    require(manifest['policy']['expected_revision']==0,'nonzero original revision')

class RecoveryRuntime(Runtime):
    def probe(self,stage):
        if not self.validating:return super().probe(stage)
        require(stage in ('schema','pre_activation'),'non-readonly recovery probe')
        p=self.manifest['policy']
        args=self.compose('hrs-strategy')+['run','--rm','--no-deps','--entrypoint','python','hrs-strategy','/app/scripts/verify_ledger_risk_release.py','--stage',stage,'--account',p['account'],'--revision','1']
        if p['enable_reduced']:args+=['--require-reduced']
        result=json.loads(self.run(args,timeout=180).splitlines()[-1])
        require(result.get('ok') is True,'recovery probe failed')
        self.fresh[stage]=result
        return result

    @classmethod
    def load(cls,root,bundle,approval):
        self=cls.__new__(cls);self.root=Path(root).resolve();self.bundle=Path(bundle).resolve();self.sha=FAILED_SHA
        self.original=self.root/'.releases'/RELEASE/FAILED_SHA
        self.directory=self.original/'manual-recovery';self.state_file=self.directory/'state.json'
        self.pin_file=self.root/'.deploy_tmp/ledger-risk-active.json';self.overlay=self.original/'images.json'
        require(not self.directory.exists(),'recovery already attempted; new review required')
        self.manifest=json.loads((self.bundle/MANIFEST).read_text());validate_manifest(self.manifest,activation=True)
        state=json.loads((self.original/'state.json').read_text());pin=json.loads(self.pin_file.read_text())
        validate_failure(state,pin,self.manifest,self.original)
        require(not (self.original/'policy-activation.json').exists(),'activation evidence already exists')
        require((self.bundle/'release-sha').read_text().strip()==FAILED_SHA,'bundle identity mismatch')
        self.production=self.manifest['expected_production'];self.images=json.loads((self.bundle/'image-digests.json').read_text())
        require(self.images==state['images'],'image digests changed')
        require(json.loads(self.overlay.read_text())=={'services':{s:{'image':self.images[SERVICE_IMAGES[s]]} for s in SERVICES}},'overlay changed')
        required=['state.json','manifest.json','images.json','database-before.dump','preflight-containers.json','preflight-proof.json','schema-proof.json','pre_activation-proof.json','image-default-transitions.json']+[s+'-effective.json' for s in SERVICES]
        require(isinstance(approval.get('reason'),str) and bool(approval['reason'].strip()),'review reason required')
        require(set(approval.get('artifacts',{}))==set(required),'complete reviewed artifact hashes required')
        for name in required:
            path=self.original/name
            require(not path.is_symlink() and digest(path.read_bytes())==approval['artifacts'][name],'original artifact mismatch: '+name)
        require(digest(self.pin_file.read_bytes())==approval.get('pin_sha256'),'reviewed pin hash mismatch')
        modules=['recover_ledger_risk_release.py','scoped_ledger_risk_release.py','ledger_risk_manifest.py','ledger_risk_runtime_config.py']
        require(set(approval.get('code',{}))==set(modules),'reviewed recovery code hashes required')
        for name in modules:require(digest((Path(__file__).parent/name).read_bytes())==approval['code'][name],'recovery code mismatch')
        require(digest(json.loads((self.original/'manifest.json').read_text()))==MANIFEST_DIGEST,'saved manifest mismatch')
        backup=self.original/'database-before.dump'
        with backup.open('rb') as stream:require(stream.read(5)==b'PGDMP' and backup.stat().st_size==BACKUP_SIZE,'backup invalid')
        for row in self.manifest['migrations']:require(digest((self.bundle/row['path']).read_bytes())==row['sha256'],'migration source drift')
        for stage in ('preflight','schema','pre_activation'):require(json.loads((self.original/(stage+'-proof.json')).read_text()).get('ok') is True,'original proof failed')
        self.capture=json.loads((self.original/'preflight-proof.json').read_text());self.before=json.loads((self.original/'preflight-containers.json').read_text())
        self.changed=[];self.validating=True;self.fresh={};self.approval=approval
        return self

    def validate_live(self):
        for path,sha in self.production['compose_sha256'].items():require(digest((self.root/path).read_bytes())==sha,'Compose drift')
        ids=set(self.run(['docker','ps','-a','--no-trunc','--filter','label=com.docker.compose.project='+self.production['project'],'--format','{{.ID}}']).splitlines())
        require(ids=={r['Id'] for r in self.before.values()},'project inventory drift')
        for service,old in self.before.items():
            row=self.inspect(service)
            require((row['Id'],row['Image'],row['State']['StartedAt'])==(old['Id'],old['Image'],old['State']['StartedAt']),'old container identity drift: '+service)
            require(digest({'Config':row['Config'],'HostConfig':row['HostConfig']})==digest({'Config':old['Config'],'HostConfig':old['HostConfig']}),'old container settings drift')
            expected='exited' if service in ('ai-tuner','execution-facts-collector') else old['State']['Status']
            require(row['State']['Status']==expected,'unexpected container lifecycle')
        for service in SERVICES:
            current=json.loads(self.run(self.compose(service)+['config','--format','json']))['services'][service]
            require(current==json.loads((self.original/(service+'-effective.json')).read_text()),'effective configuration drift')
        for image in self.images.values():self.image_proof(image)
        require(self.sql('SELECT (SELECT count(*) FROM trading.strategy_risk_policy) + (SELECT count(*) FROM trading.strategy_risk_active) + (SELECT count(*) FROM trading.strategy_risk_audit) + (SELECT count(*) FROM trading.entry_risk_reservation);')=='0','policy or reservation state no longer empty')
        self.probe('schema');same_baseline(self.capture,self.probe('pre_activation'))

    def continue_release(self):
        self.directory.mkdir(mode=0o700)
        atomic_json(self.directory/'approval.json',self.approval)
        atomic_json(self.directory/'validated-live.json',self.fresh)
        self.validating=False
        try:execute_remaining(self)
        except Exception as exc:
            self.record('failed',type(exc).__name__)
            raise

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('--bundle',required=True);p.add_argument('--approval',required=True);p.add_argument('--execute',action='store_true');a=p.parse_args()
    try:
        # Same deployment lock; no original state/proof/pin writes during validation.
        with open(Path(a.root)/'.deploy_tmp/compose_deploy.lock','r+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            runtime=RecoveryRuntime.load(a.root,a.bundle,json.loads(Path(a.approval).read_text()))
            runtime.validate_live()
            if a.execute:runtime.continue_release()
            print(json.dumps({'ok':True,'executed':a.execute,'sha':FAILED_SHA}))
        return 0
    except Exception as exc:
        print('Recovery blocked: '+type(exc).__name__,file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
