from scripts.select_funding_deploy import select_funding_action


def test_unrelated_push_keeps_collector():
    assert select_funding_action("push", "a" * 40, [("M", "docs/README.md")]) == "keep"
    assert select_funding_action("push", "a" * 40, [("M", "strategies/hrs/strategy.py")]) == "keep"


def test_sensitive_or_unknown_changes_rotate():
    for path in (
        "ai_tuner/funding_schedule/cli.py", "ai_tuner/Dockerfile",
        "ai_tuner/requirements.txt", "shared/db.py", "docker-compose.yml",
        "docker-compose.logging.yml", "database/postgres/init-scripts/14.sql",
        "database/postgres/migrations/1.sql", ".github/workflows/deploy.yml",
        "scripts/select_funding_deploy.py", "scripts/remote_deploy_runtime.sh",
        "scripts/render_remote_deploy.py", "mystery/file",
    ):
        assert select_funding_action("push", "a" * 40, [("M", path)]) == "rotate", path


def test_entire_range_and_rename_paths_are_checked():
    assert select_funding_action("push", "a" * 40, [
        ("M", "docs/README.md"), ("R100", "shared/old.py"),
        ("R100", "docs/new.md"),
    ]) == "rotate"


def test_unsafe_inputs_fail_closed():
    assert select_funding_action("workflow_dispatch", "a" * 40, []) == "rotate"
    assert select_funding_action("push", "", [("M", "docs/a.md")]) == "rotate"
    assert select_funding_action("push", "0" * 40, [("M", "docs/a.md")]) == "rotate"
    assert select_funding_action("push", "a" * 40, []) == "rotate"
    assert select_funding_action("push", "a" * 40, [("?", "docs/a.md")]) == "rotate"
    assert select_funding_action("push", "a" * 40, [("U", "docs/a.md")]) == "rotate"
