from scripts.select_deploy_mode import select_deploy_mode
from scripts.render_remote_deploy import _validate


def test_xau_review_scoped_mode():
    assert select_deploy_mode("push", "a"*40, [("A", ".github/deploy-modes/xau-review-20261113")]) == "xau-review"
    _validate("xau-review", "a"*40, "00000101", "keep")


def test_xau_bundle_does_not_select_ledger_packager():
    from pathlib import Path
    import yaml
    workflow = yaml.safe_load(Path(".github/workflows/deploy.yml").read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    for step in steps:
        if "package_ledger_risk_release.py" in step.get("run", ""):
            assert step["if"] == "needs.detect.outputs.mode == 'ledger-risk-remediation'"
