"""四消费者受限发布的权限门禁、最终复验和精确回退。"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NAMES = ['ai-tuner-snapshot', 'ai-tuner', 'trading_system-hrs-acceptance', 'trading_system-hrs']


class Runtime:
    def __init__(self, folder, module):
        self.module = module
        self.rows = {}
        self.calls = []
        self.audit_ok = True
        self.late_exit = False
        for i, name in enumerate(NAMES):
            for kind in ('old', 'new'):
                cid = f'{kind}{i}'
                self.rows[cid] = dict(Id=cid, Name='/' + name + ('' if kind == 'old' else '-candidate'),
                    Image='image', Config=dict(Cmd=['python', 'main.py'], Env=[]),
                    HostConfig=dict(RestartPolicy=dict(Name='unless-stopped' if kind == 'old' else 'no', MaximumRetryCount=0)),
                    Mounts=[], NetworkSettings=dict(Networks={}), RestartCount=0,
                    State=dict(Running=kind == 'old', Status='running' if kind == 'old' else 'created',
                               Health=dict(Status='healthy'), StartedAt='now'))
            (folder / (name + '-before.json')).write_text(json.dumps(self.rows[f'old{i}']))
            (folder / (name + '-ready.json')).write_text(json.dumps(dict(id=f'new{i}', old_id=f'old{i}', image='image')))
        (folder / 'protected-pre-release.json').write_text('{}')

    def inspect(self, name):
        return copy.deepcopy(self.rows[name] if name in self.rows else
            next(row for row in self.rows.values() if row['Name'] == '/' + name))

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if 'audit' in args:
            if not self.audit_ok:
                raise RuntimeError('permission audit rejected')
            return json.dumps(dict(ok=True, mode='audit', issues=[], manifest_sha256=
                self.module.PLAN['audit_files']['database/postgres/permissions.json']))
        if args[:3] == ('docker', 'ps', '-a'):
            return '\n'.join(row['Name'][1:] for row in self.rows.values())
        if args[1] == 'update':
            self.rows[args[-1]]['HostConfig']['RestartPolicy']['Name'] = args[2].split('=', 1)[1]
        elif args[1] == 'stop':
            self.rows[args[-1]]['State'].update(Running=False, Status='exited')
        elif args[1] == 'rename':
            self.rows[args[2]]['Name'] = '/' + args[3]
        elif args[1] == 'start':
            self.rows[args[-1]]['State'].update(Running=True, Status='running')
            if args[-1] == 'new3' and self.late_exit:
                self.rows['new0']['State'].update(Running=False, Status='exited')
        return ''


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    audit_files = {}
    for path in ('scripts/database_permissions.py', 'database/postgres/permissions.json'):
        data = (ROOT / path).read_bytes()
        (tmp_path / Path(path).name).write_bytes(data)
        audit_files[path] = hashlib.sha256(data).hexdigest()
    plan = dict(source='a' * 40, deploy_id='00000101', audit_files=audit_files,
        services=[dict(name=name, service=name, next_cmd=['python', 'main.py'], hashes={}) for name in NAMES])
    (tmp_path / 'plan.json').write_text(json.dumps(plan))
    module_path = tmp_path / 'activate.py'
    module_path.write_bytes((ROOT / 'deployment/history-live-gap/activate.py').read_bytes())
    spec = importlib.util.spec_from_file_location('history_activation', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = Runtime(tmp_path, module)
    monkeypatch.setattr(module, 'inspect', fake.inspect)
    monkeypatch.setattr(module, 'run', fake.run)
    return fake


def test_audit_failure_prevents_any_container_mutation(runtime):
    runtime.audit_ok = False
    before = copy.deepcopy(runtime.rows)
    with pytest.raises(RuntimeError, match='permission audit rejected'):
        runtime.module.activate()
    assert runtime.rows == before
    assert not any(c[:2] in [('docker', 'update'), ('docker', 'stop'), ('docker', 'start')] for c in runtime.calls)


def test_tampered_contract_prevents_audit_and_container_mutation(runtime):
    (runtime.module.ROOT / 'permissions.json').write_text('{}')
    before = copy.deepcopy(runtime.rows)
    with pytest.raises(ValueError, match='audit resource'):
        runtime.module.activate()
    assert runtime.rows == before
    assert not any('audit' in c or c[:2] == ('docker', 'stop') for c in runtime.calls)


@pytest.mark.parametrize('field,value', [('ok', False), ('mode', 'apply'),
    ('issues', [{'code': 'unknown_object'}]), ('manifest_sha256', '0' * 64)])
def test_invalid_audit_report_prevents_container_mutation(runtime, monkeypatch, field, value):
    original_run = runtime.run
    def invalid(*args, **kwargs):
        result = original_run(*args, **kwargs)
        if 'audit' in args:
            report = json.loads(result)
            report[field] = value
            return json.dumps(report)
        return result
    monkeypatch.setattr(runtime.module, 'run', invalid)
    before = copy.deepcopy(runtime.rows)
    with pytest.raises(ValueError, match='permission audit'):
        runtime.module.activate()
    assert runtime.rows == before


def test_late_candidate_exit_cannot_return_success_and_restores_all_originals(runtime):
    runtime.late_exit = True
    with pytest.raises(RuntimeError, match='exited or restarted'):
        runtime.module.activate()
    for i, name in enumerate(NAMES):
        assert runtime.inspect(name)['Id'] == f'old{i}'
        assert runtime.inspect(name)['State']['Running']
        assert not runtime.rows[f'new{i}']['State']['Running']
        assert runtime.rows[f'new{i}']['HostConfig']['RestartPolicy']['Name'] == 'no'


def test_success_audits_before_first_stop_and_checks_every_candidate(runtime):
    runtime.module.activate()
    audit = next(i for i, c in enumerate(runtime.calls) if 'audit' in c)
    first_stop = next(i for i, c in enumerate(runtime.calls) if c[:2] == ('docker', 'stop'))
    assert audit < first_stop
    for i, name in enumerate(NAMES):
        assert runtime.inspect(name)['Id'] == f'new{i}'
        assert runtime.inspect(name)['State']['Running']
        assert not runtime.rows[f'old{i}']['State']['Running']


def test_late_environment_drift_is_not_laundered_by_final_inspect(runtime, monkeypatch):
    original_run = runtime.run
    def mutate(*args, **kwargs):
        result = original_run(*args, **kwargs)
        if args[:2] == ('docker', 'start') and args[-1] == 'new3':
            runtime.rows['new0']['Config']['Env'].append('UNREVIEWED=1')
        return result
    monkeypatch.setattr(runtime.module, 'run', mutate)
    with pytest.raises(AssertionError):
        runtime.module.activate()
    assert all(runtime.inspect(name)['Id'] == f'old{i}' for i, name in enumerate(NAMES))
