from pathlib import Path

import pytest

from scripts.render_remote_deploy import RenderError, _validate
from scripts.scoped_income_attribution_release import render_service, require_images
from scripts.select_deploy_mode import (
    DeployModeError, INCOME_ATTRIBUTION_BASE, _validate_income_scope, select_deploy_mode,
)


MARKER = ".github/deploy-modes/income-attribution-20261110"


def test_income_marker_selects_scoped_mode():
    assert select_deploy_mode("push", "a" * 40, [("A", MARKER)]) == "income-attribution"
    with pytest.raises(DeployModeError):
        select_deploy_mode("push", "a" * 40, [("M", MARKER)])


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
