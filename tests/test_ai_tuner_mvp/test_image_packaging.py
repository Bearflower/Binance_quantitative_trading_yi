from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ai_tuner_image_contains_readiness_cli():
    dockerfile = (REPO_ROOT / "ai_tuner" / "Dockerfile").read_text(encoding="utf-8")

    assert (
        "COPY scripts/ai_tuner_mvp.py /app/scripts/ai_tuner_mvp.py" in dockerfile
    ), "ai-tuner 镜像必须包含生产 readiness 审计 CLI"


def test_ai_tuner_image_contains_snapshot_cli():
    dockerfile = (REPO_ROOT / "ai_tuner" / "Dockerfile").read_text(encoding="utf-8")
    assert "ai_tuner.snapshots.cli" in dockerfile


def test_snapshot_service_is_isolated_from_auto_apply_and_overrides():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["ai-tuner-snapshot"]
    assert service["command"] == ["python3", "-m", "ai_tuner.snapshots.cli", "daemon"]
    assert "AUTO_APPLY" not in service.get("environment", {})
    assert all("tuning_overrides" not in volume for volume in service.get("volumes", []))
    assert any(volume.endswith(":ro") for volume in service.get("volumes", []))
    environment = service.get("environment", [])
    assert not any(item.startswith("BINANCE_API_KEY=") for item in environment)
    assert not any(item.startswith("BINANCE_API_SECRET=") for item in environment)
    assert ".secrets/ai_tuner_snapshot.env" in service.get("env_file", [])


def test_snapshot_tables_use_dedicated_non_delete_writer_role():
    ddl = (REPO_ROOT / "database/postgres/init-scripts/13-ai-tuner-market-snapshots.sql").read_text(
        encoding="utf-8"
    )
    assert "CREATE ROLE ai_tuner_snapshot_owner NOLOGIN" in ddl
    assert "CREATE ROLE ai_tuner_snapshot_writer LOGIN" in ddl
    assert "OWNER TO ai_tuner_snapshot_owner" in ddl
    assert "REVOKE ALL ON public.ai_tuner_market_snapshots FROM PUBLIC, trading_user" in ddl
    assert "GRANT SELECT, INSERT, UPDATE ON public.ai_tuner_market_snapshots" in ddl
    assert "GRANT SELECT ON public.ai_tuner_market_snapshots TO trading_user" in ddl
    assert "GRANT DELETE" not in ddl


def test_deploy_provisions_snapshot_secret_only_on_server():
    workflow = (REPO_ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "openssl rand -hex 32" in workflow
    assert "ALTER ROLE ai_tuner_snapshot_writer LOGIN PASSWORD" in workflow
    assert "<<'MIGRATION_ADMIN_SQL'" not in workflow
    assert "<<'SNAPSHOT_ROLE_SQL'" not in workflow
    assert "PASSWORD :'migration_password';" in workflow
    assert "PASSWORD :'snapshot_password';" in workflow
    assert "SNAPSHOT_SECRET_FILE=.secrets/ai_tuner_snapshot.env" in workflow
    assert "MIGRATION_ADMIN_SECRET_FILE=.secrets/trading_migration_admin_password" in workflow
    assert "TRADING_MIGRATION_ADMIN_PASSWORD=%s" not in workflow
    assert "CREATE ROLE trading_migration_admin LOGIN SUPERUSER" in workflow
    assert "ALTER DATABASE trading_platform OWNER TO trading_migration_admin" in workflow
    assert "ALTER ROLE trading_user NOSUPERUSER NOCREATEROLE NOCREATEDB" in workflow
    assert "-U trading_migration_admin -d trading_platform -f" in workflow
    assert "pg_dumpall -U trading_migration_admin --globals-only" in workflow
    assert "PERMISSION_BOUNDARY_OK" in workflow
    assert "has_table_privilege('ai_tuner_snapshot_writer','public.ai_tuner_market_snapshots','SELECT')" in workflow
    assert "has_table_privilege('ai_tuner_snapshot_writer','public.ai_tuner_market_snapshots','INSERT')" in workflow
    assert "has_table_privilege('ai_tuner_snapshot_writer','public.ai_tuner_market_snapshots','UPDATE')" in workflow
    assert "NOT has_table_privilege('trading_user','public.ai_tuner_market_snapshots','INSERT')" in workflow
    assert "NOT has_table_privilege('trading_user','public.ai_tuner_market_snapshots','UPDATE')" in workflow
    assert "NOT has_table_privilege('trading_user','public.ai_tuner_market_snapshots','TRUNCATE')" in workflow
    assert "ALTER ROLE trading_user SUPERUSER CREATEDB CREATEROLE REPLICATION" in workflow
    assert workflow.index("CREATE ROLE trading_migration_admin LOGIN SUPERUSER") < workflow.index(
        "-U trading_migration_admin -d trading_platform -f"
    )
    assert workflow.index("pg_dumpall -U trading_migration_admin --globals-only") < workflow.index(
        "ALTER DATABASE trading_platform OWNER TO trading_migration_admin"
    )
