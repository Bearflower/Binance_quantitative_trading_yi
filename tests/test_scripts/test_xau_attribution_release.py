"""Fail-closed scope guards for the XAU-only release executor."""
import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("xau_release", ROOT / "deployment/xau-attribution/activate.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def test_overlay_replaces_only_calibrated_main_and_preserves_mount_options():
    before = {"image": "old", "environment": {"SECRET": "kept"}, "volumes": [
        {"type": "bind", "source": release.CALIBRATED_MAIN, "target": release.MAIN_TARGET, "read_only": True},
        {"type": "bind", "source": "/root/trading_system/logs", "target": "/app/logs"}],
        "mem_limit": "268435456"}
    after = release.changed_service(before, "mtcps-xau-calibrated-shadow", "new", "/release/main.py")
    assert before["image"] == "old"
    assert after["volumes"][0]["source"] == "/release/main.py"
    assert after["volumes"][0]["read_only"] is True
    assert after["volumes"][1] == before["volumes"][1]
    assert after["environment"] == {"SECRET": "kept", "SHADOW_SKIP_INITIAL_COLLECTION": "1"}
    assert after["mem_limit"] == before["mem_limit"]
    release.validate_delta(before, after, "mtcps-xau-calibrated-shadow", "new", "/release/main.py")
    after["mem_limit"] = "0"
    with pytest.raises(ValueError, match="configuration drift"):
        release.validate_delta(before, after, "mtcps-xau-calibrated-shadow", "new", "/release/main.py")


def test_unexpected_bind_mount_is_rejected():
    before = {"image": "old", "volumes": [{"type": "bind", "source": "/unknown/main.py", "target": release.MAIN_TARGET}]}
    with pytest.raises(ValueError, match="main bind"):
        release.changed_service(before, "mtcps-xau-calibrated-shadow", "new", "/release/main.py")


def test_funding_overlay_does_not_inject_shadow_environment():
    before = {"image": "old", "environment": {"A": "b"}}
    assert release.changed_service(before, "ai-tuner-funding-schedule", "new", "/release/main.py") == {
        "image": "new", "environment": {"A": "b"}}


def test_compose_paths_must_be_inside_production_root():
    valid = "/root/trading_system/docker-compose.yml,/root/trading_system/.releases/test/overlay.json"
    assert len(release.compose_paths(valid)) == 2
    for bad in ("", "/tmp/compose.yml", "docker-compose.yml", "/root/trading_system/../bad.yml"):
        with pytest.raises(ValueError):
            release.compose_paths(bad)


def test_excluded_container_change_is_rejected():
    before = {"live": {"id": "a", "image": "b", "running": True, "restarts": 0}}
    release.verify_excluded(before, copy.deepcopy(before))
    for key, value in (("id", "different"), ("image", "different"), ("running", False), ("restarts", 1)):
        after = copy.deepcopy(before)
        after["live"][key] = value
        with pytest.raises(ValueError, match="excluded"):
            release.verify_excluded(before, after)


def test_manifests_require_exact_scope_and_digest_pins():
    hashes = {"git_sha": "a" * 40, "deploy_id": "00000101", "files": {
        group: {path: "b" * 64 for path in paths} for group, paths in release.FILES.items()}}
    digests = {group: f"ghcr.io/bearflower/trading-xau-attribution-{group}@sha256:" + "c" * 64 for group in release.FILES}
    release.validate_manifests(hashes, digests)
    digests["funding"] = "ghcr.io/bearflower/trading-ai-tuner:latest"
    with pytest.raises(ValueError):
        release.validate_manifests(hashes, digests)
    digests["funding"] = "ghcr.io/bearflower/trading-xau-attribution-funding@sha256:" + "c" * 64
    hashes["files"]["funding"]["shared/database.py"] = "d" * 64
    with pytest.raises(ValueError):
        release.validate_manifests(hashes, digests)


def test_runtime_comparison_preserves_credentials_resources_and_mounts():
    before = {'Config': {'Image': 'old', 'Hostname': 'oldid', 'Env': ['TOKEN=secret'],
                         'Labels': {'com.docker.compose.config-hash': 'old', 'custom': 'same'}},
              'HostConfig': {'Memory': 123, 'Binds': [release.CALIBRATED_MAIN + ':' + release.MAIN_TARGET + ':ro']},
              'Mounts': [{'Type': 'bind', 'Source': release.CALIBRATED_MAIN, 'Destination': release.MAIN_TARGET, 'RW': False}],
              'NetworkSettings': {'Networks': {'trading': {'IPAMConfig': None}}}}
    after = copy.deepcopy(before)
    after['Config']['Image'] = 'new'
    after['Config']['Hostname'] = 'newid'
    after['Config']['Env'].append('SHADOW_SKIP_INITIAL_COLLECTION=1')
    after['Config']['Labels']['com.docker.compose.config-hash'] = 'new'
    after['HostConfig']['Binds'][0] = '/release/main.py:' + release.MAIN_TARGET + ':ro'
    after['Mounts'][0]['Source'] = '/release/main.py'
    release.validate_runtime(before, after, 'mtcps-xau-calibrated-shadow', '/release/main.py')
    after['HostConfig']['Memory'] = 0
    with pytest.raises(ValueError, match='runtime configuration'):
        release.validate_runtime(before, after, 'mtcps-xau-calibrated-shadow', '/release/main.py')


def test_release_workflow_and_dockerfiles_have_narrow_scope():
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/xau-attribution-release.yml').read_text())
    assert workflow['jobs']['release']['if'] == "github.ref == 'refs/heads/main'"
    for group, paths in release.FILES.items():
        lines = (ROOT / f'deployment/xau-attribution/Dockerfile.{group}').read_text().splitlines()
        assert f'FROM {release.BASES[group]}' in lines
        assert {line.split()[1] for line in lines if line.startswith('COPY ')} == set(paths)


def test_frozen_compose_escapes_literal_dollars_recursively():
    raw = {'environment': {'TOKEN': '${literal}$cash'}, 'command': ['sh', '-c', 'echo $VALUE'], 'number': 1}
    escaped = release.escape_compose(raw)
    assert escaped == {'environment': {'TOKEN': '$${literal}$$cash'}, 'command': ['sh', '-c', 'echo $$VALUE'], 'number': 1}
    assert raw['environment']['TOKEN'] == '${literal}$cash'


@pytest.mark.parametrize('rollback_failure', [False, True])
def test_second_recreate_failure_rolls_back_attempted_targets_only(monkeypatch, tmp_path, rollback_failure):
    plans = [(service, name, group, tmp_path / f'{service}.old', tmp_path / f'{service}.new', 'old', 'new')
             for service, (name, group) in release.TARGETS.items()]
    calls = []
    def fake_run(*args, **kwargs):
        path = args[args.index('-f') + 1]
        calls.append((path, args[-1]))
        if path == str(plans[1][4]):
            raise RuntimeError('second recreate failed')
        if rollback_failure and path == str(plans[1][3]):
            raise RuntimeError('rollback failed')
        return ''
    monkeypatch.setattr(release, 'run', fake_run)
    monkeypatch.setattr(release, 'wait_health', lambda *args: {})
    monkeypatch.setattr(release, 'validate_runtime', lambda *args, **kwargs: None)
    monkeypatch.setattr(release, 'verify_files', lambda *args: None)
    monkeypatch.setattr(release, 'verify_version', lambda *args: None)
    monkeypatch.setattr(release, 'snapshot', lambda: {})
    originals = {plan[1]: {} for plan in plans}
    hashes = {'files': {'shadow': {}, 'funding': {}}}
    expected = 'rollback requires attention' if rollback_failure else 'second recreate failed'
    with pytest.raises(RuntimeError, match=expected):
        release.execute_plans(plans, tmp_path, hashes, tmp_path / 'main.py', {}, originals, originals)
    assert calls == [(str(plans[0][4]), plans[0][0]), (str(plans[1][4]), plans[1][0]),
                     (str(plans[1][3]), plans[1][0]), (str(plans[0][3]), plans[0][0])]
    assert (tmp_path / 'rollback.json').exists()


def test_live_validation_accepts_deploy_resource_limits_and_rejects_drift():
    config = {'deploy': {'resources': {'limits': {'memory': '268435456', 'cpus': '0.25'}}}}
    image = {'Config': {'Env': [], 'Cmd': None, 'Entrypoint': None, 'WorkingDir': None, 'User': None}}
    live = {'Config': copy.deepcopy(image['Config']), 'Mounts': [],
            'HostConfig': {'Memory': 268435456, 'NanoCpus': 250000000, 'RestartPolicy': {'Name': 'no'}}}
    release.validate_live(config, live, image)
    live['HostConfig']['Memory'] += 1
    with pytest.raises(ValueError, match='memory'):
        release.validate_live(config, live, image)


def test_baseline_pins_exact_live_environment_without_current_env_additions():
    current = {'image': 'old', 'environment': {'CHANGED': 'new', 'ADDED': 'new'}, 'command': ['python']}
    live = {'Config': {'Env': ['CHANGED=original', 'INHERITED=original']}}
    pinned = release.pin_live_environment(current, live)
    assert pinned['environment'] == {'CHANGED': 'original', 'INHERITED': 'original'}
    assert current['environment']['CHANGED'] == 'new'
    assert pinned['command'] == ['python']


def test_single_service_snapshot_omits_unused_named_volume_definitions():
    original = {'services': {'one': {'volumes': [{'type': 'bind', 'source': '/logs', 'target': '/logs'}], 'depends_on': {'db': {}}}, 'db': {}},
                'volumes': {'db-data': {}}, 'networks': {'trading': {}}}
    frozen = release.single_service_config(original, 'one', original['services']['one'])
    assert set(frozen['services']) == {'one'}
    assert 'depends_on' not in frozen['services']['one']
    assert 'volumes' not in frozen
    assert frozen['networks'] == original['networks']
