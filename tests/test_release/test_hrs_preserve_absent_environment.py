"""A reviewed HRS absence pin must never become a general drift exception."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from tests.test_release.test_hrs_optimization_deploy import fake_rollout, module

KEY = 'AI_TUNER_WRITE_TOKEN'
FIELD = 'preserve_absent_environment_keys'


@pytest.mark.parametrize('manifest', [{}, {FIELD: []}])
def test_old_manifest_has_no_absence_preservation(manifest):
    assert module().preserve_absent_environment_keys(manifest) == ()


def test_only_reviewed_absence_key_is_accepted():
    assert module().preserve_absent_environment_keys({FIELD: [KEY]}) == (KEY,)


@pytest.mark.parametrize('value', [None, KEY, {}, True, [KEY, KEY], ['UNKNOWN'],
    ['HRS_RELEASE_SHA'], ['HRS_EQUITY_GATE_ENABLED'], [KEY, 'DATABASE_PASSWORD'], [None], [[]]])
def test_manifest_cannot_request_other_keys_duplicates_or_non_lists(value):
    with pytest.raises(ValueError, match='preserve_absent_environment_keys_invalid'):
        module().preserve_absent_environment_keys({FIELD: value})


def rollout(tmp_path, monkeypatch, *, requested=True, actual=None, image=None, extra=None, fail_hrs=False):
    mod, containers, calls, folder, pin = fake_rollout(tmp_path, monkeypatch, fail_hrs=fail_hrs)
    manifest_path = tmp_path/'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if requested:
        manifest[FIELD] = [KEY]
    manifest_path.write_text(json.dumps(manifest))
    if actual is not None:
        containers['trading_system-hrs']['Config']['Env'].append(KEY+'='+actual)
    original = mod.run

    def run(args, **kwargs):
        args = list(map(str, args))
        rendered = original(args, **kwargs)
        if 'config' in args:
            data = json.loads(rendered)
            environment = data['services']['hrs-strategy']['environment']
            environment.update({KEY: 'synthetic-global-token', **(extra or {})})
            overlay_path = Path(args[max(i for i, arg in enumerate(args) if arg == '-f')+1])
            environment.update(json.loads(overlay_path.read_text())['services']['hrs-strategy']['environment'])
            return json.dumps(data)
        if args[:3] == ['docker', 'image', 'inspect'] and image is not None:
            data = json.loads(rendered)
            data[0]['Config']['Env'].append(KEY+'='+image)
            return json.dumps(data)
        return rendered

    monkeypatch.setattr(mod, 'run', run)
    return mod, containers, calls, folder, pin


def assert_no_activation(calls, folder, tmp_path):
    assert not any('up' in call or call[:3] == ['docker', 'exec', '-i'] for call in calls)
    assert not (folder/'state.json').exists()
    assert not (tmp_path/'.secrets/hrs-acceptance.env').exists()


def test_candidate_and_rollback_mask_new_global_env_file_key_and_audit_only_key_names(tmp_path, monkeypatch, capsys):
    mod, containers, calls, folder, pin = rollout(tmp_path, monkeypatch)
    old_pin = json.loads(pin.read_text())
    mod.main()
    for name in ('images.json', 'rollback-images.json', 'checked-images.json'):
        overlay = json.loads((folder/name).read_text())
        assert KEY in overlay['services']['hrs-strategy']['environment']
        assert overlay['services']['hrs-strategy']['environment'][KEY] is None
        assert KEY not in overlay['services']['market-cap-service'].get('environment', {})
    state = json.loads((folder/'state.json').read_text())
    assert state['status'] == 'complete'
    assert state[FIELD] == [KEY]
    assert 'synthetic-global-token' not in json.dumps(state)+capsys.readouterr().out
    assert json.loads(Path(old_pin['overlay']).read_text())['services']['hrs-strategy'] == {'image': 'old-hrs'}


@pytest.mark.parametrize('actual', ['', 'synthetic-actual-token'])
def test_present_actual_key_even_empty_is_rejected_before_preparation(tmp_path, monkeypatch, actual):
    mod, containers, calls, folder, pin = rollout(tmp_path, monkeypatch, actual=actual)
    with pytest.raises(RuntimeError, match='preserved_environment_key_present_in_actual:'+KEY):
        mod.main()
    assert_no_activation(calls, folder, tmp_path)
    assert not (folder/'rollback-plan.json').exists()


@pytest.mark.parametrize('image', ['', 'synthetic-image-token'])
def test_candidate_image_key_even_empty_is_rejected_before_permission_or_state_changes(tmp_path, monkeypatch, image):
    mod, containers, calls, folder, pin = rollout(tmp_path, monkeypatch, image=image)
    with pytest.raises(RuntimeError, match='preserved_environment_key_present_in_candidate_image:'+KEY):
        mod.main()
    assert_no_activation(calls, folder, tmp_path)


def test_other_environment_drift_still_fails(tmp_path, monkeypatch):
    mod, containers, calls, folder, pin = rollout(tmp_path, monkeypatch, extra={'UNRELATED_SETTING': 'new'})
    with pytest.raises(RuntimeError, match='existing_environment_drift_keys:UNRELATED_SETTING$'):
        mod.main()
    assert_no_activation(calls, folder, tmp_path)


def test_old_manifest_still_rejects_new_global_key(tmp_path, monkeypatch):
    mod, containers, calls, folder, pin = rollout(tmp_path, monkeypatch, requested=False)
    with pytest.raises(RuntimeError, match='existing_environment_drift_keys:'+KEY):
        mod.main()
    assert_no_activation(calls, folder, tmp_path)
    for name in ('images.json', 'rollback-images.json'):
        assert KEY not in json.loads((folder/name).read_text())['services']['hrs-strategy']['environment']


@pytest.mark.parametrize('rollback_value', ['missing', '', 'synthetic-old-value'])
def test_existing_rollback_without_exact_absence_pin_requires_fresh_release(tmp_path, monkeypatch, rollback_value):
    mod, containers, calls, folder, pin = rollout(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, 'argv', [arg for arg in sys.argv if arg != '--apply'])
    mod.main()
    overlay_path = folder/'rollback-images.json'
    overlay = json.loads(overlay_path.read_text())
    env = overlay['services']['hrs-strategy']['environment']
    if rollback_value == 'missing':
        env.pop(KEY, None)
    else:
        env[KEY] = rollback_value
    overlay_path.write_text(json.dumps(overlay))
    before = {name: (folder/name).read_bytes() for name in ('rollback-images.json', 'rollback-plan.json')}
    with pytest.raises(RuntimeError, match='preserved_environment_rollback_requires_fresh_release'):
        mod.main()
    assert all((folder/name).read_bytes() == contents for name, contents in before.items())


def test_prepared_absence_pin_is_reusable_and_reported(tmp_path, monkeypatch, capsys):
    mod, containers, calls, folder, pin = rollout(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, 'argv', [arg for arg in sys.argv if arg != '--apply'])
    mod.main()
    before = (folder/'rollback-images.json').read_bytes()
    mod.main()
    assert (folder/'rollback-images.json').read_bytes() == before
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all(record[FIELD] == [KEY] for record in records)


def test_overlay_absence_pin_does_not_mutate_original_or_unrelated_services():
    mod = module()
    previous = {'services': {'hrs-strategy': {'environment': {KEY: 'synthetic-old-overlay'}},
                             'other': {'environment': {KEY: 'synthetic-other'}}}}
    before = deepcopy(previous)
    candidate = mod.release_overlay(previous, 'hrs', 'hub', [], 'a'*40, (KEY,))
    assert candidate['services']['hrs-strategy']['environment'][KEY] is None
    assert candidate['services']['other'] == previous['services']['other']
    assert previous == before


def test_invalid_manifest_is_rejected_before_docker_calls(tmp_path, monkeypatch):
    mod, containers, calls, folder, pin = rollout(tmp_path, monkeypatch)
    manifest_path = tmp_path/'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest[FIELD] = ['HRS_EQUITY_GATE_ENABLED']
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='preserve_absent_environment_keys_invalid'):
        mod.main()
    assert calls == []


def test_missing_historical_rollback_overlay_requires_fresh_release_without_rewriting_plan(tmp_path, monkeypatch):
    mod, containers, calls, folder, pin = rollout(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, 'argv', [arg for arg in sys.argv if arg != '--apply'])
    mod.main()
    before = (folder/'rollback-plan.json').read_bytes()
    (folder/'rollback-images.json').unlink()
    with pytest.raises(RuntimeError, match='preserved_environment_rollback_requires_fresh_release'):
        mod.main()
    assert (folder/'rollback-plan.json').read_bytes() == before
    assert not (folder/'rollback-images.json').exists()


def test_failed_candidate_restores_using_the_pinned_rollback_overlay(tmp_path, monkeypatch):
    mod, containers, calls, folder, pin = rollout(tmp_path, monkeypatch, fail_hrs=True)
    with pytest.raises(RuntimeError, match='rollout_failed_rollback_complete'):
        mod.main()
    rollback_calls = [call for call in calls if 'up' in call and str(folder/'rollback-images.json') in call]
    assert rollback_calls
    assert json.loads((folder/'rollback-images.json').read_text())['services']['hrs-strategy']['environment'][KEY] is None
    state = json.loads((folder/'state.json').read_text())
    assert state[FIELD] == [KEY] and state['rollback']['status'] == 'complete'
    assert containers['trading_system-hrs']['Config']['Image'] == 'old-hrs'
    assert containers['trading_system-market-cap']['Config']['Image'] == 'old-hub'


def test_rendered_key_resolved_from_host_instead_of_absence_is_still_rejected(tmp_path, monkeypatch):
    mod, containers, calls, folder, pin = rollout(tmp_path, monkeypatch)
    original = mod.run

    def run(args, **kwargs):
        result = original(args, **kwargs)
        if 'config' in args:
            rendered = json.loads(result)
            rendered['services']['hrs-strategy']['environment'][KEY] = 'synthetic-host-token'
            return json.dumps(rendered)
        return result

    monkeypatch.setattr(mod, 'run', run)
    with pytest.raises(RuntimeError, match='existing_environment_drift_keys:'+KEY):
        mod.main()
    assert_no_activation(calls, folder, tmp_path)
