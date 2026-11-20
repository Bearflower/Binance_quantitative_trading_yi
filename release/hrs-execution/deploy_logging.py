"""Switch the two HRS add-ons using their active Compose project and release files."""

import argparse
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


TARGETS = {
    "market-cap-service": "trading_system-market-cap",
    "hrs-acceptance-reporter": "trading_system-hrs-acceptance",
}
REPORTER = "hrs-acceptance-reporter"
IMAGE = r"ghcr\.io/bearflower/trading-hrs@sha256:[a-f0-9]{64}"
JOURNALD = {"Type": "journald", "Config": {}}
SOURCE = "/app/strategies/hrs/acceptance_report.py"


def run(*args, timeout=120):
    result = subprocess.run(args, capture_output=True, check=False, timeout=timeout)
    if result.returncode:
        # Compose diagnostics may include interpolated configuration values.
        raise RuntimeError(f"{args[0]} failed with exit {result.returncode}")
    return result.stdout


def replace_command(stack, service):
    if service not in TARGETS:
        raise ValueError("service outside logging release")
    return [*stack, "up", "-d", "--no-deps", "--no-build", "--pull", "never", service]


def switch(replace, verify_candidate, verify_original, unrelated_ids):
    before = unrelated_ids()
    try:
        for service in TARGETS:
            replace(service, rollback=False)
        verify_candidate()
        if unrelated_ids() != before:
            raise RuntimeError("unrelated container IDs changed")
    except (Exception, KeyboardInterrupt) as error:
        failures = []
        for service in reversed(TARGETS):
            try:
                replace(service, rollback=True)
            except Exception:
                failures.append(service)
        try:
            verify_original()
            if unrelated_ids() != before:
                failures.append("unrelated container IDs")
        except Exception:
            failures.append("rollback verification")
        if failures:
            raise RuntimeError(f"rollback incomplete: {', '.join(failures)}") from error
        raise RuntimeError("logging release failed and rolled back") from error


def snapshot(service):
    container, = json.loads(run("docker", "inspect", TARGETS[service]))
    labels = container["Config"]["Labels"]
    if (labels.get("com.docker.compose.project"), labels.get("com.docker.compose.service")) != (
        "trading_system", service,
    ):
        raise RuntimeError(f"unexpected Compose identity for {service}")
    health = container["State"].get("Health", {})
    return {
        "id": container["Id"], "image": container["Config"]["Image"],
        "image_id": container["Image"], "logging": container["HostConfig"]["LogConfig"],
        "files": labels["com.docker.compose.project.config_files"].split(","),
        "health": health.get("Status"), "running": container["State"]["Running"],
    }


def unrelated_ids():
    rows = run("docker", "ps", "-a", "--no-trunc", "--format", "{{.Names}} {{.ID}}").decode()
    return {name: identity for name, identity in map(str.split, rows.splitlines())
            if name not in TARGETS.values()}


def compose_services(stack):
    return json.loads(run(*stack, "config", "--format", "json"))["services"]


def compose_logging(settings):
    return {"Type": settings["driver"], "Config": settings.get("options", {})}


def validate_configs(original, before, after, reporter_image):
    expected = deepcopy(before)
    for service, state in original.items():
        settings = before[service]
        if settings["image"] != state["image"] or compose_logging(settings["logging"]) != state["logging"]:
            raise RuntimeError(f"active definition cannot restore {service}")
        expected[service]["logging"] = {"driver": "journald"}
    expected[REPORTER]["image"] = reporter_image
    if after != expected:
        raise RuntimeError("merged Compose delta is outside the logging release")


def reporter_has_journal_event(identity):
    output = run("journalctl", "-q", "--no-pager", "-n", "20", "-o", "cat",
                 f"CONTAINER_ID_FULL={identity}")
    return any(line.startswith(b"event=hrs_acceptance_report_written ") for line in output.splitlines())


def wait_for(expected, *, candidate=False, source_hash=None):
    deadline = time.monotonic() + 150
    while True:
        states = {service: snapshot(service) for service in TARGETS}
        if all(
            state["running"] and state["health"] == "healthy"
            and all(state[key] == expected[service][key] for key in ("image", "image_id", "logging"))
            and (not candidate or state["id"] != expected[service]["id"])
            for service, state in states.items()
        ):
            if not candidate:
                return states
            source = run("docker", "exec", TARGETS[REPORTER], "sha256sum", SOURCE).decode().split()[0]
            if source != source_hash:
                raise RuntimeError("reporter source hash differs from candidate")
            if reporter_has_journal_event(states[REPORTER]["id"]):
                return states
        if time.monotonic() >= deadline:
            raise RuntimeError("target health, image, logging or reporter journal verification timed out")
        time.sleep(3)


def deploy(root, bundle, image, source_hash, base_image):
    os.chdir(root)
    original = {service: snapshot(service) for service in TARGETS}
    files = original[REPORTER]["files"]
    if original["market-cap-service"]["files"] != files or original[REPORTER]["image"] != base_image:
        raise RuntimeError("active releases differ from the candidate base")
    for service, state in original.items():
        if not state["running"] or state["health"] != "healthy" or not re.fullmatch(IMAGE, state["image"]):
            raise RuntimeError(f"cannot snapshot healthy, digest-pinned {service}")
        original_image_id = run("docker", "image", "inspect", "--format", "{{.Id}}", state["image"]).decode().strip()
        if original_image_id != state["image_id"]:
            raise RuntimeError(f"rollback image for {service} is unavailable")

    old_stack = ["docker", "compose", "--project-name", "trading_system"]
    for file in files:
        old_stack.extend(("-f", file))
    candidate_file = bundle / "reporter-image.json"
    candidate_file.write_text(json.dumps({"services": {REPORTER: {"image": image}}}))
    candidate_stack = [*old_stack, "-f", str(bundle / "docker-compose.logging.yml"),
                       "-f", str(candidate_file)]
    validate_configs(original, compose_services(old_stack), compose_services(candidate_stack), image)
    before_ids = unrelated_ids()
    audit = run("python3", "scripts/database_permissions.py", "audit")
    if not json.loads(audit)["ok"]:
        raise RuntimeError("database permissions audit failed")
    (bundle / "permissions-audit.json").write_bytes(audit)
    (bundle / "before.json").write_text(json.dumps({"targets": original, "other_ids": before_ids}, indent=2))

    for service, state in original.items():
        with (bundle / f"{service}.log").open("wb") as archive:
            subprocess.run(
                ["docker", "logs", "--timestamps", state["id"]],
                stdout=archive, stderr=subprocess.STDOUT, check=True, timeout=120,
            )
    run("docker", "pull", image, timeout=300)
    candidate_id = run("docker", "image", "inspect", "--format", "{{.Id}}", image).decode().strip()
    expected = deepcopy(original)
    for state in expected.values():
        state["logging"] = JOURNALD
    expected[REPORTER].update(image=image, image_id=candidate_id)
    if unrelated_ids() != before_ids or any(snapshot(service) != state for service, state in original.items()):
        raise RuntimeError("container snapshot changed before cutover")

    def replace(service, *, rollback):
        if rollback:
            names = run("docker", "ps", "-a", "--format", "{{.Names}}").decode().splitlines()
            if TARGETS[service] in names:
                current = snapshot(service)
                if current["running"] and all(
                    current[key] == original[service][key] for key in ("image", "image_id", "logging")
                ):
                    return
        run(*replace_command(old_stack if rollback else candidate_stack, service), timeout=180)

    def verify_candidate():
        after = wait_for(expected, candidate=True, source_hash=source_hash)
        (bundle / "after.json").write_text(json.dumps({"targets": after, "other_ids": unrelated_ids()}, indent=2))

    def verify_original():
        states = wait_for(original)
        (bundle / "rollback.json").write_text(json.dumps({"targets": states, "other_ids": unrelated_ids()}, indent=2))

    switch(replace, verify_candidate, verify_original, unrelated_ids)


def install_inspection(root, bundle):
    destination = root / "ai_tuner/ops/log_retention_check.py"
    candidate = bundle / destination.name
    original = destination.read_bytes()
    expected = "f592421fa82687ab3045f1076bebc82b90194bebe3f584e9ae825f4d3ffab067"
    if hashlib.sha256(original).hexdigest() != expected:
        raise RuntimeError("production inspection differs from the reviewed baseline")
    dry_run = run("env", f"PYTHONPATH={root}", "python3", str(candidate), "--dry-run")
    if "查询失败".encode() in dry_run:
        raise RuntimeError("candidate inspection reported a log query failure")
    (bundle / "inspection-dry-run.txt").write_bytes(dry_run)
    (bundle / "inspection-before.py").write_bytes(original)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as work:
        staged = Path(work.name)
        try:
            work.write(candidate.read_bytes())
            work.flush()
            staged.chmod(destination.stat().st_mode & 0o777)
            staged.replace(destination)
        finally:
            staged.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--install-inspection", action="store_true")
    args = parser.parse_args()
    if not all(re.fullmatch(IMAGE, image) for image in (args.image, args.base_image)):
        parser.error("reporter images must use immutable trading-hrs digests")
    if not re.fullmatch("[a-f0-9]{64}", args.source_sha256):
        parser.error("invalid reporter source hash")
    os.umask(0o077)
    root = args.root.resolve()
    with (root / ".deploy_tmp/compose_deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        bundle = Path(__file__).resolve().parent
        deploy(root, bundle, args.image, args.source_sha256, args.base_image)
        if args.install_inspection:
            install_inspection(root, bundle)


if __name__ == "__main__":
    main()
