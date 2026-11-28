#!/usr/bin/env python3
"""Reviewed image replacement, one service at a time, with private runtime evidence.

No database migration, order submission, risk unlock, notification, broad compose
activation, or automatic rollback is performed here. Existing release pins stay
intact. This release records its own immutable evidence and active successor pin.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import http.client
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import full_regression_build as build_tools
from scripts.full_regression_build import assemble, read_plan


class ReleaseError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_image_ref(name, image):
    if not re.fullmatch(r'ghcr\.io/bearflower/trading-' + re.escape(name) + r'@sha256:[a-f0-9]{64}', image):
        raise ReleaseError('immutable image repository mismatch')
    return image


def network_config(row):
    result = {}
    for name, network in row['NetworkSettings']['Networks'].items():
        if network.get('IPAMConfig') or network.get('DriverOpts'):
            raise ReleaseError('custom network addressing requires separate review')
        result[name] = {'Aliases': network.get('Aliases') or []}
    return result


def runtime_fingerprint(row):
    config = copy.deepcopy(row['Config'])
    config.pop('Hostname', None)
    config.pop('MacAddress', None)
    return digest({'config': config, 'host': row['HostConfig'], 'networks': network_config(row)})


def build_container_body(old, image_id, sha, *, source_removals=()):
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id) or not re.fullmatch(r'[a-f0-9]{40}', sha):
        raise ReleaseError('invalid release identity')
    entries = old['Config'].get('Env') or []
    if any('=' not in value for value in entries) or len({value.split('=', 1)[0] for value in entries}) != len(entries):
        raise ReleaseError('ambiguous predecessor environment')
    body = copy.deepcopy(old['Config'])
    body.pop('Hostname', None)
    body.pop('MacAddress', None)
    host = copy.deepcopy(old['HostConfig'])
    # Docker create materializes this legacy nullable flag as false. Encode
    # that default explicitly; the resulting security policy is still checked
    # by exact HostConfig equality, including explicit true/false values.
    if 'OomKillDisable' in host and host['OomKillDisable'] is None:
        host['OomKillDisable'] = False
    elif 'OomKillDisable' in host and type(host['OomKillDisable']) is not bool:
        raise ReleaseError('invalid predecessor OOM policy type')
    for removal in source_removals:
        if removal['container'] != old['Name'].lstrip('/'):
            raise ReleaseError('source mount removal belongs to another container')
        exact = removal['source'] + ':' + removal['target'] + ':ro'
        if (host.get('Binds') or []).count(exact) != 1:
            raise ReleaseError('reviewed source mount is not an exact read-only bind')
        host['Binds'].remove(exact)
    # These two old directory mounts hide the newly tested Python libraries.
    # All exact configuration, writable override and receipt mounts are kept.
    if old['Name'] == '/ai-tuner':
        retained = []
        for bind in host.get('Binds') or []:
            parts = bind.split(':')
            if len(parts) not in (2, 3):
                raise ReleaseError('ambiguous bind mount')
            destination = parts[1]
            if destination in ('/app/strategies', '/app/strategies/hrs'):
                continue
            if (destination.startswith('/app/strategies/')
                    and not destination.endswith(('/config.yaml', '/tuning_overrides'))):
                raise ReleaseError('unreviewed AI strategy code mount')
            retained.append(bind)
        required = {'/app/strategies/' + strategy + '/' + item
                    for strategy in ('btc_eth', 'btc_eth_aggressive', 'new_coin', 'hrs', 'grid')
                    for item in ('config.yaml', 'tuning_overrides')}
        if not required <= {bind.split(':')[1] for bind in retained}:
            raise ReleaseError('removing AI code mounts requires all exact frozen configuration mounts')
        host['Binds'] = retained
        if any(m.get('Target', '').startswith('/app/strategies') for m in host.get('Mounts') or []):
            raise ReleaseError('unreviewed structured AI code mount')
    host['RestartPolicy'] = {'Name': 'no', 'MaximumRetryCount': 0}
    body.update(Image=image_id, HostConfig=host,
                NetworkingConfig={'EndpointsConfig': network_config(old)})
    # The candidate image's revision was verified before constructing this
    # clone. Declare it explicitly instead of inheriting or retaining old metadata.
    labels = dict(body.get('Labels') or {})
    # 窄发布来源已被本次完整源码替代，避免继承标签造成当前来源歧义。
    for key in ('codex.runtime.source', 'codex.xau.source'):
        labels.pop(key, None)
    body['Labels'] = dict(labels, **{
        'codex.full_regression.source': sha, 'org.opencontainers.image.revision': sha})
    if old['Name'] in ('/trading_system-hrs', '/trading_system-hrs-acceptance',
                       '/trading_system-execution-facts', '/trading_system-market-cap'):
        env = dict(value.split('=', 1) for value in body.get('Env') or [])
        env['HRS_IMAGE_REVISION'] = sha
        if old['Name'] == '/trading_system-hrs' or 'HRS_RELEASE_SHA' in env:
            env['HRS_RELEASE_SHA'] = sha
        body['Env'] = [key + '=' + value for key, value in env.items()]
    return body


def run(*args, timeout=180, input_text=None):
    process = subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout)
    if process.returncode:
        # Command output can contain credential-bearing inspect/config payloads.
        raise ReleaseError(f'{args[0]} failed with status {process.returncode}; output withheld')
    return process.stdout.strip()


def inspect(name):
    return json.loads(run('docker', 'inspect', name))[0]


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class DockerConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect('/var/run/docker.sock')


def create_container(name, body):
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+', name):
        raise ReleaseError('unsafe container name')
    connection = DockerConnection('localhost', timeout=60)
    try:
        connection.request('POST', '/v1.44/containers/create?name=' + name,
                           json.dumps(body), {'Content-Type': 'application/json'})
        response = connection.getresponse()
        result = json.loads(response.read())
        if response.status != 201:
            raise ReleaseError(f'Docker create failed: HTTP {response.status}')
        return result['Id']
    finally:
        connection.close()


def expected_image_sources(image_name, source):
    """Required production source from the reviewed Dockerfile COPY boundaries."""
    families = {
        'btc-eth': ('shared/', 'strategies/btc_eth/', 'strategies/btc_eth_aggressive/', 'strategies/hrs/'),
        'btc-eth-aggr': ('shared/', 'strategies/btc_eth_aggressive/', 'strategies/hrs/'),
        'new-coin': ('shared/', 'strategies/new_coin/', 'strategies/hrs/'),
        'grid': ('shared/', 'strategies/grid/', 'scripts/grid_release_guard.py', 'scripts/full_regression_live_gate.py'),
        'ai-tuner': ('ai_tuner/', 'shared/', 'strategies/hrs/', 'strategies/btc_eth_aggressive/', 'scripts/ai_tuner_mvp.py',
                     'scripts/reconcile_income_attribution.py', 'scripts/audit_algo_attribution.py',
                     'scripts/export_execution_facts.py', 'scripts/report_economic_ledger.py'),
        'data-backend': ('shared/', 'dashboard/backend/core/', 'dashboard/backend/services/', 'services/data_backend/src/'),
        'kline-service': tuple('services/kline_service/' + name + '/' for name in ('shared', 'core', 'api', 'models', 'db', 'scripts', 'src')),
        'kline-monitor': ('services/kline_monitor/src/',),
        'aggtrade-collector': ('services/aggtrade_collector/',),
        'hrs': ('ai_tuner/', 'dashboard/', 'strategies/', 'shared/', 'services/', 'scripts/', 'database/', 'deployment/', 'tools/'),
        'dashboard-api': ('ai_tuner/', 'dashboard/', 'strategies/', 'shared/', 'services/', 'scripts/', 'database/', 'deployment/', 'tools/'),
    }
    if image_name not in families:
        raise ReleaseError('unknown image source layout')
    required = {}
    for path, value in source.items():
        if not path.startswith(families[image_name]) or Path(path).suffix not in ('.py', '.js', '.html'):
            continue
        if 'tests' in PurePosixPath(path).parts or Path(path).name.startswith('tmp_'):
            continue
        relative = path
        if image_name in ('kline-service', 'kline-monitor'):
            relative = path.removeprefix('services/' + image_name.replace('-', '_') + '/')
        elif image_name == 'data-backend' and path.startswith('services/data_backend/src/'):
            relative = path.removeprefix('services/data_backend/')
        required[relative] = value
    if not required:
        raise ReleaseError('empty expected image source coverage')
    return required


def code_probe(image_name, expected, sha, deploy_id):
    required = expected_image_sources(image_name, expected)
    prefix = {'kline-service': 'services/kline_service/', 'kline-monitor': 'services/kline_monitor/'}.get(image_name, '')
    return '''import hashlib,json,pathlib
p=pathlib.Path('/app'); expected=%r; required=%r; prefix=%r; image=%r
actual={}
for f in p.rglob('*'):
 if not f.is_file() or f.suffix not in ('.py','.js','.html') or '__pycache__' in f.parts: continue
 relative=str(f.relative_to(p)); source=prefix+relative
 if image=='data-backend' and relative.startswith('src/'): source='services/data_backend/'+relative
 if source not in expected: raise RuntimeError('untracked image source: '+relative)
 value=hashlib.sha256(f.read_bytes()).hexdigest()
 if value!=expected[source]: raise RuntimeError('source mismatch: '+relative)
 if f.suffix=='.py': compile(f.read_text(),relative,'exec')
 actual[relative]=value
if not required.keys() <= actual.keys(): raise RuntimeError('required image source is missing')
version=(p/'VERSION').read_text().splitlines()
if %r not in version or %r not in version: raise RuntimeError('VERSION mismatch')
print(json.dumps({'hashes':actual,'source_count':len(actual),'version':version}))
''' % (expected, required, prefix, image_name, 'GIT_SHA=' + sha, 'DEPLOY_ID=' + deploy_id)


def runtime_code_probe(proof):
    return '''import pathlib,hashlib
expected=%r
for path,value in expected.items():
 if hashlib.sha256((pathlib.Path('/app')/path).read_bytes()).hexdigest()!=value:
  raise RuntimeError('runtime source mismatch: '+path)
''' % proof['hashes']


def validate_mount_coverage(body, proof):
    """No retained runtime mount may replace newly verified executable source."""
    host = body['HostConfig']
    if host.get('VolumesFrom'):
        raise ReleaseError('volumes-from requires separate review')
    targets = [value.split(':')[1] for value in host.get('Binds') or []]
    targets += [mount.get('Target', '') for mount in host.get('Mounts') or []]
    targets += list(host.get('Tmpfs') or {}) + list(body.get('Volumes') or {})
    code = [PurePosixPath('/app') / path for path in proof['hashes']]
    for target in targets:
        path = PurePosixPath(target)
        if not path.is_absolute() or '..' in path.parts:
            raise ReleaseError('ambiguous mount destination')
        if any(item == path or path in item.parents for item in code):
            raise ReleaseError('runtime mount would hide verified image source: ' + target)


def verified_source_removals(old, proof, manifest):
    allowed = {
        ('trading_system-mtcps-xau-calibrated-shadow', '/app/strategies/btc_eth/shadow/main.py'),
        ('trading_system-hrs-acceptance', '/app/strategies/hrs/acceptance_report.py'),
    }
    if manifest.get('schema_version') != 1 or not isinstance(manifest.get('mounts'), list):
        raise ReleaseError('invalid source mount removal manifest')
    rows = [r for r in manifest['mounts'] if r.get('container') == old['Name'].lstrip('/')]
    for row in rows:
        if ((row.get('container'), row.get('target')) not in allowed
                or not _sha(row.get('old_sha256'))
                or row['target'].removeprefix('/app/') not in proof['hashes']
                or hashlib.sha256(Path(row['source']).read_bytes()).hexdigest() != row['old_sha256']):
            raise ReleaseError('reviewed mounted source or candidate replacement proof differs')
    if len({r['target'] for r in rows}) != len(rows):
        raise ReleaseError('duplicate source mount removal')
    return rows


def validate_staged_runtime(new, body):
    expected = {key: value for key, value in body.items() if key not in ('HostConfig', 'NetworkingConfig')}
    actual = copy.deepcopy(new['Config'])
    for value in (expected, actual):
        value.pop('Hostname', None)
        value.pop('MacAddress', None)
    if actual != expected or new['HostConfig'] != body['HostConfig']:
        raise ReleaseError('staged runtime configuration differs from reviewed clone')
    wanted = body['NetworkingConfig']['EndpointsConfig']
    actual_networks = network_config(new)
    if set(wanted) != set(actual_networks):
        raise ReleaseError('staged network set changed')
    for name, values in wanted.items():
        if not set(values['Aliases']) <= set(actual_networks[name]['Aliases']):
            raise ReleaseError('staged network aliases changed')


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def _fresh(value, now):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= now - value <= 180


def private_json(directory, relative):
    candidate = Path(relative)
    if candidate.is_absolute() or '..' in candidate.parts:
        raise ReleaseError('evidence path must stay inside release directory')
    path = (directory / candidate).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ReleaseError('missing or escaped release evidence')
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def validate_live_gate(directory, gate, sha, name, old, now):
    if (gate.get('schema_version') != 1 or gate.get('source') != sha or gate.get('container') != name
            or gate.get('old_id') != old['Id'] or gate.get('ok') is not True
            or gate.get('read_only') is not True or not _fresh(gate.get('observed_at'), now)):
        raise ReleaseError('fresh release-bound read-only account gate required')
    baseline, baseline_hash = private_json(directory, 'live-baseline.json')
    if baseline.get('schema_version') != 1 or baseline.get('source') != sha or gate.get('baseline_sha256') != baseline_hash:
        raise ReleaseError('account baseline is not bound to this release')
    env = dict(value.split('=', 1) for value in old['Config'].get('Env') or [])
    if env.get('TRADE_ACCOUNT_ID') and baseline.get('main', {}).get('account') != env['TRADE_ACCOUNT_ID']:
        raise ReleaseError('main account does not match predecessor runtime')
    if env.get('GRID_ACCOUNT_ID') and baseline.get('grid', {}).get('account') != env['GRID_ACCOUNT_ID']:
        raise ReleaseError('grid account does not match predecessor runtime')
    for scope in ('main', 'grid'):
        reference = gate.get('evidence', {}).get(scope, {})
        evidence, actual_hash = private_json(directory, reference.get('path', ''))
        if reference.get('sha256') != actual_hash:
            raise ReleaseError('account evidence checksum mismatch')
        policy = baseline.get(scope, {})
        keys = ['account', 'account_mode', 'account_identity_sha256', 'risk_policy_sha256']
        keys += ['allocation_sha256'] if scope == 'main' else ['continuity_sha256']
        if (not isinstance(policy.get('account'), str) or not policy['account']
                or policy.get('account_mode') != 'classic'
                or any(not _sha(policy.get(key)) for key in keys if key.endswith('_sha256'))
                or any(evidence.get(key) != policy.get(key) for key in keys)):
            raise ReleaseError('account identity, mode, allocation or risk policy changed')
        if (evidence.get('source') != sha or evidence.get('container') != name or evidence.get('old_id') != old['Id']
                or evidence.get('ok') is not True or evidence.get('read_only') is not True
                or not _fresh(evidence.get('observed_at'), now)):
            raise ReleaseError('account evidence is not fresh or release-bound')
        requests = evidence.get('requests')
        if (not isinstance(requests, list) or not requests or any(
                not isinstance(row, dict) or row.get('method') != 'GET'
                or (row.get('signed') is not True and not (scope == 'grid' and row.get('signed') is False
                    and row.get('path') in ('/fapi/v1/premiumIndex', '/fapi/v2/ticker/price', '/fapi/v1/exchangeInfo')))
                or not isinstance(row.get('path'), str) or not row['path'].startswith('/') for row in requests)):
            raise ReleaseError('signed GET request trace required')
        paths = {row['path'].rsplit('/', 1)[-1] for row in requests if row['path'].startswith('/fapi/')}
        if not {'account', 'positionRisk', 'openOrders', 'openAlgoOrders'} <= paths:
            raise ReleaseError('classic account, position and both order views are required')
        if scope == 'main':
            from decimal import Decimal, InvalidOperation
            try:
                equity = Decimal(str(evidence.get('equity')))
            except InvalidOperation:
                raise ReleaseError('invalid main account equity') from None
            positions = evidence.get('position_exposure')
            if (not equity.is_finite() or equity <= 0 or not isinstance(positions, list)
                    or type(evidence.get('protected_positions')) is not int
                    or evidence['protected_positions'] != len(positions)
                    or evidence.get('writes_report_metadata') is not False
                    or any(not _sha(evidence.get(key)) for key in ('positions_sha256', 'ordinary_orders_sha256', 'algo_orders_sha256'))):
                raise ReleaseError('main account protection evidence incomplete')
            try:
                if any(not isinstance(row, dict) or not isinstance(row.get('symbol'), str) or not row['symbol']
                       or row.get('positionSide') not in ('BOTH', 'LONG', 'SHORT')
                       or not Decimal(str(row.get('positionAmt'))).is_finite()
                       or Decimal(str(row['positionAmt'])) == 0 for row in positions):
                    raise ReleaseError('invalid protected position evidence')
            except (InvalidOperation, KeyError):
                raise ReleaseError('invalid protected position evidence') from None
        else:
            state = evidence.get('engine_state')
            if (evidence.get('finite_balances') is not True or evidence.get('reconciled') is not True
                    or evidence.get('protected') is not True or evidence.get('risk_status') != 'ALLOW'
                    or evidence.get('snapshot_stable') is not True or evidence.get('live_journal_untouched') is not True
                    or evidence.get('heartbeat_fresh') is not True
                    or not _sha(evidence.get('snapshot_sha256')) or not _sha(evidence.get('risk_history_sha256'))
                    or type(evidence.get('ordinary_orders')) is not int or evidence['ordinary_orders'] < 0
                    or type(evidence.get('algo_orders')) is not int or evidence['algo_orders'] < 0
                    or not isinstance(state, list) or not state or digest(state) != evidence.get('engine_state_sha256')
                    or any(not isinstance(row, dict) or type(row.get('id')) is not int
                           or row.get('state') not in ('ACTIVE', 'PAUSED', 'FLAT') for row in state)):
                raise ReleaseError('grid ownership, protection or current risk evidence incomplete')
            if any(row['state'] == 'ACTIVE' for row in state) and evidence.get('stream_ready') is not True:
                raise ReleaseError('active grid user stream is not ready')
    return baseline_hash


def validate_maintenance(result, baseline, now):
    from decimal import Decimal, InvalidOperation
    if (result.get('ready_for_writer') is not True or result.get('outcome') != 'flat_ready'
            or result.get('pause_complete') is not True or result.get('risk_status') != 'ALLOW'
            or not _fresh(result.get('observed_at'), now)
            or not _fresh(result.get('lock_released_at'), now)
            or result.get('before') != result.get('after')):
        raise ReleaseError('grid maintenance did not prove a safe writer handoff')
    for key in ('account', 'account_mode', 'account_identity_sha256', 'risk_policy_sha256', 'continuity_sha256'):
        if result.get('after', {}).get(key) != baseline.get(key):
            raise ReleaseError('grid maintenance changed fixed account evidence')
    samples = result.get('samples')
    try:
        flat_net = isinstance(samples, list) and all(Decimal(str(r.get('journal_net'))).is_finite()
                                                   and Decimal(str(r.get('journal_net'))) == 0 for r in samples)
    except (InvalidOperation, TypeError):
        flat_net = False
    if not flat_net or len(samples) != 2 or any(
            row.get('flat') is not True or row.get('protected') is not True
            or row.get('ordinary_orders') != 0 or row.get('algo_orders') != 0
            or row.get('journal_active_orders') != 0 or row.get('journal_active_protections') != 0
            or not _fresh(row.get('observed_at'), now)
            for row in samples):
        raise ReleaseError('grid maintenance requires two fresh flat snapshots')
    if (samples[1]['observed_at'] - samples[0]['observed_at'] < 5
            or not _fresh(result.get('lock_acquired_at'), now)
            or not result['lock_acquired_at'] <= samples[0]['observed_at']
                <= samples[1]['observed_at'] <= result['lock_released_at']):
        raise ReleaseError('grid maintenance sampling interval or lock lifetime invalid')


class Release:
    def __init__(self, bundle, expected_sha):
        self.bundle = bundle.resolve()
        self.project = Path('/root/trading_system')
        self.build = json.loads((self.bundle / 'image-digests.json').read_text())
        checked = assemble(list(self.build['images'].values()), self.build['source_sha'],
                           self.build['run_id'], self.build['run_attempt'])
        if self.build != checked:
            raise ReleaseError('build artifact does not match reviewed image plan')
        self.sha = self.build['source_sha']
        if self.sha != expected_sha:
            raise ReleaseError('artifact differs from reviewed source SHA')
        self.version = dict(line.split('=', 1) for line in (self.bundle / 'VERSION').read_text().splitlines())
        if (self.version.get('GIT_SHA') != self.sha
                or self.version.get('BUILD_RUN_ID') != self.build['run_id']
                or self.version.get('BUILD_RUN_ATTEMPT') != self.build['run_attempt']
                or not re.fullmatch(r'[A-F0-9]{8}', self.version.get('DEPLOY_ID', ''))):
            raise ReleaseError('artifact VERSION identity mismatch')
        self.source = json.loads((self.bundle / 'source-files.json').read_text())['files']
        for path, value in self.source.items():
            if PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or not _sha(value):
                raise ReleaseError('invalid source inventory')
        for path in ('VERSION', 'deployment/full-regression/build-plan.json', 'deployment/full-regression/source-mount-removals.json',
                     'scripts/deploy_full_regression.py', 'scripts/full_regression_build.py',
                     'scripts/grid_release_guard.py', 'scripts/full_regression_live_gate.py',
                     'scripts/database_permissions.py', 'database/postgres/permissions.json'):
            if hashlib.sha256((self.bundle / path).read_bytes()).hexdigest() != self.source.get(path):
                raise ReleaseError('release bundle source mismatch: ' + path)
        if (hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != self.source['scripts/deploy_full_regression.py']
                or hashlib.sha256(Path(build_tools.__file__).read_bytes()).hexdigest() != self.source['scripts/full_regression_build.py']
                or hashlib.sha256(build_tools.PLAN.read_bytes()).hexdigest() != self.source['deployment/full-regression/build-plan.json']):
            raise ReleaseError('executing release tools differ from reviewed bundle')
        self.directory = self.project / '.releases/full-regression' / self.sha
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.containers = {container: row for row in read_plan()['images'] for container in row['containers']}

    def source_removals(self, old, proof):
        manifest = json.loads((self.bundle / 'deployment/full-regression/source-mount-removals.json').read_text())
        return verified_source_removals(old, proof, manifest)

    def grid_tool(self, old, image_id, proof, *, apply=False, expected=None):
        """Run a reviewed, single-purpose process without relying on a live executor."""
        helper_path = 'scripts/grid_release_guard.py'
        helper = (self.bundle / helper_path).read_text()
        if hashlib.sha256(helper.encode()).hexdigest() != self.source[helper_path]:
            raise ReleaseError('grid maintenance helper differs from reviewed source')
        body = build_container_body(old, image_id, self.sha)
        validate_mount_coverage(body, proof)
        body.update(Entrypoint=['python'], Cmd=['-B', '-'], OpenStdin=True, StdinOnce=True,
                    AttachStdin=True, AttachStdout=True, AttachStderr=True, Tty=False,
                    Healthcheck={'Test': ['NONE']})
        # No public listener, executable override, or automatic restart for helpers.
        body['HostConfig']['PortBindings'] = {}
        body['HostConfig']['ReadonlyRootfs'] = True
        body['HostConfig']['Tmpfs'] = dict(body['HostConfig'].get('Tmpfs') or {})
        body['HostConfig']['Tmpfs']['/tmp'] = 'rw,nosuid,nodev,size=268435456'
        if not apply:
            body['HostConfig']['Binds'] = [':'.join(bind.split(':')[:2]) + ':ro'
                                           for bind in body['HostConfig'].get('Binds') or []]
            for mount in body['HostConfig'].get('Mounts') or []:
                mount['ReadOnly'] = True
        body['NetworkingConfig']['EndpointsConfig'] = {
            name: {'Aliases': []} for name in body['NetworkingConfig']['EndpointsConfig']}
        action = 'maintenance' if apply else 'probe'
        token = str(time.time_ns())
        helper_id = create_container('grid-release-' + action + '-' + token, body)
        write_private(self.directory / ('grid-' + action + '-' + token + '-container.json'),
                      dict(id=helper_id, source=self.sha, image=image_id, helper_sha256=self.source[helper_path], apply=apply))
        payload = runtime_code_probe(proof) + '\nimport types,sys,asyncio,os,json,logging\nlogging.disable(logging.CRITICAL)\n'
        payload += "m=types.ModuleType('reviewed_grid_release_guard');sys.modules[m.__name__]=m\n"
        payload += 'exec(compile(' + repr(helper) + ", 'reviewed_grid_release_guard.py', 'exec'),m.__dict__)\n"
        call = ('m.maintain_grid(os.environ,' + repr(expected) + ')' if apply else 'm.inspect_grid(os.environ)')
        payload += 'result=asyncio.run(' + call + ')\n'
        payload += "print('GRID_RELEASE_RESULT='+json.dumps(result,sort_keys=True,default=str))\n"
        output = run('docker', 'start', '-ai', helper_id, input_text=payload, timeout=600)
        current = inspect(helper_id)
        if current['State']['Running'] or current['State'].get('ExitCode') != 0:
            raise ReleaseError('grid helper did not finish normally; candidate remains stopped')
        lines = [line for line in output.splitlines() if line.startswith('GRID_RELEASE_RESULT=')]
        if len(lines) != 1:
            raise ReleaseError('grid helper did not provide a unique proof')
        result = json.loads(lines[0].split('=', 1)[1])
        result.update(source=self.sha, image=image_id, helper_id=helper_id, helper_sha256=self.source[helper_path])
        write_private(self.directory / ('grid-' + action + '-' + token + '-result.json'), result)
        return result

    def grid_probe(self, name):
        """Refresh target-bound grid evidence without requiring an idle account."""
        target = inspect(name)
        grid = inspect('trading_system-grid-executor')
        staged, _ = private_json(self.directory, 'trading_system-grid-executor-staged.json')
        proof, _ = private_json(self.directory, 'trading_system-grid-executor-image-proof.json')
        if (digest(proof) != staged.get('image_proof_sha256') or grid['Id'] != staged['old_id']
                or runtime_fingerprint(grid) != staged['old_runtime']):
            raise ReleaseError('grid probe runtime or source differs from staged evidence')
        result = self.grid_tool(grid, staged['image'], proof)
        result.update(container=name, old_id=target['Id'], ok=True, read_only=True)
        relative = name + '-grid-live.json'
        write_private(self.directory / relative, result)
        print(json.dumps({'grid_evidence': relative, 'sha256': hashlib.sha256(
            (self.directory / relative).read_bytes()).hexdigest(), 'source': self.sha}), flush=True)

    def pin(self):
        path = self.project / '.deploy_tmp/full-regression-active.json'
        active = json.loads(path.read_text()) if path.exists() else {'source': self.sha, 'services': {}}
        if active.get('source') != self.sha or not isinstance(active.get('services'), dict):
            raise ReleaseError('another full regression successor is already active')
        if any(row.get('status', 'active') != 'active' for row in active['services'].values()):
            raise ReleaseError('prior activation requires manual review before another service')
        return path, active

    def supersede(self, expected_proof_sha256):
        """Admit a reviewed successor under the CLI deployment lock; touch no service.

        A historical pin can describe containers replaced by another approved
        release. Bind the new admission to fresh actual runtimes, preserve the
        exact old pin, and never reuse its staged candidates or live gates.
        """
        proof, proof_sha = private_json(self.directory, 'supersede-proof.json')
        path = self.project / '.deploy_tmp/full-regression-active.json'
        previous_bytes = path.read_bytes()
        previous = json.loads(previous_bytes)
        scopes = set(self.containers) | {r['container'] for r in read_plan()['preserve_existing']}
        if (not _sha(expected_proof_sha256) or proof_sha != expected_proof_sha256
                or proof.get('schema_version') != 1 or proof.get('source') != self.sha
                or proof.get('previous_source') != previous.get('source')
                or previous.get('source') == self.sha
                or proof.get('previous_pin_sha256') != hashlib.sha256(previous_bytes).hexdigest()
                or not isinstance(previous.get('services'), dict)
                or any(r.get('status', 'active') != 'active' for r in previous['services'].values())
                or not isinstance(proof.get('containers'), dict) or set(proof['containers']) != scopes):
            raise ReleaseError('successor admission identity or prior status invalid')
        self.audit('supersede')
        observed = proof.get('observed_at')
        if type(observed) not in (int, float) or not 0 <= time.time() - observed <= 180:
            raise ReleaseError('successor admission evidence stale')
        for name in sorted(scopes):
            row = inspect(name)
            expected = dict(id=row['Id'], image=row['Image'], runtime_sha256=runtime_fingerprint(row))
            if not row['State']['Running'] or proof['containers'][name] != expected:
                raise ReleaseError('successor admission runtime changed: ' + name)
        if not _fresh(observed, time.time()) or path.read_bytes() != previous_bytes:
            raise ReleaseError('previous active pin changed during admission')
        archive = self.directory / 'previous-active-pin.json'
        with archive.open('xb') as handle:
            os.chmod(archive, 0o600)
            handle.write(previous_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        write_private(path, dict(source=self.sha, services={}, predecessors=proof['containers'],
                                 supersede_proof_sha256=proof_sha))
        print(json.dumps(dict(admitted_source=self.sha, predecessor_containers=len(scopes))), flush=True)

    def audit(self, stage):
        result = subprocess.run([sys.executable, str(self.bundle / 'scripts/database_permissions.py'),
                                 'audit', '--manifest', str(self.bundle / 'database/postgres/permissions.json')],
                                cwd=self.project, capture_output=True, text=True, timeout=180)
        try:
            proof = json.loads(result.stdout)
        except ValueError:
            raise ReleaseError('permission audit did not return a proof') from None
        write_private(self.directory / (stage + '-permissions.json'), proof)
        expected = self.source['database/postgres/permissions.json']
        if result.returncode or proof.get('ok') is not True or proof.get('manifest_sha256') != expected:
            raise ReleaseError('candidate permission contract audit failed')

    def stage(self, name):
        _, active = self.pin()
        if 'predecessors' in active:
            admitted = active['predecessors'].get(name)
            current = inspect(name)
            if admitted != dict(id=current['Id'], image=current['Image'],
                                runtime_sha256=runtime_fingerprint(current)):
                raise ReleaseError('admitted predecessor runtime changed')
        row = self.containers[name]
        record = self.directory / (name + '-staged.json')
        if record.exists():
            raise ReleaseError('service already staged; inspect its evidence before continuing')
        self.audit(name + '-stage')
        old = inspect(name)
        if 'predecessors' in active and active['predecessors'].get(name) != dict(
                id=old['Id'], image=old['Image'], runtime_sha256=runtime_fingerprint(old)):
            raise ReleaseError('admitted predecessor runtime changed')
        if not old['State']['Running']:
            raise ReleaseError('predecessor is not running')
        if old['Config'].get('Healthcheck', {}).get('Test', ['NONE'])[0] == 'NONE':
            raise ReleaseError('reviewed healthcheck required before staging')
        write_private(self.directory / (name + '-before.json'), old)
        image = validate_image_ref(row['name'], self.build['images'][row['name']]['image'])
        run('docker', 'pull', image, timeout=600)
        metadata = json.loads(run('docker', 'image', 'inspect', image))[0]
        if (metadata['Config'].get('Labels', {}).get('org.opencontainers.image.revision') != self.sha
                or image not in metadata.get('RepoDigests', [])
                or metadata.get('Os') != 'linux' or metadata.get('Architecture') != 'amd64'):
            raise ReleaseError('image revision label mismatch')
        proof = json.loads(run('docker', 'run', '--rm', '-i', '--network', 'none', '--read-only',
                               '--entrypoint', 'python', image, '-B', '-',
                               input_text=code_probe(row['name'], self.source, self.sha, self.version['DEPLOY_ID'])))
        write_private(self.directory / (name + '-image-proof.json'), proof)
        removals = self.source_removals(old, proof)
        body = build_container_body(old, metadata['Id'], self.sha, source_removals=removals)
        validate_mount_coverage(body, proof)
        new_id = create_container(name + '-ready-' + self.sha[:8], body)
        new = inspect(new_id)
        if new['Image'] != metadata['Id'] or new['State']['Status'] != 'created':
            raise ReleaseError('staged container identity mismatch')
        validate_staged_runtime(new, body)
        write_private(record, {'source': self.sha, 'old_id': old['Id'], 'new_id': new_id,
                              'image': metadata['Id'], 'old_runtime': runtime_fingerprint(old),
                              'new_runtime': runtime_fingerprint(new), 'image_proof_sha256': digest(proof),
                              'source_mount_removals': removals, 'staged_at': time.time()})
        print(json.dumps({'staged': name, 'source': self.sha, 'source_files_verified': proof['source_count']}), flush=True)

    def activate(self, name):
        pin_path, active = self.pin()  # Reject conflicting pins before touching any container.
        staged = json.loads((self.directory / (name + '-staged.json')).read_text())
        state_path = self.directory / (name + '-activation.json')
        if state_path.exists() or name in active['services']:
            raise ReleaseError('activation already attempted; no automatic replay or rollback')
        if staged.get('source') != self.sha:
            raise ReleaseError('staging evidence belongs to another release')
        self.audit(name + '-activation')
        old, new = inspect(name), inspect(staged['new_id'])
        if (old['Id'] != staged['old_id'] or not old['State']['Running']
                or runtime_fingerprint(old) != staged['old_runtime']
                or new['Image'] != staged['image'] or new['State']['Status'] != 'created'
                or runtime_fingerprint(new) != staged['new_runtime']):
            raise ReleaseError('runtime changed after staging')
        proof = json.loads((self.directory / (name + '-image-proof.json')).read_text())
        if digest(proof) != staged.get('image_proof_sha256'):
            raise ReleaseError('staged source proof changed')
        if self.source_removals(old, proof) != staged.get('source_mount_removals', []):
            raise ReleaseError('source mount removal proof changed after staging')
        gate, gate_hash = private_json(self.directory, 'live-gate.json')
        validate_live_gate(self.directory, gate, self.sha, name, old, time.time())
        state = dict(staged, service=name, status='activating', phase='preflight_complete',
                     live_gate_sha256=gate_hash, no_automatic_rollback=True, started_at=time.time())
        state['phase_history'] = [dict(phase='preflight_complete', at=time.time())]
        write_private(state_path, state)
        active['services'][name] = {'image': new['Image'], 'container_id': new['Id'], 'old_id': old['Id'],
                                    'evidence': str(state_path), 'status': 'activating'}
        write_private(pin_path, active)
        def phase(value):
            state['phase'] = value
            state['phase_history'].append(dict(phase=value, at=time.time()))
            write_private(state_path, state)
        try:
            run('docker', 'update', '--restart=no', old['Id'])
            phase('predecessor_restart_disabled')
            if name == 'trading_system-grid-executor':
                phase('grid_sigint_requested')
                run('docker', 'kill', '--signal=SIGINT', old['Id'])
                deadline = time.monotonic() + 180
                while inspect(old['Id'])['State']['Running']:
                    if time.monotonic() >= deadline:
                        raise ReleaseError('grid SIGINT timed out; no SIGKILL or candidate start permitted')
                    time.sleep(1)
            else:
                run('docker', 'stop', '-t', '60', old['Id'])
            if inspect(old['Id'])['State']['Running']:
                raise ReleaseError('predecessor is still running')
            phase('predecessor_stopped')
            if name == 'trading_system-grid-executor':
                baseline, _ = private_json(self.directory, 'live-baseline.json')
                phase('grid_maintenance_requested')
                maintenance = self.grid_tool(old, new['Image'], proof, apply=True, expected=baseline['grid'])
                state['maintenance'] = maintenance
                write_private(state_path, state)
                validate_maintenance(maintenance, baseline['grid'], time.time())
                current_old = inspect(old['Id'])
                if (current_old['State']['Running'] or current_old['HostConfig']['RestartPolicy']['Name'] != 'no'):
                    raise ReleaseError('predecessor resumed during maintenance')
                phase('grid_flat_handoff_verified')
            run('docker', 'rename', old['Id'], name + '-before-full-' + self.sha[:8])
            phase('predecessor_renamed')
            run('docker', 'rename', new['Id'], name)
            phase('candidate_renamed')
            # Recheck evidence age immediately before the first possible writer start.
            validate_live_gate(self.directory, gate, self.sha, name, old, time.time())
            if name == 'trading_system-grid-executor':
                validate_maintenance(maintenance, baseline['grid'], time.time())
            phase('candidate_start_requested')
            run('docker', 'start', new['Id'])
            phase('candidate_started')
            deadline = time.monotonic() + 240
            while True:
                current = inspect(new['Id'])
                if current['Image'] != staged['image'] or not current['State']['Running'] or current['RestartCount']:
                    raise ReleaseError('new container identity changed, stopped or restarted')
                health = current['State'].get('Health', {}).get('Status')
                if health == 'healthy':
                    break
                if health is None or health == 'unhealthy' or time.monotonic() > deadline:
                    raise ReleaseError('new container did not become healthy')
                time.sleep(2)
            phase('health_verified')
            run('docker', 'exec', '-i', new['Id'], 'python', '-B', '-', input_text=runtime_code_probe(proof))
            phase('source_verified')
            restart = old['HostConfig']['RestartPolicy']
            policy = restart['Name'] or 'no'
            if policy == 'on-failure' and restart.get('MaximumRetryCount'):
                policy += ':' + str(restart['MaximumRetryCount'])
            run('docker', 'update', '--restart=' + policy, new['Id'])
            state.update(status='active', phase='restart_restored', verified_at=time.time(), health=health,
                         source_files_verified=proof['source_count'])
            active['services'][name]['status'] = 'active'
            write_private(pin_path, active)
            write_private(state_path, state)
            print(json.dumps({'active': name, 'source': self.sha, 'health': health}), flush=True)
        except BaseException as exc:
            state.update(status='failed', error=type(exc).__name__, failed_at=time.time())
            active['services'][name]['status'] = 'failed'
            write_private(state_path, state)
            write_private(pin_path, active)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stage', 'activate', 'grid_probe', 'supersede'))
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--container')
    parser.add_argument('--supersede-proof-sha256')
    parser.add_argument('--expected-sha', required=True)
    args = parser.parse_args()
    release = Release(args.bundle, args.expected_sha)
    if args.action != 'supersede' and args.container not in release.containers:
        raise ReleaseError('container is outside this reviewed release')
    with (release.project / '.deploy_tmp/compose_deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'supersede':
            release.supersede(args.supersede_proof_sha256)
        else:
            getattr(release, args.action)(args.container)


if __name__ == '__main__':
    main()
