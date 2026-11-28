#!/usr/bin/env python3
"""服务器巡检修复的双镜像构建和精确实例发布；不迁移、不交易、不发送通知。"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = Path('deployment/server-inspection/plan.json')
AUDIT_FILES = ('scripts/database_permissions.py', 'database/postgres/permissions.json')
INVENTORY_FILE = 'ai_tuner/ops/container_inventory.py'
BUNDLE_FILES = (str(PLAN_PATH), 'scripts/server_inspection_release.py', INVENTORY_FILE, *AUDIT_FILES)
TARGETS = {'hrs-acceptance': ('trading_system-hrs-acceptance', 'strategies/hrs/acceptance_report.py', 'ghcr.io/bearflower/trading-hrs'),
           'ai-tuner': ('ai-tuner', 'ai_tuner/reconciler/nightly_exit.py', 'ghcr.io/bearflower/trading-ai-tuner')}


class ReleaseError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def valid_sha(value, length=64):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is not None


def validate_plan(doc):
    require(doc.get('version') == 1 and isinstance(doc.get('targets'), list), 'invalid release plan')
    require(len(doc['targets']) == 2 and {r.get('name') for r in doc['targets']} == set(TARGETS), 'target scope mismatch')
    for row in doc['targets']:
        name, path, repository = TARGETS[row['name']]
        require(row.get('container') == name and row.get('files') == [path] and row.get('repository') == repository, 'source scope mismatch')
        require(valid_sha(row.get('old_id')) and re.fullmatch(r'sha256:[a-f0-9]{64}', row.get('old_image', '')), 'invalid predecessor identity')
        require(row.get('base') == repository + '@' + row['old_image'], 'base must match pinned predecessor')
        expected = {'HRS_ACCEPTANCE_MAX_SOURCE_PAYLOAD_BYTES': '8388608'} if row['name'] == 'hrs-acceptance' else {}
        require(row.get('environment') == expected, 'environment scope mismatch')
        require(row.get('modules') == [path[:-3].replace('/', '.')], 'import scope mismatch')
    return doc


def read_plan(path):
    return validate_plan(json.loads(Path(path).read_text()))


def run(*args, timeout=180):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    require(result.returncode == 0, f'{args[0]} failed with status {result.returncode}; output withheld')
    return result.stdout.strip()


def inspect(name):
    return json.loads(run('docker', 'inspect', name))[0]


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def network_config(row):
    result = {}
    for name, value in row['NetworkSettings']['Networks'].items():
        require(not value.get('IPAMConfig') and not value.get('DriverOpts'), 'custom network addressing requires review')
        result[name] = {'Aliases': value.get('Aliases') or []}
    return result


def fingerprint(row):
    config = copy.deepcopy(row['Config'])
    config.pop('Hostname', None)
    config.pop('MacAddress', None)
    return digest({'config': config, 'host': row['HostConfig'], 'networks': network_config(row)})


def mount_signature(row):
    return sorted((m['Type'], m['Source'], m['Destination'], m['RW']) for m in row['Mounts'])


def clone_body(old, target, image, source):
    require(old['Id'] == target['old_id'] and old['Image'] == target['old_image']
            and old['Name'] == '/' + target['container'] and old['State']['Running'], 'predecessor identity drift')
    require(valid_sha(source, 40) and re.fullmatch(r'sha256:[a-f0-9]{64}', image), 'invalid release identity')
    entries = old['Config'].get('Env') or []
    require(all('=' in value for value in entries) and len({v.split('=', 1)[0] for v in entries}) == len(entries), 'ambiguous environment')
    for changed in target['files'] + ['VERSION']:
        path = PurePosixPath('/app') / changed
        require(not any(path == PurePosixPath(m['Destination']) or PurePosixPath(m['Destination']) in path.parents
                        for m in old['Mounts']), 'runtime mount hides release source')
        require(not any(path == PurePosixPath(b.split(':')[1]) or PurePosixPath(b.split(':')[1]) in path.parents
                        for b in old['HostConfig'].get('Binds') or []), 'code mount hides release source')
    body = copy.deepcopy(old['Config'])
    body.pop('Hostname', None)
    body.pop('MacAddress', None)
    env = dict(v.split('=', 1) for v in entries)
    env.update(target['environment'])
    body['Env'] = [k + '=' + v for k, v in env.items()]
    body['Labels'] = dict(body.get('Labels') or {}, **{'codex.server-inspection.source': source, 'org.opencontainers.image.revision': source})
    host = copy.deepcopy(old['HostConfig'])
    if host.get('OomKillDisable') is None and 'OomKillDisable' in host:
        host['OomKillDisable'] = False
    host['RestartPolicy'] = {'Name': 'no', 'MaximumRetryCount': 0}
    body.update(Image=image, HostConfig=host, NetworkingConfig={'EndpointsConfig': network_config(old)})
    return body


def verify_protected(protected, getter=inspect):
    for name, expected in protected.items():
        current = getter(name)
        require(current['Id'] == expected['id'] and current['State']['Running']
                and current['State']['StartedAt'] == expected['started'], 'protected container changed: ' + name)


def validate_report_probe(value, memory):
    require(value.get('database_snapshot_coherent') is True and not value.get('source_error_type'), 'report source remains unavailable')
    require(type(value.get('rss_kib')) is int and 0 < value['rss_kib'] * 1024 <= memory * .8, 'report RSS leaves insufficient headroom')


def parse_timestamp(value):
    """Docker 纳秒时间戳先截到微秒，兼容宿主 Python 3.9/3.10。"""
    from datetime import datetime
    require(isinstance(value, str), 'invalid timestamp')
    normalized = re.sub(r'(\.\d{6})\d+(?=Z$|[+-]\d{2}:\d{2}$)', r'\1', value)
    try:
        result = datetime.fromisoformat(normalized.replace('Z', '+00:00'))
    except ValueError:
        raise ReleaseError('invalid timestamp') from None
    require(result.tzinfo is not None, 'timestamp lacks timezone')
    return result


def retirement_registry(registry, previous, targets, containers, evidence, module_path=None):
    """保留既有登记，只追加本次已停止、已有健康接替者的精确身份。"""
    module_path = module_path or ROOT / INVENTORY_FILE
    spec = importlib.util.spec_from_file_location('inspection_inventory', module_path)
    inventory = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inventory)
    candidate = copy.deepcopy(registry)
    require(candidate.get('version') == 1 and isinstance(candidate.get('entries'), list), 'invalid retirement registry')
    for target in targets:
        item = next(c for c in previous if c['Id'] == target['old_id'])
        entry = inventory.lifecycle_entry(item, successor=target['container'], lifecycle='retired',
            reason='本次巡检修复发布已由通过健康及源码核验的新实例接替', evidence=evidence)
        existing = [e for e in candidate['entries'] if e['id'] == item['Id'] or e['name'] == item['Name'].lstrip('/')]
        require(not existing or existing == [entry], 'retirement identity already registered differently')
        if not existing:
            candidate['entries'].append(entry)
    _, retired, issues = inventory.partition(containers, candidate)
    require(not issues and {t['old_id'] for t in targets} <= {c['Id'] for c in retired}, 'retirement successor validation failed')
    return candidate


class Engine(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect('/var/run/docker.sock')


def create(name, body):
    require(re.fullmatch(r'[a-zA-Z0-9_.-]+', name), 'invalid container name')
    connection = Engine('localhost', timeout=60)
    try:
        connection.request('POST', '/v1.44/containers/create?name=' + name, json.dumps(body), {'Content-Type': 'application/json'})
        response = connection.getresponse()
        payload = json.loads(response.read())
        require(response.status == 201, 'Docker create failed: ' + str(response.status))
        return payload['Id']
    finally:
        connection.close()


def prepare(name, output, source, deploy_id):
    target = next(r for r in read_plan(ROOT / PLAN_PATH)['targets'] if r['name'] == name)
    require(valid_sha(source, 40) and re.fullmatch('[0-9A-F]{8}', deploy_id), 'invalid build identity')
    output.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for relative in target['files']:
        destination = output / 'app' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
        hashes[relative] = sha256(destination)
    (output / 'app/VERSION').write_text(f'GIT_SHA={source}\nDEPLOY_ID={deploy_id}\nBUILD_RUN_ID={os.environ["GITHUB_RUN_ID"]}\n')
    (output / 'Dockerfile').write_text(f'FROM {target["base"]}\nCOPY app/ /app/\nLABEL org.opencontainers.image.revision={source}\n')
    private_write(output / 'receipt.json', dict(target, source=source, deploy_id=deploy_id, hashes=hashes))


def code_check(receipt):
    return ("import hashlib,importlib,pathlib; expected=" + repr(receipt['hashes']) + "; "
            "assert all(hashlib.sha256((pathlib.Path('/app')/p).read_bytes()).hexdigest()==h for p,h in expected.items()); "
            "v=pathlib.Path('/app/VERSION').read_text(); assert " + repr('GIT_SHA=' + receipt['source']) + " in v; assert "
            + repr('DEPLOY_ID=' + receipt['deploy_id']) + " in v; [importlib.import_module(m) for m in " + repr(receipt['modules']) + "]; print('source_verified')")


def finish_build(receipt_path, image):
    receipt = json.loads(receipt_path.read_text())
    require(re.fullmatch(re.escape(receipt['repository']) + r'@sha256:[a-f0-9]{64}', image), 'unexpected image repository')
    run('docker', 'pull', image, timeout=600)
    run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp',
        '--tmpfs', '/app/logs:rw,mode=1777', '--entrypoint', 'python', image, '-B', '-c', code_check(receipt))
    receipt['image'] = image
    private_write(receipt_path, receipt)


def bundle(receipts, output, source):
    plan = read_plan(ROOT / PLAN_PATH)
    rows = [json.loads(p.read_text()) for p in receipts.rglob('receipt.json')]
    require(len(rows) == 2 and {r['name'] for r in rows} == set(TARGETS), 'incomplete build receipts')
    for row in rows:
        target = next(t for t in plan['targets'] if t['name'] == row['name'])
        require(all(row.get(k) == v for k, v in target.items()) and row['source'] == source, 'receipt plan mismatch')
        require(row['hashes'] == {p: sha256(ROOT / p) for p in target['files']}, 'receipt source mismatch')
        require(re.fullmatch(re.escape(row['repository']) + r'@sha256:[a-f0-9]{64}', row.get('image', '')), 'unverified image receipt')
    require(len({r['deploy_id'] for r in rows}) == 1, 'build version mismatch')
    output.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for relative in BUNDLE_FILES:
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
        hashes[relative] = sha256(destination)
    private_write(output / 'release.json', dict(source=source, deploy_id=rows[0]['deploy_id'], targets=rows, files=hashes))


class Release:
    def __init__(self, bundle_path, source, project=Path('/root/trading_system')):
        self.bundle = bundle_path.resolve()
        self.doc = json.loads((self.bundle / 'release.json').read_text())
        require(valid_sha(source, 40) and self.doc['source'] == source, 'release source mismatch')
        require(set(self.doc['files']) == set(BUNDLE_FILES), 'bundle file scope mismatch')
        require(all(sha256(self.bundle / p) == h for p, h in self.doc['files'].items()), 'bundle checksum mismatch')
        require(sha256(__file__) == self.doc['files']['scripts/server_inspection_release.py'], 'executor mismatch')
        plan = read_plan(self.bundle / PLAN_PATH)
        self.rows = self.doc['targets']
        require(len(self.rows) == 2 and {r['name'] for r in self.rows} == set(TARGETS), 'release target mismatch')
        for r in self.rows:
            t = next(t for t in plan['targets'] if t['name'] == r['name'])
            require(all(r.get(k) == v for k, v in t.items()) and r['source'] == source
                    and r['deploy_id'] == self.doc['deploy_id'], 'release target provenance mismatch')
            require(set(r['hashes']) == set(t['files']) and all(valid_sha(h) for h in r['hashes'].values()), 'release hash scope mismatch')
            require(re.fullmatch(re.escape(r['repository']) + r'@sha256:[a-f0-9]{64}', r.get('image', '')), 'invalid release image')
        self.source, self.project = source, project
        self.state = project / '.releases/server-inspection' / source
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)

    def save(self, name, value):
        private_write(self.state / name, value)

    def audit(self, phase):
        result = subprocess.run([sys.executable, str(self.bundle / AUDIT_FILES[0]), 'audit', '--manifest', str(self.bundle / AUDIT_FILES[1])],
                                capture_output=True, text=True, timeout=300)
        try:
            evidence = json.loads(result.stdout)
        except ValueError:
            raise ReleaseError('permission audit returned no proof') from None
        self.save(phase + '-permissions.json', evidence)
        require(result.returncode == 0 and evidence.get('ok') is True and evidence.get('issues') == []
                and evidence.get('mode') == 'audit' and evidence.get('manifest_sha256') == self.doc['files'][AUDIT_FILES[1]], 'permission audit failed')

    def verify_image(self, row):
        run('docker', 'pull', row['image'], timeout=600)
        image = json.loads(run('docker', 'image', 'inspect', row['image']))[0]
        base = json.loads(run('docker', 'image', 'inspect', row['old_image']))[0]
        require(row['image'] in image.get('RepoDigests', []) and image['Config'].get('Labels', {}).get('org.opencontainers.image.revision') == self.source,
                'image provenance mismatch')
        require(image['Os'] == 'linux' and image['Architecture'] == 'amd64'
                and image['RootFS']['Layers'][:len(base['RootFS']['Layers'])] == base['RootFS']['Layers'], 'image base mismatch')
        run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp',
            '--tmpfs', '/app/logs:rw,mode=1777', '--entrypoint', 'python', row['image'], '-B', '-c', code_check(row))
        self.save(row['name'] + '-image.json', {'image_id': image['Id'], 'digest': row['image'], 'hashes': row['hashes']})
        return image['Id']

    def verify_clone(self, old, current, body, active=False):
        expected_config = {k: v for k, v in body.items() if k not in ('HostConfig', 'NetworkingConfig')}
        actual = copy.deepcopy(current['Config']); actual.pop('Hostname', None); actual.pop('MacAddress', None)
        require(actual == expected_config, 'candidate configuration drift')
        expected_host = copy.deepcopy(body['HostConfig'])
        if active:
            expected_host['RestartPolicy'] = old['HostConfig']['RestartPolicy']
        actual_host = copy.deepcopy(current['HostConfig'])
        # Docker create reports false, while restart-policy update may return
        # null for the same default. Preserve true and every other host field.
        for host in (expected_host, actual_host):
            if 'OomKillDisable' in host and host['OomKillDisable'] is None:
                host['OomKillDisable'] = False
        require(actual_host == expected_host and mount_signature(current) == mount_signature(old), 'candidate host or mount drift')
        require(set(current['NetworkSettings']['Networks']) == set(old['NetworkSettings']['Networks']), 'candidate network drift')
        for name, value in network_config(old).items():
            require(set(value['Aliases']) <= set(network_config(current)[name]['Aliases']), 'candidate network alias drift')

    def report_probe(self, old, body):
        probe = copy.deepcopy(body)
        probe['Cmd'] = ['-B', '-c', "import asyncio,json,os,resource; from types import SimpleNamespace; import asyncpg; from strategies.hrs.acceptance_report import build_report\nasync def main():\n p=await asyncpg.create_pool(os.environ['DATABASE_URL'],min_size=1,max_size=1)\n try:\n  r=await build_report(SimpleNamespace(pool=p)); print(json.dumps({k:r.get(k) for k in ('status','database_snapshot_coherent','source_error_type','source_error_code')} | {'rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))\n finally: await p.close()\nasyncio.run(main())"]
        probe['Entrypoint'] = ['python']
        probe['Healthcheck'] = {'Test': ['NONE']}
        probe.pop('Volumes', None); probe.pop('ExposedPorts', None)
        probe['HostConfig'].update(Binds=[], Mounts=[], ReadonlyRootfs=True, PortBindings={}, Tmpfs={'/tmp':'rw,nosuid,nodev,size=16m', '/app/logs':'rw,nosuid,nodev,size=16m,mode=1777'})
        probe['NetworkingConfig'] = {'EndpointsConfig': {name: {} for name in old['NetworkSettings']['Networks']}}
        probe_id = create('inspection-source-probe-' + self.source[:12], probe)
        self.save('probe-identity.json', {'id': probe_id, 'image': body['Image']})
        try:
            output = run('docker', 'start', '-a', probe_id, timeout=300)
            state = inspect(probe_id)['State']
            require(state['ExitCode'] == 0 and not state['OOMKilled'], 'report source probe crashed')
            value = json.loads(output.splitlines()[-1])
            self.save('report-probe.json', value)
            validate_report_probe(value, old['HostConfig']['Memory'])
        finally:
            run('docker', 'rm', '-f', probe_id)

    def register_retirements(self):
        registry_path = self.project / '.ops/container_retirements.json'
        lock_path = self.project / '.ops/container_retirements.lock'
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            original = json.loads(registry_path.read_text()) if registry_path.exists() else {'version': 1, 'entries': []}
            if not (self.state / 'retirement-registry-before.json').exists():
                self.save('retirement-registry-before.json', original)
            ids = run('docker', 'ps', '-aq').split()
            containers = json.loads(run('docker', 'inspect', *ids))
            previous = [next(c for c in containers if c['Id'] == row['old_id']) for row in self.rows]
            candidate = retirement_registry(original, previous, self.rows, containers, str(self.state / 'activation.json'), self.bundle / INVENTORY_FILE)
            self.save('retirement-registry-proposed.json', candidate)
            private_write(registry_path, candidate)
            self.save('retirement-registry-applied.json', {'sha256': sha256(registry_path), 'added': [r['old_id'] for r in self.rows]})

    def fresh_report(self, identifier, started):
        deadline = time.monotonic() + 300
        code = "import json,pathlib; r=json.loads(pathlib.Path('/app/logs/hrs-acceptance.json').read_text()); print(json.dumps({k:r.get(k) for k in ('generated_at','database_snapshot_coherent','source_error_type','source_error_code')}))"
        boundary = parse_timestamp(started)
        while time.monotonic() < deadline:
            value = json.loads(run('docker', 'exec', identifier, 'python', '-B', '-c', code))
            generated = parse_timestamp(value['generated_at'])
            if generated >= boundary:
                require(value.get('database_snapshot_coherent') is True and not value.get('source_error_type'), 'new report source failed')
                self.save('report-live.json', value)
                return
            time.sleep(2)
        raise ReleaseError('new report not written before timeout')

    def stage(self):
        require(not (self.state / 'staged.json').exists(), 'stage already exists; review before retry')
        protected = {}
        targets = {r['container'] for r in self.rows}
        for name in run('docker', 'ps', '--format', '{{.Names}}').splitlines():
            if name not in targets:
                c = inspect(name)
                protected[name] = {'id': c['Id'], 'started': c['State']['StartedAt']}
        self.save('protected.json', protected)
        self.audit('stage')
        prepared = []
        for row in self.rows:
            old = inspect(row['container'])
            require(old['Id'] == row['old_id'] and old['Image'] == row['old_image'] and old['State']['Running'], 'predecessor changed')
            self.save(row['name'] + '-before.json', old)
            image = self.verify_image(row)
            body = clone_body(old, row, image, self.source)
            if row['name'] == 'hrs-acceptance':
                require(old['HostConfig']['Memory'] == 268435456, 'reporter memory cap changed')
                self.report_probe(old, body)
            new_id = create(row['container'] + '-inspection-ready-' + self.source[:8], body)
            self.save(row['name'] + '-candidate.json', {'id': new_id, 'old_id': old['Id'], 'image': image, 'status': 'created_unverified'})
            current = inspect(new_id)
            self.verify_clone(old, current, body)
            require(current['State']['Status'] == 'created' and current['Image'] == image, 'candidate state mismatch')
            self.save(row['name'] + '-body.json', body)
            prepared.append({'name': row['name'], 'old_id': old['Id'], 'new_id': new_id,
                             'old_fingerprint': fingerprint(old), 'new_fingerprint': fingerprint(current), 'image': image})
        verify_protected(protected)
        self.save('staged.json', {'source': self.source, 'targets': prepared})
        print(json.dumps({'staged': [r['container'] for r in self.rows], 'source': self.source}))

    def healthy(self, identifier):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            current = inspect(identifier)
            require(current['State']['Running'] and current['RestartCount'] == 0 and not current['State']['OOMKilled'], 'candidate exited or restarted')
            if current['State'].get('Health', {}).get('Status') == 'healthy':
                return current
            require(current['State'].get('Health', {}).get('Status') != 'unhealthy', 'candidate unhealthy')
            time.sleep(2)
        raise ReleaseError('candidate healthcheck timed out')

    def rollback(self, row, old, new_id):
        run('docker', 'update', '--restart=no', new_id)
        new = inspect(new_id)
        if new['State']['Running']:
            run('docker', 'stop', '-t', '60', new_id)
        if inspect(new_id)['Name'] == '/' + row['container']:
            run('docker', 'rename', new_id, row['container'] + '-inspection-failed-' + self.source[:8])
        if inspect(old['Id'])['Name'] != '/' + row['container']:
            run('docker', 'rename', old['Id'], row['container'])
        policy = old['HostConfig']['RestartPolicy']
        value = policy['Name'] or 'no'
        if value == 'on-failure' and policy['MaximumRetryCount']:
            value += ':' + str(policy['MaximumRetryCount'])
        run('docker', 'update', '--restart=' + value, old['Id'])
        if not inspect(old['Id'])['State']['Running']:
            run('docker', 'start', old['Id'])
        restored = self.healthy(old['Id'])
        require(restored['Id'] == row['old_id'] and restored['Image'] == row['old_image'], 'rollback identity mismatch')

    def verify_current(self, state):
        require(state['source'] == self.source and len(state['targets']) == 2, 'incomplete active evidence')
        protected = json.loads((self.state / 'protected.json').read_text())
        verify_protected(protected)
        for row in self.rows:
            record = next(v for v in state['targets'] if v['name'] == row['name'])
            require(record.get('phase') == 'healthy', 'candidate not verified')
            current = self.healthy(record['new_id'])
            require(inspect(row['container'])['Id'] == record['new_id'] and current['Image'] == record['image']
                    and current['State']['StartedAt'] == record['started'], 'active identity changed')
            old = json.loads((self.state / (row['name'] + '-before.json')).read_text())
            body = json.loads((self.state / (row['name'] + '-body.json')).read_text())
            self.verify_clone(old, current, body, active=True)
            run('docker', 'exec', current['Id'], 'python', '-B', '-c', code_check(row))

    def finalize(self):
        state = json.loads((self.state / 'activation.json').read_text())
        require(state.get('status') in ('runtime_verified', 'complete'), 'runtime is not verified')
        self.verify_current(state)
        self.register_retirements()
        state['status'] = 'complete'
        self.save('activation.json', state)
        print(json.dumps({'status':'complete', 'source':self.source, 'changed':[r['container'] for r in self.rows]}))

    def activate(self):
        require(not (self.state / 'activation.json').exists(), 'activation already attempted; review before retry')
        staged = json.loads((self.state / 'staged.json').read_text())
        protected = json.loads((self.state / 'protected.json').read_text())
        require(staged['source'] == self.source, 'staged source mismatch')
        verify_protected(protected)
        self.audit('activation')
        originals = {}
        for row in self.rows:
            record = next(r for r in staged['targets'] if r['name'] == row['name'])
            old, new = inspect(row['container']), inspect(record['new_id'])
            require(old['Id'] == record['old_id'] == row['old_id'] and old['State']['Running']
                    and fingerprint(old) == record['old_fingerprint'], 'predecessor changed after stage')
            require(new['State']['Status'] == 'created' and fingerprint(new) == record['new_fingerprint'], 'candidate changed after stage')
            originals[row['name']] = old
        attempted = []
        state = {'source': self.source, 'status': 'activating', 'targets': []}
        self.save('activation.json', state)
        try:
            for row in self.rows:
                record = next(r for r in staged['targets'] if r['name'] == row['name'])
                old = originals[row['name']]
                new_id = record['new_id']
                attempted.append((row, old, new_id))
                state['targets'].append({'name': row['name'], 'old_id': old['Id'], 'new_id': new_id, 'phase': 'stopping'})
                self.save('activation.json', state)
                run('docker', 'update', '--restart=no', old['Id'])
                run('docker', 'stop', '-t', '60', old['Id'])
                run('docker', 'rename', old['Id'], row['container'] + '-inspection-before-' + self.source[:8])
                run('docker', 'rename', new_id, row['container'])
                run('docker', 'start', new_id)
                current = self.healthy(new_id)
                run('docker', 'exec', new_id, 'python', '-B', '-c', code_check(row))
                if row['name'] == 'hrs-acceptance':
                    self.fresh_report(new_id, current['State']['StartedAt'])
                policy = old['HostConfig']['RestartPolicy']
                restart = policy['Name'] or 'no'
                if restart == 'on-failure' and policy['MaximumRetryCount']:
                    restart += ':' + str(policy['MaximumRetryCount'])
                run('docker', 'update', '--restart=' + restart, new_id)
                current = inspect(new_id)
                body = json.loads((self.state / (row['name'] + '-body.json')).read_text())
                self.verify_clone(old, current, body, active=True)
                require(inspect(row['container'])['Id'] == new_id, 'canonical container identity changed')
                state['targets'][-1].update(phase='healthy', image=current['Image'], started=current['State']['StartedAt'])
                self.save('activation.json', state)
                verify_protected(protected)
            self.verify_current(state)
        except BaseException:
            failures = []
            for row, old, new_id in reversed(attempted):
                try:
                    self.rollback(row, old, new_id)
                except Exception as exc:
                    failures.append({'name':row['name'], 'error_type':type(exc).__name__})
            state.update(status='failed', rollback_failures=failures)
            self.save('activation.json', state)
            raise
        state['status'] = 'runtime_verified'
        self.save('activation.json', state)
        self.finalize()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['matrix','prepare','finish-build','bundle','stage','activate','finalize'])
    parser.add_argument('--name'); parser.add_argument('--output', type=Path); parser.add_argument('--source')
    parser.add_argument('--deploy-id'); parser.add_argument('--receipt', type=Path); parser.add_argument('--image')
    parser.add_argument('--receipts', type=Path); parser.add_argument('--bundle', type=Path)
    args = parser.parse_args()
    if args.action == 'matrix': print(json.dumps({'include': read_plan(ROOT/PLAN_PATH)['targets']}))
    elif args.action == 'prepare': prepare(args.name,args.output,args.source,args.deploy_id)
    elif args.action == 'finish-build': finish_build(args.receipt,args.image)
    elif args.action == 'bundle': bundle(args.receipts,args.output,args.source)
    else:
        release = Release(args.bundle,args.source)
        with (release.project/'.deploy_tmp/compose_deploy.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            getattr(release,args.action)()


if __name__ == '__main__':
    main()
