import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICE = COMPOSE["services"]["mtcps-xau-calibrated-shadow"]
WORKFLOW = (ROOT / ".github/workflows/deploy.yml").read_text()
CALIBRATED_CONTAINER = "trading_system-mtcps-xau-calibrated-shadow"
CALIBRATED_SERVICE = "mtcps-xau-calibrated-shadow"
FIRST_CREATE_MARKER = (
    ROOT / ".github" / "deploy-modes" / "xau-calibrated-shadow-first-create"
)


def _env_map(service):
    return dict(item.split("=", 1) for item in service["environment"])


def test_calibrated_service_is_isolated_and_read_only():
    env = _env_map(SERVICE)
    assert SERVICE["container_name"] == "trading_system-mtcps-xau-calibrated-shadow"
    assert SERVICE["command"] == ["python", "-m", "strategies.btc_eth.shadow.main"]
    assert env["INSTANCE_ID"] == "mtcps_xau_calibrated_shadow"
    assert env["SHADOW_PROFILES"] == "xau_calibrated"
    assert env["SHADOW_RUN_ID_PREFIX"] == "xau-calibrated-shadow"
    assert env["SHADOW_CRON"] == "20 * * * *"
    assert env["EXECUTION_MODE"] == "shadow_batch"
    assert env["REAL_ORDERING_ENABLED"] == "false"
    assert env["STATE_REPORTING_ENABLED"] == "false"
    assert env["CAPITAL_STRATEGY_ID"] == ""
    assert env["CIRCUIT_BREAKER_POOL"] == ""


def test_calibrated_service_has_independent_runtime_paths():
    volumes = SERVICE["volumes"]
    assert "./logs/mtcps_xau_calibrated_shadow:/app/logs" in volumes
    assert "./data/mtcps_xau_calibrated_shadow:/app/data" in volumes
    assert all("strategies/btc_eth/shadow" not in volume for volume in volumes)


def _shell_tokens(block):
    return set(re.findall(r"[A-Za-z0-9_.-]+", block))


def _residual_cleanup_tokens():
    match = re.search(r'RESIDUAL_CANDIDATES="(?P<body>.*?)"', WORKFLOW, re.DOTALL)
    assert match is not None, "deploy.yml 缺少 RESIDUAL_CANDIDATES 清理清单"
    return _shell_tokens(match.group("body"))


def _long_running_health_gate_tokens():
    section = re.search(
        r"# 5\. 等全部长期运行容器就绪(?P<body>.*?)(?:# 6\.|\Z)",
        WORKFLOW,
        re.DOTALL,
    )
    assert section is not None, "deploy.yml 缺少长期运行容器就绪检查区段"
    loop = re.search(
        r"for\s+container\s+in\s+(?P<containers>.*?);\s*do",
        section.group("body"),
        re.DOTALL,
    )
    assert loop is not None, "长期运行容器就绪区段缺少 for container 清单"
    return _shell_tokens(loop.group("containers"))


def test_residual_cleanup_includes_calibrated_container():
    assert CALIBRATED_CONTAINER in _residual_cleanup_tokens()


def test_long_running_health_gate_includes_calibrated_container():
    assert CALIBRATED_CONTAINER in _long_running_health_gate_tokens()


def _calibrated_first_create_block():
    match = re.search(
        r"# calibrated-first-create begin(?P<body>.*?)"
        r"# calibrated-first-create end",
        WORKFLOW,
        re.DOTALL,
    )
    assert match is not None, "deploy.yml 缺少 calibrated 首次创建目标化分支"
    return match.group("body")


def test_calibrated_healthcheck_uses_runtime_readiness_probe():
    assert SERVICE["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "strategies.btc_eth.shadow.readiness",
    ]


def test_first_create_mode_is_selected_by_one_time_changed_file_signal():
    assert FIRST_CREATE_MARKER.is_file()
    assert str(FIRST_CREATE_MARKER.relative_to(ROOT)) in WORKFLOW
    assert 'DEPLOY_MODE="calibrated-first-create"' in WORKFLOW
    assert 'git diff "$BASE" HEAD --name-only' in WORKFLOW
    selection = re.search(
        r"- name: 🎯 选择部署模式(?P<body>.*?)(?=\n\s+- name:)",
        WORKFLOW,
        re.DOTALL,
    )
    assert selection is not None
    assert "docker inspect" not in selection.group("body")
    assert 'github.event_name }}" = "push"' in selection.group("body")
    assert "非 push 触发，使用普通全量部署" in selection.group("body")


def test_first_create_remote_path_targets_only_calibrated_service():
    block = _calibrated_first_create_block()

    _assert_targeted_block_safe(block)


def _assert_targeted_block_safe(block):
    pull_commands = [
        line.strip()
        for line in block.splitlines()
        if line.strip().startswith("docker compose pull")
    ]
    up_commands = [
        line.strip()
        for line in block.splitlines()
        if line.strip().startswith("docker compose up")
    ]
    assert pull_commands == [f"docker compose pull {CALIBRATED_SERVICE}"]
    assert up_commands == [
        f"docker compose up -d --no-deps {CALIBRATED_SERVICE}"
    ]
    assert "btc-eth-strategy" not in block
    assert "mtcps-xau-shadow" not in block
    assert "--remove-orphans" not in block
    assert "docker compose run" not in block
    assert "docker compose restart" not in block
    assert "psql" not in block
    assert f"docker compose stop {CALIBRATED_SERVICE}" in block


@pytest.mark.parametrize(
    "mutate",
    [
        lambda block: block.replace(
            f"docker compose pull {CALIBRATED_SERVICE}",
            "docker compose pull",
        ),
        lambda block: block.replace("--no-deps ", ""),
        lambda block: block.replace(
            f"docker compose up -d --no-deps {CALIBRATED_SERVICE}",
            "docker compose up -d --remove-orphans",
        ),
    ],
)
def test_targeted_deploy_guard_detects_dangerous_command_mutations(mutate):
    with pytest.raises(AssertionError):
        _assert_targeted_block_safe(mutate(_calibrated_first_create_block()))


def test_ordinary_deploy_keeps_full_compose_path_outside_first_create_block():
    ordinary = WORKFLOW.replace(_calibrated_first_create_block(), "")

    assert "docker compose pull\n" in ordinary
    assert "docker compose up -d --remove-orphans" in ordinary
