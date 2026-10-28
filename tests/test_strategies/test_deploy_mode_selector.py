from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MARKER = ".github/deploy-modes/xau-calibrated-shadow-first-create"


def test_added_marker_selects_targeted_mode():
    from scripts.select_deploy_mode import select_deploy_mode

    assert select_deploy_mode("push", "abc", [("A", MARKER)]) == (
        "calibrated-first-create"
    )


@pytest.mark.parametrize("status", ["M", "D"])
def test_modified_or_deleted_marker_does_not_select_targeted_mode(status):
    from scripts.select_deploy_mode import select_deploy_mode

    assert select_deploy_mode("push", "abc", [(status, MARKER)]) == "full"


def test_workflow_dispatch_remains_full():
    from scripts.select_deploy_mode import select_deploy_mode

    assert select_deploy_mode("workflow_dispatch", "", []) == "full"


@pytest.mark.parametrize("base", ["", "0" * 40])
def test_push_without_unambiguous_base_fails_closed(base):
    from scripts.select_deploy_mode import DeployModeError, select_deploy_mode

    with pytest.raises(DeployModeError):
        select_deploy_mode("push", base, [])


def test_duplicate_marker_statuses_fail_closed():
    from scripts.select_deploy_mode import DeployModeError, select_deploy_mode

    with pytest.raises(DeployModeError):
        select_deploy_mode("push", "abc", [("A", MARKER), ("M", MARKER)])


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=repo, text=True, stderr=subprocess.STDOUT
    ).strip()


def test_cli_uses_whole_multi_commit_push_range(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    marker = repo / MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("first rollout\n", encoding="utf-8")
    _git(repo, "add", MARKER)
    _git(repo, "commit", "-m", "add marker")
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-m", "later commit")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/select_deploy_mode.py"),
            "--event-name",
            "push",
            "--base",
            base,
            "--head",
            "HEAD",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "calibrated-first-create"


def test_cli_rejects_unreachable_push_base(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/select_deploy_mode.py"),
            "--event-name",
            "push",
            "--base",
            "f" * 40,
            "--head",
            "HEAD",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert result.stdout.strip() != "full"
