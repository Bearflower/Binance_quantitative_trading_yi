"""Fail-closed scope guards for the XAU-only release executor."""
import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("xau_release", ROOT / "deployment/xau-attribution/activate.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)










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
    hashes['audit_files'] = {path: 'd' * 64 for path in (
        'scripts/database_permissions.py', 'database/postgres/permissions.json')}
    digests = {group: f"ghcr.io/bearflower/trading-xau-attribution-{group}@sha256:" + "c" * 64 for group in release.FILES}
    release.validate_manifests(hashes, digests)
    digests["shadow"] = "ghcr.io/bearflower/trading-ai-tuner:latest"
    with pytest.raises(ValueError):
        release.validate_manifests(hashes, digests)
    digests["shadow"] = "ghcr.io/bearflower/trading-xau-attribution-shadow@sha256:" + "c" * 64
    hashes["files"]["shadow"]["shared/database.py"] = "d" * 64
    with pytest.raises(ValueError):
        release.validate_manifests(hashes, digests)




def test_release_workflow_and_dockerfiles_have_narrow_scope():
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/xau-attribution-release.yml').read_text())
    assert workflow['jobs']['release']['if'] == "github.ref == 'refs/heads/main'"
    gate = next(step['run'] for step in workflow['jobs']['release']['steps']
                if step.get('name') == 'Focused regression gate')
    assert 'tests/test_ai_tuner_mvp/test_funding_schedule*.py' in gate
    assert 'tests/test_strategies/test_xau_entry_intent.py' in gate
    assert set(release.FILES) == {'shadow'}
    assert set(release.FILES['shadow']) == {
        'strategies/btc_eth/shadow/main.py', 'strategies/btc_eth/shadow/collector.py', 'VERSION'}
    assert 'tests/test_strategies/test_xau_version_provenance.py' in gate
    for group, paths in release.FILES.items():
        lines = (ROOT / f'deployment/xau-attribution/Dockerfile.{group}').read_text().splitlines()
        assert f'FROM {release.BASES[group]}' in lines
        assert {line.split()[1] for line in lines if line.startswith('COPY ')} == set(paths)














def test_permission_audit_uses_verified_current_release_bundle(monkeypatch, tmp_path):
    import hashlib
    paths = ['scripts/database_permissions.py', 'database/postgres/permissions.json']
    expected = {}
    for path in paths:
        dest = tmp_path / Path(path).name
        dest.write_text(path)
        expected[path] = hashlib.sha256(dest.read_bytes()).hexdigest()
    calls = []
    monkeypatch.setattr(release, 'run', lambda *args, **kwargs: calls.append(args) or '{"ok":true}')
    assert hasattr(release, 'audit_permissions'), '必须校验并执行本次发布包中的审计资源'
    release.audit_permissions(tmp_path, expected)
    assert calls == [('python3', str(tmp_path / 'database_permissions.py'), 'audit',
                      '--manifest', str(tmp_path / 'permissions.json'))]
    (tmp_path / 'permissions.json').write_text('tampered')
    with pytest.raises(ValueError, match='audit resource hash'):
        release.audit_permissions(tmp_path, expected)
    assert len(calls) == 1


def test_missing_audit_resource_manifest_is_rejected():
    hashes = {'git_sha': 'a' * 40, 'deploy_id': '00000101', 'files': {
        group: {path: 'b' * 64 for path in paths} for group, paths in release.FILES.items()}}
    digests = {'shadow': 'ghcr.io/bearflower/trading-xau-attribution-shadow@sha256:' + 'c' * 64}
    with pytest.raises(ValueError, match='audit resource scope'):
        release.validate_manifests(hashes, digests)
