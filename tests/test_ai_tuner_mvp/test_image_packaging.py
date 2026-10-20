from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ai_tuner_image_contains_readiness_cli():
    dockerfile = (REPO_ROOT / "ai_tuner" / "Dockerfile").read_text(encoding="utf-8")

    assert (
        "COPY scripts/ai_tuner_mvp.py /app/scripts/ai_tuner_mvp.py" in dockerfile
    ), "ai-tuner 镜像必须包含生产 readiness 审计 CLI"
