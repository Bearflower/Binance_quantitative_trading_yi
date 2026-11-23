"""Activate two reviewed images against the pinned runtime, with rollback."""
import fcntl
import hashlib
from decimal import Decimal
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path('/root/trading_system')
PIN = ROOT / '.deploy_tmp/ledger-risk-active.json'
PREDECESSOR = '471844f45bacbf3b313e6ecb6d6c9370b112a573'
TARGETS = {
    'original': ('btc-eth-strategy', 'trading_system-btc_eth',
                 'sha256:d37823f72d56112fe80ff025009ca5585452d42d71816578dc5ceae98c081576'),
    'aggressive': ('btc-eth-aggressive-strategy', 'trading_system-btc_eth_aggressive',
                   'sha256:97b1a37f23c4ec622bd7e11aa7dd8fbd7d6520f24aa568b481f5438f064473a0'),
}
PERMISSIONS = {
    'database_permissions.py': 'edae14be4ca16b43e5cd481f96f0a5b1ebcebc29b436d8485284c208a2622d60',
    'permissions.json': '57a484abe1826f408ccd3cfc73d134ed18d57c8709c4d3e57bdd2a9a158322fb',
}


def run(*args, input=None, timeout=240):
    reply = subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           cwd=ROOT, check=False, timeout=timeout)
    if reply.returncode:
        raise RuntimeError(f'{args[0]} {args[1]} exited {reply.returncode}')
    return reply.stdout.strip()


def read(path):
    return json.loads(path.read_text())


def atomic(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as out:
        tmp.chmod(0o600)
        json.dump(value, out, indent=2)
    tmp.replace(path)


def inspect(name):
    return json.loads(run('docker', 'inspect', name))[0]


def unrelated():
    excluded = {info[1] for info in TARGETS.values()}
    rows = run('docker', 'ps', '-aq', '--no-trunc').decode().splitlines()
    return {r['Name']: (r['Id'], r['Image'])
            for r in json.loads(run('docker', 'inspect', *rows))
            if r['Name'].lstrip('/') not in excluded}


def signed_snapshot(bundle):
    return json.loads(run('docker', 'exec', '-i', TARGETS['aggressive'][1], 'python', '-',
                          input=(bundle / 'exchange.py').read_bytes()))


def healthy(name, image):
    for _ in range(80):
        row = inspect(name)
        if row['Image'] == image and row['State'].get('Health', {}).get('Status') == 'healthy':
            return
        time.sleep(3)
    raise RuntimeError(f'health verification failed for {name}')


def audit_permissions(pin):
    contract = Path(pin['state']).parent
    for file, wanted in PERMISSIONS.items():
        if hashlib.sha256((contract / file).read_bytes()).hexdigest() != wanted:
            raise RuntimeError('permission contract hash mismatch')
    result = json.loads(run('python3', str(contract / 'database_permissions.py'), 'audit',
                            '--manifest', str(contract / 'permissions.json')))
    if not result.get('ok') or result.get('manifest_sha256') != PERMISSIONS['permissions.json']:
        raise RuntimeError('database permission audit rejected deployment')


def fingerprints(container, hashes):
    for name, data in hashes.items():
        output = run('docker', 'exec', container, 'sha256sum', '/app/' + name).decode()
        if output.split()[0] != data['after']:
            raise RuntimeError('running code hash mismatch: ' + name)


def sql(query):
    return run('docker', 'exec', 'trading_system-postgres', 'psql', '-X', '-At',
               '-v', 'ON_ERROR_STOP=1', '-U', 'trading_user', '-d', 'trading_platform',
               '-c', query)


def policy_fingerprint():
    return sql("SELECT md5(jsonb_build_array("
               "(SELECT jsonb_agg(to_jsonb(p) ORDER BY to_jsonb(p)::text) FROM trading.strategy_risk_active p),"
               "(SELECT jsonb_agg(to_jsonb(a) ORDER BY to_jsonb(a)::text) FROM public.capital_allocation a)"
               ")::text)")


def candidate_probe(image, revision, source):
    code = ("import hashlib,json,pathlib,sys; root=pathlib.Path('/app');"
            "print(json.dumps([(root/'VERSION').read_text(),"
            "{p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in sys.argv[1:]}]))")
    version, checksums = json.loads(run('docker', 'run', '--rm', '--network', 'none',
                                       '--entrypoint', 'python', image, '-c', code, *source))
    if 'GIT_SHA=' + revision not in version.splitlines() or not re.search(r'^DEPLOY_ID=[A-F0-9]{8}$', version, re.M):
        raise RuntimeError('candidate runtime marker mismatch')
    if checksums != {path: proof['after'] for path, proof in source.items()}:
        raise RuntimeError('candidate source checksum mismatch')
    return version


def started(container):
    message = '策略服务运行中'.encode()
    for _ in range(60):
        row = inspect(container)
        if row['RestartCount'] != 0:
            raise RuntimeError('strategy restarted after replacement')
        result = subprocess.run(['docker', 'logs', '--since', row['State']['StartedAt'], container],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10, check=True)
        if message in result.stdout:
            return
        if b'Traceback (most recent call last)' in result.stdout:
            raise RuntimeError('strategy reported a startup exception')
        time.sleep(3)
    raise RuntimeError('strategy scheduler initialization was not observed')


def main(revision, bundle, images):
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('invalid revision')
    bundle = Path(bundle).resolve()
    images = read(Path(images))
    if set(images) != set(TARGETS):
        raise ValueError('missing target image')
    hashes = read(bundle / 'sources.json')
    pin = read(PIN)
    if pin['sha'] == revision and pin['status'] == 'active' and read(Path(pin['state']))['status'] == 'complete':
        for variant, (_, container, _) in TARGETS.items():
            fingerprints(container, hashes[variant])
        print(json.dumps({'sha': revision, 'status': 'already_active'}))
        return
    if pin['sha'] != PREDECESSOR or pin['status'] != 'active' or read(Path(pin['state']))['status'] != 'complete':
        raise RuntimeError('active predecessor changed')
    release = ROOT / '.releases/mtpcs-entry-intent' / revision
    release.mkdir(parents=True, exist_ok=True)
    if release.is_symlink():
        raise RuntimeError('unexpected release path')
    release.chmod(0o700)
    old_overlay = Path(pin['overlay'])
    overlay = read(old_overlay)
    new_overlay = release / 'images.json'
    old_commands, new_commands, expected, versions = {}, {}, {}, {}
    before = unrelated()
    for variant, (service, container, base) in TARGETS.items():
        if inspect(container)['Image'] != base:
            raise RuntimeError('running base changed: ' + service)
        image = images[variant]
        repo = {'original': 'trading-btc-eth', 'aggressive': 'trading-btc-eth-aggr'}[variant]
        if not re.fullmatch(r'ghcr\.io/bearflower/' + repo + r'@sha256:[a-f0-9]{64}', image):
            raise ValueError('candidate digest is not pinned')
        run('docker', 'pull', image, timeout=450)
        expected[variant] = run('docker', 'image', 'inspect', '-f', '{{.Id}}', image).decode()
        versions[variant] = candidate_probe(image, revision, hashes[variant])
        overlay['services'][service]['image'] = image
        compose = ['docker', 'compose', '--project-directory', str(ROOT), '-p', 'trading_system']
        for name in pin['stacks'][service]:
            if Path(name).name != name:
                raise ValueError('unexpected compose path')
            compose.extend(['-f', str(ROOT / name)])
        old_commands[variant] = [*compose, '-f', str(old_overlay)]
        new_commands[variant] = [*compose, '-f', str(new_overlay)]
    atomic(new_overlay, overlay)
    for variant, (service, _, _) in TARGETS.items():
        old = json.loads(run(*old_commands[variant], 'config', '--format', 'json'))['services'][service]
        new = json.loads(run(*new_commands[variant], 'config', '--format', 'json'))['services'][service]
        old_image = run('docker', 'image', 'inspect', '-f', '{{.Id}}', old['image']).decode()
        if old_image != TARGETS[variant][2]:
            raise RuntimeError('predecessor image does not resolve for rollback')
        old['image'] = images[variant]
        if old != new:
            raise RuntimeError('unexpected compose change: ' + service)
    audit_permissions(pin)
    policy = policy_fingerprint()
    if sql("SELECT count(*) FROM trading.entry_risk_reservation "
           "WHERE strategy_id='btc_eth_aggressive' AND account_id='default' AND state='reserved'") != b'0':
        raise RuntimeError('an unsubmitted aggressive reservation needs reconciliation')
    market = signed_snapshot(bundle)
    if market['orders']:
        raise RuntimeError('working ordinary orders on MTPCS symbols')
    for symbol, _, qty in market['positions']:
        amount = Decimal(qty)
        protected = sum(Decimal(x[4]) for x in market['algos']
                        if x[0] == symbol and x[2] == ('SELL' if amount > 0 else 'BUY')
                        and x[3] in ('STOP', 'STOP_MARKET') and x[5] in (True, 'true'))
        if protected < abs(amount):
            raise RuntimeError('position protection incomplete: ' + symbol)
    state = release / 'state.json'
    atomic(state, dict(sha=revision, status='activating', before=market))
    atomic(PIN, dict(pin, status='activating', successor_sha=revision))
    changed = []
    try:
        for variant, (service, container, _) in TARGETS.items():
            changed.append(variant)
            run(*new_commands[variant], 'up', '-d', '--no-deps', '--pull', 'never', service)
            healthy(container, expected[variant])
            version = run('docker', 'exec', container, 'cat', '/app/VERSION').decode()
            if versions[variant].strip() != version:
                raise RuntimeError('runtime revision mismatch')
            fingerprints(container, hashes[variant])
            started(container)
        after = signed_snapshot(bundle)
        if unrelated() != before or policy_fingerprint() != policy:
            raise RuntimeError('market state or excluded containers changed during rollout')
        atomic(state, dict(sha=revision, status='complete', images=images, before=market, after=after))
        atomic(PIN, dict(pin, sha=revision, predecessor_sha=pin['sha'], status='active',
                         state=str(state), overlay=str(new_overlay)))
    except BaseException:
        failed = []
        for variant in reversed(changed):
            service, container, base = TARGETS[variant]
            try:
                run(*old_commands[variant], 'up', '-d', '--no-deps', '--pull', 'never', service)
                healthy(container, base)
            except Exception:
                failed.append(service)
        status = 'rollback_failed' if failed else 'rolled_back'
        atomic(state, dict(sha=revision, status=status, before=market, failed=failed))
        atomic(PIN, dict(pin, status='rollback_failed') if failed else pin)
        raise


if __name__ == '__main__':
    with (ROOT / '.deploy_tmp/compose_deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        main(*sys.argv[1:])
