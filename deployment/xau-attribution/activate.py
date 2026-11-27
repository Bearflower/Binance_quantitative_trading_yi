#!/usr/bin/env python3
"""仅按完整容器 ID 切换两个 XAU 影子实例；保留旧容器供精确回退。"""
from __future__ import annotations
import argparse
import copy
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import time

ROOT = Path('/root/trading_system')
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



def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--package', type=Path)
    mode.add_argument('--recover', type=Path, help='只恢复本执行器已落盘的精确容器切换')
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
        if args.recover:
            recover_release(args.recover.resolve())
        else:
            activate(args.package.resolve())



def save(path, value):
    """切换阶段必须先落盘；私有证据包含环境信息，禁止写入 CI 输出。"""
    temporary = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def network_config(info):
    result = {}
    for name, config in info['NetworkSettings']['Networks'].items():
        require(not config.get('IPAMConfig') and not config.get('DriverOpts'), 'custom network requires review')
        aliases = [v for v in config.get('Aliases') or [] if v not in (info['Id'], info['Id'][:12])]
        result[name] = {'Aliases': sorted(aliases)}
    return result


def normalized_config(info):
    value = copy.deepcopy(info['Config'])
    value.pop('Hostname', None)
    value.pop('MacAddress', None)
    return value


def normalized_host(info):
    value = copy.deepcopy(info['HostConfig'])
    if value.get('OomKillDisable') is None:
        value['OomKillDisable'] = False
    return value


def clone_body(old, image, sha):
    """只克隆已审定的影子运行配置；不枚举 Compose service。"""
    require(old['Name'].lstrip('/') in {v[0] for v in TARGETS.values()}, 'target outside scope')
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', image) and re.fullmatch(r'[a-f0-9]{40}', sha), 'invalid clone identity')
    require(old['Config'].get('Hostname') in ('', old['Id'][:12]), 'custom hostname requires review')
    require(not old['Config'].get('MacAddress') and not old['Config'].get('Domainname'), 'custom address requires review')
    require(not old['HostConfig'].get('VolumesFrom') and not old['HostConfig'].get('Mounts'), 'structured volume requires review')
    require(all(m['Type'] == 'bind' and m['Destination'] in ('/app/logs', '/app/data') for m in old['Mounts']), 'mount outside reviewed scope')
    require(not old['Config'].get('Volumes'), 'image volumes require review')
    require(old['Config'].get('Healthcheck', {}).get('Test', ['NONE'])[0] != 'NONE', 'missing healthcheck')
    env = old['Config'].get('Env') or []
    require(len(env) == len({v.split('=', 1)[0] for v in env}) and all('=' in v for v in env), 'ambiguous environment')
    env_map = dict(v.split('=', 1) for v in env)
    require(env_map.get('SHADOW_SKIP_INITIAL_COLLECTION') == '1', 'startup replay must already be disabled')
    require(not any(env_map.get(k) for k in ('GIT_COMMIT', 'GIT_SHA')), 'environment overrides code provenance')
    body = normalized_config(old)
    body['Image'] = image
    body['Labels'] = dict(body.get('Labels') or {}, **{'org.opencontainers.image.revision': sha, 'codex.xau.source': sha})
    body['HostConfig'] = normalized_host(old)
    body['HostConfig']['RestartPolicy'] = {'Name': 'no', 'MaximumRetryCount': 0}
    body['NetworkingConfig'] = {'EndpointsConfig': network_config(old)}
    return body


def mounts(info):
    return sorted(info['Mounts'], key=lambda m: m['Destination'])


def validate_clone(old, new, image, sha, *, staged):
    require(new['Config'].get('Hostname') in ('', new['Id'][:12]), 'candidate has custom hostname')
    body = clone_body(old, image, sha)
    expected_config = {k: v for k, v in body.items() if k not in ('HostConfig', 'NetworkingConfig')}
    expected_host = body['HostConfig']
    if not staged:
        expected_host['RestartPolicy'] = old['HostConfig']['RestartPolicy']
    require(normalized_config(new) == expected_config, 'cloned process configuration changed')
    require(normalized_host(new) == expected_host, 'cloned host configuration changed')
    require(network_config(old) == network_config(new), 'cloned network aliases changed')
    require(mounts(old) == mounts(new), 'cloned mounts changed')


def verify_same_runtime(old, current):
    require(current['Id'] == old['Id'] and current['Image'] == old['Image'], 'predecessor identity changed')
    require(normalized_config(current) == normalized_config(old)
            and normalized_host(current) == normalized_host(old)
            and network_config(current) == network_config(old)
            and mounts(current) == mounts(old), 'predecessor runtime changed')


def verify_excluded(before, after, extras=None):
    names = {name for name, _ in TARGETS.values()}
    extras = extras or {}
    for name, expected in extras.items():
        require(name not in before and after.get(name) == expected, 'owned backup changed')
    require({k: v for k, v in before.items() if k not in names}
            == {k: v for k, v in after.items() if k not in names and k not in extras}, 'excluded container changed')


class DockerConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect('/var/run/docker.sock')


def create_container(name, body):
    require(re.fullmatch(r'[a-zA-Z0-9_.-]+', name), 'invalid staged name')
    connection = DockerConnection('localhost', timeout=60)
    try:
        connection.request('POST', '/v1.44/containers/create?name=' + name, json.dumps(body), {'Content-Type': 'application/json'})
        response = connection.getresponse()
        payload = json.loads(response.read())
        require(response.status == 201, 'Docker create rejected')
        require(re.fullmatch(r'[a-f0-9]{64}', payload.get('Id', '')), 'invalid created ID')
        return payload['Id']
    finally:
        connection.close()


def restart_arg(policy):
    name = policy['Name']
    require(name in ('no', 'always', 'unless-stopped', 'on-failure'), 'unsupported restart policy')
    return name + (':' + str(policy['MaximumRetryCount']) if name == 'on-failure' else '')


def set_phase(release, plan, phase):
    plan['phase'] = phase
    save(release / (plan['name'] + '.state.json'), plan)


def restore_plan(plan, release):
    """只恢复已登记的两个精确 ID；无法证明名称归属则停止。"""
    name, old_id, new_id = plan['name'], plan['old']['Id'], plan['new_id']
    require(all(re.fullmatch(r'[a-f0-9]{64}', value) for value in (old_id, new_id)), 'rollback requires full IDs')
    old, new = inspect(old_id), inspect(new_id)
    require(old['Name'].lstrip('/') in (name, plan['backup_name']), 'rollback predecessor renamed externally')
    require(new['Name'].lstrip('/') in (name, plan['stage_name']), 'rollback candidate renamed externally')
    require(old['HostConfig']['RestartPolicy'] in (
        plan['old']['HostConfig']['RestartPolicy'], {'Name': 'no', 'MaximumRetryCount': 0}), 'rollback restart policy drift')
    normalized_old = copy.deepcopy(old)
    normalized_old['HostConfig']['RestartPolicy'] = plan['old']['HostConfig']['RestartPolicy']
    verify_same_runtime(plan['old'], normalized_old)
    canonical = snapshot().get(name)
    require(canonical is None or canonical['id'] in (old_id, new_id), 'canonical name owned by another container')
    set_phase(release, plan, 'rollback_stop_candidate')
    run('docker', 'update', '--restart=no', new_id)
    if new['State']['Running']:
        run('docker', 'stop', '-t', '60', new_id)
    if new['Name'].lstrip('/') == name:
        set_phase(release, plan, 'rollback_rename_candidate')
        run('docker', 'rename', new_id, plan['stage_name'])
    if old['Name'].lstrip('/') != name:
        set_phase(release, plan, 'rollback_rename_predecessor')
        run('docker', 'rename', old_id, name)
    set_phase(release, plan, 'rollback_restore_predecessor')
    run('docker', 'update', '--restart=' + restart_arg(plan['old']['HostConfig']['RestartPolicy']), old_id)
    if not old['State']['Running']:
        run('docker', 'start', old_id)
    restored = wait_health(old_id, plan['old']['Image'], zero_restarts=False)
    verify_same_runtime(plan['old'], restored)
    require(inspect(name)['Id'] == old_id, 'rollback canonical identity mismatch')
    verify_files(['docker', 'exec', old_id], plan['old_hashes'])
    set_phase(release, plan, 'rolled_back')


def switch_plan(plan, release, hashes):
    old = plan['old']
    require(all(re.fullmatch(r'[a-f0-9]{64}', value) for value in (old['Id'], plan['new_id'])), 'switch requires full IDs')
    require(inspect(plan['name'])['Id'] == old['Id'], 'canonical target changed')
    verify_same_runtime(old, inspect(old['Id']))
    require(healthy(inspect(old['Id'])), 'predecessor unhealthy')
    candidate = inspect(plan['new_id'])
    require(candidate['State']['Status'] == 'created', 'candidate already started')
    validate_clone(old, candidate, plan['new_image'], hashes['git_sha'], staged=True)
    for phase, command in [
        ('disable_predecessor_restart', ('docker', 'update', '--restart=no', old['Id'])),
        ('stop_predecessor', ('docker', 'stop', '-t', '60', old['Id'])),
        ('rename_predecessor', ('docker', 'rename', old['Id'], plan['backup_name'])),
        ('rename_candidate', ('docker', 'rename', plan['new_id'], plan['name'])),
        ('start_candidate', ('docker', 'start', plan['new_id'])),
    ]:
        set_phase(release, plan, phase)
        run(*command)
    wait_health(plan['new_id'], plan['new_image'])
    set_phase(release, plan, 'restore_candidate_restart')
    run('docker', 'update', '--restart=' + restart_arg(old['HostConfig']['RestartPolicy']), plan['new_id'])
    validate_clone(old, inspect(plan['new_id']), plan['new_image'], hashes['git_sha'], staged=False)
    require(inspect(plan['name'])['Id'] == plan['new_id'], 'new canonical identity mismatch')
    verify_files(['docker', 'exec', plan['new_id']], hashes['files']['shadow'])
    verify_version(plan['new_id'], hashes)
    logs = run('docker', 'logs', '--since', inspect(plan['new_id'])['State']['StartedAt'], plan['new_id'], include_stderr=True)
    require(not re.search(r'(?im)\b(?:ERROR|CRITICAL|FATAL)\b|Traceback \(most recent call last\)', logs), 'candidate startup errors')
    set_phase(release, plan, 'verified')


def execute_plans(plans, release, hashes, before):
    attempted = []
    try:
        for plan in plans:
            attempted.append(plan)
            switch_plan(plan, release, hashes)
        # 第一目标可能在第二目标切换期间退出；成功落盘前必须重新核对全部目标。
        for plan in plans:
            candidate = inspect(plan['new_id'])
            require(healthy(candidate) and candidate['RestartCount'] == 0
                    and inspect(plan['name'])['Id'] == plan['new_id'], 'final candidate is not healthy/current')
            validate_clone(plan['old'], candidate, plan['new_image'], hashes['git_sha'], staged=False)
            verify_files(['docker', 'exec', plan['new_id']], hashes['files']['shadow'])
            verify_version(plan['new_id'], hashes)
        after = snapshot()
        extras = {}
        for plan in plans:
            backup = inspect(plan['old']['Id'])
            require(backup['Name'].lstrip('/') == plan['backup_name'] and not backup['State']['Running']
                    and backup['HostConfig']['RestartPolicy']['Name'] == 'no', 'backup is not safely stopped')
            expected_backup = copy.deepcopy(plan['old'])
            expected_backup['HostConfig']['RestartPolicy'] = {'Name': 'no', 'MaximumRetryCount': 0}
            verify_same_runtime(expected_backup, backup)
            extras[plan['backup_name']] = {'id': backup['Id'], 'image': backup['Image'], 'running': False,
                                         'restarts': backup['RestartCount'], 'health': backup['State'].get('Health', {}).get('Status')}
        verify_excluded(before, after, extras)
        save(release / 'after.json', after)
    except BaseException:
        failures = []
        for plan in reversed(attempted):
            try:
                restore_plan(plan, release)
            except Exception as exc:
                failures.append({'target': plan['name'], 'type': type(exc).__name__})
        # 未能恢复的候选/原容器全部保留，严禁扩大清理范围。
        for plan in plans:
            if plan not in attempted or plan.get('phase') == 'rolled_back':
                cleanup_candidate(plan, release)
        save(release / 'rollback.json', {'failures': failures, 'containers': snapshot()})
        if failures:
            raise RuntimeError('exact-ID rollback requires attention')
        verify_excluded(before, snapshot())
        raise
    # 运行验证全部通过后进入提交阶段。结果落盘/SSH 输出异常不再触发回退，
    # 避免原子替换成功但 fsync/输出失败时出现 verified 与已回退并存。
    # 提交中断时保留已验证候选与停机原容器，依据持久结果人工核验或 --recover。
    save(release / 'result.json', {'status': 'verified', 'git_sha': hashes['git_sha'], 'targets': list(TARGETS)})
    print(json.dumps({'status': 'verified', 'release': str(release), 'targets': list(TARGETS)}))


def activate(package):
    hashes = json.loads((package / 'hashes.json').read_text())
    digests = json.loads((package / 'digests.json').read_text())
    validate_manifests(hashes, digests)
    release = ROOT / '.releases/xau-attribution' / hashes['git_sha']
    require(not release.exists(), 'release already attempted')
    release.mkdir(parents=True, mode=0o700)
    for filename in ('hashes.json', 'digests.json', 'activate.py', 'database_permissions.py', 'permissions.json'):
        (release / filename).write_bytes((package / filename).read_bytes())
        (release / filename).chmod(0o600)
    before = snapshot()
    save(release / 'before.json', before)
    run('docker', 'pull', BASES['shadow'], timeout=300)
    base_image = json.loads(run('docker', 'image', 'inspect', BASES['shadow']))[0]
    run('docker', 'pull', digests['shadow'], timeout=300)
    new_image = json.loads(run('docker', 'image', 'inspect', digests['shadow']))[0]
    require(new_image['Config'] == base_image['Config'], 'image runtime defaults changed')
    # 校验镜像代码；只启动无网络的 sha256sum 辅助进程。
    temp = run('docker', 'create', '--network', 'none', '--entrypoint', 'sha256sum', digests['shadow'], *['/app/' + p for p in FILES['shadow']]).strip()
    try:
        output = run('docker', 'start', '-a', temp)
        require(inspect(temp)['State']['ExitCode'] == 0, 'image checksum failed')
        actual = {line.split(None, 1)[1].strip().removeprefix('/app/'): line.split()[0] for line in output.splitlines()}
        require(actual == hashes['files']['shadow'], 'image files mismatch')
    finally:
        run('docker', 'rm', temp)
    plans = []
    try:
        for service, (name, group) in TARGETS.items():
            old = inspect(name)
            require(healthy(old) and old['Image'] == base_image['Id'], 'live predecessor drift')
            body = clone_body(old, new_image['Id'], hashes['git_sha'])
            stage_name = name + '-ready-xau-' + hashes['git_sha'][:8]
            backup_name = name + '-before-xau-' + hashes['git_sha'][:8]
            require(stage_name not in before and backup_name not in before, 'owned name collision')
            plan = {'name': name, 'old': old, 'body': body, 'stage_name': stage_name, 'backup_name': backup_name,
                    'new_image': new_image['Id'], 'old_hashes': file_hashes(old['Id'], FILES['shadow'])}
            # 先保存创建意图，SIGKILL 后可按唯一名称定位未启动候选。
            set_phase(release, plan, 'creating_candidate')
            plan['new_id'] = create_container(stage_name, body)
            plans.append(plan)
            set_phase(release, plan, 'staged')
            validate_clone(old, inspect(plan['new_id']), new_image['Id'], hashes['git_sha'], staged=True)
        save(release / 'plans.json', plans)
        # 候选必须保持未启动，权限核验之后才允许暂停任何旧进程。
        audit = audit_permissions(release, hashes['audit_files'])
        (release / 'database-audit.txt').write_text(audit)
    except BaseException:
        for plan in plans:
            require(inspect(plan['new_id'])['State']['Status'] == 'created', 'unexpected staged writer')
            cleanup_candidate(plan, release)
        raise
    execute_plans(plans, release, hashes, before)


def cleanup_candidate(plan, release):
    require(not inspect(plan['new_id'])['State']['Running'], 'cannot clean a running candidate')
    set_phase(release, plan, 'cleanup_candidate')
    run('docker', 'rm', plan['new_id'])
    set_phase(release, plan, 'candidate_removed')


def recover_release(release):
    """中断后从私有阶段记录恢复；不接受任意目录或其他服务的记录。"""
    require(release.parent == ROOT / '.releases/xau-attribution'
            and re.fullmatch(r'[a-f0-9]{40}', release.name), 'invalid recovery directory')
    require(not (release / 'result.json').exists(), 'verified release cannot be automatically rolled back')
    before = json.loads((release / 'before.json').read_text())
    plans = []
    for name, _ in TARGETS.values():
        path = release / (name + '.state.json')
        if not path.exists():
            continue
        plan = json.loads(path.read_text())
        require(plan['name'] == name and plan['old']['Id'] == before[name]['id'], 'recovery predecessor mismatch')
        require(plan['stage_name'] == name + '-ready-xau-' + release.name[:8]
                and plan['backup_name'] == name + '-before-xau-' + release.name[:8], 'recovery names mismatch')
        if plan.get('phase') in ('rolled_back', 'cleanup_candidate', 'candidate_removed') and plan.get('new_id') not in {v['id'] for v in snapshot().values()}:
            restored = inspect(name)
            verify_same_runtime(plan['old'], restored)
            require(healthy(restored), 'restored predecessor is unhealthy')
            verify_files(['docker', 'exec', restored['Id']], plan['old_hashes'])
            continue
        if 'new_id' not in plan:
            # 创建请求可能已成功但回执未落盘；只认精确名称、未启动和完整配置。
            matches = [value['id'] for key, value in snapshot().items() if key == plan['stage_name']]
            if not matches:
                continue
            plan['new_id'] = matches[0]
            require(inspect(plan['new_id'])['State']['Status'] == 'created', 'unacknowledged candidate started')
            validate_clone(plan['old'], inspect(plan['new_id']), plan['new_image'], release.name, staged=True)
        plans.append(plan)
    for plan in reversed(plans):
        restore_plan(plan, release)
        cleanup_candidate(plan, release)
    verify_excluded(before, snapshot())
    save(release / 'recovery.json', {'status': 'original_ids_restored', 'targets': [p['name'] for p in plans]})


if __name__ == '__main__':
    main()
