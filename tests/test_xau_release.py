from scripts.select_deploy_mode import select_deploy_mode
from scripts.render_remote_deploy import _validate


def test_xau_review_scoped_mode():
    assert select_deploy_mode("push", "a"*40, [("A", ".github/deploy-modes/xau-review-20261113")]) == "xau-review"
    _validate("xau-review", "a"*40, "00000101", "keep")
