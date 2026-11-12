import pytest

from scripts.render_remote_deploy import _validate
from scripts.scoped_seven_gaps_release import IMAGE_PATHS, IMAGE_SERVICES
from scripts.select_deploy_mode import (
    DeployModeError, SEVEN_GAPS_BASE, SEVEN_GAPS_MARKER,
    _validate_seven_gaps_scope, select_deploy_mode,
)


def test_release_requires_one_new_marker_and_reviewed_base():
    assert select_deploy_mode("push", SEVEN_GAPS_BASE, [("A", SEVEN_GAPS_MARKER)]) == "seven-code-gaps"
    _validate_seven_gaps_scope(SEVEN_GAPS_BASE, [("A", SEVEN_GAPS_MARKER),
                                                   ("M", "strategies/new_coin/executor.py")])
    for changes in ([("M", SEVEN_GAPS_MARKER)],
                    [("A", SEVEN_GAPS_MARKER), ("A", ".github/deploy-modes/another")]):
        with pytest.raises(DeployModeError):
            select_deploy_mode("push", SEVEN_GAPS_BASE, changes)
    with pytest.raises(DeployModeError):
        _validate_seven_gaps_scope("a" * 40, [("A", SEVEN_GAPS_MARKER)])
    with pytest.raises(DeployModeError):
        _validate_seven_gaps_scope(SEVEN_GAPS_BASE, [("A", SEVEN_GAPS_MARKER),
                                                    ("M", "docker-compose.yml")])


def test_funding_rotation_forbidden_and_exact_image_inventory():
    _validate("seven-code-gaps", "a" * 40, "00000001", "keep")
    with pytest.raises(ValueError):
        _validate("seven-code-gaps", "a" * 40, "00000001", "rotate")
    assert set(IMAGE_PATHS) == set(IMAGE_SERVICES)
    assert len(IMAGE_SERVICES) == 5
    assert "shared/accounting/cashflow_roi.py" in IMAGE_PATHS["dashboard-api"]
    assert "strategies/new_coin/recovery_evidence.py" in IMAGE_PATHS["new-coin"]
