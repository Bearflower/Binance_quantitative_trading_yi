import importlib.util
from pathlib import Path

import pytest

ENTRY = Path(__file__).resolve().parents[2] / "release/hrs-execution/deploy_logging.py"
TARGETS = {"market-cap-service": "hub", "hrs-acceptance-reporter": "reporter"}
OLD = {"Type": "json-file", "Config": {"max-file": "3", "max-size": "10m"}}
NEW = {"Type": "journald", "Config": {}}


def load_entry():
    spec = importlib.util.spec_from_file_location("deploy_logging", ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("failure", ["after_hub", "after_reporter", None])
def test_targeted_rollout_restores_original_images_and_logging(failure):
    deploy = load_entry()
    active = {
        service: {"image": image, "logging": OLD, "id": service + "-old"}
        for service, image in TARGETS.items()
    }
    original = {service: dict(state) for service, state in active.items()}
    unrelated = {"trading_system-hrs": "live-hrs-id", "postgres": "database-id"}
    original_unrelated = dict(unrelated)
    calls = []

    def replace(service, *, rollback):
        assert service in TARGETS
        calls.append((service, rollback))
        if rollback:
            active[service] = dict(original[service], id=service + "-rollback")
        else:
            active[service] = {
                "image": "new-reporter" if service == "hrs-acceptance-reporter" else "hub",
                "logging": NEW,
                "id": service + "-new",
            }
            if failure == "after_hub" and service == "market-cap-service":
                raise RuntimeError("partial replacement")

    def verify_candidate():
        if failure == "after_reporter":
            raise RuntimeError("candidate failed verification")
        assert all(state["logging"] == NEW for state in active.values())

    def verify_original():
        for service, original_state in original.items():
            assert active[service]["image"] == original_state["image"]
            assert active[service]["logging"] == original_state["logging"]

    if failure:
        with pytest.raises(RuntimeError, match="rolled back"):
            deploy.switch(replace, verify_candidate, verify_original, lambda: dict(unrelated))
        assert calls[-2:] == [("hrs-acceptance-reporter", True), ("market-cap-service", True)]
    else:
        deploy.switch(replace, verify_candidate, verify_original, lambda: dict(unrelated))
        assert calls == [("market-cap-service", False), ("hrs-acceptance-reporter", False)]
    assert unrelated == original_unrelated


def test_compose_command_limits_both_paths_to_the_named_service():
    deploy = load_entry()
    before = ["docker", "compose", "-f", "old.json"]
    after = before + ["-f", "logging.yml"]
    for stack in (before, after):
        assert deploy.replace_command(stack, "market-cap-service") == stack + [
            "up", "-d", "--no-deps", "--no-build", "--pull", "never", "market-cap-service",
        ]
        with pytest.raises(ValueError):
            deploy.replace_command(stack, "hrs-strategy")


def test_deploy_rolls_back_real_commands_after_second_compose_fails(tmp_path, monkeypatch):
    import copy
    import json

    deploy = load_entry()
    hub, reporter = deploy.TARGETS
    refs = {hub: "hub-old", reporter: "reporter-old"}
    previous_files = [str(tmp_path / "original.json")]
    active = {
        service: {
            "id": service + "-old", "image": ref, "image_id": ref + "-image-id",
            "logging": OLD, "files": previous_files, "running": True, "health": "healthy",
        }
        for service, ref in refs.items()
    }
    before = copy.deepcopy(active)
    strategies = {"trading_system-hrs": "original-hrs", "postgres": "original-postgres"}
    untouched = dict(strategies)
    calls = []
    base_config = {
        service: {"image": ref, "logging": {"driver": "json-file", "options": OLD["Config"]}}
        for service, ref in refs.items()
    }
    base_config["hrs-strategy"] = {"image": "real-trading-strategy"}
    next_config = copy.deepcopy(base_config)
    next_config[reporter]["image"] = "reporter-new"
    for service in deploy.TARGETS:
        next_config[service]["logging"] = {"driver": "journald"}

    def fake_run(*args, timeout=120):
        if args[:2] == ("python3", "scripts/database_permissions.py"):
            return b'{"ok":true}'
        if args[:3] == ("docker", "image", "inspect"):
            return (args[-1] + "-image-id").encode()
        if args[:2] == ("docker", "pull"):
            return b""
        if args[:3] == ("docker", "ps", "-a"):
            return "\n".join(deploy.TARGETS[s] for s in active).encode()
        if "up" in args:
            service = args[-1]
            assert service in deploy.TARGETS
            assert args[-6:-1] == ("-d", "--no-deps", "--no-build", "--pull", "never")
            candidate = str(tmp_path / "reporter-image.json") in args
            calls.append((service, candidate))
            if not candidate:
                active[service] = dict(before[service], id=service + "-restored")
            elif service == hub:
                active[hub].update(id="hub-new", logging=NEW)
            elif service == reporter:
                active.pop(reporter)  # Compose removed the old container before creation failed.
                raise RuntimeError("injected Compose failure")
            return b""
        raise AssertionError(args)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(deploy, "run", fake_run)
    monkeypatch.setattr(deploy, "IMAGE", r"(hub|reporter)-(old|new)")
    monkeypatch.setattr(deploy, "snapshot", lambda service: copy.deepcopy(active[service]))
    monkeypatch.setattr(deploy, "unrelated_ids", lambda: dict(strategies))
    monkeypatch.setattr(deploy, "compose_services", lambda stack: base_config if len(stack) == 6 else next_config)
    monkeypatch.setattr(deploy.subprocess, "run", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="rolled back"):
        deploy.deploy(tmp_path, tmp_path, "reporter-new", "candidate-hash", refs[reporter])

    assert calls == [(hub, True), (reporter, True), (reporter, False), (hub, False)]
    recorded = json.loads((tmp_path / "rollback.json").read_text())
    for service, state in before.items():
        assert recorded["targets"][service]["logging"] == state["logging"]
        assert recorded["targets"][service]["image"] == state["image"]
        assert recorded["targets"][service]["image_id"] == state["image_id"]
    assert recorded["other_ids"] == untouched == strategies
