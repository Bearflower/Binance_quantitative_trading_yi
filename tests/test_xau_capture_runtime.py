import json
from types import SimpleNamespace
from scripts.capture_xau_runtime import capture


def test_capture_reads_commit_from_version_without_image_labels(tmp_path, monkeypatch):
    def command(args, **kwargs):
        if args[:2] == ["docker", "inspect"]:
            rows = [
                {"Name": "/" + name, "Id": "container", "Image": "sha256:image",
                 "Config": {"Env": [], "Labels": None},
                 "State": {"Status": "running", "StartedAt": "2026-11-13",
                           "Health": {"Status": "healthy"}}, "RestartCount": 0}
                for name in ("trading_system-mtcps-xau-shadow", "trading_system-mtcps-xau-calibrated-shadow")
            ]
            return SimpleNamespace(stdout=json.dumps(rows), returncode=0)
        if args[:2] == ["docker", "exec"]:
            return SimpleNamespace(stdout="GIT_SHA=" + "a"*40 + "\n", returncode=0)
        return SimpleNamespace(stdout="100M", returncode=0)
    monkeypatch.setattr("scripts.capture_xau_runtime.subprocess.run", command)
    path = tmp_path / "runtime.json"
    capture(path)
    evidence = json.loads(path.read_text())
    assert all(row["git_sha"] == "a"*40 for row in evidence["containers"].values())


def test_capture_keeps_unhealthy_container_visible_when_exec_fails(tmp_path, monkeypatch):
    def command(args, **kwargs):
        if args[:2] == ["docker", "inspect"]:
            return SimpleNamespace(stdout=json.dumps([{
                "Name": "/trading_system-mtcps-xau-shadow", "Id": "old", "Image": "image",
                "Config": {"Env": [], "Labels": None},
                "State": {"Status": "exited", "StartedAt": "old"}, "RestartCount": 2,
            }]), returncode=0)
        if args[:2] == ["docker", "exec"]:
            if kwargs.get("check"):
                import subprocess
                raise subprocess.CalledProcessError(1, args)
            return SimpleNamespace(stdout="", returncode=1)
        return SimpleNamespace(stdout="100M", returncode=0)
    monkeypatch.setattr("scripts.capture_xau_runtime.subprocess.run", command)
    path = tmp_path / "runtime.json"
    capture(path)
    assert json.loads(path.read_text())["containers"]["trading_system-mtcps-xau-shadow"]["status"] == "exited"
