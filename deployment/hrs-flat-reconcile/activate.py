"""Activate HRS flat reconciliation and its daily inspection, retaining the live configuration."""
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time

from deploy_optimization import (
    atomic, compose, environment, initialized_proof, inspect, preflight,
    read_initialization, run, utc,
)

ROOT = Path('/root/trading_system')
CONTAINER = 'trading_system-hrs'
SERVICE = 'hrs-strategy'
PIN = '214835f26d3a7e2d5fbaccbb2cd63d99ff455042'
OVERLAY = '1b01b232e92dd4ef056833a28aafa7495da088b5062bbbd908edce7341adb1ae'
BASE = 'sha256:275cc48238529ccb29069576c53bb67c2234298ecb33f66950418620309c9b40'
SOURCES = ('shared/entry_risk_policy.py', 'shared/position_ownership.py', 'strategies/hrs/strategy.py')
INSPECTOR = 'ai_tuner/ops/hrs_acceptance.py'
INSPECTOR_BASE = '7444052d6fb2121513886cbacfd019900fd69752e4c4160bc9e674d8ceb68cb6'


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory():
    ids = run(['docker', 'ps', '-aq', '--no-trunc']).splitlines()
    rows = json.loads(run(['docker', 'inspect', *ids]))
    return {r['Name'].lstrip('/'): (r['Id'], r['Image'], r['State']['StartedAt']) for r in rows}


def permission_audit(bundle):
    output = run(['python3', bundle/'scripts/database_permissions.py', 'audit',
                  '--manifest', bundle/'database/postgres/permissions.json'])
    result = json.loads(output)
    if result.get('ok') is not True or result.get('issues'):
        raise RuntimeError('database permission audit failed')
    return result['manifest_sha256']


def policy_hash():
    sql = """SELECT md5(jsonb_build_array(
      (SELECT jsonb_agg(to_jsonb(r) ORDER BY to_jsonb(r)::text) FROM trading.strategy_risk_policy r),
      (SELECT jsonb_agg(to_jsonb(r) ORDER BY to_jsonb(r)::text) FROM trading.strategy_risk_active r),
      (SELECT jsonb_agg(to_jsonb(r) ORDER BY to_jsonb(r)::text) FROM public.capital_allocation r))::text)"""
    return run(['docker', 'exec', 'trading_system-postgres', 'psql', '-XAt', '-v', 'ON_ERROR_STOP=1',
                '-U', 'trading_user', '-d', 'trading_platform', '-c', sql])


def replace_inspector(path, content, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.hrs-acceptance-', delete=False) as handle:
        temp = Path(handle.name)
        try:
            handle.write(content)
            handle.close()
            temp.chmod(mode)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def wait_ready(image_id, sha, config_hash):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        current = inspect(CONTAINER)
        if (current['Image'] == image_id
                and current['State'].get('Health', {}).get('Status') == 'healthy'):
            try:
                started = current['State']['StartedAt']
                return initialized_proof(read_initialization(), started, sha, config_hash)
            except RuntimeError:
                pass
        time.sleep(3)
    raise RuntimeError('HRS health or initialization verification timed out')


def main(sha, image_digest, bundle_name):
    if not re.fullmatch('[0-9a-f]{40}', sha) or not re.fullmatch('sha256:[0-9a-f]{64}', image_digest):
        raise ValueError('invalid release identity')
    bundle = Path(bundle_name).resolve()
    if bundle != ROOT / '.deploy_tmp' / ('hrs-flat-reconcile-' + sha):
        raise ValueError('invalid release staging directory')
    manifest = json.loads((bundle/'manifest.json').read_text())
    if manifest['sha'] != sha:
        raise RuntimeError('staging revision differs')
    for name, digest in manifest['files'].items():
        path = (bundle/name).resolve()
        if not path.is_relative_to(bundle) or sha256(path) != digest:
            raise RuntimeError('staging files differ')

    pin_path = ROOT/'.deploy_tmp/ledger-risk-active.json'
    pin = json.loads(pin_path.read_text())
    original = ROOT/'.releases/hrs-history-gap'/PIN
    previous = original/'images.json'
    predecessor = json.loads((original/'state.json').read_text())
    if (sha256(previous) != OVERLAY or predecessor.get('sha') != PIN
            or predecessor.get('status') != 'complete'):
        raise RuntimeError('reviewed predecessor incomplete')
    old_image = inspect(CONTAINER)
    previous_runtime = (old_image['Image'], environment(old_image).get('HRS_RELEASE_SHA'))
    if (pin['sha'] != PIN or pin['status'] != 'active' or Path(pin['overlay']) != previous
            or previous_runtime != (BASE, PIN)):
        raise RuntimeError('reviewed live predecessor changed')
    rollback_pin = pin.copy()
    inspector = ROOT/INSPECTOR
    inspector_before = inspector.read_bytes()
    inspector_mode = inspector.stat().st_mode & 0o777
    if hashlib.sha256(inspector_before).hexdigest() != INSPECTOR_BASE:
        raise RuntimeError('live HRS inspector has changed')
    inspector_candidate = (bundle/INSPECTOR).read_bytes()
    compile(inspector_candidate, INSPECTOR, 'exec')
    for unit in ('trading-hrs-acceptance.timer', 'trading-daily-digest.timer'):
        if run(['systemctl', 'is-active', unit]) != 'active':
            raise RuntimeError(f'expected active inspection timer: {unit}')
    baseline = read_initialization()
    config_hash = baseline['loaded_config']['config_hash']
    if not config_hash or baseline.get('evidence_context_serializable') is not True:
        raise RuntimeError('old configuration or evidence context unverified')

    files = pin['stacks'][SERVICE]
    if files != ['docker-compose.yml', 'docker-compose.override.yml', 'docker-compose.logging.yml']:
        raise RuntimeError('reviewed HRS compose stack changed')
    files = [ROOT/f for f in files]
    original_command = compose(ROOT, files+[previous])
    image = 'ghcr.io/bearflower/trading-hrs@' + image_digest
    run(['docker', 'pull', image], timeout=600)
    checked_id = run(['docker', 'image', 'inspect', '-f', '{{.Id}}', image])
    proof_code = (
        "from pathlib import Path;import hashlib,json;print(json.dumps({p:hashlib.sha256("
        "Path('/app',p).read_bytes()).hexdigest() for p in " + repr((*SOURCES, 'VERSION')) + "}))"
    )
    proof = json.loads(run(['docker', 'run', '--rm', '--network', 'none',
                            '--entrypoint', 'python', image, '-c', proof_code]))
    if any(proof.get(p) != manifest['files'][p] for p in (*SOURCES, 'VERSION')):
        raise RuntimeError('candidate source proof differs')

    release = ROOT/'.releases/hrs-flat-reconcile'/sha
    release.mkdir(mode=0o700, parents=True, exist_ok=False)
    inspector_backup = release/'hrs_acceptance.previous.py'
    inspector_backup.write_bytes(inspector_before)
    inspector_backup.chmod(0o600)
    overlay = json.loads(previous.read_text())
    hotfix = overlay['services'][SERVICE]
    hotfix['image'] = image
    hotfix['environment']['HRS_RELEASE_SHA'] = sha
    target = release/'images.json'
    atomic(target, overlay)
    candidate_command = compose(ROOT, files+[target])
    old = json.loads(run(original_command+['config', '--format', 'json']))
    new = json.loads(run(candidate_command+['config', '--format', 'json']))
    old['services'][SERVICE]['image'] = image
    old['services'][SERVICE]['environment']['HRS_RELEASE_SHA'] = sha
    if old != new:
        raise RuntimeError('effective compose configuration drift')

    permission_manifest = permission_audit(bundle)
    policies = policy_hash()
    before = inventory()
    protection = preflight()
    if protection.get('account_id') != 'default':
        raise RuntimeError('unexpected HRS account')
    atomic(release/'state.json', dict(sha=sha, status='preflight', protection=protection))
    atomic(pin_path, dict(pin, status='activating', successor_sha=sha))
    inspector_changed = False
    try:
        run(candidate_command+['up', '-d', '--no-deps', '--pull', 'never', SERVICE])
        initialized = wait_ready(checked_id, sha, config_hash)
        fresh_protection = preflight()
        after = inventory()
        if (fresh_protection['account_id'] != protection['account_id']
                or fresh_protection['positions'] != protection['positions']
                or fresh_protection['filled_retained_reservations'] > protection['filled_retained_reservations']
                or policy_hash() != policies
                or set(before) != set(after)
                or any(after[n] != v for n,v in before.items() if n != CONTAINER)
                or permission_audit(bundle) != permission_manifest):
            raise RuntimeError('protection, policy, permission or excluded runtime drift')
        if sha256(inspector) != INSPECTOR_BASE:
            raise RuntimeError('live HRS inspector changed during activation')
        replace_inspector(inspector, inspector_candidate, inspector_mode)
        inspector_changed = True
        if sha256(inspector) != manifest['files'][INSPECTOR]:
            raise RuntimeError('deployed inspector checksum differs')
        state = release/'state.json'
        atomic(state, dict(sha=sha, status='complete', changed=[CONTAINER], protection=fresh_protection,
                           permission_manifest=permission_manifest, source_proof=proof,
                           initialization=initialized, image=image, inspector_sha256=sha256(inspector)))
        atomic(pin_path, dict(pin, sha=sha, status='active', predecessor_sha=pin['sha'],
                              successor_sha=sha, overlay=str(target), state=str(state)))
        print(json.dumps(dict(sha=sha, status='complete', changed=[CONTAINER],
                              protected_positions=len(fresh_protection['positions']))), flush=True)
    except Exception:
        try:
            if inspector_changed:
                replace_inspector(inspector, inspector_before, inspector_mode)
                if sha256(inspector) != INSPECTOR_BASE:
                    raise RuntimeError('inspector rollback checksum differs')
            run(original_command+['up', '-d', '--no-deps', '--pull', 'never', SERVICE])
            wait_ready(BASE, PIN, config_hash)
            preflight()
            atomic(pin_path, rollback_pin)
            atomic(release/'state.json', dict(sha=sha, status='failed', rollback='restored'))
        except Exception:
            atomic(release/'state.json', dict(sha=sha, status='failed', rollback='unverified'))
            raise RuntimeError('HRS release failed and rollback requires review') from None
        raise RuntimeError('HRS release failed; verified predecessor restored') from None


if __name__ == '__main__':
    with (ROOT/'.deploy_tmp/compose_deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        main(*sys.argv[1:])
