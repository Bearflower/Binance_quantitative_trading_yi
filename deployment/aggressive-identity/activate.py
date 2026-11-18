"""One-image pinned aggressive release; keep failed market transitions for review."""
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

ROOT = Path('/root/trading_system')
PIN_SHA = '7d5b70047af0bc40b54ddc9cd9953e9460bb2d64'
OVERLAY_SHA = 'b13268fc899bab75c2eade92cb1b5624a81f183845f4927a567083b0c464a5b9'
BASE_IMAGE = 'sha256:a432f964360e98bb9796c264cfe80f9ccb051d0da865ac611127a8d341335ef5'
SERVICE = 'btc-eth-aggressive-strategy'
CONTAINER = 'trading_system-btc_eth_aggressive'
SOURCE = ('shared/entry_risk_policy.py', 'shared/position_ownership.py',
          'strategies/btc_eth_aggressive/main.py',
          'strategies/btc_eth_aggressive/strategy.py')
GATE = 'deployment/aggressive-identity/exchange_gate.py'

def run(args, *, timeout=180, input=None):
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=timeout,
                            input=input,
                            env={k: v for k, v in os.environ.items() if k != 'COMPOSE_FILE'})
    if result.returncode:
        raise RuntimeError('command failed: ' + ' '.join(args[:2]))
    return result.stdout.strip()

def sha(data):
    return hashlib.sha256(data).hexdigest()

def atomic(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('x') as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temp, path)

def inspect(name):
    return json.loads(run(['docker', 'inspect', name]))[0]

def inventory():
    names = run(['docker', 'ps', '-aq', '--no-trunc']).splitlines()
    return {r['Name'].lstrip('/'): (r['Id'], r['Image'], r['State']['StartedAt'])
            for r in json.loads(run(['docker', 'inspect', *names]))}

def policy_hash():
    sql = "SELECT md5(jsonb_build_array((SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM trading.strategy_risk_policy t),(SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM trading.strategy_risk_active t),(SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM public.capital_allocation t))::text)"
    return run(['docker', 'exec', 'trading_system-postgres', 'psql', '-X', '-At',
                '-v', 'ON_ERROR_STOP=1', '-U', 'trading_user', '-d', 'trading_platform', '-c', sql])

def gate(bundle):
    result = run(['docker', 'exec', '-i', 'trading_system-hrs', 'python', '-'],
                 input=(bundle / GATE).read_text())
    proof = json.loads(result.splitlines()[-1])
    if proof.get('ok') is not True or proof.get('read_only') is not True:
        raise RuntimeError('read-only exchange gate failed')
    return proof

def same_exchange(before, after):
    keys = ('position_exposure', 'ordinary_orders_sha256', 'algo_orders_sha256',
            'allocation_sha256', 'reservations_sha256')
    if any(before.get(k) != after.get(k) for k in keys):
        raise RuntimeError('exchange or allocation changed during release')

def main(revision, digest, bundle_name):
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('invalid immutable identity')
    bundle = Path(bundle_name).resolve()
    if not bundle.is_relative_to(ROOT / '.deploy_tmp'):
        raise ValueError('bundle outside staging')
    manifest = json.loads((bundle / 'manifest.json').read_text())
    if manifest['sha'] != revision or set(manifest['files']) != set(SOURCE) | {GATE}:
        raise ValueError('source manifest mismatch')
    for name, expected in manifest['files'].items():
        path = (bundle / name).resolve()
        if not path.is_relative_to(bundle) or sha(path.read_bytes()) != expected:
            raise ValueError('source checksum mismatch')
    pin_file = ROOT / '.deploy_tmp/ledger-risk-active.json'
    pin = json.loads(pin_file.read_text())
    old_overlay = Path(pin['overlay'])
    if (pin.get('sha') != PIN_SHA or pin.get('status') != 'active'
            or not old_overlay.resolve().is_relative_to(ROOT / '.releases')
            or sha(old_overlay.read_bytes()) != OVERLAY_SHA):
        raise RuntimeError('reviewed active pin changed')
    state = json.loads(Path(pin['state']).read_text())
    if state.get('status') != 'complete' or state.get('sha') != PIN_SHA:
        raise RuntimeError('predecessor incomplete')
    before = inventory()
    if before[CONTAINER][1] != BASE_IMAGE or inspect(CONTAINER)['State']['Health']['Status'] != 'healthy':
        raise RuntimeError('aggressive predecessor image or health changed')
    pending = run(['docker', 'exec', 'trading_system-postgres', 'psql', '-X', '-At',
                   '-v', 'ON_ERROR_STOP=1', '-U', 'trading_user', '-d', 'trading_platform', '-c',
                   "SELECT count(*) FROM trading.entry_risk_reservation WHERE account_id='default' AND strategy_id='btc_eth_aggressive' AND state='reserved' AND NOT COALESCE(geometry->'signal' ? 'entry_decision_identity',false)"])
    if pending != '0':
        raise RuntimeError('legacy reserved intent requires reconciliation')
    exchange = gate(bundle)
    policies = policy_hash()
    release = ROOT / '.releases/aggressive-identity' / revision
    release.mkdir(parents=True, exist_ok=False)
    os.chmod(release, 0o700)
    image = 'ghcr.io/bearflower/trading-btc-eth-aggr@' + digest
    run(['docker', 'pull', image], timeout=600)
    code = "import hashlib,json,pathlib; print(json.dumps({'version':pathlib.Path('/app/VERSION').read_text(),'files':{p:hashlib.sha256(pathlib.Path('/app',p).read_bytes()).hexdigest() for p in " + repr(SOURCE) + "}}))"
    proof = json.loads(run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python', image, '-c', code]))
    if proof['files'] != {p: manifest['files'][p] for p in SOURCE} or 'GIT_SHA=' + revision not in proof['version'].splitlines():
        raise RuntimeError('image source mismatch')
    overlay = json.loads(old_overlay.read_text())
    overlay['services'][SERVICE]['image'] = image
    successor = release / 'images.json'
    atomic(successor, overlay)
    stacks = pin['stacks'][SERVICE]
    if not stacks or stacks[0] != 'docker-compose.yml' or not set(stacks) <= {'docker-compose.yml', 'docker-compose.override.yml', 'docker-compose.logging.yml'}:
        raise RuntimeError('unexpected compose stack')
    compose = ['docker', 'compose', '--project-directory', str(ROOT), '-p', 'trading_system']
    for name in stacks:
        compose.extend(['-f', str(ROOT / name)])
    old = json.loads(run(compose + ['-f', str(old_overlay), 'config', '--format', 'json']))
    new = json.loads(run(compose + ['-f', str(successor), 'config', '--format', 'json']))
    old['services'][SERVICE]['image'] = image
    if old != new:
        raise RuntimeError('compose drift beyond aggressive image')
    same_exchange(exchange, gate(bundle))
    if inventory() != before or policy_hash() != policies:
        raise RuntimeError('state changed before activation')
    atomic(release / 'state.json', {'sha': revision, 'status': 'preflight', 'changed': []})
    atomic(pin_file, dict(pin, status='activating', successor_sha=revision))
    try:
        run(compose + ['-f', str(successor), 'up', '-d', '--no-deps', '--pull', 'never', SERVICE])
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            current = inspect(CONTAINER)
            if current['State']['Status'] == 'running' and current['State'].get('Health', {}).get('Status') == 'healthy':
                break
            time.sleep(3)
        else:
            raise RuntimeError('aggressive health timeout')
        if current['Image'] != run(['docker', 'image', 'inspect', '-f', '{{.Id}}', image]):
            raise RuntimeError('running image mismatch')
        after = inventory()
        exchange_after = gate(bundle)
        same_exchange(exchange, exchange_after)
        if (set(before) != set(after) or any(after[n] != v for n, v in before.items() if n != CONTAINER)
                or policy_hash() != policies):
            raise RuntimeError('excluded state changed')
        atomic(release / 'exchange-before.json', exchange)
        atomic(release / 'exchange-after.json', exchange_after)
        atomic(release / 'state.json', {'sha': revision, 'status': 'complete', 'changed': [SERVICE],
                                      'source_proof': proof, 'policy_unchanged': True,
                                      'other_containers_unchanged': True})
        atomic(pin_file, dict(pin, sha=revision, status='active', predecessor_sha=PIN_SHA,
                              overlay=str(successor), state=str(release / 'state.json')))
        print(json.dumps({'sha': revision, 'status': 'complete', 'changed': [SERVICE]}))
    except BaseException:
        atomic(release / 'failure.json', {'status': 'failed', 'requires_review': True})
        raise

if __name__ == '__main__':
    with (ROOT / '.deploy_tmp/compose_deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        main(*sys.argv[1:])
