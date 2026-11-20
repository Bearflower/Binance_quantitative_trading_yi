"""Activate the HRS pool-circuit denial evidence, retaining the original release configuration."""
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys
import time

from deploy_optimization import (
    atomic, compose, environment, initialized_proof, inspect, preflight,
    read_initialization, run, utc,
)

ROOT = Path('/root/trading_system')
CONTAINER = 'trading_system-hrs'
SERVICE = 'hrs-strategy'
PIN = '097e6975430efd8391d324eaaf04cd342047bb9f'
OVERLAY = '095a28850eaffb81dc6595c2a3eb39ebb82f45fffdbe7003d4e4af962c1a44d3'
PREDECESSOR_HRS = PIN
BASE = 'sha256:2142c4d0cebccdc46f8c3c78aa67c9c5d5c767ebd690519a14f4f74d3cb82499'
SOURCES = ('strategies/hrs/strategy.py',)
HISTORY_RECEIPT = '0e29f6ae16ac4e0bea4346df54c93d72b0921138ae3c73675a48ef33c84c1692'


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


def verify_history_receipt():
    sql = """SELECT count(*) FROM trading.hrs_equity_reconciliation WHERE account_id='default'
        AND receipt_sha256='"""+HISTORY_RECEIPT+"""' AND receipt->>'kind'='reviewed_historical_mark_gap_v1'
        AND receipt->>'backfilled_points'='640' AND receipt->>'retained_mark_bars'='634'"""
    output = run(['docker','exec','trading_system-postgres','psql','-XAt','-v','ON_ERROR_STOP=1',
                  '-U','trading_user','-d','trading_platform','-c',sql])
    if output != '1':
        raise RuntimeError('reviewed HRS history receipt missing')


def wait_ready(image_id, sha, config_hash):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        current = inspect(CONTAINER)
        if (current['Image'] == image_id
                and current['State'].get('Health', {}).get('Status') == 'healthy'):
            try:
                started = current['State']['StartedAt']
                initialized = initialized_proof(read_initialization(), started, sha, config_hash)
                return initialized
            except RuntimeError:
                pass
        time.sleep(3)
    raise RuntimeError('HRS health or initialization verification timed out')


def main(sha, image_digest, bundle_name):
    if not re.fullmatch('[0-9a-f]{40}', sha) or not re.fullmatch('sha256:[0-9a-f]{64}', image_digest):
        raise ValueError('invalid release identity')
    bundle = Path(bundle_name).resolve()
    if bundle != ROOT / '.deploy_tmp' / ('hrs-pool-denial-' + sha):
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
    original = ROOT/'.releases/hrs-flat-reconcile'/PIN
    previous = original/'images.json'
    predecessor = json.loads((original/'state.json').read_text())
    if (sha256(previous) != OVERLAY or predecessor.get('sha') != PIN
            or predecessor.get('status') != 'complete'):
        raise RuntimeError('reviewed predecessor incomplete')
    old_image = inspect(CONTAINER)
    previous_runtime = (old_image['Image'], environment(old_image).get('HRS_RELEASE_SHA'))
    if (pin['sha'] != PIN or pin['status'] != 'active' or Path(pin['overlay']) != previous
            or previous_runtime != (BASE, PREDECESSOR_HRS)):
        raise RuntimeError('reviewed live predecessor changed')
    rollback_pin = pin.copy()
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

    release = ROOT/'.releases/hrs-pool-denial'/sha
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
    verify_history_receipt()
    before = inventory()
    protection = preflight()
    if protection.get('account_id') != 'default' or protection['positions']:
        raise RuntimeError('expected independently verified flat HRS')
    atomic(release/'state.json', dict(sha=sha, status='preflight', protection=protection))
    atomic(pin_path, dict(pin, status='activating', successor_sha=sha))
    try:
        run(candidate_command+['up', '-d', '--no-deps', '--pull', 'never', SERVICE])
        initialized = wait_ready(checked_id, sha, config_hash)
        verify_history_receipt()
        replay = run(['docker','exec','-i',CONTAINER,'python','-'],
                     input=(bundle/'deployment/hrs-pool-denial/replay.py').read_text())
        if replay.splitlines()[-1:] != ['HRS_POOL_REPLAY_OK']:
            raise RuntimeError('deployed pool decision replay missing')
        fresh_protection = preflight()
        after = inventory()
        if (fresh_protection['account_id'] != protection['account_id'] or policy_hash() != policies
                or set(before) != set(after)
                or any(after[n] != v for n,v in before.items() if n != CONTAINER)
                or permission_audit(bundle) != permission_manifest):
            raise RuntimeError('protection, policy, permission or excluded runtime drift')
        state = release/'state.json'
        atomic(state, dict(sha=sha, status='complete', changed=[CONTAINER], protection=fresh_protection,
                           permission_manifest=permission_manifest, source_proof=proof,
                           initialization=initialized, history_receipt_sha256=HISTORY_RECEIPT,
                           pool_replay='passed', image=image))
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
