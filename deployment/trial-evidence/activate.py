"""Scoped CI activation: one AI Tuner image with existing evidence migrations."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime

ROOT = Path('/root/trading_system')
PIN_SHA = 'c02b045654c73c8c5cb7e89c5735e9e72eb1b392'
OVERLAY_SHA = 'd961341a4585454d2f8fd18a8c88c0522c30977d05333be329a1536fc8af1eee'
BASE_IMAGE = 'sha256:8cc131057bd8130231fb5a2f7defb040bbd5c5f94f8449f81e27b2bda4c46316'
MIGRATIONS = ()


def run(args, *, input=None, timeout=180):
    result = subprocess.run(args, cwd=ROOT, text=True, input=input,
                            capture_output=True, timeout=timeout,
                            env={k:v for k,v in os.environ.items() if k != 'COMPOSE_FILE'})
    if result.returncode:
        # Docker inspect/compose outputs can contain credentials; never print them.
        raise RuntimeError('command failed: ' + ' '.join(args[:2]))
    return result.stdout.strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('x') as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, sort_keys=True, indent=2)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def inventory():
    ids = run(['docker','ps','-aq','--no-trunc']).splitlines()
    rows = json.loads(run(['docker','inspect',*ids]))
    return {r['Name'].lstrip('/'): (r['Id'],r['Image'],r['State']['StartedAt']) for r in rows}


def policy_hash():
    sql = "SELECT md5(jsonb_build_array((SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM trading.strategy_risk_policy t),(SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM trading.strategy_risk_active t),(SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM public.capital_allocation t))::text)"
    return run(['docker','exec','trading_system-postgres','psql','-X','-At','-v','ON_ERROR_STOP=1',
                '-U','trading_user','-d','trading_platform','-c',sql])


def fresh_observation(observation, started_ms):
    risk = observation.get('account-risk-latest.json', {})
    activity = observation.get('entry-activity-latest.json', {})
    return (risk.get('account_id') == 'default'
            and risk.get('budget_id') == 'default-pm-5pct-20261117-v1'
            and bool(risk.get('baseline')) and risk.get('as_of_ms', 0) >= started_ms
            and activity.get('account_id') == 'default'
            and activity.get('source') == 'signed_pm_get_order_v1'
            and activity.get('finished_ms', 0) >= started_ms and bool(activity.get('batch_id')))


def main(sha, image_digest, bundle):
    if not re.fullmatch('[0-9a-f]{40}',sha) or not re.fullmatch('sha256:[0-9a-f]{64}',image_digest):
        raise ValueError('invalid release identity')
    bundle = Path(bundle).resolve()
    if not bundle.is_relative_to(ROOT / '.deploy_tmp'):
        raise ValueError('bundle outside deploy staging')
    manifest = json.loads((bundle/'manifest.json').read_text())
    if manifest['sha'] != sha:
        raise ValueError('bundle SHA mismatch')
    for name, checksum in manifest['files'].items():
        path = (bundle/name).resolve()
        if not path.is_relative_to(bundle) or digest(path.read_bytes()) != checksum:
            raise ValueError('bundle source mismatch')
    pin_path = ROOT/'.deploy_tmp/ledger-risk-active.json'
    pin = json.loads(pin_path.read_text())
    previous = Path(pin['overlay'])
    if (pin.get('sha') != PIN_SHA or pin.get('status') != 'active'
            or not previous.resolve().is_relative_to(ROOT/'.releases')
            or digest(previous.read_bytes()) != OVERLAY_SHA):
        raise RuntimeError('reviewed active release changed')
    before = inventory()
    if before['ai-tuner'][1] != BASE_IMAGE:
        raise RuntimeError('reviewed AI Tuner image changed')
    state = json.loads(Path(pin['state']).read_text())
    if state.get('sha') != PIN_SHA or state.get('status') != 'complete':
        raise RuntimeError('predecessor incomplete')
    release = ROOT/'.releases/trial-evidence'/sha
    release.mkdir(parents=True, exist_ok=False)
    os.chmod(release,0o700)
    image = 'ghcr.io/bearflower/trading-ai-tuner@'+image_digest
    run(['docker','pull',image],timeout=600)
    source_paths = [p for p in manifest['files'] if p.startswith(('ai_tuner/','shared/'))]
    code = "import hashlib,json,pathlib;print(json.dumps({p:hashlib.sha256(pathlib.Path('/app',p).read_bytes()).hexdigest() for p in "+repr(source_paths)+"}))"
    proof = json.loads(run(['docker','run','--rm','--network','none','--entrypoint','python',image,'-c',code]))
    if any(proof.get(p) != manifest['files'][p] for p in source_paths):
        raise RuntimeError('built image source mismatch')
    overlay = json.loads(previous.read_text())
    overlay['services']['ai-tuner']['image'] = image
    target = release/'images.json'
    atomic(target,overlay)
    stacks = pin['stacks']['ai-tuner']
    if stacks != ['docker-compose.yml','docker-compose.logging.yml']:
        raise RuntimeError('AI Tuner compose stack changed')
    compose = ['docker','compose','--project-directory',str(ROOT),'-p','trading_system']
    for name in stacks:
        compose.extend(['-f',str(ROOT/name)])
    old = json.loads(run(compose+['-f',str(previous),'config','--format','json']))
    new = json.loads(run(compose+['-f',str(target),'config','--format','json']))
    old['services']['ai-tuner']['image'] = image
    if old != new:
        raise RuntimeError('effective configuration drift beyond one image')
    policies = policy_hash()
    atomic(release/'state.json',dict(status='preflight',sha=sha,image=image,source_proof=proof))
    # Each migration owns its transaction, verified as additive and idempotent.
    for name in MIGRATIONS:
        run(['docker','exec','-i','trading_system-postgres','psql','-X','-v','ON_ERROR_STOP=1',
             '-U','trading_migration_admin','-d','trading_platform'],
             input=(bundle/'database/postgres/migrations'/name).read_text())
    if policy_hash() != policies or inventory() != before:
        raise RuntimeError('policies/allocations/containers changed before activation')
    atomic(pin_path,dict(pin,status='activating',successor_sha=sha))
    run(['docker','tag',BASE_IMAGE,'trial-evidence-rollback:'+sha])
    try:
        run(compose+['-f',str(target),'up','-d','--no-deps','--pull','never','ai-tuner'])
        deadline = time.monotonic()+300
        while time.monotonic()<deadline:
            rows = json.loads(run(['docker','inspect','ai-tuner']))
            current = rows[0]
            if (current['State']['Status']=='running'
                    and current['State'].get('Health',{}).get('Status')=='healthy'):
                break
            time.sleep(3)
        else:
            raise RuntimeError('AI Tuner health timeout')
        if current['Image'] != run(['docker','image','inspect','-f','{{.Id}}',image]):
            raise RuntimeError('running image mismatch')
        started_ms = int(datetime.fromisoformat(current['State']['StartedAt'].replace('Z','+00:00')).timestamp()*1000)
        # Wait for actual scheduler invocations, not merely Python imports.
        code = "import json,pathlib; p=pathlib.Path('/app/logs/trial_evidence'); print(json.dumps({n:json.loads((p/n).read_text()) for n in ('account-risk-latest.json','entry-activity-latest.json')}))"
        observation = None
        while time.monotonic()<deadline:
            try:
                observation = json.loads(run(['docker','exec','ai-tuner','python','-c',code]))
                risk = observation['account-risk-latest.json']
                if fresh_observation(observation, started_ms):
                    break
            except RuntimeError:
                pass
            time.sleep(3)
        else:
            raise RuntimeError('scheduled evidence collection missing valid baseline')
        after = inventory()
        if (set(before)!=set(after) or any(after[n]!=v for n,v in before.items() if n!='ai-tuner')
                or policy_hash()!=policies):
            raise RuntimeError('excluded containers/policies/allocations changed')
        atomic(release/'observation.json',observation)
        atomic(release/'state.json',dict(sha=sha,status='complete',changed=['ai-tuner'],image=image,
                    source_proof=proof,other_containers_unchanged=True,risk_policy_unchanged=True,
                    migrations=list(MIGRATIONS),completed_at_ms=int(time.time()*1000)))
        atomic(pin_path,dict(pin,sha=sha,status='active',predecessor_sha=pin['sha'],
                            successor_sha=sha,overlay=str(target),state=str(release/'state.json')))
        print(json.dumps(dict(status='complete',sha=sha,changed=['ai-tuner'],
                              risk_status=risk['status'])))
    except BaseException:
        # New evidence schema remains append-only; never erase facts or rebase NAV.
        atomic(release/'failure.json',dict(status='failed',sha=sha,requires_review=True))
        raise

if __name__=='__main__':
    with (ROOT/'.deploy_tmp/compose_deploy.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        main(*sys.argv[1:])
