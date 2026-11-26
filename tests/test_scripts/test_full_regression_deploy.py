"""Release construction must preserve the running account and data boundaries."""
import copy

import pytest

from scripts.deploy_full_regression import (
    ReleaseError, build_container_body, runtime_fingerprint, validate_image_ref,
)


def running(name='ai-tuner'):
    binds = [
        '/host/strategies:/app/strategies:rw',
        '/release/hrs:/app/strategies/hrs:ro',
        '/frozen/hrs.yaml:/app/strategies/hrs/config.yaml:ro',
        '/frozen/overrides:/app/strategies/hrs/tuning_overrides:rw',
        '/evidence:/run/classic-trading:ro',
        '/logs:/app/logs:rw',
    ]
    binds += [f'/frozen/{strategy}/{item}:/app/strategies/{strategy}/{item}:rw'
              for strategy in ('btc_eth', 'btc_eth_aggressive', 'new_coin', 'grid')
              for item in ('config.yaml', 'tuning_overrides')]
    return {'Id': 'a' * 64, 'Name': '/' + name, 'Image': 'sha256:' + 'b' * 64,
            'Config': {'Hostname': 'old-host', 'Env': ['ACCOUNT_MODE=classic', 'SECRET=not-printed'],
                       'Cmd': ['python', 'main.py'], 'Entrypoint': None,
                       'Labels': {'com.docker.compose.project': 'trading_system'},
                       'User': '1000:1000', 'Healthcheck': {'Test': ['CMD', 'check']}},
            'HostConfig': {'Binds': binds, 'RestartPolicy': {'Name': 'unless-stopped', 'MaximumRetryCount': 0}},
            'NetworkSettings': {'Networks': {'trading_system': {'Aliases': ['ai-tuner'], 'IPAMConfig': None, 'DriverOpts': None}}}}


def test_ai_code_mounts_are_removed_but_exact_configuration_and_account_are_kept():
    old = running()
    before = copy.deepcopy(old)
    body = build_container_body(old, 'sha256:' + 'c' * 64, 'd' * 40)
    assert old == before
    assert body['HostConfig']['Binds'] == old['HostConfig']['Binds'][2:]
    assert body['Env'] == old['Config']['Env']
    assert body['Cmd'] == old['Config']['Cmd']
    assert body['Healthcheck'] == old['Config']['Healthcheck']
    assert body['User'] == '1000:1000'
    assert body['NetworkingConfig']['EndpointsConfig']['trading_system']['Aliases'] == ['ai-tuner']
    assert body['HostConfig']['RestartPolicy']['Name'] == 'no'


def test_non_ai_runtime_does_not_remove_any_data_mount():
    old = running('trading_system-hrs')
    body = build_container_body(old, 'sha256:' + 'c' * 64, 'd' * 40)
    assert body['HostConfig']['Binds'] == old['HostConfig']['Binds']


def test_ai_unknown_strategy_directory_mount_blocks_release():
    old = running()
    old['HostConfig']['Binds'].append('/unknown:/app/strategies/grid:rw')
    with pytest.raises(ReleaseError, match='code mount'):
        build_container_body(old, 'sha256:' + 'c' * 64, 'd' * 40)


def test_runtime_fingerprint_detects_secret_config_and_network_drift_without_exposing_them():
    old = running()
    for mutate in (lambda row: row['Config']['Env'].append('SECRET=changed'),
                   lambda row: row['HostConfig']['Binds'].append('/x:/app/data:rw'),
                   lambda row: row['NetworkSettings']['Networks']['trading_system']['Aliases'].append('other')):
        changed = copy.deepcopy(old)
        mutate(changed)
        assert runtime_fingerprint(old) != runtime_fingerprint(changed)
    assert len(runtime_fingerprint(old)) == 64


@pytest.mark.parametrize('image', ['ghcr.io/bearflower/trading-grid:latest',
                                  'ghcr.io/other/trading-grid@sha256:' + 'a' * 64,
                                  'ghcr.io/bearflower/trading-hrs@sha256:' + 'a' * 64])
def test_only_matching_immutable_repository_is_accepted(image):
    with pytest.raises(ReleaseError):
        validate_image_ref('grid', image)


def test_matching_immutable_digest_is_accepted():
    image = 'ghcr.io/bearflower/trading-grid@sha256:' + 'a' * 64
    assert validate_image_ref('grid', image) == image

# Real gate and failure transitions below use a fully local Docker substitute.
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

from scripts import deploy_full_regression as release_mod

SHA = 'd' * 40
NOW = 1000.0


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gate_files(release, old):
    common = dict(source=SHA, container=old['Name'].lstrip('/'), old_id=old['Id'], observed_at=NOW,
                  read_only=True, ok=True)
    trace = [dict(method='GET', path='/fapi/v3/' + endpoint, signed=True)
             for endpoint in ('account', 'positionRisk', 'openOrders', 'openAlgoOrders')]
    state = [dict(id=1, state='ACTIVE', lock_reason=None, updated_at_ms=123)]
    baseline = dict(schema_version=1, source=SHA,
                    main=dict(account='default', account_mode='classic', account_identity_sha256='1' * 64,
                              allocation_sha256='2' * 64, risk_policy_sha256='3' * 64),
                    grid=dict(account='grid-subaccount', account_mode='classic', account_identity_sha256='4' * 64,
                              risk_policy_sha256='5' * 64, continuity_sha256='a' * 64))
    gate = dict(common, schema_version=1,
                baseline_sha256=write_json(release.directory / 'live-baseline.json', baseline), evidence={})
    main = dict(common, **baseline['main'], requests=trace, equity='100', positions_sha256='6' * 64,
                ordinary_orders_sha256='7' * 64, algo_orders_sha256='8' * 64, position_exposure=[],
                protected_positions=0, writes_report_metadata=False)
    grid = dict(common, **baseline['grid'], requests=trace, finite_balances=True, flat=True,
                ordinary_orders=0, algo_orders=0, engine_state=state, engine_state_sha256=release_mod.digest(state),
                protected=True, reconciled=True, risk_status='ALLOW', snapshot_stable=True,
                heartbeat_fresh=True, stream_ready=True,
                live_journal_untouched=True, snapshot_sha256='b' * 64, risk_history_sha256='c' * 64)
    for scope, evidence in [('main', main), ('grid', grid)]:
        relative = 'evidence/' + scope + '.json'
        gate['evidence'][scope] = dict(path=relative, sha256=write_json(release.directory / relative, evidence))
    write_json(release.directory / 'live-gate.json', gate)
    return gate


@pytest.fixture
def activation(tmp_path, monkeypatch):
    release = release_mod.Release.__new__(release_mod.Release)
    release.project = tmp_path / 'project'
    (release.project / '.deploy_tmp').mkdir(parents=True)
    release.directory = tmp_path / 'release'
    release.directory.mkdir()
    release.sha = SHA
    release.audit = Mock()
    release.source_removals = Mock(return_value=[])
    old = running()
    old['State'] = dict(Running=True, Status='running', Health=dict(Status='healthy'))
    old['RestartCount'] = 0
    old['Config']['Env'] += ['TRADE_ACCOUNT_ID=default']
    body = build_container_body(old, 'sha256:' + 'c' * 64, SHA)
    new = dict(Id='c' * 64, Name='/ai-tuner-ready-' + SHA[:8], Image=body['Image'],
               Config={key: value for key, value in body.items() if key not in ('HostConfig', 'NetworkingConfig')},
               HostConfig=copy.deepcopy(body['HostConfig']),
               NetworkSettings=dict(Networks=copy.deepcopy(body['NetworkingConfig']['EndpointsConfig'])),
               State=dict(Running=False, Status='created', Health=dict(Status='starting')), RestartCount=0)
    proof = dict(hashes={'ai_tuner/main.py': '9' * 64}, source_count=1)
    staged = dict(source=SHA, old_id=old['Id'], new_id=new['Id'], image=new['Image'],
                  old_runtime=runtime_fingerprint(old), new_runtime=runtime_fingerprint(new),
                  image_proof_sha256=release_mod.digest(proof))
    write_json(release.directory / 'ai-tuner-staged.json', staged)
    write_json(release.directory / 'ai-tuner-image-proof.json', proof)
    gate_files(release, old)
    commands = []

    def fake_run(*args, **kwargs):
        commands.append((args, kwargs))
        if args[1] == 'stop':
            old['State'].update(Running=False, Status='exited')
        if args[1] == 'start':
            new['State'].update(Running=True, Status='running', Health=dict(Status='healthy'))
        return ''

    monkeypatch.setattr(release_mod, 'inspect', lambda name: old if name in ('ai-tuner', old['Id']) else new)
    monkeypatch.setattr(release_mod, 'run', fake_run)
    monkeypatch.setattr(release_mod.time, 'time', lambda: NOW)
    return release, old, new, commands, fake_run


def test_activation_records_successor_only_after_health_and_source_verification(activation):
    release, old, new, commands, _ = activation
    release.activate('ai-tuner')
    state = json.loads((release.directory / 'ai-tuner-activation.json').read_text())
    pin = json.loads((release.project / '.deploy_tmp/full-regression-active.json').read_text())
    assert state['status'] == 'active' and pin['services']['ai-tuner']['status'] == 'active'
    assert old['State']['Running'] is False and new['State']['Running'] is True
    execution = next(item for item in commands if item[0][1] == 'exec')
    assert '-i' in execution[0] and execution[0][-1] == '-' and '-c' not in execution[0]
    assert execution[1]['input_text']
    assert all(item[0][1] not in ('rm', 'restart') for item in commands)
    with pytest.raises(ReleaseError, match='already attempted'):
        release.activate('ai-tuner')


@pytest.mark.parametrize('failure', ['permissions', 'pin', 'source', 'runtime', 'staged_source'])
def test_preflight_failure_never_mutates_predecessor(activation, failure):
    release, old, new, commands, _ = activation
    if failure == 'permissions':
        release.audit.side_effect = ReleaseError('denied')
    elif failure == 'pin':
        write_json(release.project / '.deploy_tmp/full-regression-active.json', {'source': 'e' * 40, 'services': {}})
    elif failure == 'source':
        proof = release.directory / 'ai-tuner-image-proof.json'
        write_json(proof, dict(hashes={}, source_count=0))
    elif failure == 'runtime':
        old['Config']['Env'].append('SECRET=drift')
    else:
        staged = json.loads((release.directory / 'ai-tuner-staged.json').read_text())
        staged['source'] = 'e' * 40
        write_json(release.directory / 'ai-tuner-staged.json', staged)
    with pytest.raises(ReleaseError):
        release.activate('ai-tuner')
    assert not commands and old['State']['Running'] is True
    assert not (release.directory / 'ai-tuner-activation.json').exists()


@pytest.mark.parametrize('failure', ['bare_flags', 'old', 'future', 'wrong_account', 'wrong_policy', 'missing_hash',
                                    'write_request', 'missing_view', 'changed_lock', 'unprotected', 'escaped', 'tampered'])
def test_incomplete_or_unbound_live_evidence_blocks_before_stop(activation, failure):
    release, old, _, commands, _ = activation
    gate = json.loads((release.directory / 'live-gate.json').read_text())
    evidence_path = release.directory / gate['evidence']['main']['path']
    evidence = json.loads(evidence_path.read_text())
    if failure == 'bare_flags':
        gate.pop('evidence')
    elif failure in ('old', 'future'):
        evidence['observed_at'] = NOW - 181 if failure == 'old' else NOW + 1
    elif failure == 'wrong_account':
        evidence['account'] = 'another-account'
    elif failure == 'wrong_policy':
        evidence['risk_policy_sha256'] = 'a' * 64
    elif failure == 'missing_hash':
        evidence.pop('positions_sha256')
    elif failure == 'write_request':
        evidence['requests'][0]['method'] = 'POST'
    elif failure == 'missing_view':
        evidence['requests'].pop()
    elif failure == 'unprotected':
        evidence['position_exposure'] = [dict(symbol='BTCUSDT', positionSide='BOTH', positionAmt='1')]
    elif failure == 'changed_lock':
        grid_path = release.directory / gate['evidence']['grid']['path']
        grid = json.loads(grid_path.read_text())
        grid['engine_state'][0]['state'] = 'IDLE'
        gate['evidence']['grid']['sha256'] = write_json(grid_path, grid)
    elif failure == 'escaped':
        gate['evidence']['main']['path'] = '../outside.json'
    elif failure == 'tampered':
        evidence_path.write_text('{}')
    if failure not in ('bare_flags', 'changed_lock', 'escaped', 'tampered'):
        gate['evidence']['main']['sha256'] = write_json(evidence_path, evidence)
    write_json(release.directory / 'live-gate.json', gate)
    with pytest.raises(ReleaseError):
        release.activate('ai-tuner')
    assert not commands and old['State']['Running'] is True


@pytest.mark.parametrize('operation', ['stop', 'rename', 'start', 'exec'])
def test_activation_failure_keeps_both_containers_and_blocks_automatic_replay(activation, monkeypatch, operation):
    release, old, new, commands, fake_run = activation
    def fail(*args, **kwargs):
        if args[1] == operation:
            commands.append((args, kwargs))
            raise ReleaseError('injected failure')
        return fake_run(*args, **kwargs)
    monkeypatch.setattr(release_mod, 'run', fail)
    with pytest.raises(ReleaseError):
        release.activate('ai-tuner')
    state = json.loads((release.directory / 'ai-tuner-activation.json').read_text())
    pin = json.loads((release.project / '.deploy_tmp/full-regression-active.json').read_text())
    assert state['status'] == pin['services']['ai-tuner']['status'] == 'failed'
    assert all(command[0][1] not in ('rm', 'restart') for command in commands)
    assert not any(command[0][1] == 'start' and command[0][-1] == old['Id'] for command in commands)
    if operation == 'exec':
        assert new['State']['Running'] is True
    count = len(commands)
    with pytest.raises(ReleaseError, match='manual review'):
        release.activate('ai-tuner')
    assert len(commands) == count


def test_gate_expiry_during_stop_prevents_candidate_start(activation, monkeypatch):
    release, old, _, commands, fake_run = activation
    clock = [NOW]
    monkeypatch.setattr(release_mod.time, 'time', lambda: clock[0])
    def slow_stop(*args, **kwargs):
        result = fake_run(*args, **kwargs)
        if args[1] == 'stop':
            clock[0] += 181
        return result
    monkeypatch.setattr(release_mod, 'run', slow_stop)
    with pytest.raises(ReleaseError, match='fresh'):
        release.activate('ai-tuner')
    assert old['State']['Running'] is False
    assert not any(item[0][1] == 'start' for item in commands)


def test_runtime_clone_detects_resource_or_security_setting_drift():
    old = running('grid')
    old['HostConfig'].update(Memory=1234, ReadonlyRootfs=True, SecurityOpt=['no-new-privileges'])
    body = build_container_body(old, 'sha256:' + 'c' * 64, SHA)
    new = dict(Config={key: value for key, value in body.items() if key not in ('HostConfig', 'NetworkingConfig')},
               HostConfig=copy.deepcopy(body['HostConfig']),
               NetworkSettings=dict(Networks=copy.deepcopy(body['NetworkingConfig']['EndpointsConfig'])))
    release_mod.validate_staged_runtime(new, body)
    new['HostConfig']['Memory'] = 0
    with pytest.raises(ReleaseError, match='configuration'):
        release_mod.validate_staged_runtime(new, body)


def test_any_mount_hiding_verified_source_is_rejected_before_start():
    body = build_container_body(running('grid'), 'sha256:' + 'c' * 64, SHA)
    with pytest.raises(ReleaseError, match='hide'):
        release_mod.validate_mount_coverage(body, {'hashes': {'strategies/grid/main.py': 'a' * 64}})


def test_missing_required_image_source_does_not_pass_subset_proof(tmp_path):
    import subprocess
    (tmp_path / 'shared').mkdir()
    file = tmp_path / 'shared/one.py'
    file.write_text('value=1\n')
    (tmp_path / 'VERSION').write_text('GIT_SHA=' + SHA + '\nDEPLOY_ID=00000001\n')
    source = {'shared/one.py': hashlib.sha256(file.read_bytes()).hexdigest(), 'strategies/grid/__main__.py': 'a' * 64}
    code = release_mod.code_probe('grid', source, SHA, '00000001').replace("pathlib.Path('/app')", 'pathlib.Path(' + repr(str(tmp_path)) + ')')
    result = subprocess.run([__import__('sys').executable, '-'], input=code, text=True, capture_output=True)
    assert result.returncode != 0 and 'required image source is missing' in result.stderr


def test_probe_payload_is_sent_via_stdin_instead_of_argument(monkeypatch):
    payload = 'x' * 500_000
    mocked = Mock(return_value=type('Result', (), {'returncode': 0, 'stdout': '{}'})())
    monkeypatch.setattr(release_mod.subprocess, 'run', mocked)
    assert release_mod.run('docker', 'run', '-i', 'image', 'python', '-', input_text=payload) == '{}'
    assert mocked.call_args.kwargs['input'] == payload
    assert sum(map(len, mocked.call_args.args[0])) < 1000


def test_ai_directory_removal_needs_every_existing_frozen_config_mount():
    old = running()
    old['HostConfig']['Binds'] = [bind for bind in old['HostConfig']['Binds'] if '/grid/config.yaml:' not in bind]
    with pytest.raises(ReleaseError, match='all exact frozen'):
        build_container_body(old, 'sha256:' + 'c' * 64, SHA)


def test_stage_probes_image_via_stdin_without_starting_or_stopping_runtime(activation, monkeypatch):
    release, old, new, _, _ = activation
    (release.directory / 'ai-tuner-staged.json').unlink()
    ref = 'ghcr.io/bearflower/trading-ai-tuner@sha256:' + 'c' * 64
    release.containers = {'ai-tuner': {'name': 'ai-tuner'}}
    release.build = {'images': {'ai-tuner': {'image': ref}}}
    release.source = {'ai_tuner/main.py': '9' * 64}
    release.version = {'DEPLOY_ID': '00000001'}
    calls = []
    def docker(*args, **kwargs):
        calls.append((args, kwargs))
        if args[1] == 'image':
            return json.dumps([{'Id': new['Image'], 'Config': {'Labels': {'org.opencontainers.image.revision': SHA}},
                               'RepoDigests': [ref], 'Os': 'linux', 'Architecture': 'amd64'}])
        if args[1] == 'run':
            return json.dumps({'hashes': release.source, 'source_count': 1})
        assert args[1] == 'pull'
        return ''
    monkeypatch.setattr(release_mod, 'run', docker)
    monkeypatch.setattr(release_mod, 'create_container', Mock(return_value=new['Id']))
    release.stage('ai-tuner')
    probe = next(item for item in calls if item[0][1] == 'run')
    assert probe[0][-1] == '-' and '-i' in probe[0] and '-c' not in probe[0]
    assert 'required=' in probe[1]['input_text']
    assert old['State']['Running'] is True and new['State']['Running'] is False
    staged = json.loads((release.directory / 'ai-tuner-staged.json').read_text())
    assert staged['image_proof_sha256']


@pytest.mark.parametrize('failure', ['returncode', 'not_ok', 'wrong_manifest', 'not_json'])
def test_permission_gate_requires_successful_candidate_manifest_proof(tmp_path, monkeypatch, failure):
    release = release_mod.Release.__new__(release_mod.Release)
    release.bundle = release.project = tmp_path
    release.directory = tmp_path / 'evidence'
    release.source = {'database/postgres/permissions.json': 'a' * 64}
    proof = {'ok': failure != 'not_ok', 'manifest_sha256': ('b' if failure == 'wrong_manifest' else 'a') * 64}
    stdout = 'bad JSON' if failure == 'not_json' else json.dumps(proof)
    monkeypatch.setattr(release_mod.subprocess, 'run', Mock(return_value=type('Result', (), {
        'returncode': int(failure == 'returncode'), 'stdout': stdout})()))
    with pytest.raises(ReleaseError, match='permission'):
        release.audit('preflight')


@pytest.mark.parametrize('failure', ['expected_sha', 'build_attempt', 'executing_tool'])
def test_release_identity_is_bound_to_reviewed_sha_and_actual_tools(tmp_path, failure):
    import shutil
    from scripts.full_regression_build import assemble, build_receipt, read_plan
    rows = [build_receipt(item['name'], 'sha256:' + 'b' * 64, SHA, '123', '1') for item in read_plan()['images']]
    write_json(tmp_path / 'image-digests.json', assemble(rows, SHA, '123', '1'))
    (tmp_path / 'VERSION').write_text(f'GIT_SHA={SHA}\nDEPLOY_ID=00000001\nBUILD_RUN_ID=123\nBUILD_RUN_ATTEMPT=1\n')
    sources = {'VERSION': hashlib.sha256((tmp_path / 'VERSION').read_bytes()).hexdigest()}
    for path in ('scripts/deploy_full_regression.py', 'scripts/full_regression_build.py', 'scripts/database_permissions.py',
                 'scripts/grid_release_guard.py', 'scripts/full_regression_live_gate.py',
                 'database/postgres/permissions.json', 'deployment/full-regression/build-plan.json',
                 'deployment/full-regression/source-mount-removals.json'):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(release_mod.build_tools.ROOT / path, target)
        sources[path] = hashlib.sha256(target.read_bytes()).hexdigest()
    if failure == 'build_attempt':
        (tmp_path / 'VERSION').write_text((tmp_path / 'VERSION').read_text().replace('ATTEMPT=1', 'ATTEMPT=2'))
    if failure == 'executing_tool':
        target = tmp_path / 'scripts/deploy_full_regression.py'
        target.write_text(target.read_text() + '\n# modified bundle copy\n')
        sources['scripts/deploy_full_regression.py'] = hashlib.sha256(target.read_bytes()).hexdigest()
    write_json(tmp_path / 'source-files.json', {'files': sources})
    with pytest.raises(ReleaseError):
        release_mod.Release(tmp_path, 'e' * 40 if failure == 'expected_sha' else SHA)


def test_non_grid_activation_accepts_natural_owned_grid_orders_and_new_state(activation):
    release, old, _, _, _ = activation
    gate = gate_files(release, old)
    ref = gate['evidence']['grid']
    path = release.directory / ref['path']
    evidence = json.loads(path.read_text())
    evidence.update(flat=False, ordinary_orders=11, algo_orders=1)
    evidence['engine_state'][0]['updated_at_ms'] += 10
    evidence['engine_state_sha256'] = release_mod.digest(evidence['engine_state'])
    ref['sha256'] = write_json(path, evidence)
    write_json(release.directory / 'live-gate.json', gate)
    release.activate('ai-tuner')


def maintenance_proof(release):
    baseline = json.loads((release.directory / 'live-baseline.json').read_text())['grid']
    binding = dict(baseline, risk_history_sha256='f' * 64)
    sample = dict(flat=True, protected=True, ordinary_orders=0, algo_orders=0,
                  journal_active_orders=0, journal_active_protections=0, journal_net='0.000', observed_at=NOW)
    return dict(ready_for_writer=True, outcome='flat_ready', pause_complete=True, risk_status='ALLOW',
                observed_at=NOW, lock_acquired_at=NOW-6, lock_released_at=NOW,
                before=binding, after=binding, samples=[dict(sample, observed_at=NOW-5), dict(sample)])


@pytest.fixture
def grid_activation(activation, monkeypatch):
    release, old, new, commands, fake_run = activation
    name = 'trading_system-grid-executor'
    old['Name'] = '/' + name
    new['Name'] = '/' + name + '-ready-' + SHA[:8]
    staged = json.loads((release.directory / 'ai-tuner-staged.json').read_text())
    staged.update(old_runtime=runtime_fingerprint(old), new_runtime=runtime_fingerprint(new))
    write_json(release.directory / (name + '-staged.json'), staged)
    (release.directory / (name + '-image-proof.json')).write_bytes((release.directory / 'ai-tuner-image-proof.json').read_bytes())
    gate_files(release, old)
    monkeypatch.setattr(release_mod, 'inspect', lambda key: old if key in (name, old['Id']) else new)
    def docker(*args, **kwargs):
        if args[1] == 'update' and args[-1] == old['Id']:
            old['HostConfig']['RestartPolicy'] = {'Name': 'no', 'MaximumRetryCount': 0}
        if args[1] == 'kill':
            old['State'].update(Running=False, Status='exited')
        return fake_run(*args, **kwargs)
    monkeypatch.setattr(release_mod, 'run', docker)
    release.grid_tool = Mock(return_value=maintenance_proof(release))
    return release, old, new, commands, docker


def test_grid_requires_explicit_sigint_and_locked_maintenance_before_start(grid_activation):
    release, old, new, commands, _ = grid_activation
    release.activate('trading_system-grid-executor')
    assert ('docker', 'kill', '--signal=SIGINT', old['Id']) in [c[0] for c in commands]
    assert not any(c[0][1] == 'stop' for c in commands)
    release.grid_tool.assert_called_once()
    assert release.grid_tool.call_args.kwargs['apply'] is True
    assert new['State']['Running'] is True
    state = json.loads((release.directory / 'trading_system-grid-executor-activation.json').read_text())
    assert state['maintenance']['ready_for_writer'] is True


@pytest.mark.parametrize('failure', ['nonflat', 'no_stop', 'identity', 'history', 'stale', 'incomplete', 'net', 'too_fast', 'lock'])
def test_failed_grid_maintenance_cannot_start_candidate_or_restore_old(grid_activation, failure):
    release, old, new, commands, _ = grid_activation
    result = maintenance_proof(release)
    if failure == 'nonflat': result['samples'][0]['flat'] = False
    elif failure == 'no_stop': result['samples'][0]['protected'] = False
    elif failure == 'identity': result['after'] = dict(result['before'], account_identity_sha256='0' * 64)
    elif failure == 'history': result['after'] = dict(result['before'], risk_history_sha256='0' * 64)
    elif failure == 'stale': result['observed_at'] -= 181
    elif failure == 'incomplete': result['pause_complete'] = False
    elif failure == 'net': result['samples'][0]['journal_net'] = '0.1'
    elif failure == 'too_fast': result['samples'][0]['observed_at'] = NOW-4
    else: result['lock_released_at'] = NOW-1
    release.grid_tool.return_value = result
    with pytest.raises(ReleaseError):
        release.activate('trading_system-grid-executor')
    assert not new['State']['Running']
    assert not any(c[0][1] == 'start' for c in commands)
    assert not any(c[0][1] == 'rm' for c in commands)


def test_sigint_timeout_keeps_old_running_without_force_kill_or_helper(grid_activation, monkeypatch):
    release, old, new, commands, docker = grid_activation
    clock = [0]
    monkeypatch.setattr(release_mod.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(release_mod.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + 200))
    def ignored_sigint(*args, **kwargs):
        if args[1] == 'kill':
            commands.append((args, kwargs))
            return ''
        return docker(*args, **kwargs)
    monkeypatch.setattr(release_mod, 'run', ignored_sigint)
    with pytest.raises(ReleaseError, match='SIGINT timed out'):
        release.activate('trading_system-grid-executor')
    assert old['State']['Running'] and not new['State']['Running']
    release.grid_tool.assert_not_called()
    assert [c[0] for c in commands if c[0][1] == 'kill'] == [('docker', 'kill', '--signal=SIGINT', old['Id'])]


def test_exact_reviewed_source_mount_removal_needs_old_hash_and_image_replacement(tmp_path):
    old = running('trading_system-mtcps-xau-calibrated-shadow')
    source = tmp_path / 'main.py'; source.write_text('old=1\n')
    target = '/app/strategies/btc_eth/shadow/main.py'
    old['HostConfig']['Binds'] = [str(source) + ':' + target + ':ro', '/config:/app/config.yaml:ro']
    row = dict(container=old['Name'][1:], source=str(source), target=target,
               old_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    proof = dict(hashes={target.removeprefix('/app/'): 'c' * 64})
    removals = release_mod.verified_source_removals(old, proof, dict(schema_version=1, mounts=[row]))
    body = build_container_body(old, 'sha256:' + 'c' * 64, SHA, source_removals=removals)
    assert body['HostConfig']['Binds'] == ['/config:/app/config.yaml:ro']
    source.write_text('tampered=1\n')
    with pytest.raises(ReleaseError, match='source'):
        release_mod.verified_source_removals(old, proof, dict(schema_version=1, mounts=[row]))


@pytest.mark.parametrize('apply', [False, True])
def test_grid_helper_uses_verified_image_private_env_and_frozen_stdin_source(tmp_path, monkeypatch, apply):
    release = release_mod.Release.__new__(release_mod.Release)
    release.sha, release.bundle, release.directory = SHA, tmp_path / 'bundle', tmp_path / 'proof'
    path = release.bundle / 'scripts/grid_release_guard.py'
    path.parent.mkdir(parents=True)
    path.write_text('REVIEWED_HELPER=True\n')
    release.source = {'scripts/grid_release_guard.py': hashlib.sha256(path.read_bytes()).hexdigest()}
    old = running('trading_system-grid-executor')
    old['HostConfig'].update(Binds=['/data:/app/data:rw'], Tmpfs=None)
    proof = dict(hashes={'strategies/grid/execution/main.py': 'c' * 64})
    create = Mock(return_value='9' * 64)
    monkeypatch.setattr(release_mod, 'create_container', create)
    command = Mock(return_value='GRID_RELEASE_RESULT={"ready_for_writer":false}\n')
    monkeypatch.setattr(release_mod, 'run', command)
    monkeypatch.setattr(release_mod, 'inspect', lambda id: dict(State=dict(Running=False, ExitCode=0)))
    result = release.grid_tool(old, 'sha256:' + 'c' * 64, proof, apply=apply, expected={'account':'grid'})
    body = create.call_args.args[1]
    assert body['Env'] == old['Config']['Env']
    assert body['HostConfig']['RestartPolicy']['Name'] == 'no'
    assert body['HostConfig']['ReadonlyRootfs'] is True and '/tmp' in body['HostConfig']['Tmpfs']
    assert body['HostConfig']['Binds'] == ['/data:/app/data:' + ('rw' if apply else 'ro')]
    assert body['Entrypoint'] == ['python'] and body['Cmd'] == ['-B','-']
    assert all(not value['Aliases'] for value in body['NetworkingConfig']['EndpointsConfig'].values())
    args = command.call_args.args
    assert args == ('docker','start','-ai','9' * 64)
    payload = command.call_args.kwargs['input_text']
    assert 'REVIEWED_HELPER' in payload and 'SECRET=not-printed' not in payload
    assert result['helper_sha256'] == release.source['scripts/grid_release_guard.py']


def test_helper_source_drift_is_rejected_before_container_creation(tmp_path, monkeypatch):
    release = release_mod.Release.__new__(release_mod.Release)
    release.sha, release.bundle = SHA, tmp_path
    path = tmp_path / 'scripts/grid_release_guard.py'; path.parent.mkdir()
    path.write_text('changed=True\n')
    release.source = {'scripts/grid_release_guard.py': '0' * 64}
    create = Mock(); monkeypatch.setattr(release_mod, 'create_container', create)
    with pytest.raises(ReleaseError, match='reviewed source'):
        release.grid_tool(running(), 'sha256:' + 'c' * 64, dict(hashes={}))
    create.assert_not_called()


def test_grid_probe_preserves_helper_observation_start_time(grid_activation):
    release, old, _, _, _ = grid_activation
    release.grid_tool.return_value = dict(observed_at=NOW-60, source=SHA, read_only=True)
    release.grid_probe('trading_system-grid-executor')
    proof = json.loads((release.directory / 'trading_system-grid-executor-grid-live.json').read_text())
    assert proof['observed_at'] == NOW-60 and proof['old_id'] == old['Id']


def test_legacy_unset_oom_flag_matches_docker_create_without_relaxing_validation():
    old = running('trading_system-grid-executor')
    old['HostConfig']['OomKillDisable'] = None
    body = build_container_body(old, 'sha256:' + 'c' * 64, SHA)
    new = dict(Config={key:value for key,value in body.items() if key not in ('HostConfig','NetworkingConfig')},
               HostConfig=copy.deepcopy(body['HostConfig']),
               NetworkSettings=dict(Networks=copy.deepcopy(body['NetworkingConfig']['EndpointsConfig'])))
    # Actual first staging evidence: Docker 29.6 / API 1.44 materializes the
    # predecessor's legacy null field as false; every other field matched.
    new['HostConfig']['OomKillDisable'] = False
    release_mod.validate_staged_runtime(new, body)
    assert body['HostConfig']['OomKillDisable'] is False
    assert old['HostConfig']['OomKillDisable'] is None


@pytest.mark.parametrize('disabled', [True, False])
def test_explicit_oom_policy_is_preserved_and_opposite_runtime_flag_rejected(disabled):
    old = running('trading_system-grid-executor')
    old['HostConfig']['OomKillDisable'] = disabled
    body = build_container_body(old, 'sha256:' + 'c' * 64, SHA)
    assert body['HostConfig']['OomKillDisable'] is disabled
    actual_host = copy.deepcopy(body['HostConfig'])
    actual_host['OomKillDisable'] = not disabled
    new = dict(Config={key:value for key,value in body.items() if key not in ('HostConfig','NetworkingConfig')},
               HostConfig=actual_host,
               NetworkSettings=dict(Networks=copy.deepcopy(body['NetworkingConfig']['EndpointsConfig'])))
    with pytest.raises(ReleaseError, match='configuration'):
        release_mod.validate_staged_runtime(new, body)


@pytest.mark.parametrize('invalid', [0, 1, 'false'])
def test_non_boolean_oom_policy_cannot_be_mistaken_for_false_or_true(invalid):
    old = running('trading_system-grid-executor')
    old['HostConfig']['OomKillDisable'] = invalid
    with pytest.raises(ReleaseError, match='OOM policy type'):
        build_container_body(old, 'sha256:' + 'c' * 64, SHA)


def test_missing_oom_flag_is_not_invented():
    old = running('trading_system-grid-executor')
    assert 'OomKillDisable' not in old['HostConfig']
    body = build_container_body(old, 'sha256:' + 'c' * 64, SHA)
    assert 'OomKillDisable' not in body['HostConfig']
