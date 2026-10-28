import hashlib
import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "archive_container_logs.sh"


def test_archive_keeps_json_logs_and_skips_journald(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
case "$1" in
  ps) printf 'legacy-service\\nnew-service\\n' ;;
  inspect)
    case "$3" in
      '{{.HostConfig.LogConfig.Type}}')
        [ "$4" = legacy-service ] && echo json-file || echo journald ;;
      '{{.Id}}')
        [ "$4" = legacy-service ] && echo old-container-id || echo new-container-id ;;
    esac ;;
  logs)
    [ "$3" = legacy-service ] || exit 7
    printf '2026-10-27T10:00:00Z old message\\n' ;;
esac
"""
    )
    docker.chmod(0o755)
    archive = tmp_path / "archive"
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["bash", str(SCRIPT), str(archive)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    log_file = archive / "legacy-service.log"
    assert log_file.read_text() == "2026-10-27T10:00:00Z old message\n"
    assert not (archive / "new-service.log").exists()
    manifest = (archive / "manifest.tsv").read_text()
    assert "legacy-service\told-container-id" in manifest
    assert hashlib.sha256(log_file.read_bytes()).hexdigest() in manifest
    assert archive.stat().st_mode & 0o777 == 0o700
    assert log_file.stat().st_mode & 0o777 == 0o600
