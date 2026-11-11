from pathlib import Path

import pytest

from scripts.render_remote_deploy import RenderError, _validate
from scripts.scoped_income_attribution_release import render_service, require_images, refresh_existing, timer_active
from scripts.select_deploy_mode import (
    DeployModeError, INCOME_ATTRIBUTION_BASE, INCOME_ATTRIBUTION_REFRESH_BASE,
    _validate_income_scope, select_deploy_mode,
)


MARKER = ".github/deploy-modes/income-attribution-20261110"
REFRESH_MARKER = ".github/deploy-modes/income-attribution-refresh-20261111"


def test_income_marker_selects_scoped_mode():
    assert select_deploy_mode("push", "a" * 40, [("A", MARKER)]) == "income-attribution"
    with pytest.raises(DeployModeError):
        select_deploy_mode("push", "a" * 40, [("M", MARKER)])


def test_refresh_marker_selects_scoped_mode_from_current_main():
    assert select_deploy_mode("push", "a" * 40, [("A", REFRESH_MARKER)]) == "income-attribution"
    _validate_income_scope(INCOME_ATTRIBUTION_REFRESH_BASE, [("A", REFRESH_MARKER),
                                      ("M", "scripts/scoped_income_attribution_release.py")])
    with pytest.raises(DeployModeError):
        _validate_income_scope(INCOME_ATTRIBUTION_REFRESH_BASE, [("A", REFRESH_MARKER),
                                          ("M", "strategies/hrs/main.py")])


def test_inactive_predecessor_is_a_valid_refresh_state(monkeypatch):
    import scripts.scoped_income_attribution_release as release_module
    class Result:
        returncode = 3
    monkeypatch.setattr(release_module.subprocess, "run", lambda *_, **__: Result())
    assert timer_active("income-attribution-20261110.timer") is False


def test_scoped_mode_requires_funding_keep():
    _validate("income-attribution", "a" * 40, "12345678", "keep")
    with pytest.raises(RenderError):
        _validate("income-attribution", "a" * 40, "12345678", "rotate")


def test_income_scope_rejects_strategy_code_and_wrong_base():
    changes = [("A", MARKER), ("M", "dashboard/frontend/js/main.js")]
    _validate_income_scope(INCOME_ATTRIBUTION_BASE, changes)
    with pytest.raises(DeployModeError):
        _validate_income_scope("a" * 40, changes)
    with pytest.raises(DeployModeError):
        _validate_income_scope(INCOME_ATTRIBUTION_BASE, changes + [("M", "strategies/hrs/main.py")])


def test_bundle_rejects_unpinned_image(tmp_path):
    (tmp_path / "image-digests.json").write_text(
        '{"hrs":"ghcr.io/bearflower/trading-hrs:latest","dashboard-api":"bad"}'
    )
    with pytest.raises(ValueError):
        require_images(tmp_path)


def test_systemd_template_accepts_digest_at_sign(tmp_path):
    template = "ExecStart=@RELEASE_DIR@/run\nEnvironment=IMAGE=@ATTRIBUTION_IMAGE@\n"
    image = "ghcr.io/bearflower/trading-hrs@sha256:" + "a" * 64
    rendered = render_service(template, tmp_path, image)
    assert image in rendered
    assert "@RELEASE_DIR@" not in rendered


def test_release_runner_is_repository_owned():
    root = Path(__file__).resolve().parents[2]
    assert (root / "scripts/run_income_attribution.sh").is_file()
    assert (root / "deployment/systemd/income-attribution.service").is_file()
    assert (root / "deployment/systemd/income-attribution.timer").is_file()


def test_refresh_replaces_existing_service_and_removes_hotfix_overlay(tmp_path, monkeypatch):
    import scripts.scoped_income_attribution_release as release_module
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    (systemd / "income-attribution.service").write_text("old service")
    (systemd / "income-attribution.timer").write_text("old timer")
    dropin = systemd / "income-attribution.service.d"
    dropin.mkdir()
    (dropin / "20261111-proof-stability.conf").write_text("old overlay")
    release = tmp_path / "release"
    release.mkdir()
    (release / "income-attribution.service").write_text("new service")
    (release / "income-attribution.timer").write_text("new timer")
    commands = []
    monkeypatch.setattr(release_module, "SYSTEMD_DIR", systemd)
    monkeypatch.setattr(release_module, "run", lambda *args, **_: commands.append(args) or "active")
    monkeypatch.setattr(release_module, "wait_inactive", lambda *_: None)
    monkeypatch.setattr(release_module, "container_ids", lambda: {"hrs": "same"})
    monkeypatch.setattr(release_module, "dashboard_ready", lambda: True)
    refresh_existing(release, {"hrs": "pinned-image", "dashboard-api": "unused"}, {"hrs": "same"})
    assert (systemd / "income-attribution.service").read_text() == "new service"
    assert not (dropin / "20261111-proof-stability.conf").exists()
    assert ("systemctl", "start", "income-attribution.service") in commands
    assert ("systemctl", "disable", "--now", "income-attribution-20261110.timer") not in commands


def test_refresh_rolls_back_service_and_hotfix_overlay_on_failed_start(tmp_path, monkeypatch):
    import scripts.scoped_income_attribution_release as release_module
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    (systemd / "income-attribution.service").write_text("old service")
    (systemd / "income-attribution.timer").write_text("old timer")
    dropin = systemd / "income-attribution.service.d"
    dropin.mkdir()
    (dropin / "20261111-proof-stability.conf").write_text("old overlay")
    release = tmp_path / "release"
    release.mkdir()
    (release / "income-attribution.service").write_text("new service")
    (release / "income-attribution.timer").write_text("new timer")
    starts = 0
    def fake_run(*args, **_):
        nonlocal starts
        if args == ("systemctl", "start", "income-attribution.service"):
            starts += 1
            if starts == 1:
                raise RuntimeError("new writer failed")
        return "active"
    monkeypatch.setattr(release_module, "SYSTEMD_DIR", systemd)
    monkeypatch.setattr(release_module, "run", fake_run)
    monkeypatch.setattr(release_module, "wait_inactive", lambda *_: None)
    monkeypatch.setattr(release_module, "container_ids", lambda: {"hrs": "same"})
    monkeypatch.setattr(release_module, "dashboard_ready", lambda: True)
    with pytest.raises(RuntimeError, match="new writer failed"):
        refresh_existing(release, {"hrs": "pinned-image", "dashboard-api": "unused"}, {"hrs": "same"})
    assert starts == 2
    assert (systemd / "income-attribution.service").read_text() == "old service"
    assert (dropin / "20261111-proof-stability.conf").read_text() == "old overlay"
