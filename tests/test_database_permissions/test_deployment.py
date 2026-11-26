"""切换部署必须先迁移、应用权限和审计，然后启动受影响的服务。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_account_cutover_stages_both_migrations_and_contract():
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    step = workflow.split("- name: 同步账户切换迁移及权限契约", 1)[1].split("\n      - ", 1)[0]
    for name in ("database_permissions.py", "permissions.json", "2026-11-25-account-mode-equity.sql",
                 "2026-11-25-classic-entry-observation.sql", "2026-11-25-account-fact-provenance.sql"):
        assert name in step


def test_cutover_gates_old_services_before_activation():
    script = (ROOT / "scripts/remote_deploy_runtime.sh").read_text()
    gate = script.split('if [ "$CUTOVER_ENABLED" = 1 ]; then')[-1]
    assert gate.index("account-mode-equity.sql") < gate.index("database_permissions.py\" apply")
    assert gate.index("account-fact-provenance.sql") < gate.index("database_permissions.py\" apply")
    assert gate.index("classic-entry-observation.sql") < gate.index("database_permissions.py\" apply")
    assert gate.index("database_permissions.py\" apply") < gate.index("database_permissions.py\" audit")
    assert gate.index("database_permissions.py\" audit") < gate.index("activate_full_services")
    assert 'database_permissions.py" audit' in script.split('if [ "$DEPLOY_MODE" = "xau-review" ]')[0]
