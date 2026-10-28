import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICE = COMPOSE["services"]["mtcps-xau-calibrated-shadow"]
WORKFLOW = (ROOT / ".github/workflows/deploy.yml").read_text()
CALIBRATED_CONTAINER = "trading_system-mtcps-xau-calibrated-shadow"


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
