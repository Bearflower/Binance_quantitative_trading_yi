"""Activate the HRS entry hotfix, retaining the original release configuration."""
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys
import time

from deploy_optimization import (
    atomic, compose, environment, initialized_proof, inspect, preflight,
    read_initialization, run,
)

ROOT = Path('/root/trading_system')
CONTAINER = 'trading_system-hrs'
SERVICE = 'hrs-strategy'
PIN = '9bc1231cb50a7a49328fea7fec60d2a2ee5ba35d'
OVERLAY = '2a2cd10322c3e6707306616d6571beee2cfc77f735c3c29a9eb7333d0e1aa076'
PREDECESSOR_HRS = 'f144eb073c14ae3fdeb7f2d4239eced97aec97e1'
BASE = 'sha256:4d7cd6ad3fcd50d3ec2ed51936200c451a860da1a236c9da5e2a17eb4251df75'
INTERRUPTED = 'a45e47758a7d895628481852871f4f0b8c68fdf7'
INTERRUPTED_IMAGE = 'sha256:6f33f52ecb3e6bb9e3492c52a601e4610393049200617a9e22a411b827a2e38c'

SOURCES = ('shared/entry_risk_policy.py', 'strategies/hrs/executor.py', 'strategies/hrs/core_decision.py')


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


def wait_ready(image_id, sha, config_hash):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        current = inspect(CONTAINER)
        if (current['Image'] == image_id
                and current['State'].get('Health', {}).get('Status') == 'healthy'):
            try:
                return initialized_proof(read_initialization(), current['State']['StartedAt'], sha, config_hash)
            except RuntimeError:
                pass
        time.sleep(3)
    raise RuntimeError('HRS health or initialization verification timed out')


def main(sha, image_digest, bundle_name):
    if not re.fullmatch('[0-9a-f]{40}', sha) or not re.fullmatch('sha256:[0-9a-f]{64}', image_digest):
        raise ValueError('invalid release identity')
    bundle = Path(bundle_name).resolve()
    if bundle != ROOT / '.deploy_tmp' / ('hrs-entry-hotfix-' + sha):
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
    original = ROOT/'.releases/trial-evidence'/PIN
    previous = original/'images.json'
    predecessor = json.loads((original/'state.json').read_text())
    if (sha256(previous) != OVERLAY or predecessor.get('sha') != PIN
            or predecessor.get('status') != 'complete'):
        raise RuntimeError('reviewed predecessor incomplete')
    old_image = inspect(CONTAINER)
    previous_runtime = (old_image['Image'], environment(old_image).get('HRS_RELEASE_SHA'))
    established = (pin['sha'] == PIN and pin['status'] == 'active'
                   and Path(pin['overlay']) == previous and previous_runtime == (BASE, PREDECESSOR_HRS))
    interrupted_dir = ROOT/'.releases/hrs-entry-hotfix'/INTERRUPTED
    interrupted_runtime = previous_runtime == (INTERRUPTED_IMAGE, INTERRUPTED)
    interrupted_pin = (pin['sha'] == PIN and pin['status'] == 'activating'
                       and pin.get('successor_sha') == INTERRUPTED and Path(pin['overlay']) == previous)
    finished_pin = (pin['sha'] == INTERRUPTED and pin['status'] == 'active'
                    and pin.get('predecessor_sha') == PIN
                    and Path(pin['overlay']) == interrupted_dir/'images.json')
    if not established:
        if not interrupted_runtime or not (interrupted_pin or finished_pin):
            raise RuntimeError('reviewed live predecessor changed')
        prior_overlay = json.loads(previous.read_text())
        first_image = 'ghcr.io/bearflower/trading-hrs@' + INTERRUPTED_IMAGE
        prior_overlay['services'][SERVICE]['image'] = first_image
        prior_overlay['services'][SERVICE]['environment']['HRS_RELEASE_SHA'] = INTERRUPTED
        if prior_overlay != json.loads((interrupted_dir/'images.json').read_text()):
            raise RuntimeError('interrupted candidate changed configuration')
    rollback_pin = dict(pin, sha=PIN, status='active', overlay=str(previous),
                        state=str(original/'state.json'), successor_sha=PIN,
                        predecessor_sha='d9c4e6c1b1dabe3594ef07150e2be46bc1018b3f')
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
        "Path('/app',p).read_bytes()).hexdigest() for p in " + repr(SOURCES) + "}))"
    )
    proof = json.loads(run(['docker', 'run', '--rm', '--network', 'none',
                            '--entrypoint', 'python', image, '-c', proof_code]))
    if any(proof.get(p) != manifest['files'][p] for p in SOURCES):
        raise RuntimeError('candidate source proof differs')

    release = ROOT/'.releases/hrs-entry-hotfix'/sha
    release.mkdir(mode=0o700, parents=True, exist_ok=False)
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
        raise RuntimeError('unexpected account scope')
    atomic(release/'state.json', dict(sha=sha, status='preflight', protection=protection))
    atomic(pin_path, dict(pin, status='activating', successor_sha=sha))
    try:
        run(candidate_command+['up', '-d', '--no-deps', '--pull', 'never', SERVICE])
        initialized = wait_ready(checked_id, sha, config_hash)
        fresh_protection = preflight()
        after = inventory()
        if (fresh_protection != protection or policy_hash() != policies
                or set(before) != set(after)
                or any(after[n] != v for n,v in before.items() if n != CONTAINER)
                or permission_audit(bundle) != permission_manifest):
            raise RuntimeError('protection, policy, permission or excluded runtime drift')
        state = release/'state.json'
        atomic(state, dict(sha=sha, status='complete', changed=[CONTAINER], protection=fresh_protection,
                           permission_manifest=permission_manifest, source_proof=proof,
                           initialization=initialized, image=image, superseded_interrupted_release=not established))
        atomic(pin_path, dict(pin, sha=sha, status='active', predecessor_sha=pin['sha'],
                              successor_sha=sha, overlay=str(target), state=str(state)))
        print(json.dumps(dict(sha=sha, status='complete', changed=[CONTAINER],
                              protected_positions=len(fresh_protection['positions']))), flush=True)
    except Exception:
        try:
            run(original_command+['up', '-d', '--no-deps', '--pull', 'never', SERVICE])
            wait_ready(BASE, PREDECESSOR_HRS, config_hash)
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
