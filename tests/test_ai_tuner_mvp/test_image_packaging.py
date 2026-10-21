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
