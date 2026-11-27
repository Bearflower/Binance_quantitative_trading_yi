#!/usr/bin/env python3
"""XAU 版本追踪受限发布；仅更新两个影子容器。

Private resolved Compose snapshots contain environment values and are kept 0600.
Public evidence contains only identities, health, hashes, and code provenance.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

ROOT = Path('/root/trading_system')
MAIN_TARGET = '/app/strategies/btc_eth/shadow/main.py'
CALIBRATED_MAIN = '/root/trading_system/.releases/xau-attribution/603a2a4e2cceac83ceee1328dfc14f78ddd33ad5/main.py'
BASES = {
    'shadow': 'ghcr.io/bearflower/trading-btc-eth@sha256:80a5d2fc78f768f659f9ba5855013704ea3628f691d2afe9c48ed6550488962c',
}
TARGETS = {
    'mtcps-xau-shadow': ('trading_system-mtcps-xau-shadow', 'shadow'),
    'mtcps-xau-calibrated-shadow': ('trading_system-mtcps-xau-calibrated-shadow', 'shadow'),
}
FILES = {
    'shadow': ['strategies/btc_eth/shadow/collector.py', 'strategies/btc_eth/shadow/main.py', 'VERSION'],
}
AUDIT_FILES = ('scripts/database_permissions.py', 'database/postgres/permissions.json')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(*args, timeout=180, include_stderr=False):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    if result.returncode:
        # Never expose resolved environments or arbitrary container output in CI logs.
        raise RuntimeError(f'{args[0]} operation failed (exit {result.returncode})')
    return result.stdout + (result.stderr if include_stderr else "")


def inspect(name):
    return json.loads(run('docker', 'inspect', name))[0]


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    path.chmod(0o600)


def escape_compose(value):
    """Compose reparses frozen config; protect already resolved dollar literals."""
    if isinstance(value, str):
        return value.replace('$', '$$')
    if isinstance(value, list):
        return [escape_compose(item) for item in value]
    if isinstance(value, dict):
        return {key: escape_compose(item) for key, item in value.items()}
    return value


def single_service_config(original, service, config):
    frozen = copy.deepcopy(original)
    frozen['services'] = {service: copy.deepcopy(config)}
    frozen['services'][service].pop('depends_on', None)
    # Compose prunes unused named-volume definitions on rendering a reduced stack.
    used = {mount['source'] for mount in config.get('volumes', []) if mount.get('type') == 'volume'}
    volumes = {name: value for name, value in frozen.get('volumes', {}).items() if name in used}
    if volumes:
        frozen['volumes'] = volumes
    else:
        frozen.pop('volumes', None)
    return frozen


def freeze_compose(path, config):
    save(path, escape_compose(config))
    reparsed = json.loads(run(*compose_command(path), 'config', '--format', 'json'))
    require(reparsed == config, 'frozen Compose configuration changed on reparse')


def snapshot():
    ids = run('docker', 'ps', '-aq').split()
    if not ids:
        return {}
    data = json.loads(run('docker', 'inspect', *ids))
    return {item['Name'].lstrip('/'): {
        'id': item['Id'], 'image': item['Image'],
        'running': item['State']['Running'], 'restarts': item['RestartCount'],
        'health': item['State'].get('Health', {}).get('Status'),
    } for item in data}


def verify_excluded(before, after):
    names = {name for name, _ in TARGETS.values()}
    require({k: v for k, v in before.items() if k not in names}
            == {k: v for k, v in after.items() if k not in names}, 'excluded container changed')


def compose_paths(label):
    paths = label.split(',')
    for name in paths:
        path = Path(name)
        require(path.is_absolute() and '..' not in path.parts and path.is_relative_to(ROOT),
                'Compose path outside production root')
    return paths


def changed_service(before, service, image, main_source):
    require(service in TARGETS, 'service outside release scope')
    after = copy.deepcopy(before)
    after['image'] = image
    if TARGETS[service][1] == 'shadow':
        after.setdefault('environment', {})['SHADOW_SKIP_INITIAL_COLLECTION'] = '1'
    mounts = [v for v in after.get('volumes', []) if v.get('target') == MAIN_TARGET]
    require(not mounts, 'unexpected main bind')
    return after


def validate_delta(before, after, service, image, main_source):
    require(after == changed_service(before, service, image, main_source), 'configuration drift beyond release scope')


def validate_manifests(hashes, digests):
    require(re.fullmatch(r'[0-9a-f]{40}', hashes.get('git_sha', '')), 'invalid git SHA')
    require(re.fullmatch(r'[0-9A-F]{8,}', hashes.get('deploy_id', '')), 'invalid deploy ID')
    require(set(digests) == set(FILES) and set(hashes.get('files', {})) == set(FILES), 'invalid artifact scope')
    require(set(hashes.get('audit_files', {})) == set(AUDIT_FILES), 'invalid audit resource scope')
    require(all(re.fullmatch(r'[0-9a-f]{64}', value) for value in hashes['audit_files'].values()),
            'invalid audit resource hash')
    for group, paths in FILES.items():
        require(re.fullmatch(r'ghcr.io/bearflower/trading-xau-attribution-' + group + r'@sha256:[0-9a-f]{64}', digests[group]), 'image must be digest pinned')
        expected = hashes['files'][group]
        require(set(expected) == set(paths), 'unexpected deployed files')
        require(all(re.fullmatch(r'[0-9a-f]{64}', value) for value in expected.values()), 'invalid file hash')


def healthy(info):
    return info['State'].get('Running') is True and info['State'].get('Health', {}).get('Status') == 'healthy'


def wait_health(name, expected_image, zero_restarts=True):
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        info = inspect(name)
        require(info['Image'] == expected_image, f'{name}: unexpected image')
        if zero_restarts:
            require(info['RestartCount'] == 0, f'{name}: restarted')
        if healthy(info):
            return info
        time.sleep(3)
    raise RuntimeError(f'{name}: health deadline exceeded')


def pin_live_environment(config, info):
    """A concurrent completed release may update root .env for other consumers.

    Freeze the exact existing target environment, including image defaults;
    replacing the mapping prevents new root keys leaking into this scoped release.
    """
    pinned = copy.deepcopy(config)
    pinned['environment'] = dict(value.split('=', 1) for value in info['Config'].get('Env', []))
    # 后续完整镜像发布已移除此精确来源的 main 挂载，旧 Compose 标签仍指向旧快照。
    # 仅当实际容器也没有该挂载时修正基线，禁止重新引入旧代码或移除未知挂载。
    if not any(m.get('Destination') == MAIN_TARGET for m in info.get('Mounts', [])):
        if 'volumes' in pinned:
            pinned['volumes'] = [m for m in pinned['volumes'] if not (
                m.get('type') == 'bind' and m.get('target') == MAIN_TARGET
                and m.get('source') == CALIBRATED_MAIN and m.get('read_only') is True)]
    return pinned


def validate_live(config, info, image_info):
    """Reject common drift between current Compose files and the running target."""
    image_config = image_info['Config']
    expected_env = dict(value.split('=', 1) for value in image_config.get('Env', []))
    expected_env.update({k: str(v) for k, v in config.get('environment', {}).items() if v is not None})
    actual_env = dict(value.split('=', 1) for value in info['Config'].get('Env', []))
    require(expected_env == actual_env, 'live environment differs from resolved Compose')
    for key, compose_key in [('Cmd', 'command'), ('Entrypoint', 'entrypoint'), ('WorkingDir', 'working_dir'), ('User', 'user')]:
        expected = config.get(compose_key, image_config.get(key))
        actual = info['Config'].get(key)
        if key in ('User', 'WorkingDir'):
            expected, actual = expected or '', actual or ''
        require(expected == actual, f'live {compose_key} differs from resolved Compose')
    expected_mounts = {}
    for mount in config.get('volumes', []):
        source = mount.get('source', '')
        if mount['type'] == 'volume':
            # Named-volume engine paths are not Compose source names.
            expected_mounts[mount['target']] = (mount['type'], source, not mount.get('read_only', False))
        else:
            expected_mounts[mount['target']] = (mount['type'], source, not mount.get('read_only', False))
    actual_mounts = {m['Destination']: (m['Type'], m.get('Name', '') if m['Type'] == 'volume' else m['Source'], m['RW']) for m in info['Mounts']}
    require(expected_mounts == actual_mounts, 'live mounts differ from resolved Compose')
    host = info['HostConfig']
    limits = config.get('deploy', {}).get('resources', {}).get('limits', {})
    require(int(config.get('mem_limit', limits.get('memory', 0))) == host.get('Memory', 0), 'live memory differs from resolved Compose')
    require(int(float(config.get('cpus', limits.get('cpus', 0))) * 1_000_000_000) == host.get('NanoCpus', 0), 'live CPU differs from resolved Compose')
    require(config.get('restart', 'no').split(':')[0] == host['RestartPolicy']['Name'], 'live restart policy differs')
    require(bool(config.get('privileged', False)) == host.get('Privileged', False), 'live privileged differs')
    require(bool(config.get('read_only', False)) == host.get('ReadonlyRootfs', False), 'live readonly differs')


def runtime_config(info):
    config = copy.deepcopy(info['Config'])
    for key in ['Image', 'Hostname']:
        config.pop(key, None)
    config['Env'] = dict(value.split('=', 1) for value in config.get('Env', []))
    for key in ['config-hash', 'image', 'project.config_files', 'version', 'depends_on']:
        config.get('Labels', {}).pop('com.docker.compose.' + key, None)
    networks = {name: {'IPAMConfig': net.get('IPAMConfig'), 'DriverOpts': net.get('DriverOpts'),
                       'Aliases': sorted(a for a in net.get('Aliases', []) or [] if not re.fullmatch(r'[0-9a-f]{12,64}', a))}
                for name, net in info['NetworkSettings']['Networks'].items()}
    return {'config': config, 'host': copy.deepcopy(info['HostConfig']),
            'mounts': sorted(copy.deepcopy(info['Mounts']), key=lambda value: value['Destination']),
            'networks': networks}


def validate_runtime(before, after, service, main_source, rollback=False):
    expected = runtime_config(before)
    if not rollback:
        if TARGETS[service][1] == 'shadow':
            expected['config']['Env']['SHADOW_SKIP_INITIAL_COLLECTION'] = '1'
    require(expected == runtime_config(after), 'runtime configuration changed beyond release scope')


def file_hashes(name, paths):
    output = run('docker', 'exec', name, 'sha256sum', *['/app/' + path for path in paths])
    return {line.split(None, 1)[1].strip().removeprefix('/app/'): line.split()[0] for line in output.splitlines()}


def verify_files(command, expected):
    output = run(*command, 'sha256sum', *['/app/' + p for p in expected])
    actual = {}
    for line in output.splitlines():
        digest, path = line.split(None, 1)
        actual[path.strip().removeprefix('/app/')] = digest
    require(actual == expected, 'deployed file hash mismatch')


def verify_version(name, hashes):
    version = run('docker', 'exec', name, 'cat', '/app/VERSION')
    values = dict(line.split('=', 1) for line in version.splitlines() if '=' in line)
    require(values.get('GIT_COMMIT') == hashes['git_sha'] and values.get('GIT_SHA') == hashes['git_sha'] and values.get('DEPLOY_ID') == hashes['deploy_id'], 'VERSION provenance mismatch')


def audit_permissions(release, expected):
    """只运行本次发布包中通过哈希校验的权威审计，不修改持久权限。"""
    require(set(expected) == set(AUDIT_FILES), 'invalid audit resource scope')
    for path, digest in expected.items():
        require(hashlib.sha256((release / Path(path).name).read_bytes()).hexdigest() == digest,
                'audit resource hash mismatch')
    return run('python3', str(release / 'database_permissions.py'), 'audit',
               '--manifest', str(release / 'permissions.json'), timeout=300)


def compose_command(path):
    return ['docker', 'compose', '--profile', '*', '--project-directory', str(ROOT), '-p', 'trading_system', '-f', str(path)]


def execute_plans(plans, release, hashes, main, before, originals, original_hashes):
    attempted = []
    try:
        for plan in plans:
            service, name, group, old_path, new_path, old_id, new_id = plan
            attempted.append(plan)  # Include partially failed recreate in rollback.
            run(*compose_command(new_path), 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', service)
            live = wait_health(name, new_id)
            validate_runtime(originals[name], live, service, str(main))
            verify_files(['docker', 'exec', name], hashes['files'][group])
            verify_version(name, hashes)
            verify_excluded(before, snapshot())
        for service, name, group, old_path, new_path, old_id, new_id in plans:
            wait_health(name, new_id)
            logs = run('docker', 'logs', '--since', inspect(name)['State']['StartedAt'], name, include_stderr=True)
            require(not re.search(r'(?im)\b(?:ERROR|CRITICAL|FATAL)\b|Traceback \(most recent call last\)', logs), f'{name}: startup errors')
        after = snapshot()
        verify_excluded(before, after)
        save(release / 'after.json', after)
        save(release / 'result.json', {'status': 'verified', 'git_sha': hashes['git_sha'], 'deploy_id': hashes['deploy_id'], 'targets': list(TARGETS)})
        print(json.dumps({'status': 'verified', 'release': str(release), 'targets': list(TARGETS)}))
    except BaseException:
        failures = []
        for service, name, group, old_path, new_path, old_id, new_id in reversed(attempted):
            try:
                run(*compose_command(old_path), 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', service)
                restored = wait_health(name, old_id)
                validate_runtime(originals[name], restored, service, str(main), rollback=True)
                verify_files(['docker', 'exec', name], original_hashes[name])
            except Exception as exc:
                failures.append(f'{name}: {type(exc).__name__}')
        save(release / 'rollback.json', {'failures': failures, 'containers': snapshot()})
        verify_excluded(before, snapshot())
        if failures:
            raise RuntimeError('rollback requires attention: ' + ', '.join(failures))
        raise


def activate(package):
    hashes = json.loads((package / 'hashes.json').read_text())
    digests = json.loads((package / 'digests.json').read_text())
    validate_manifests(hashes, digests)
    release = ROOT / '.releases/xau-attribution' / hashes['git_sha']
    require(not release.exists(), 'release SHA already attempted; inspect prior evidence before retry')
    release.mkdir(parents=True, mode=0o700)
    os.chmod(release, 0o700)
    for filename in ['hashes.json', 'digests.json', 'main.py', 'activate.py',
                     'database_permissions.py', 'permissions.json']:
        (release / filename).write_bytes((package / filename).read_bytes())
        (release / filename).chmod(0o600)
    main = release / 'main.py'
    require(hashlib.sha256(main.read_bytes()).hexdigest() == hashes['files']['shadow']['strategies/btc_eth/shadow/main.py'], 'uploaded main hash mismatch')
    before = snapshot()
    save(release / 'before.json', before)
    plans = []
    originals = {}
    original_hashes = {}
    for service, (name, group) in TARGETS.items():
        info = inspect(name)
        require(healthy(info), f'{name}: original unhealthy')
        live_env = dict(value.split('=', 1) for value in info['Config'].get('Env', []))
        require(not any(live_env.get(key) for key in ('GIT_COMMIT', 'GIT_SHA')), f'{name}: environment overrides VERSION provenance')
        originals[name] = info
        original_hashes[name] = file_hashes(name, FILES[group])
        labels = info['Config']['Labels']
        require(labels.get('com.docker.compose.project') == 'trading_system'
                and labels.get('com.docker.compose.service') == service, 'unexpected compose ownership')
        require(labels.get('com.docker.compose.project.working_dir') == str(ROOT), 'unexpected compose working directory')
        # Resolve the pinned manifest to the engine's config image ID, not a tag.
        run('docker', 'pull', BASES[group], timeout=300)
        old_image = json.loads(run('docker', 'image', 'inspect', BASES[group]))[0]
        require(info['Image'] == old_image['Id'], f'{name}: original image drift')
        command = ['docker', 'compose', '--profile', '*', '--project-directory', str(ROOT), '-p', 'trading_system']
        paths = compose_paths(labels.get('com.docker.compose.project.config_files', ''))
        for path in paths:
            require(Path(path).is_file(), 'missing original Compose file')
            command += ['-f', path]
        original = json.loads(run(*command, 'config', '--format', 'json'))
        baseline = pin_live_environment(original['services'][service], info)
        original['services'][service] = baseline
        validate_live(baseline, info, old_image)
        baseline_path = release / f'{service}.live-baseline.json'
        freeze_compose(baseline_path, original)
        command = compose_command(baseline_path)
        # Pin rollback to the exact local image ID, regardless of mutable original tags.
        rollback = single_service_config(original, service, baseline)
        rollback['services'][service]['image'] = info['Image']
        rollback['services'][service].pop('depends_on', None)
        old_path = release / f'{service}.rollback.json'
        freeze_compose(old_path, rollback)
        run('docker', 'pull', digests[group], timeout=300)
        new_image = json.loads(run('docker', 'image', 'inspect', digests[group]))[0]
        # New layers must preserve the inherited process/environment/resources.
        require(new_image['Config'] == old_image['Config'], 'thin image changed runtime defaults')
        # A temporary stopped container allows image-only verification without executing code.
        temp = run('docker', 'create', '--network', 'none', '--entrypoint', 'sha256sum', digests[group], *['/app/' + p for p in FILES[group]]).strip()
        try:
            output = run('docker', 'start', '-a', temp)
            require(inspect(temp)['State']['ExitCode'] == 0, 'image checksum process failed')
            actual = {line.split(None, 1)[1].strip().removeprefix('/app/'): line.split()[0] for line in output.splitlines()}
            require(actual == hashes['files'][group], 'image-only hash mismatch')
        finally:
            run('docker', 'rm', '-f', temp)
        after = changed_service(baseline, service, digests[group], str(main))
        # 只叠加镜像和跳过首轮采集标记，运行时挂载必须原样保留。
        overlay = {'services': {service: {'image': digests[group]}}}
        if group == 'shadow':
            overlay['services'][service]['environment'] = {'SHADOW_SKIP_INITIAL_COLLECTION': '1'}
        overlay_path = release / f'{service}.overlay.json'
        save(overlay_path, overlay)
        resolved = json.loads(run(*command, '-f', str(overlay_path), 'config', '--format', 'json'))
        validate_delta(baseline, resolved['services'][service], service, digests[group], str(main))
        for other in original['services']:
            if other != service:
                require(original['services'][other] == resolved['services'][other], 'overlay changed another service')
        frozen = single_service_config(resolved, service, after)
        new_path = release / f'{service}.activate.json'
        freeze_compose(new_path, frozen)
        plans.append((service, name, group, old_path, new_path, info['Image'], new_image['Id']))
    save(release / 'original-hashes.json', original_hashes)
    verify_excluded(before, snapshot())
    # Mandatory read-only authoritative permissions gate, immediately before activation.
    audit = audit_permissions(release, hashes['audit_files'])
    (release / 'database-audit.txt').write_text(audit)
    (release / 'database-audit.txt').chmod(0o600)
    execute_plans(plans, release, hashes, main, before, originals, original_hashes)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    def interrupted(signum, frame):
        raise RuntimeError(f'release interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    lock = ROOT / '.deploy_tmp/compose_deploy.lock'
    lock.parent.mkdir(exist_ok=True)
    with lock.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        activate(args.package.resolve())


if __name__ == '__main__':
    main()
