"""Deploy the reviewed source-only recovery fix under the shared production lock."""
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
SERVICE = 'new-coin-strategy'
CONTAINER = 'trading_system-new_coin'
BASE_TAG = 'local/new-coin-ownership:1878e773988c54bebefc0f07e717a3f64b8a2c31'
BASE_ID = 'sha256:2120f4e0111d1f4708f3df57c7b8aac2b4ad72ab0e3a7343c37f5b3b350c076b'
SOURCES = {
    'strategy.py': '18623dc8a5270b40735eaa701ba32e731a5c4de747c06aef161f3ec16bd10724',
    'recovery_evidence.py': '7ba574860da111c9e6006c2d382740ea49d2a3b274965c53f8ca364e3660d0af',
    'executor.py': '5e10db26cf1d7221ddc656601a1c51b1afba7fc75ee057b3833108535a39fac4',
}
SOURCE_DIR = '/app/strategies/new_coin'


def run(*args, timeout=90):
    r = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=timeout,
                       env={k: v for k, v in os.environ.items() if k != 'COMPOSE_FILE'})
    if r.returncode:
        raise RuntimeError(f'{args[0]} {args[1]} failed')
    return r.stdout.strip()


def write(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('x') as fd:
        os.chmod(temporary, 0o600)
        json.dump(value, fd, sort_keys=True)
        fd.flush()
        os.fsync(fd.fileno())
    os.replace(temporary, path)


def idle():
    sql = """
      SELECT
          (SELECT count(*) FROM new_coin.short_positions WHERE status <> 'closed') +
          (SELECT count(*) FROM trading.position_claims
             WHERE strategy='new_coin' AND claim_state IN ('PENDING','ACTIVE')) +
          (SELECT count(*) FROM trading.entry_risk_reservation
             WHERE account_id='default' AND strategy_id='new_coin' AND state<>'released')
    """
    return run('docker','exec','trading_system-postgres','psql','-X','-At','-v','ON_ERROR_STOP=1',
               '-U','trading_user','-d','trading_platform','-c',sql) == '0'


def main(revision, strategy_sha, evidence_sha, executor_sha, audit_tool_sha, audit_manifest_sha, staging):
    checksums = dict(zip(SOURCES, (strategy_sha, evidence_sha, executor_sha)))
    audit_files = {'database_permissions.py': audit_tool_sha, 'permissions.json': audit_manifest_sha}
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or any(not re.fullmatch(r'[0-9a-f]{64}', checksum)
                                                            for checksum in (checksums | audit_files).values()):
        raise ValueError('invalid release identity')
    staging = Path(staging).resolve()
    if staging != ROOT / '.deploy_tmp' / f'new-coin-ownership-{revision}':
        raise ValueError('invalid staging directory')
    if any(hashlib.sha256((staging/name).read_bytes()).hexdigest() != checksum
           for name, checksum in (checksums | audit_files).items()):
        raise ValueError('staged source checksum mismatch')
    pin = json.loads(PIN.read_text())
    old_overlay, old_state = (Path(pin[key]).resolve() for key in ('overlay','state'))
    if not all(path.is_relative_to(ROOT/'.releases') for path in (old_overlay,old_state)):
        raise ValueError('invalid predecessor paths')
    predecessor = json.loads(old_state.read_text())
    if pin['status'] != 'active' or predecessor['status'] != 'complete' or predecessor['sha'] != pin['sha']:
        raise ValueError('active predecessor is incomplete')
    overlays = json.loads(old_overlay.read_text())
    if overlays['services'][SERVICE]['image'] != BASE_TAG:
        raise RuntimeError('active New Coin image changed')
    if run('docker','inspect',CONTAINER,'--format','{{.Image}}') != BASE_ID:
        raise RuntimeError('running New Coin image changed')
    if run('docker','image','inspect',BASE_TAG,'--format','{{.Id}}') != BASE_ID:
        raise RuntimeError('New Coin base tag drift')
    if any(run('docker','exec',CONTAINER,'sha256sum',f'{SOURCE_DIR}/{name}').split()[0] != checksum
           for name, checksum in SOURCES.items()):
        raise RuntimeError('running New Coin source changed')
    if not idle():
        raise RuntimeError('New Coin has active exposure or entry intents')

    release = ROOT/'.releases'/'new-coin-ownership'/revision
    release.mkdir(mode=0o700, parents=True)
    image = f'local/new-coin-ownership:{revision}'
    run('docker','build','--network','none','-t',image,str(staging),timeout=120)
    image_id = run('docker','image','inspect',image,'--format','{{.Id}}')
    overlays['services'][SERVICE]['image'] = image
    overlay = release/'images.json'
    write(overlay, overlays)
    stacks = pin['stacks'][SERVICE]
    if stacks != ['docker-compose.yml','docker-compose.logging.yml']:
        raise RuntimeError('New Coin compose inputs changed')
    compose = ['docker','compose','--project-directory',str(ROOT),'-p','trading_system']
    for path in stacks:
        compose += ['-f',str(ROOT/path)]
    prior_config = json.loads(run(*compose,'-f',str(old_overlay),'config','--format','json'))
    new_config = json.loads(run(*compose,'-f',str(overlay),'config','--format','json'))
    prior_config['services'][SERVICE]['image'] = image
    audit = json.loads(run('python3', str(staging/'database_permissions.py'), 'audit',
                           '--manifest', str(staging/'permissions.json')))
    if audit.get('ok') is not True or audit.get('manifest_sha256') != audit_manifest_sha:
        raise RuntimeError('production database permission audit failed')
    if new_config != prior_config or not idle():
        raise RuntimeError('effective configuration or New Coin exposure changed')
    write(PIN, dict(pin,status='activating',successor_sha=revision))
    try:
        run(*compose,'-f',str(overlay),'up','-d','--no-deps','--pull','never',SERVICE)
        for _ in range(40):
            live = run('docker','inspect',CONTAINER,'--format','{{.Image}} {{.State.Health.Status}}')
            if live == f'{image_id} healthy':
                break
            time.sleep(3)
        else:
            raise RuntimeError('New Coin health or image verification failed')
        if any(run('docker','exec',CONTAINER,'sha256sum',f'{SOURCE_DIR}/{name}').split()[0] != checksum
               for name, checksum in checksums.items()):
            raise RuntimeError('running New Coin source checksum mismatch')
        state = release/'state.json'
        write(state, dict(sha=revision,status='complete',changed=[SERVICE],image=image,source_sha256=checksums))
        write(PIN, dict(pin,sha=revision,status='active',predecessor_sha=pin['sha'],
                        successor_sha=revision,overlay=str(overlay),state=str(state)))
        print('Verified New Coin recovery source and container health')
    except BaseException:
        write(release/'failure.json', dict(sha=revision,status='failed',requires_review=True))
        raise


if __name__ == '__main__':
    with (ROOT/'.deploy_tmp/compose_deploy.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        main(*sys.argv[1:])
