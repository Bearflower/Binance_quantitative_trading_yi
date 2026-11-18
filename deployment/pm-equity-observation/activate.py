"""Activate the valuation sampler on the inspected AI Tuner image only."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


ROOT = Path('/root/trading_system')
PIN = ROOT / '.deploy_tmp/ledger-risk-active.json'
SERVICE = 'ai-tuner'
BASE_ID = 'sha256:97cb758159ae41089a5e133a5d2cbf8686d3ca9a66e5db9f88ee63494735f5ff'
BASE_IMAGE = 'ghcr.io/bearflower/trading-ai-tuner@' + BASE_ID
BASE_JOBS_SHA = '3d1b0e058c0b2753aa71ff5c8c126f628d5a0b13527a67cb659a1291f601a7be'
JOBS = '/app/ai_tuner/monitor/evidence_jobs.py'
BRIDGE = '/app/shared/accounting/pm_equity_bridge.py'


def run(*args, timeout=90):
    result = subprocess.run(
        args, cwd=ROOT, text=True, capture_output=True, timeout=timeout,
        env={key: value for key, value in os.environ.items() if key != 'COMPOSE_FILE'},
    )
    if result.returncode:
        raise RuntimeError(f'{args[0]} {args[1]} failed')
    return result.stdout.strip()


def write(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('x') as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def assert_source(path, expected):
    actual = run('docker', 'exec', SERVICE, 'sha256sum', path).split()[0]
    if actual != expected:
        raise RuntimeError(f'source checksum mismatch for {Path(path).name}')


def running_health():
    return run('docker', 'inspect', SERVICE, '--format', '{{.Image}} {{.State.Health.Status}}')


def policy_hash():
    sql = """SELECT md5(jsonb_build_array(
        (SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM trading.strategy_risk_policy t),
        (SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM trading.strategy_risk_active t),
        (SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM public.capital_allocation t)
      )::text)"""
    return run('docker', 'exec', 'trading_system-postgres', 'psql', '-X', '-At',
               '-v', 'ON_ERROR_STOP=1', '-U', 'trading_user', '-d', 'trading_platform', '-c', sql)


def latest_sample(started):
    check = """import json, sys
from pathlib import Path
files = [p for p in Path('/app/logs/trial_evidence/pm-equity').glob('*/*.json')
         if p.stat().st_mtime_ns >= int(sys.argv[1])]
if files:
    sample = json.loads(max(files, key=lambda p: p.stat().st_mtime_ns).read_text())
    if (sample.get('account_scope') == 'default'
            and sample.get('method') == 'pm-usdt-equity-bridge-v1'
            and sample.get('status') in ('VERIFIED_POINT', 'BLOCKED_DATA')):
        print(sample['status'])
"""
    return run('docker', 'exec', SERVICE, 'python', '-c', check, str(started))


def main(revision, jobs_sha, equity_sha, staging):
    if (not re.fullmatch(r'[0-9a-f]{40}', revision)
            or any(not re.fullmatch(r'[0-9a-f]{64}', x) for x in (jobs_sha, equity_sha))):
        raise ValueError('invalid release identity')
    paths = {JOBS: jobs_sha, BRIDGE: equity_sha}
    staging = Path(staging).resolve()
    if staging != ROOT / '.deploy_tmp' / f'pm-equity-observation-{revision}':
        raise ValueError('unexpected staging directory')
    if any(hashlib.sha256((staging / Path(path).name).read_bytes()).hexdigest() != sha
           for path, sha in paths.items()):
        raise RuntimeError('staged source checksum mismatch')

    pin = json.loads(PIN.read_text())
    previous, prior_state = (Path(pin[key]).resolve() for key in ('overlay', 'state'))
    if not all(path.is_relative_to(ROOT / '.releases') for path in (previous, prior_state)):
        raise RuntimeError('invalid predecessor paths')
    state = json.loads(prior_state.read_text())
    overlay = json.loads(previous.read_text())
    if (pin['status'] != 'active' or state['status'] != 'complete' or state['sha'] != pin['sha']
            or overlay['services'][SERVICE]['image'] != BASE_IMAGE
            or running_health() != f'{BASE_ID} healthy'
            or run('docker', 'image', 'inspect', BASE_IMAGE, '--format', '{{.Id}}') != BASE_ID):
        raise RuntimeError('reviewed AI Tuner predecessor changed')
    assert_source(JOBS, BASE_JOBS_SHA)
    policy = policy_hash()

    release = ROOT / '.releases' / 'pm-equity-observation' / revision
    release.mkdir(mode=0o700, parents=True)
    image = f'local/pm-equity-observation:{revision}'
    run('docker', 'build', '--network', 'none', '--build-arg', f'BASE_IMAGE={BASE_IMAGE}',
        '-t', image, str(staging), timeout=180)
    image_id = run('docker', 'image', 'inspect', image, '--format', '{{.Id}}')
    overlay['services'][SERVICE]['image'] = image
    candidate = release / 'images.json'
    write(candidate, overlay)
    stacks = pin['stacks'][SERVICE]
    if stacks != ['docker-compose.yml', 'docker-compose.logging.yml']:
        raise RuntimeError('AI Tuner compose stack changed')
    compose = ['docker', 'compose', '--project-directory', str(ROOT), '-p', 'trading_system']
    for stack in stacks:
        compose.extend(['-f', str(ROOT / stack)])
    old = json.loads(run(*compose, '-f', str(previous), 'config', '--format', 'json'))
    new = json.loads(run(*compose, '-f', str(candidate), 'config', '--format', 'json'))
    old['services'][SERVICE]['image'] = image
    if new != old or policy_hash() != policy:
        raise RuntimeError('effective compose, policy or allocation changed')
    write(PIN, dict(pin, status='activating', successor_sha=revision))
    try:
        started = time.time_ns()
        run(*compose, '-f', str(candidate), 'up', '-d', '--no-deps', '--pull', 'never', SERVICE)
        for _ in range(40):
            if running_health() == f'{image_id} healthy':
                observation = latest_sample(started)
                if observation:
                    break
            time.sleep(3)
        else:
            raise RuntimeError('scheduler did not persist a new valuation observation')
        for path, sha in paths.items():
            assert_source(path, sha)
        if policy_hash() != policy:
            raise RuntimeError('policies or allocations changed')
        receipt = release / 'state.json'
        write(receipt, dict(sha=revision, status='complete', changed=[SERVICE],
                            image=image, valuation_observation=observation, policy_hash=policy))
        write(PIN, dict(pin, sha=revision, status='active', predecessor_sha=pin['sha'],
                        successor_sha=revision, overlay=str(candidate), state=str(receipt)))
        print(f'AI Tuner healthy, fresh persisted observation: {observation}')
    except BaseException:
        run(*compose, '-f', str(previous), 'up', '-d', '--no-deps', '--pull', 'never', SERVICE)
        for _ in range(40):
            if running_health() == f'{BASE_ID} healthy':
                write(PIN, pin)
                write(release / 'failure.json', dict(sha=revision, status='rolled_back'))
                break
            time.sleep(3)
        raise


if __name__ == '__main__':
    with (ROOT / '.deploy_tmp/compose_deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        main(*sys.argv[1:])
