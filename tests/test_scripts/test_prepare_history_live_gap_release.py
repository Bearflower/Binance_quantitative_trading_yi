import hashlib
import json
from pathlib import Path

import pytest

from scripts.prepare_history_live_gap_release import ORDER, prepare


ROOT = Path(__file__).resolve().parents[2]


def _artifacts(tmp_path, source):
    plan = json.loads((ROOT / 'deploy/history-tolerance/bases.json').read_text())
    artifacts = tmp_path / 'artifacts'
    for group in plan['groups']:
        folder = artifacts / f'history-tolerance-{group["name"]}'
        folder.mkdir(parents=True)
        receipt = dict(group, source=source, deploy_id='00000101',
                       digest_ref=f'{group["repository"]}@sha256:{"a" * 64}',
                       hashes={path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                               for path in group['files']})
        (folder / 'source.json').write_text(json.dumps(receipt))
    return artifacts


def test_release_manifest_binds_exact_consumers_and_bases(tmp_path):
    source = 'b' * 40
    output = prepare(ROOT, _artifacts(tmp_path, source), source, tmp_path / 'release')
    plan = json.loads((output / 'plan.json').read_text())
    bases = {group['name']: group['base'].split('@')[1] for group in
             json.loads((ROOT / 'deploy/history-tolerance/bases.json').read_text())['groups']}
    assert [row['name'] for row in plan['services']] == list(ORDER)
    assert all(row['image'] == bases[row['group']] for row in plan['services'])
    assert len({row['group'] for row in plan['services']}) == 2
    assert (output / 'activate.py').read_bytes() == (
        ROOT / 'deployment/history-live-gap/activate.py').read_bytes()


def test_release_manifest_rejects_other_source(tmp_path):
    artifacts = _artifacts(tmp_path, 'b' * 40)
    with pytest.raises(ValueError, match='unreviewed_artifact'):
        prepare(ROOT, artifacts, 'c' * 40, tmp_path / 'release')
