import re
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICE = COMPOSE["services"]["mtcps-xau-calibrated-shadow"]
WORKFLOW = (ROOT / ".github/workflows/deploy.yml").read_text()
MODE_SELECTOR = (ROOT / "scripts/select_deploy_mode.py").read_text()
CALIBRATED_CONTAINER = "trading_system-mtcps-xau-calibrated-shadow"
CALIBRATED_SERVICE = "mtcps-xau-calibrated-shadow"
FIRST_CREATE_MARKER = (
    ROOT / ".github" / "deploy-modes" / "xau-calibrated-shadow-first-create"
)
REMOTE_WRAPPER = ROOT / "scripts" / "run_remote_deploy.sh"
REMOTE_RUNTIME = ROOT / "scripts" / "remote_deploy_runtime.sh"
RETRY_MARKER = (
    ROOT
    / ".github"
    / "deploy-modes"
    / "xau-calibrated-shadow-first-create-retry-1"
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
    match = re.search(
        r'RESIDUAL_CANDIDATES="(?P<body>.*?)"',
        _remote_deploy_script(),
        re.DOTALL,
    )
    assert match is not None, "deploy.yml 缺少 RESIDUAL_CANDIDATES 清理清单"
    return _shell_tokens(match.group("body"))


def _long_running_health_gate_tokens():
    section = re.search(
        r"# 5\. 等全部长期运行容器就绪(?P<body>.*?)(?:# 6\.|\Z)",
        _remote_deploy_script(),
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
        _remote_deploy_script(),
        re.DOTALL,
    )
    assert match is not None, "deploy.yml 缺少 calibrated 首次创建目标化分支"
    return match.group("body")


def _remote_deploy_script():
    assert REMOTE_RUNTIME.is_file(), "仓库缺少远端部署 runtime 脚本"
    return REMOTE_RUNTIME.read_text(encoding="utf-8")


def _remote_script_through_targeted_exit():
    prefix = _remote_deploy_script().split("# calibrated-first-create end", 1)[0]
    assert 'exit 0' in prefix
    return prefix


def test_calibrated_healthcheck_uses_runtime_readiness_probe():
    assert SERVICE["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "strategies.btc_eth.shadow.readiness",
    ]


def test_first_create_mode_is_selected_by_one_time_changed_file_signal():
    assert FIRST_CREATE_MARKER.is_file()
    assert str(FIRST_CREATE_MARKER.relative_to(ROOT)) in MODE_SELECTOR
    assert 'return "calibrated-first-create"' in MODE_SELECTOR
    assert "scripts/select_deploy_mode.py" in WORKFLOW
    selection = re.search(
        r"- name: 🎯 选择部署模式(?P<body>.*?)(?=\n\s+- name:)",
        WORKFLOW,
        re.DOTALL,
    )
    assert selection is not None
    assert "docker inspect" not in selection.group("body")
    assert '--event-name "${{ github.event_name }}"' in selection.group("body")


def test_retry_marker_exists_for_run_120_parse_failure():
    assert RETRY_MARKER.is_file()


def test_each_workflow_run_block_stays_below_github_expression_limit():
    parsed = yaml.safe_load(WORKFLOW)
    for job_name, job in parsed["jobs"].items():
        for step in job.get("steps", []):
            if "run" in step:
                assert len(step["run"]) < 21000, (
                    f"{job_name}/{step.get('name')} run block is {len(step['run'])} chars"
                )


def test_deploy_job_requires_successful_build_matrix():
    parsed = yaml.safe_load(WORKFLOW)
    condition = parsed["jobs"]["deploy"]["if"]
    assert "needs.detect.result == 'success'" in condition
    assert "needs.build-all.result == 'success'" in condition
    assert "cancelled" not in condition
    assert "skipped" not in condition


def test_remote_runtime_is_repository_script_and_bash_syntax_valid():
    assert REMOTE_RUNTIME.is_file()
    result = subprocess.run(
        ["bash", "-n", str(REMOTE_RUNTIME)], text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr


def test_extracted_runtime_preserves_logging_overlay_and_pre_up_archive():
    runtime = _remote_deploy_script()
    assert "export COMPOSE_FILE=docker-compose.yml:docker-compose.logging.yml" in runtime
    archive_at = runtime.index("bash scripts/archive_container_logs.sh")
    full_pull_at = runtime.index('echo "📥 docker compose pull..."')
    assert archive_at < full_pull_at

    upload = _workflow_step("📤 上传 docker-compose.yml（scp）")
    assert "docker-compose.logging.yml" in upload
    assert "scripts/archive_container_logs.sh" in upload


def _assert_calibrated_build_override(block):
    selector_at = block.index("scripts/select_deploy_mode.py")
    ordinary_at = block.index("shared/ 或 docker-compose.yml / VERSION")
    assert selector_at < ordinary_at
    assert 'if [ "$DEPLOY_MODE" = "calibrated-first-create" ]' in block
    assert 'if [ "$SERVICE_NAME" = "btc-eth" ]' in block
    assert 'echo "should_build=true" >> "$GITHUB_OUTPUT"' in block
    assert 'echo "should_build=false" >> "$GITHUB_OUTPUT"' in block


def test_calibrated_marker_builds_only_btc_eth_before_ordinary_matrix_rules():
    decision = _workflow_step("⏭️ 判断是否需要构建")
    _assert_calibrated_build_override(decision)


def test_calibrated_build_override_guard_detects_missing_btc_eth_binding():
    decision = _workflow_step("⏭️ 判断是否需要构建")
    with pytest.raises((AssertionError, ValueError)):
        _assert_calibrated_build_override(
            decision.replace('SERVICE_NAME" = "btc-eth', 'SERVICE_NAME" = "hrs')
        )


def _run_build_decision(block, service_name, repo, base, tmp_path):
    rendered = (
        block.replace("${{ github.event.before }}", base)
        .replace("${{ github.event_name }}", "push")
        .replace("${{ matrix.service.name }}", service_name)
    )
    output = tmp_path / f"{service_name}.out"
    result = subprocess.run(
        ["bash", "-c", rendered],
        cwd=repo,
        env={"PATH": str(Path("/usr/bin")) + ":/bin:/usr/local/bin", "GITHUB_OUTPUT": str(output)},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return values["should_build"]


def _init_retry_marker_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
    )
    (repo / "scripts").mkdir()
    (repo / "scripts" / "select_deploy_mode.py").write_text(
        MODE_SELECTOR
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    marker = repo / ".github/deploy-modes/xau-calibrated-shadow-first-create-retry-1"
    marker.parent.mkdir(parents=True)
    marker.write_text("retry-1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "retry marker"], cwd=repo, check=True)
    return repo, base


def _assert_only_btc_eth_builds(block, tmp_path):
    parsed = yaml.safe_load(WORKFLOW)
    services = [
        service["name"]
        for service in parsed["jobs"]["build-all"]["strategy"]["matrix"]["service"]
    ]
    assert len(services) == 11
    repo, base = _init_retry_marker_repo(tmp_path)
    actual = {
        service: _run_build_decision(block, service, repo, base, tmp_path)
        for service in services
    }
    assert actual == {
        service: "true" if service == "btc-eth" else "false"
        for service in services
    }


def test_retry_marker_real_build_decision_only_builds_btc_eth(tmp_path):
    _assert_only_btc_eth_builds(_workflow_step("⏭️ 判断是否需要构建"), tmp_path)


def test_real_build_decision_guard_detects_hrs_mutation(tmp_path):
    decision = _workflow_step("⏭️ 判断是否需要构建")
    mutated = decision.replace(
        'if [ "$SERVICE_NAME" = "btc-eth" ]; then',
        'if [ "$SERVICE_NAME" = "btc-eth" ] || [ "$SERVICE_NAME" = "hrs" ]; then',
    )
    with pytest.raises(AssertionError):
        _assert_only_btc_eth_builds(mutated, tmp_path)


def test_first_create_remote_path_targets_only_calibrated_service():
    block = _calibrated_first_create_block()

    _assert_targeted_block_safe(block)


def test_targeted_remote_prefix_has_no_full_deploy_side_effects():
    prefix = _remote_script_through_targeted_exit()

    for forbidden in (
        "docker system prune",
        "docker builder prune",
        "RESIDUAL_CANDIDATES",
        "docker compose run",
        "docker compose restart",
        "psql",
        "init-scripts",
    ):
        assert forbidden not in prefix
    assert "docker login ghcr.io" not in prefix
    _assert_targeted_block_safe(prefix)


def test_init_scripts_sync_is_full_deploy_only():
    sync_step = re.search(
        r"- name: 📦 同步数据库 init-scripts(?P<body>.*?)(?=\n\s+- name:)",
        WORKFLOW,
        re.DOTALL,
    )
    assert sync_step is not None
    assert "if: steps.deployment_mode.outputs.mode == 'full'" in sync_step.group("body")


def _workflow_step(name):
    match = re.search(
        rf"- name: {re.escape(name)}(?P<body>.*?)(?=\n\s+- name:|\Z)",
        WORKFLOW,
        re.DOTALL,
    )
    assert match is not None, f"deploy.yml 缺少步骤: {name}"
    return match.group("body")


def test_ghcr_token_is_streamed_to_independent_login_and_never_in_script():
    assert "__GHCR_TOKEN__" not in WORKFLOW
    assert 'sed -i "s|__GHCR_TOKEN__' not in WORKFLOW
    login = _workflow_step("🔐 远端登录 GHCR")
    assert "GHCR_PULL_TOKEN: ${{ secrets.GHCR_PULL_TOKEN }}" in login
    assert 'printf \'%s\' "$GHCR_PULL_TOKEN" | ssh ' in login
    assert "set -o pipefail" in login
    assert "docker login ghcr.io -u bearflower --password-stdin" in login
    assert "for i in 1 2 3" in login


def _assert_remote_script_contains_no_secret_token(script):
    assert "GHCR_PULL_TOKEN" not in script
    assert "${{ secrets." not in script
    assert "__GHCR_TOKEN__" not in script
    assert re.search(r"(?i)(secret.*token|token.*secret)", script) is None


def test_generated_remote_deploy_script_contains_no_secret_token_expression():
    _assert_remote_script_contains_no_secret_token(_remote_deploy_script())


def test_remote_script_secret_guard_detects_injection_mutation():
    mutated = _remote_deploy_script() + '\necho "${{ secrets.GHCR_PULL_TOKEN }}"\n'
    with pytest.raises(AssertionError):
        _assert_remote_script_contains_no_secret_token(mutated)


def _assert_upload_step_contains_no_secret_token(step):
    assert "GHCR_PULL_TOKEN" not in step
    assert "${{ secrets." not in step
    assert re.search(r"(?i)(secret.*token|token.*secret)", step) is None


def test_upload_step_contains_no_secret_and_ghcr_secret_is_login_only():
    upload = _workflow_step("🚀 生成并上传远程部署脚本")
    login = _workflow_step("🔐 远端登录 GHCR")

    _assert_upload_step_contains_no_secret_token(upload)
    assert "${{ secrets.GHCR_PULL_TOKEN }}" in login
    assert WORKFLOW.count("${{ secrets.GHCR_PULL_TOKEN }}") == 1
    assert "GHCR_PULL_TOKEN" not in WORKFLOW.replace(login, "", 1)


def test_upload_step_secret_guard_detects_post_render_injection():
    upload = _workflow_step("🚀 生成并上传远程部署脚本")
    mutation = (
        '\nprintf \'%s\' "${{ secrets.GHCR_PULL_TOKEN }}" '
        '>> "$LOCAL_DEPLOY_SCRIPT"\n'
    )
    render_end_line = '--deploy-id "$DEPLOY_ID"'
    render_end = upload.index(render_end_line) + len(render_end_line)
    mutated = upload[:render_end] + mutation + upload[render_end:]

    with pytest.raises(AssertionError):
        _assert_upload_step_contains_no_secret_token(mutated)


def test_remote_deploy_script_path_is_unique_private_and_not_tmp():
    assert "/tmp/deploy.sh" not in WORKFLOW
    expected = (
        "/root/trading_system/.deploy_tmp/"
        "deploy-${{ github.run_id }}-${{ github.run_attempt }}.sh"
    )
    assert expected in WORKFLOW
    upload = _workflow_step("🚀 生成并上传远程部署脚本")
    assert 'mkdir -p /root/trading_system/.deploy_tmp' in upload
    assert 'chmod 700 /root/trading_system/.deploy_tmp' in upload
    assert 'chmod 700 \\"$REMOTE_DEPLOY_SCRIPT\\"' in upload
    assert "python3 scripts/render_remote_deploy.py" in upload
    assert "sed -i" not in upload


def test_remote_execution_cleanup_preserves_success_or_failure_exit_code():
    step = _workflow_step("▶️ 执行远程部署脚本")
    assert "retry_connect" not in step
    assert 'ssh 43.156.242.184 bash -s -- "$REMOTE_DEPLOY_SCRIPT"' in step
    assert "< scripts/run_remote_deploy.sh" in step


@pytest.mark.parametrize("exit_code", [0, 23, 255])
def test_remote_execution_wrapper_preserves_exit_and_removes_script(
    tmp_path,
    exit_code,
):
    private_dir = tmp_path / ".deploy_tmp"
    private_dir.mkdir(mode=0o700)
    (private_dir / ".keep").write_text("keep", encoding="utf-8")
    deploy_script = private_dir / "deploy-test.sh"
    deploy_script.write_text(f"#!/usr/bin/env bash\nexit {exit_code}\n", encoding="utf-8")
    deploy_script.chmod(0o700)

    result = subprocess.run(
        ["bash", str(REMOTE_WRAPPER), str(deploy_script)],
        text=True,
        capture_output=True,
    )

    assert result.returncode == exit_code
    assert not deploy_script.exists()


def test_workflow_yaml_and_deploy_shell_blocks_are_syntax_valid():
    parsed = yaml.safe_load(WORKFLOW)
    deploy_steps = parsed["jobs"]["deploy"]["steps"]
    for step in deploy_steps:
        if "run" not in step:
            continue
        shell = re.sub(r"\$\{\{.*?\}\}", "1", step["run"])
        result = subprocess.run(
            ["bash", "-n"], input=shell, text=True, capture_output=True
        )
        assert result.returncode == 0, f"{step.get('name')}: {result.stderr}"

    remote_result = subprocess.run(
        ["bash", "-n"],
        input=_remote_deploy_script(),
        text=True,
        capture_output=True,
    )
    assert remote_result.returncode == 0, remote_result.stderr

    wrapper_result = subprocess.run(
        ["bash", "-n", str(REMOTE_WRAPPER)], text=True, capture_output=True
    )
    assert wrapper_result.returncode == 0, wrapper_result.stderr


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
    ordinary = _remote_deploy_script().replace(_calibrated_first_create_block(), "")

    assert "docker compose pull\n" in ordinary
    assert "docker compose up -d --remove-orphans" in ordinary
