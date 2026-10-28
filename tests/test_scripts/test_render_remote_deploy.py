from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "scripts" / "remote_deploy_runtime.sh"


def _valid_values():
    return {
        "mode": "calibrated-first-create",
        "commit_sha": "a" * 40,
        "deploy_id": "00007801",
    }


def test_render_valid_runtime_is_complete_private_and_bash_valid(tmp_path):
    from scripts.render_remote_deploy import render_remote_deploy

    output = tmp_path / "deploy.sh"
    render_remote_deploy(source=RUNTIME, output=output, **_valid_values())

    rendered = output.read_text(encoding="utf-8")
    assert "__DEPLOY_" not in rendered
    assert "__COMMIT_SHA__" not in rendered
    assert 'DEPLOY_MODE="calibrated-first-create"' in rendered
    assert f'COMMIT_SHA="{"a" * 40}"' in rendered
    assert 'DEPLOY_ID="00007801"' in rendered
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert subprocess.run(["bash", "-n", str(output)]).returncode == 0


@pytest.mark.parametrize(
    "source_text",
    [
        'DEPLOY_MODE="__DEPLOY_MODE__"\nCOMMIT_SHA="__COMMIT_SHA__"\n',
        (
            'DEPLOY_MODE="__DEPLOY_MODE__"\n'
            'COMMIT_SHA="__COMMIT_SHA__"\n'
            'DEPLOY_ID="__DEPLOY_ID__"\n'
            'echo "__DEPLOY_ID__"\n'
        ),
    ],
)
def test_render_rejects_missing_or_duplicate_placeholder(tmp_path, source_text):
    from scripts.render_remote_deploy import RenderError, render_remote_deploy

    source = tmp_path / "source.sh"
    source.write_text(source_text, encoding="utf-8")
    with pytest.raises(RenderError, match="placeholder"):
        render_remote_deploy(source=source, output=tmp_path / "out.sh", **_valid_values())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "targeted|sed"),
        ("mode", "full&echo-pwned"),
        ("commit_sha", "a" * 39 + "|"),
        ("commit_sha", "$(touch /tmp/nope)"),
        ("deploy_id", "0000780&"),
        ("deploy_id", "deadbeef"),
    ],
)
def test_render_rejects_invalid_or_sed_metacharacter_input(tmp_path, field, value):
    from scripts.render_remote_deploy import RenderError, render_remote_deploy

    values = _valid_values()
    values[field] = value
    output = tmp_path / "deploy.sh"
    output.write_text("previous-safe-output", encoding="utf-8")

    with pytest.raises(RenderError):
        render_remote_deploy(source=RUNTIME, output=output, **values)

    assert output.read_text(encoding="utf-8") == "previous-safe-output"
